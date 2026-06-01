#!/usr/bin/env python3
"""
Simple local receiver for FileWatchRestPy testing.

Supports:
- application/json with text or base64 file content
- multipart/form-data with `metadata` and `file` parts
"""

from __future__ import annotations

import argparse
import base64
import json

from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    output_dir: Path = Path("received")

    def _json_response(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _metadata_path_for(target_file: Path) -> Path:
        return target_file.with_name(f"{target_file.name}_metadata.json")

    def _save_json_payload(self, payload: dict, target_dir: Path) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = payload["filename"]
        target_file = target_dir / filename

        self._metadata_path_for(target_file).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        kind = payload.get("content_kind", "text")
        encoding = payload.get("content_encoding", "utf-8")
        content = payload.get("content", "")

        if kind == "binary":
            if encoding != "base64":
                raise ValueError(f"unsupported binary encoding: {encoding}")
            data = base64.b64decode(content, validate=True)
            target_file.write_bytes(data)
        else:
            target_file.write_text(content, encoding=encoding)

        expected_size = payload.get("size")
        actual_size = target_file.stat().st_size
        if isinstance(expected_size, int) and expected_size != actual_size:
            raise ValueError(f"size mismatch expected={expected_size} actual={actual_size}")

        return target_file

    def _handle_json(self, raw: bytes) -> None:
        payload = json.loads(raw.decode("utf-8"))
        target_file = self._save_json_payload(payload, self.output_dir)
        kind = payload.get("content_kind", "text")
        self._json_response(
            201,
            {"ok": True, "saved_to": str(target_file), "content_kind": kind, "size": target_file.stat().st_size},
        )

    def _handle_multipart(self, content_type: str, raw: bytes) -> None:
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw
        )

        metadata = None
        file_name = None
        file_bytes = None

        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "metadata":
                payload = part.get_payload(decode=True) or b""
                metadata = json.loads(payload.decode(part.get_content_charset() or "utf-8"))
            elif name == "file":
                file_name = part.get_filename() or "upload.bin"
                file_bytes = part.get_payload(decode=True) or b""

        if metadata is None or file_bytes is None:
            self._json_response(400, {"ok": False, "error": "multipart request must include metadata and file"})
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        target_file = self.output_dir / file_name
        self._metadata_path_for(target_file).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        target_file.write_bytes(file_bytes)

        self._json_response(
            201,
            {"ok": True, "saved_to": str(target_file), "content_kind": "multipart", "size": target_file.stat().st_size},
        )

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            content_type = self.headers.get("Content-Type", "")
            raw = self.rfile.read(length)

            if content_type.startswith("application/json"):
                self._handle_json(raw)
                return
            if content_type.startswith("multipart/form-data"):
                self._handle_multipart(content_type, raw)
                return

            self._json_response(415, {"ok": False, "error": "unsupported content type", "content_type": content_type})
        except json.JSONDecodeError as exc:
            self._json_response(400, {"ok": False, "error": f"invalid json: {exc}"})
        except (ValueError, base64.binascii.Error) as exc:
            self._json_response(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - last-resort handler
            self._json_response(500, {"ok": False, "error": repr(exc)})

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--output-dir", default="received")
    args = parser.parse_args()

    Handler.output_dir = Path(args.output_dir).resolve()
    Handler.output_dir.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening on http://{args.host}:{args.port} output_dir={Handler.output_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()
