"""A local record of every live order, so a pick is bought at most once.

Plain JSON at `data/ledger.json`. Open it in any editor. An entry is written BEFORE the order
is posted; if the process dies mid-post, the entry stays in `submitting` and blocks a second
buy of the same pick until you check Polymarket and remove or resolve it by hand.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

BLOCKING_STATES = frozenset({"submitting", "accepted", "unknown"})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict) or not isinstance(loaded.get("orders"), dict):
                raise SystemExit(f"{path}: not a potd-trader ledger (expected an `orders` object)")
            self._entries = loaded["orders"]

    def get(self, key: str) -> dict[str, Any] | None:
        return self._entries.get(key)

    def blocks(self, key: str) -> bool:
        """True when this pick already has an order that must not be repeated."""
        entry = self._entries.get(key)
        return entry is not None and entry.get("state") in BLOCKING_STATES

    def spent_today_usd(self, pick_date: str) -> Decimal:
        total = Decimal("0")
        for entry in self._entries.values():
            if entry.get("pick_date") == pick_date and entry.get("state") in BLOCKING_STATES:
                total += Decimal(str(entry.get("stake_usd", "0")))
        return total

    def start(self, key: str, **fields: Any) -> None:
        self._entries[key] = {"state": "submitting", "created_at": _now(), **fields}
        self._flush()

    def finish(self, key: str, state: str, **fields: Any) -> None:
        entry = self._entries.setdefault(key, {"created_at": _now()})
        entry.update({"state": state, "updated_at": _now(), **fields})
        self._flush()

    def entries(self) -> list[tuple[str, dict[str, Any]]]:
        return sorted(self._entries.items(), key=lambda item: item[1].get("created_at", ""))

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"format": "potd-trader.ledger.v1", "orders": self._entries}
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, default=str)
                handle.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
