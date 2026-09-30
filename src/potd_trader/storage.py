"""Flushed atomic local files and process locks on Windows, macOS and Linux.

All writers must use the same lock path on one local filesystem. Do not unlink lock files:
locking the old inode while another process creates a new one defeats mutual exclusion.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import portalocker

if os.name == "nt":
    from portalocker import portalocker as platform_locking

    # The default msvcrt lock gives up after ten seconds. LockFileEx waits through
    # long submissions, preserving live off's existing acknowledgment barrier.
    platform_locking.LOCKER = platform_locking.Win32Locker


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            yield
        finally:
            portalocker.unlock(handle)


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        # Windows cannot open a directory this way. File content is flushed on
        # every platform; only POSIX also flushes the replacement's directory entry.
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)
