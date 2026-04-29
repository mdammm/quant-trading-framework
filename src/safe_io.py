"""
Atomic-write + file-lock helpers for tracker JSON state.

A47 fix (2026-04-28): tracker files were written via the simple form
    path.write_text(json.dumps(data))
which truncates the file before writing. A crash, kill, or concurrent write
during the 1-50 ms write window leaves the file partial or empty. The next
reader (dashboard, daily_runner, 4h_runner) sees JSONDecodeError and may
interpret it as "no trades", losing state. Worse, two concurrent writers
clobber each other regardless of size.

This module replaces those calls with `atomic_write_json` (temp + os.replace,
which is atomic at the filesystem level on POSIX), and wraps any
read-modify-write loop in `file_lock` (fcntl LOCK_EX) so two processes
cannot interleave a load -> mutate -> save cycle.

Pre-live blocker per audit. Paper-mode tolerable; live trading not.

Use:
    from safe_io import atomic_write_json, file_lock

    atomic_write_json(path, payload)

    with file_lock(path):
        data = json.loads(path.read_text()) if path.exists() else {}
        data["k"] = "v"
        atomic_write_json(path, data)
"""

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Optional


def atomic_write_json(path: Path, data: Any, indent: int = 2,
                      default: Optional[Any] = str) -> None:
    """Write `data` as JSON to `path` atomically.

    Strategy: serialize to a sibling temp file first, fsync, then os.replace.
    os.replace is guaranteed atomic on POSIX — readers see either the old
    content or the new content, never anything in between. The .tmp suffix
    makes it visible if a write crashes mid-flight (rare but recoverable).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(data, indent=indent, default=default)
    # Open with explicit fsync so the bytes are on disk before the rename.
    # On macOS this matters: without fsync a power loss after rename can
    # surface a zero-length file even though the rename "succeeded".
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, serialized.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0):
    """Block-acquire an exclusive POSIX advisory lock on `path` (creates a
    sibling .lock file).  Use to gate a load -> mutate -> save cycle so two
    processes cannot interleave.

    The lock file is separate from the data file so the lock survives the
    `os.replace` in atomic_write_json (which would invalidate any lock held
    on the data file's old inode).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"file_lock({path}) timed out after {timeout}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def safe_read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, return `default` on missing/corrupted file.

    Catches the JSONDecodeError that A47 was meant to make impossible, but
    still occurs for files written by code paths not yet migrated to
    atomic_write_json, or for legacy state from before the migration.
    """
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, IOError):
        return default
