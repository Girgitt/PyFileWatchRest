# FileWatchRestPy two-stage minimal

Minimal Python watcher which posts new or modified files to a REST endpoint.

## Two-stage processing model

A file is not posted directly from the watched directory.

```text
watch_dir/file.csv
  -> processing/20260513_153000_123456_ab12cd34_file.csv
  -> POST to REST endpoint
  -> processed/20260513_153000_123456_ab12cd34_file.csv
```

If the process crashes or posting fails after the claim step, the file remains in `processing`.
The watcher and startup scan ignore both `processing` and `processed`, so a permanently bad file cannot
cause a repeated crash/retry loop.

`processing` can also be purged by age to prevent disk overfill during long endpoint outages.
This is controlled by:

```json
{
  "processing_retention_days": 7,
  "purge_processing_on_startup": true,
  "purge_processing_after_each_attempt": true
}
```

Set `"processing_retention_days": 0` to disable deletion from `processing`.

Manual recovery is explicit:

- inspect the file in `processing`
- fix endpoint/config/file problem
- move the file back to `watch_dir` if it should be retried

## Install for development

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json
python filewatchrest_min.py --config config.json
```

## Build with cx_Freeze

```cmd
pip install -r requirements.txt
python setup.py build
```

The executable will be under `build\exe.*\FileWatchRestPy.exe`.

Copy the whole `build\exe.*` directory to something like:

```text
C:\opt\FileWatchRestPy
```

Then copy/edit:

```text
config.example.json -> C:\opt\FileWatchRestPy\config.json
```

## Install with NSSM

Edit `install_nssm.cmd` paths, then run it from an Administrator command prompt.

Typical command form:

```cmd
nssm install FileWatchRestPy "C:\opt\FileWatchRestPy\FileWatchRestPy.exe" "--config" "C:\opt\FileWatchRestPy\config.json"
nssm set FileWatchRestPy AppDirectory "C:\opt\FileWatchRestPy"
nssm set FileWatchRestPy AppExit Default Restart
nssm start FileWatchRestPy
```

## Upload format

Default in this package is JSON mode:

```json
{
  "path": "C:\\temp\\watch\\file.csv",
  "filename": "file.csv",
  "size": 123,
  "last_write_time": "2026-05-13T15:10:20.123456",
  "processing_path": "C:\\temp\\watch\\processing\\20260513_151020_123456_ab12cd34_file.csv",
  "claimed_at": "2026-05-13T15:10:21.123456",
  "content": "file content here"
}
```

Set `"upload_mode": "multipart"` to send `metadata` JSON plus a streamed `file` part instead.
