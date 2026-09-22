"""Authoritative stop and submission barrier, shared by CLI and running watchers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values

from .storage import atomic_write, file_lock


class LiveControl:
    def __init__(self, env_path: Path) -> None:
        self.env_path = env_path.resolve()
        self.halt_path = self.env_path.parent / "HALT"
        self.lock_path = self.env_path.parent / ".live.lock"

    def enabled(self, started_live: bool) -> bool:
        if not started_live or self.halt_path.exists():
            return False
        # A file's explicit no beats inherited LIVE=yes. Deletion or a missing LIVE field
        # also fails closed; environment-only deployments must create this small control file.
        if not self.env_path.is_file():
            return False
        return dotenv_values(self.env_path, interpolate=False).get("LIVE") == "yes"

    @contextmanager
    def submission(self, started_live: bool) -> Iterator[bool]:
        # Held through the exchange call: live off cannot report success while a prior
        # submission is still in flight. A blocked network call can delay that acknowledgment.
        with file_lock(self.lock_path):
            yield self.enabled(started_live)

    def set_live(self, live: bool) -> None:
        if not live:
            # Signal before waiting so another watcher cannot begin while off awaits the lock.
            atomic_write(self.halt_path, "Stopped by potd-trader live off\n")
        with file_lock(self.lock_path):
            lines = self.env_path.read_text(encoding="utf-8").splitlines()
            kept = [line for line in lines if not _live_assignment(line)]
            kept.append(f"LIVE={'yes' if live else 'no'}")
            atomic_write(self.env_path, "\n".join(kept) + "\n")
            if live:
                self.halt_path.unlink(missing_ok=True)


def _live_assignment(line: str) -> bool:
    left = line.split("=", 1)[0].strip().removeprefix("export ").strip()
    return left == "LIVE"
