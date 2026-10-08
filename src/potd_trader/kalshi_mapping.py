"""Explicit reviewed contract mappings; no market discovery or name-based trading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from polymarket import PublicClient
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from .oxinsider import Pick
from .storage import atomic_write, file_lock


class MappingError(RuntimeError):
    """Missing or changed contract evidence means no trade is permitted."""


@dataclass(frozen=True)
class SourceFacts:
    fingerprint: str
    condition_id: str
    token_id: str
    outcome_index: Literal[0, 1]
    label: str
    question: str
    description: str
    resolution_source: str
    kickoff: datetime


def source_facts(pick: Pick) -> SourceFacts:
    """Read a unique provider market using SDK 0.10.0 and bind its selected outcome.

    The SDK owns Gamma parsing and token lookup. Its context manager closes all transports.
    Neither the pick's display label nor an event slug supplies contract identity.
    """
    authorization = pick.entry_authorization
    now = datetime.now(UTC)
    if (
        authorization is None
        or pick.token_id is None
        or authorization.token_id != pick.token_id
        or not authorization.issued_at <= now < authorization.expires_at
    ):
        raise MappingError("Source pick has no current matching entry authorization")
    with PublicClient() as client:
        page = client.list_markets(clob_token_ids=pick.token_id, page_size=2).first_page()
    if len(page.items) != 1:
        raise MappingError("Source token does not identify exactly one provider market")
    market = page.items[0]
    condition_id = str(market.condition_id).lower() if market.condition_id is not None else None
    outcomes = (market.outcomes.yes, market.outcomes.no)
    token_ids = tuple(
        str(outcome.token_id) if outcome.token_id is not None else None for outcome in outcomes
    )
    if (
        condition_id != authorization.condition_id.lower()
        or token_ids[authorization.outcome_index] != pick.token_id
        or token_ids[0] is None
        or token_ids[1] is None
        or token_ids[0] == token_ids[1]
    ):
        raise MappingError("Source provider identity disagrees with the pick authorization")
    if (
        market.state.closed is not False
        or market.state.accepting_orders is not True
        or market.state.combo_status not in (None, "disabled")
    ):
        raise MappingError("Source market is unavailable, closed, or a combination contract")
    kickoff = market.sports.game_start_time
    selected = outcomes[authorization.outcome_index]
    question = market.question
    description = market.description
    resolution_source = market.resolution.source
    if (
        not question
        or not question.strip()
        or not description
        or not description.strip()
        or not resolution_source
        or not resolution_source.strip()
        or any(not outcome.label.strip() for outcome in outcomes)
        or outcomes[0].label == outcomes[1].label
        or kickoff is None
        or kickoff.tzinfo is None
        or kickoff <= now
    ):
        raise MappingError(
            "Source provider lacks verifiable rules, outcome, resolution source, or future kickoff"
        )
    snapshot = {
        "format": "potd-trader.kalshi-source.v1",
        "condition_id": condition_id,
        "token_id": pick.token_id,
        "outcome_index": authorization.outcome_index,
        "outcomes": [
            {"token_id": token_ids[index], "label": outcome.label}
            for index, outcome in enumerate(outcomes)
        ],
        "question": question,
        "group_item_title": market.group_item_title,
        "description": description,
        "resolution_source": resolution_source,
        "kickoff": kickoff.astimezone(UTC).isoformat(),
        "sports_market_type": market.sports.sports_market_type,
        "game_id": market.sports.game_id,
        "events": [
            {"id": str(event.id), "slug": event.slug, "title": event.title}
            for event in market.events
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if condition_id is None:
        raise MappingError("Source provider condition is missing")
    return SourceFacts(
        fingerprint=fingerprint,
        condition_id=condition_id,
        token_id=pick.token_id,
        outcome_index=authorization.outcome_index,
        label=selected.label,
        question=question,
        description=description,
        resolution_source=resolution_source,
        kickoff=kickoff,
    )


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    source_token_id: str = Field(pattern=r"^[1-9][0-9]*$")
    source_condition_id: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_ticker: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    destination_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["yes", "no"]
    max_price: Decimal = Field(gt=0, lt=1)
    environment: Literal["demo", "production"]
    kickoff: AwareDatetime
    reviewed_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _times(self) -> Self:
        if not self.reviewed_at < self.expires_at <= self.kickoff:
            raise ValueError("mapping review must precede expiry, and expiry cannot exceed kickoff")
        return self


class _MappingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["potd-trader.kalshi-mappings.v1"]
    mappings: list[Mapping] = Field(max_length=5000)


class MappingStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _read(self) -> list[Mapping]:
        if not self.path.exists():
            return []
        try:
            if self.path.stat().st_size > 2_000_000:
                raise ValueError("mapping file is too large")
            mappings = _MappingFile.model_validate_json(
                self.path.read_text(encoding="utf-8")
            ).mappings
        except (OSError, ValueError, ValidationError) as exc:
            raise MappingError("Invalid Kalshi mappings file; no orders are permitted") from exc
        self._validate_identities(mappings)
        return mappings

    @staticmethod
    def _validate_identities(mappings: list[Mapping]) -> None:
        targets: dict[tuple[str, str], str] = {}
        sources: dict[tuple[str, str], tuple[str, str, str]] = {}
        reviews: dict[tuple[str, str, datetime], Mapping] = {}
        for mapping in mappings:
            target = (mapping.environment, mapping.destination_ticker)
            source = (mapping.environment, mapping.source_token_id)
            destination = (
                mapping.source_condition_id.lower(),
                mapping.destination_ticker,
                mapping.outcome,
            )
            if target in targets and targets[target] != mapping.source_token_id:
                raise MappingError("Multiple source tokens map to the same Kalshi market")
            if source in sources and sources[source] != destination:
                raise MappingError("A source token cannot be reassigned to another Kalshi contract")
            review = (*source, mapping.reviewed_at)
            if review in reviews and reviews[review] != mapping:
                raise MappingError("Conflicting Kalshi mappings have the same review timestamp")
            targets[target] = mapping.source_token_id
            sources[source] = destination
            reviews[review] = mapping

    def get(self, token: str, env: Literal["demo", "production"]) -> Mapping | None:
        with file_lock(self.lock_path):
            matches = [
                mapping
                for mapping in self._read()
                if mapping.source_token_id == token and mapping.environment == env
            ]
        return max(matches, key=lambda mapping: mapping.reviewed_at) if matches else None

    def save(self, mapping: Mapping) -> None:
        """Persist an explicitly reviewed revision and retain previous contract evidence."""
        now = datetime.now(UTC)
        if mapping.reviewed_at > now or mapping.expires_at <= now:
            raise MappingError(
                "Mapping review must be current and its expiry must be in the future"
            )
        with file_lock(self.lock_path):
            entries = self._read()
            if mapping in entries:
                return
            entries.append(mapping)
            self._validate_identities(entries)
            try:
                payload = _MappingFile(format="potd-trader.kalshi-mappings.v1", mappings=entries)
            except ValidationError as exc:
                raise MappingError(
                    "Kalshi mapping history is full; no mapping was changed"
                ) from exc
            atomic_write(self.path, payload.model_dump_json(indent=2) + "\n")

    def entries(self) -> list[Mapping]:
        with file_lock(self.lock_path):
            return self._read()
