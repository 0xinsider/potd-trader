"""Read the Pick of the Day from the 0xinsider API.

Contract: https://docs.0xinsider.com/api-reference/endpoint/get-pick-of-the-day

Only the API key leaves this machine on this path. The response is public pick data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import __version__
from .config import MAX_DAILY_PICKS, validate_api_origin

PICK_PATH = "/api/v1/pick-of-the-day"


class EntryAuthorization(BaseModel):
    """A provider-published ceiling, never permission to bypass the local guards."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    version: Literal[1]
    authorization_id: UUID
    policy_version: int = Field(ge=1)
    condition_id: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    token_id: str = Field(pattern=r"^[1-9][0-9]*$")
    outcome_index: Literal[0, 1]
    max_entry_price: Decimal = Field(gt=0, lt=1)
    issued_at: AwareDatetime
    expires_at: AwareDatetime


class Pick(BaseModel):
    """One ranked pick. Safety fields from the API; unrelated additive fields are ignored."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    pick_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    pick_rank: int = Field(default=1, ge=1, le=MAX_DAILY_PICKS)
    outcome: str
    matchup: str | None = None
    pick_outcome_label: str | None = None
    position: str | None = None
    category: str | None = None
    display_category: str | None = None
    token_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")
    backed_price: Decimal | None = Field(default=None, ge=0, le=1)
    odds_display: str | None = None
    release_at: AwareDatetime | None = None
    game_started: bool | None = Field(default=None, strict=True)
    is_locked: bool | None = Field(default=None, strict=True)
    entry_authorization: EntryAuthorization | None = None
    event_slug: str | None = None

    @field_validator("pick_date")
    @classmethod
    def _date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @property
    def key(self) -> str:
        """Stable identity for the ledger: one product day, one slot, one CLOB token."""
        return f"{self.pick_date}#{self.pick_rank}#{self.token_id or 'no-token'}"

    @property
    def label(self) -> str:
        side = self.pick_outcome_label or self.position or "?"
        game = self.matchup or self.event_slug or "?"
        return f"#{self.pick_rank} {side} ({game})"


class ScheduledSlot(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    pick_rank: int = Field(ge=1, le=MAX_DAILY_PICKS)
    release_at: AwareDatetime
    kickoff: AwareDatetime | None = None
    retry_at: AwareDatetime | None = None


@dataclass(frozen=True)
class Slate:
    """A 200 response: the released picks plus any same-day slots still to come."""

    pick_date: str | None
    picks: tuple[Pick, ...]
    scheduled: tuple[ScheduledSlot, ...]
    etag: str | None
    proof_pending: tuple[ScheduledSlot, ...] = ()

    @property
    def next_release_at(self) -> datetime | None:
        times = [slot.release_at for slot in self.scheduled]
        times.extend(slot.retry_at for slot in self.proof_pending if slot.retry_at is not None)
        return min(times) if times else None


@dataclass(frozen=True)
class NotReleased:
    """A 404 with `error.reason="pick_not_released"`: a schedule, not an outage."""

    retry_at: datetime | None


@dataclass(frozen=True)
class NotModified:
    """A 304: nothing changed since the ETag we sent."""


@dataclass(frozen=True)
class TryLater:
    """A 429 or 503: the server named the instant to come back."""

    status: int
    reason: str | None
    retry_at: datetime | None


PickResult = Slate | NotReleased | NotModified | TryLater


class OxinsiderError(RuntimeError):
    """A response that means stop and fix something, not wait."""


def _retry_instant(headers: httpx.Headers, body: dict[str, Any] | None) -> datetime | None:
    error = (body or {}).get("error")
    error = error if isinstance(error, dict) else {}
    raw = error.get("retry_at")
    if isinstance(raw, str):
        try:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if instant.tzinfo is not None:
                return instant
        except ValueError:
            pass
    retry_after = headers.get("Retry-After")
    if retry_after:
        if retry_after.isdigit():
            return datetime.now(UTC) + timedelta(seconds=int(retry_after))
        try:
            instant = parsedate_to_datetime(retry_after)
            return instant if instant.tzinfo is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json(parse_float=Decimal)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _parse_slate(payload: dict[str, Any], etag: str | None) -> Slate:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise OxinsiderError("pick-of-the-day: response body is not an object")
    rows = data.get("picks")
    if rows is None:
        # Legacy single-pick shape: the pick is the envelope itself.
        picks: tuple[Pick, ...] = (Pick.model_validate(data),)
    else:
        if not isinstance(rows, list):
            raise OxinsiderError("pick-of-the-day: `picks` is not a list")
        picks = tuple(Pick.model_validate(row) for row in rows)
    ranks = [pick.pick_rank for pick in picks]
    tokens = [pick.token_id for pick in picks if pick.token_id is not None]
    if len(ranks) != len(set(ranks)) or len(tokens) != len(set(tokens)):
        raise OxinsiderError("pick-of-the-day: duplicate slot or token; refusing the whole slate")
    if any(pick.pick_date != data.get("pick_date") for pick in picks):
        raise OxinsiderError("pick-of-the-day: inconsistent product dates")
    scheduled_rows = data.get("scheduled_picks") or []
    scheduled = tuple(ScheduledSlot.model_validate(row) for row in scheduled_rows)
    pending = tuple(
        ScheduledSlot.model_validate(row) for row in data.get("proof_pending_picks", [])
    )
    return Slate(
        pick_date=data.get("pick_date"),
        picks=picks,
        scheduled=scheduled,
        etag=etag,
        proof_pending=pending,
    )


def parse_slate(payload: dict[str, Any], etag: str | None) -> Slate:
    try:
        return _parse_slate(payload, etag)
    except (ValidationError, ValueError, TypeError) as exc:
        raise OxinsiderError(
            "pick-of-the-day: malformed safety fields; refusing the slate"
        ) from exc


class OxinsiderClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=validate_api_origin(base_url),
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": f"potd-trader/{__version__} (+https://github.com/0xinsider/potd-trader)",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def pick_of_the_day(self, etag: str | None = None) -> PickResult:
        headers = {"If-None-Match": etag} if etag else {}
        response = self._client.get(PICK_PATH, headers=headers)
        body = _json_or_none(response)
        error = (body or {}).get("error")
        error = error if isinstance(error, dict) else {}
        reason = error.get("reason") if isinstance(error.get("reason"), str) else None

        if response.status_code == 200:
            if body is None:
                raise OxinsiderError("pick-of-the-day: 200 without a JSON body")
            return parse_slate(body, response.headers.get("ETag"))
        if response.status_code == 304:
            return NotModified()
        if response.status_code == 404 and reason == "pick_not_released":
            return NotReleased(retry_at=_retry_instant(response.headers, body))
        if response.status_code in (429, 503):
            return TryLater(
                status=response.status_code,
                reason=reason,
                retry_at=_retry_instant(response.headers, body),
            )
        if response.status_code == 401:
            raise OxinsiderError(
                "0xinsider rejected the API key (401). Check OXINSIDER_API_KEY: it must be a live "
                "key (oxi_sk_live_...) from https://0xinsider.com/developers."
            )
        if response.status_code in (402, 403):
            raise OxinsiderError(
                f"0xinsider answered {response.status_code}: the Pick of the Day endpoint needs an "
                "active Pro subscription on this key. https://0xinsider.com/pricing"
            )
        raise OxinsiderError(f"pick-of-the-day: unexpected HTTP {response.status_code}")
