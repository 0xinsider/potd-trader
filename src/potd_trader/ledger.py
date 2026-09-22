"""Crash-safe JSON order intents with inter-process duplicate and budget reservations.

The lock covers reload/check/reserve, and every update reloads under the same lock. A
submitting/unknown intent remains blocked and charged until manually reconciled with the
exchange. Old v1 entries are read unchanged; timestamps, never feed dates, own spending.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .storage import atomic_write, file_lock

BLOCKING_STATES = frozenset({"submitting", "accepted", "unknown"})
STATES = BLOCKING_STATES | {"rejected"}


class LedgerError(RuntimeError):
    """Local safety state cannot be trusted; stop without submitting."""


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(loaded.get("orders"), dict):
                raise ValueError("expected an orders object")
            entries: dict[str, dict[str, Any]] = loaded["orders"]
            for entry in entries.values():
                if not isinstance(entry, dict) or entry.get("state") not in STATES:
                    raise ValueError("invalid order state")
                if not isinstance(entry.get("stake_usd"), str):
                    raise ValueError("stake must be a decimal string")
                stake = Decimal(entry["stake_usd"])
                created = datetime.fromisoformat(entry["created_at"])
                if not stake.is_finite() or stake <= 0 or created.tzinfo is None:
                    raise ValueError("invalid amount or local timestamp")
                if not all(entry.get(k) for k in ("pick_date", "pick_rank", "token_id")):
                    raise ValueError("incomplete order identity")
            return entries
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise LedgerError(
                f"{self.path}: invalid ledger; restore or reconcile before trading"
            ) from exc

    @staticmethod
    def _blocked(
        entries: dict[str, dict[str, Any]],
        key: str,
        pick_date: str,
        pick_rank: int,
        token_id: str,
    ) -> bool:
        return any(
            entry["state"] in BLOCKING_STATES
            and (
                existing_key == key
                or entry["token_id"] == token_id
                or (entry["pick_date"] == pick_date and entry["pick_rank"] == pick_rank)
            )
            for existing_key, entry in entries.items()
        )

    def blocks(self, key: str, *, pick_date: str, pick_rank: int, token_id: str) -> bool:
        with file_lock(self.lock_path):
            return self._blocked(self._read(), key, pick_date, pick_rank, token_id)

    @staticmethod
    def _spent(entries: dict[str, dict[str, Any]], now: datetime) -> Decimal:
        # Unresolved orders keep consuming capacity across midnight: they may still settle.
        return sum(
            (
                Decimal(entry["stake_usd"])
                for entry in entries.values()
                if entry["state"] in {"submitting", "unknown"}
                or (
                    entry["state"] == "accepted"
                    and datetime.fromisoformat(entry["created_at"]).astimezone(UTC).date()
                    == now.astimezone(UTC).date()
                )
            ),
            Decimal("0"),
        )

    def spent_today_usd(self, now: datetime | None = None) -> Decimal:
        with file_lock(self.lock_path):
            return self._spent(self._read(), now or datetime.now(UTC))

    def reserve(
        self,
        key: str,
        *,
        daily_cap: Decimal,
        stake_usd: Decimal,
        pick_date: str,
        pick_rank: int,
        token_id: str,
        now: datetime | None = None,
        **fields: Any,
    ) -> str | None:
        """Atomically reserve identity AND budget. None succeeds; a reason declines."""
        if (
            not daily_cap.is_finite()
            or daily_cap <= 0
            or not stake_usd.is_finite()
            or stake_usd <= 0
        ):
            raise LedgerError("live orders require a positive finite stake and DAILY_CAP_USD")
        with file_lock(self.lock_path):
            entries = self._read()
            if self._blocked(entries, key, pick_date, pick_rank, token_id):
                return "pick, slot, or token already reserved in the ledger"
            instant = now or datetime.now(UTC)
            if self._spent(entries, instant) + stake_usd > daily_cap:
                return "DAILY_CAP_USD would be exceeded by this reservation"
            entries[key] = {
                **fields,
                "state": "submitting",
                "created_at": instant.isoformat(),
                "pick_date": pick_date,
                "pick_rank": pick_rank,
                "token_id": token_id,
                "stake_usd": str(stake_usd),
            }
            self._flush(entries)
        return None

    def finish(self, key: str, state: str, **fields: Any) -> None:
        if state not in STATES - {"submitting"}:
            raise LedgerError("invalid completion state")
        with file_lock(self.lock_path):
            entries = self._read()
            if key not in entries or entries[key]["state"] != "submitting":
                raise LedgerError("order completion has no pending reservation")
            entries[key].update(fields)
            entries[key].update(state=state, updated_at=datetime.now(UTC).isoformat())
            self._flush(entries)

    def entries(self) -> list[tuple[str, dict[str, Any]]]:
        with file_lock(self.lock_path):
            return sorted(self._read().items(), key=lambda item: item[1]["created_at"])

    def _flush(self, entries: dict[str, dict[str, Any]]) -> None:
        atomic_write(
            self.path,
            json.dumps(
                {"format": "potd-trader.ledger.v1", "orders": entries},
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
