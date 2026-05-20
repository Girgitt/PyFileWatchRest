#!/usr/bin/env python3
"""
Minimal two-stage file watcher -> REST poster.

Processing model:
    inbox/watch_dir file
        -> atomically claim by moving to processing_dir
        -> POST from processing_dir
        -> on success move to processed_dir
        -> on failure/crash leave in processing_dir

Important property:
- watched input files are never marked as "already processed forever" by path
- a bad file cannot cause an endless retry loop because it is moved out of the input area
- after a crash, files already claimed remain in processing_dir for manual inspection/recovery
- files not yet claimed remain in watch_dir and are picked up by startup_scan

Dependencies:
    pip install watchdog requests
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import queue
import shutil
import signal
import sys
import threading
import time
import uuid
import psutil

from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver


@dataclass
class Config:
    watch_dir: Path
    endpoint: str

    allowed_extensions: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    include_subdirectories: bool = False

    debounce_ms: int = 1000
    startup_scan: bool = True

    post_file_contents: bool = True
    upload_mode: str = "multipart"  # multipart or json
    headers: dict[str, str] = field(default_factory=dict)
    bearer_token: Optional[str] = None

    retry_count: int = 3
    retry_delay_ms: int = 500
    request_timeout_seconds: int = 60

    file_ready_timeout_ms: int = 10000
    file_stable_ms: int = 500

    processing_dir: Path = Path("processing")
    processed_dir: Path = Path("processed")

    move_processed_files: bool = True
    processed_retention_days: int = 14
    purge_processed_on_startup: bool = True
    purge_processed_after_each_success: bool = True

    # Warn about old processing files. 0 disables warnings.
    processing_stale_warn_hours: int = 24

    # Delete old files from processing_dir to prevent disk overfill during long endpoint outages.
    # 0 disables deletion.
    processing_retention_days: int = 7
    purge_processing_on_startup: bool = True
    purge_processing_after_each_attempt: bool = True

    observer_type: str = "native"  # native or polling
    polling_interval_seconds: float = 2.0

    log_file: Path = Path("logs/pyfilewatchrest.log")
    log_level: str = "INFO"
    log_retained_days: int = 14
    
    memory_restart_mb: int = 512
    memory_check_interval_seconds: int = 30
    memory_restart_consecutive_checks: int = 3



def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))

    cfg = Config(
        watch_dir=Path(raw["watch_dir"]),
        endpoint=str(raw["endpoint"]),
    )

    for key, value in raw.items():
        if not hasattr(cfg, key):
            continue
        if key in {"watch_dir", "processing_dir", "processed_dir", "log_file"}:
            setattr(cfg, key, Path(value))
        else:
            setattr(cfg, key, value)

    cfg.watch_dir = cfg.watch_dir.resolve()

    if not cfg.processing_dir.is_absolute():
        cfg.processing_dir = (cfg.watch_dir / cfg.processing_dir).resolve()
    else:
        cfg.processing_dir = cfg.processing_dir.resolve()

    if not cfg.processed_dir.is_absolute():
        cfg.processed_dir = (cfg.watch_dir / cfg.processed_dir).resolve()
    else:
        cfg.processed_dir = cfg.processed_dir.resolve()

    if not cfg.log_file.is_absolute():
        cfg.log_file = (Path.cwd() / cfg.log_file).resolve()

    cfg.allowed_extensions = [
        ext.lower() if ext.startswith(".") else "." + ext.lower()
        for ext in cfg.allowed_extensions
    ]

    cfg.upload_mode = cfg.upload_mode.lower().strip()
    if cfg.upload_mode not in {"multipart", "json"}:
        raise ValueError("upload_mode must be 'multipart' or 'json'")

    cfg.observer_type = cfg.observer_type.lower().strip()
    if cfg.observer_type not in {"native", "polling"}:
        raise ValueError("observer_type must be 'native' or 'polling'")

    return cfg


def setup_logging(cfg: Config) -> None:
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        cfg.log_file,
        when="midnight",
        backupCount=max(0, int(cfg.log_retained_days)),
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)



def start_memory_watchdog(cfg: Config) -> threading.Thread | None:
    limit_mb = int(cfg.memory_restart_mb)
    if limit_mb <= 0:
        logging.info("memory watchdog disabled")
        return None

    interval = max(5, int(cfg.memory_check_interval_seconds))
    required_hits = max(1, int(cfg.memory_restart_consecutive_checks))
    limit_bytes = limit_mb * 1024 * 1024

    def run() -> None:
        proc = psutil.Process(os.getpid())
        hits = 0

        while True:
            try:
                rss = proc.memory_info().rss

                if rss > limit_bytes:
                    hits += 1
                    logging.warning(
                        "memory above threshold rss_mb=%.1f limit_mb=%s hit=%s/%s",
                        rss / 1024 / 1024,
                        limit_mb,
                        hits,
                        required_hits,
                    )
                else:
                    hits = 0
                    logging.debug(
                        "memory ok rss_mb=%.1f limit_mb=%s",
                        rss / 1024 / 1024,
                        limit_mb,
                    )

                if hits >= required_hits:
                    logging.critical(
                        "memory threshold exceeded repeatedly; exiting for NSSM restart "
                        "rss_mb=%.1f limit_mb=%s",
                        rss / 1024 / 1024,
                        limit_mb,
                    )
                    logging.shutdown()
                    os._exit(75)

            except Exception:
                logging.exception("memory watchdog error")

            time.sleep(interval)

    t = threading.Thread(target=run, name="memory-watchdog", daemon=True)
    t.start()
    return t


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
    except FileNotFoundError:
        try:
            path.absolute().relative_to(parent.absolute())
            return True
        except Exception:
            return False


def fingerprint(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = path.stat()
        if not path.is_file():
            return None
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


class RestPoster:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.endpoint, self.basic_auth = self._parse_endpoint_auth(cfg.endpoint)

    @staticmethod
    def _parse_endpoint_auth(endpoint: str) -> tuple[str, Optional[tuple[str, str]]]:
        parts = urlsplit(endpoint)
        if parts.username is None:
            return endpoint, None

        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parts.port is not None:
            host = f"{host}:{parts.port}"

        sanitized_endpoint = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
        password = parts.password or ""
        return sanitized_endpoint, (parts.username, password)

    def post_file(self, processing_path: Path, original_path: Path) -> bool:
        try:
            st = processing_path.stat()
            meta = {
                # Keep "path" as the original watched path, because many receivers expect that.
                "path": str(original_path),
                "filename": original_path.name,
                "size": st.st_size,
                "last_write_time": datetime.fromtimestamp(st.st_mtime).isoformat(),
                # Extra trace fields for this two-stage implementation.
                "processing_path": str(processing_path),
                "claimed_at": datetime.now().isoformat(),
            }
        except Exception as exc:
            logging.exception("cannot stat processing file before post path=%s error=%r", processing_path, exc)
            return False

        headers = dict(self.cfg.headers or {})
        if self.cfg.bearer_token:
            token = self.cfg.bearer_token
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            headers["Authorization"] = f"Bearer {token}"

        auth = None if "Authorization" in headers else self.basic_auth
        if self.basic_auth and "Authorization" in headers:
            logging.warning("endpoint contains basic auth credentials but Authorization header is already set; using Authorization header")

        for attempt in range(1, self.cfg.retry_count + 1):
            try:
                if self.cfg.post_file_contents:
                    if self.cfg.upload_mode == "multipart":
                        with processing_path.open("rb") as f:
                            files = {
                                "metadata": (
                                    None,
                                    json.dumps(meta, ensure_ascii=False),
                                    "application/json",
                                ),
                                "file": (
                                    original_path.name,
                                    f,
                                    "application/octet-stream",
                                ),
                            }
                            r = self.session.post(
                                self.endpoint,
                                files=files,
                                headers=headers,
                                auth=auth,
                                timeout=self.cfg.request_timeout_seconds,
                            )
                    else:
                        # JSON mode reads the file into memory. Use only for small/text files.
                        data = dict(meta)
                        data["content"] = processing_path.read_text(encoding="utf-8", errors="replace")
                        r = self.session.post(
                            self.endpoint,
                            json=data,
                            headers=headers,
                            auth=auth,
                            timeout=self.cfg.request_timeout_seconds,
                        )
                else:
                    r = self.session.post(
                        self.endpoint,
                        json=meta,
                        headers=headers,
                        auth=auth,
                        timeout=self.cfg.request_timeout_seconds,
                    )

                if 200 <= r.status_code < 300:
                    logging.info("posted original=%s endpoint=%s status=%s", original_path, self.endpoint, r.status_code)
                    return True

                logging.warning(
                    "post failed original=%s processing=%s status=%s response=%s",
                    original_path,
                    processing_path,
                    r.status_code,
                    r.text[:500],
                )

            except Exception as exc:
                logging.warning(
                    "post exception original=%s processing=%s attempt=%s error=%r",
                    original_path,
                    processing_path,
                    attempt,
                    exc,
                )

            if attempt < self.cfg.retry_count:
                time.sleep(max(0, self.cfg.retry_delay_ms) / 1000.0)

        return False


class Processor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.poster = RestPoster(cfg)

        self._lock = threading.Lock()
        self._pending: dict[Path, float] = {}

        # Suppress repeated native watcher events before claim.
        # This is short-lived duplicate suppression only, not "already processed forever".
        self._last_scheduled_fp: dict[Path, tuple[int, int]] = {}

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="processor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def schedule(self, path: Path, reason: str) -> None:
        try:
            path = path.resolve()
            if not self.should_consider_input(path):
                return

            fp = fingerprint(path)
            if fp is not None and self._last_scheduled_fp.get(path) == fp:
                logging.debug("duplicate event ignored reason=%s path=%s fp=%s", reason, path, fp)
                return

            if fp is not None:
                self._last_scheduled_fp[path] = fp

            with self._lock:
                self._pending[path] = time.monotonic()

            logging.debug("scheduled reason=%s path=%s", reason, path)

        except Exception:
            logging.exception("unexpected schedule error path=%s reason=%s", path, reason)

    def should_consider_input(self, path: Path) -> bool:
        if is_under(path, self.cfg.processing_dir):
            logging.debug("ignored processing-dir path=%s", path)
            return False

        if is_under(path, self.cfg.processed_dir):
            logging.debug("ignored processed-dir path=%s", path)
            return False

        if self.cfg.allowed_extensions:
            if path.suffix.lower() not in self.cfg.allowed_extensions:
                logging.debug("ignored extension path=%s", path)
                return False

        if self.cfg.exclude_patterns:
            name = path.name
            for pattern in self.cfg.exclude_patterns:
                if fnmatch.fnmatch(name, pattern):
                    logging.debug("ignored pattern=%s path=%s", pattern, path)
                    return False

        return True

    def _loop(self) -> None:
        debounce_seconds = max(0, self.cfg.debounce_ms) / 1000.0

        while not self._stop.is_set():
            try:
                now = time.monotonic()
                due: list[Path] = []

                with self._lock:
                    for path, ts in list(self._pending.items()):
                        if now - ts >= debounce_seconds:
                            due.append(path)
                            self._pending.pop(path, None)

                for path in due:
                    try:
                        self.process_input_file(path)
                    except Exception:
                        # This is the safety net that prevents the processor thread from dying.
                        logging.exception("unexpected processing error input=%s", path)

                self._stop.wait(0.2)

            except Exception:
                # Extreme safety net. If this repeats, NSSM logs will show it.
                logging.exception("unexpected processor-loop error")
                self._stop.wait(1.0)

    def process_input_file(self, input_path: Path) -> None:
        if not self.should_consider_input(input_path):
            return

        if not wait_until_ready(input_path, self.cfg):
            logging.warning("input file not ready or missing path=%s", input_path)
            return

        claimed_path = self.claim_to_processing(input_path)
        if claimed_path is None:
            # Usually means another event/thread/process already moved it.
            return

        try:
            success = self.poster.post_file(claimed_path, original_path=input_path)

            if success:
                if self.cfg.move_processed_files:
                    self.move_to_processed(claimed_path, original_path=input_path)
                    if self.cfg.purge_processed_after_each_success:
                        purge_processed(self.cfg)
                else:
                    # If not moving to processed, leave successfully posted file in processing.
                    # This is intentional: input files should not return to watch_dir automatically.
                    logging.info("successfully posted; leaving in processing because move_processed_files=false path=%s", claimed_path)
            else:
                # Important: no automatic retry loop. The file remains quarantined until retention purges it
                # or an operator moves it back to watch_dir for retry.
                logging.error("posting failed; leaving file in processing path=%s original=%s", claimed_path, input_path)

        except Exception:
            # Important: after a crash-level exception, the claimed file remains in processing.
            logging.exception("unexpected post/move error; file remains in processing path=%s original=%s", claimed_path, input_path)
            raise
        finally:
            if self.cfg.purge_processing_after_each_attempt:
                purge_processing(self.cfg)

    def claim_to_processing(self, input_path: Path) -> Optional[Path]:
        try:
            if not input_path.exists():
                return None

            rel_parent = relative_parent_under_watch(input_path, self.cfg.watch_dir)
            dest_dir = self.cfg.processing_dir / rel_parent
            dest_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            token = uuid.uuid4().hex[:8]
            dest = dest_dir / f"{timestamp}_{token}_{input_path.name}"

            # On the same filesystem this is an atomic rename. Because processing_dir
            # defaults under watch_dir, it should normally be same volume.
            os.replace(str(input_path), str(dest))

            self._pending.pop(input_path, None)
            self._last_scheduled_fp.pop(input_path, None)

            logging.info("claimed input=%s processing=%s", input_path, dest)
            return dest

        except FileNotFoundError:
            return None
        except PermissionError:
            logging.warning("cannot claim file due to permission/lock path=%s", input_path)
            return None
        except Exception:
            logging.exception("failed to claim file path=%s", input_path)
            return None

    def move_to_processed(self, processing_path: Path, original_path: Path) -> bool:
        try:
            if not processing_path.exists():
                return False

            rel_parent = relative_parent_under_watch(original_path, self.cfg.watch_dir)
            dest_dir = self.cfg.processed_dir / rel_parent
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Preserve the processing filename so claim id/timestamp remains visible.
            dest = dest_dir / processing_path.name
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{processing_path.stem}_{counter}{processing_path.suffix}"
                counter += 1

            shutil.move(str(processing_path), str(dest))
            logging.info("moved processed src=%s dst=%s", processing_path, dest)
            return True

        except Exception as exc:
            logging.exception("failed to move processed file path=%s error=%r", processing_path, exc)
            return False


def relative_parent_under_watch(path: Path, watch_dir: Path) -> Path:
    try:
        rel = path.parent.resolve().relative_to(watch_dir.resolve())
        if str(rel) == ".":
            return Path()
        return rel
    except ValueError:
        return Path()


def wait_until_ready(path: Path, cfg: Config) -> bool:
    timeout = max(0, cfg.file_ready_timeout_ms) / 1000.0
    stable_for = max(0, cfg.file_stable_ms) / 1000.0

    deadline = time.monotonic() + timeout
    last_fp: Optional[tuple[int, int]] = None
    stable_since: Optional[float] = None

    while True:
        now = time.monotonic()

        try:
            fp = fingerprint(path)
            if fp is not None:
                # Ensure it can be opened for reading.
                with path.open("rb"):
                    pass

                if fp == last_fp:
                    if stable_since is None:
                        stable_since = now
                    if now - stable_since >= stable_for:
                        return True
                else:
                    last_fp = fp
                    stable_since = now

        except OSError:
            pass

        if timeout == 0:
            return path.exists() and path.is_file()

        if now >= deadline:
            return False

        time.sleep(0.1)


def purge_processed(cfg: Config) -> None:
    days = int(cfg.processed_retention_days)
    if days <= 0:
        return

    root = cfg.processed_dir
    if not root.exists():
        return

    cutoff = time.time() - days * 86400
    deleted = 0

    for p in root.rglob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except OSError:
            logging.warning("failed to delete old processed file path=%s", p)

    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if p.is_dir():
                p.rmdir()
        except OSError:
            pass

    if deleted:
        logging.info("purged old processed files count=%s root=%s", deleted, root)



def purge_processing(cfg: Config) -> None:
    """Delete stale files from processing_dir.

    This is intentionally separate from purge_processed because processing files are failed/unknown
    files. Enable this only when bounded disk usage is more important than indefinite retention.
    """
    days = int(cfg.processing_retention_days)
    if days <= 0:
        return

    root = cfg.processing_dir
    if not root.exists():
        return

    cutoff = time.time() - days * 86400
    deleted = 0

    for p in root.rglob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
                logging.warning("purged stale processing file path=%s retention_days=%s", p, days)
        except OSError:
            logging.warning("failed to delete stale processing file path=%s", p)

    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if p.is_dir():
                p.rmdir()
        except OSError:
            pass

    if deleted:
        logging.warning("purged stale processing files count=%s root=%s retention_days=%s", deleted, root, days)


def warn_stale_processing(cfg: Config) -> None:
    hours = int(cfg.processing_stale_warn_hours)
    if hours <= 0 or not cfg.processing_dir.exists():
        return

    cutoff = time.time() - hours * 3600
    count = 0

    for p in cfg.processing_dir.rglob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                count += 1
                logging.warning("stale processing file path=%s age_hours_gt=%s", p, hours)
        except OSError:
            pass

    if count:
        logging.warning("stale processing files count=%s dir=%s", count, cfg.processing_dir)


class WatchHandler(FileSystemEventHandler):
    def __init__(self, processor: Processor):
        super().__init__()
        self.processor = processor

    def on_created(self, event):
        if not event.is_directory:
            self.processor.schedule(Path(event.src_path), "created")

    def on_modified(self, event):
        if not event.is_directory:
            self.processor.schedule(Path(event.src_path), "modified")

    def on_moved(self, event):
        if not event.is_directory:
            self.processor.schedule(Path(event.dest_path), "moved")


def startup_scan(cfg: Config, processor: Processor) -> None:
    if not cfg.startup_scan:
        return

    iterator = cfg.watch_dir.rglob("*") if cfg.include_subdirectories else cfg.watch_dir.glob("*")
    count = 0

    for p in iterator:
        try:
            if p.is_file() and processor.should_consider_input(p):
                processor.schedule(p, "startup_scan")
                count += 1
        except OSError:
            continue

    logging.info("startup scan scheduled input files=%s", count)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json", help="Path to JSON configuration file")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(cfg)

    logging.info("starting FileWatchRestPy two-stage config=%s watch_dir=%s", args.config, cfg.watch_dir)

    cfg.watch_dir.mkdir(parents=True, exist_ok=True)
    cfg.processing_dir.mkdir(parents=True, exist_ok=True)
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)

    if cfg.purge_processed_on_startup:
        purge_processed(cfg)

    if cfg.purge_processing_on_startup:
        purge_processing(cfg)

    warn_stale_processing(cfg)

    processor = Processor(cfg)
    processor.start()
    startup_scan(cfg, processor)

    handler = WatchHandler(processor)

    if cfg.observer_type == "polling":
        observer = PollingObserver(timeout=cfg.polling_interval_seconds)
    else:
        observer = Observer()

    observer.schedule(handler, str(cfg.watch_dir), recursive=cfg.include_subdirectories)
    observer.start()

    stop_event = threading.Event()

    def request_stop(signum=None, frame=None):
        logging.info("stop requested signal=%s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    
    start_memory_watchdog(cfg)

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        logging.info("stopping")
        observer.stop()
        observer.join(timeout=10)
        processor.stop()

    logging.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
