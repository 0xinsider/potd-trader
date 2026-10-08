"""Reviewed destination contracts, bounded exchange debits, and one IOC submission."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from .control import LiveControl
from .kalshi import KalshiClient, KalshiNotSubmitted, MarketBundle
from .kalshi_config import KalshiSettings
from .kalshi_mapping import Mapping, MappingError, MappingStore, source_facts
from .ledger import Ledger
from .oxinsider import Pick, PickAccess

log = logging.getLogger("potd-trader")


@dataclass(frozen=True)
class KalshiPlan:
    pick: Pick
    access: PickAccess
    buy: bool
    reason: str
    mapping: Mapping | None = None
    bundle: MarketBundle | None = None
    count: int = 0
    max_price: Decimal = Decimal("0")
    reserve_usd: Decimal = Decimal("0")
    fee_bound: Decimal = Decimal("0")
    valid_until: datetime | None = None

    @property
    def key(self) -> str:
        return f"kalshi:{self.pick.key}"

    @property
    def target_identity(self) -> str:
        if self.mapping is None:
            raise ValueError("no destination identity on declined plan")
        # One destination market cannot be bought twice, including opposite sides.
        return f"kalshi:{self.mapping.environment}:{self.mapping.destination_ticker}"


def plan_pick(
    pick: Pick,
    access: PickAccess,
    *,
    settings: KalshiSettings,
    ledger: Ledger,
    mappings: MappingStore,
    client: KalshiClient,
    committed: Decimal,
    reserved_picks: int,
) -> KalshiPlan:
    def skip(reason: str) -> KalshiPlan:
        return KalshiPlan(pick, access, False, reason)

    now = datetime.now(UTC)
    started_at = now
    if pick.pick_date != now.astimezone(ZoneInfo("America/New_York")).date().isoformat():
        return skip("pick is not from the current New York date")
    if pick.is_locked is True or pick.release_at is None or pick.release_at > now:
        return skip("pick is not verifiably released")
    if pick.outcome != "pending" or pick.game_started:
        return skip("pick is settled or its game has started")
    auth = pick.entry_authorization
    if auth is None or pick.token_id is None:
        return skip("pick has no source entry authorization or token")
    if auth.token_id != pick.token_id or not auth.issued_at <= now < auth.expires_at:
        return skip("source entry authorization is mismatched or expired")
    if pick.backed_price is None or not 0 < pick.backed_price < 1:
        return skip("source backed price is unavailable; cannot enforce slippage")
    mapping = mappings.get(pick.token_id, settings.environment)
    if mapping is None:
        return skip("no reviewed mapping; run kalshi map for this pick and contract")
    if mapping.source_condition_id.lower() != auth.condition_id.lower():
        return skip("reviewed source condition disagrees with the pick authorization")
    if not mapping.reviewed_at <= now < mapping.expires_at:
        return skip("reviewed mapping has expired or is not yet valid")
    if reserved_picks >= access.daily_pick_limit:
        return skip("authenticated daily pick allowance is already reserved")
    if ledger.blocks(
        f"kalshi:{pick.key}",
        pick_date=pick.pick_date,
        pick_rank=pick.pick_rank,
        token_id=f"kalshi:{settings.environment}:{mapping.destination_ticker}",
    ):
        return skip("pick, slot, or destination market is already reserved")
    if settings.stake_usd <= 0:
        return skip("STAKE_USD is zero")
    try:
        facts = source_facts(pick)
    except MappingError as exc:
        return skip(str(exc))
    if facts.fingerprint != mapping.source_fingerprint or facts.kickoff != mapping.kickoff:
        return skip("source identity, outcome, rules or kickoff changed; review the mapping again")
    if (
        facts.condition_id.lower() != auth.condition_id.lower()
        or facts.outcome_index != auth.outcome_index
    ):
        return skip("source provider identity disagrees with the authorization")
    bundle = client.bundle(mapping.destination_ticker)
    if bundle.fingerprint != mapping.destination_fingerprint:
        return skip("Kalshi identity or settlement terms changed; review the mapping again")
    if not client.is_tradable(bundle):
        return skip("Kalshi market or its exchange shard is not accepting trades")
    now = datetime.now(UTC)
    buffer = timedelta(minutes=settings.kickoff_buffer_minutes)
    deadline = min(
        mapping.kickoff - buffer,
        auth.expires_at - buffer,
        mapping.expires_at - buffer,
        started_at + timedelta(seconds=30),
    )
    if deadline <= now:
        return skip("kickoff, mapping or source authorization is inside the timing buffer")
    ceiling = min(
        mapping.max_price,
        settings.max_price,
        auth.max_entry_price,
        pick.backed_price * (1 + settings.max_slippage_pct / 100),
    )
    fee_per_contract = bundle.fee_per_contract
    quantity = int(
        (settings.stake_usd / (ceiling + fee_per_contract)).to_integral_value(ROUND_DOWN)
    )
    if quantity < 1:
        return skip("stake cannot fund one contract plus the conservative exchange fee reserve")
    offers = client.offers(bundle, mapping.outcome)
    depth = Decimal("0")
    quote: Decimal | None = None
    for price, size in offers:
        if price > ceiling:
            break
        depth += size
        if depth >= quantity:
            quote = price
            break
    if quote is None:
        return skip("insufficient resting depth inside the source and destination price ceilings")
    fee_bound = fee_per_contract * quantity
    reserve = quote * quantity + fee_bound
    if committed + reserve > settings.daily_cap_usd:
        return skip("DAILY_CAP_USD would be exceeded including the exchange fee reserve")
    if datetime.now(UTC) >= deadline:
        return skip("quote or kickoff expired during provider checks")
    return KalshiPlan(
        pick,
        access,
        True,
        "reviewed contract and all guards passed",
        mapping,
        bundle,
        quantity,
        quote,
        reserve,
        fee_bound,
        deadline,
    )


def execute(
    plan: KalshiPlan,
    *,
    settings: KalshiSettings,
    ledger: Ledger,
    mappings: MappingStore,
    client: KalshiClient,
) -> bool:
    control = LiveControl(settings.control_env_path)
    if not plan.buy or not control.enabled(settings.is_live):
        return False
    settings.bind_account()
    plan = plan_pick(
        plan.pick,
        plan.access,
        settings=settings,
        ledger=ledger,
        mappings=mappings,
        client=client,
        committed=ledger.spent_today_usd(),
        reserved_picks=ledger.reserved_picks(plan.pick.pick_date),
    )
    if not plan.buy:
        log.info("SKIP %s | submission recheck: %s", plan.pick.label, plan.reason)
        return False
    if plan.bundle is None or plan.mapping is None or plan.valid_until is None:
        raise ValueError("buy plan has incomplete destination fields")
    deadline = plan.valid_until
    if client.has_exposure(plan.bundle):
        log.warning("SKIP %s | existing destination position or resting order", plan.pick.label)
        return False
    if client.balance(plan.bundle.exchange_index) < plan.reserve_usd:
        log.warning(
            "SKIP %s | insufficient available balance on the destination shard", plan.pick.label
        )
        return False
    client_order_id = str(uuid4())
    with control.submission(settings.is_live) as enabled:
        if not enabled or datetime.now(UTC) >= plan.valid_until:
            return False
        # File-only configuration is reread inside the barrier. A changed account,
        # environment or sizing never inherits a previously planned permission.
        current = KalshiSettings.load()
        if current.model_dump() != settings.model_dump():
            raise ValueError("configuration changed; restart this run before submitting")
        declined = ledger.reserve(
            plan.key,
            daily_cap=settings.daily_cap_usd,
            daily_pick_limit=plan.access.daily_pick_limit,
            stake_usd=plan.reserve_usd,
            pick_date=plan.pick.pick_date,
            pick_rank=plan.pick.pick_rank,
            token_id=plan.target_identity,
            provider="kalshi",
            environment=settings.environment,
            ticker=plan.mapping.destination_ticker,
            outcome=plan.mapping.outcome,
            count=plan.count,
            client_order_id=client_order_id,
            exchange_index=plan.bundle.exchange_index,
            label=plan.pick.label,
            max_price=str(plan.max_price),
            fee_bound=str(plan.fee_bound),
            source_token_id=plan.pick.token_id,
        )
        if declined:
            log.info("SKIP %s | %s", plan.pick.label, declined)
            return False
        if not control.enabled(settings.is_live) or datetime.now(UTC) >= plan.valid_until:
            ledger.finish(plan.key, "rejected", message="stopped or expired before post")
            return False
        try:
            receipt = client.submit_buy(
                plan.bundle,
                plan.mapping.outcome,
                plan.count,
                plan.max_price,
                client_order_id,
                before_post=lambda: (
                    KalshiSettings.load().model_dump() == settings.model_dump()
                    and control.enabled(settings.is_live)
                    and datetime.now(UTC) < deadline
                ),
            )
        except KalshiNotSubmitted as exc:
            ledger.finish(plan.key, "rejected", message=str(exc))
            log.info("SKIP %s | provider submission guard: %s", plan.pick.label, exc)
            return False
        except Exception as exc:
            ledger.finish(plan.key, "unknown", error=type(exc).__name__)
            log.error(
                "Kalshi result unknown; reservation retained. Run kalshi reconcile; never repost."
            )
            raise
        if (
            receipt.client_order_id not in (None, client_order_id)
            or receipt.fill_count < 0
            or receipt.fill_count > plan.count
            or receipt.remaining_count != 0
        ):
            ledger.finish(
                plan.key,
                "unknown",
                order_id=receipt.order_id,
                message="unexpected IOC receipt; reconcile before any retry",
            )
            raise ValueError("unexpected Kalshi IOC receipt")
        if receipt.fill_count > 0:
            if receipt.average_fill_price is None or receipt.average_fee_paid is None:
                ledger.finish(
                    plan.key,
                    "unknown",
                    order_id=receipt.order_id,
                    message="fill debit unavailable; reservation retained",
                )
                raise ValueError("Kalshi filled receipt has no price/fee evidence")
            outcome_price = (
                receipt.average_fill_price
                if plan.mapping.outcome == "yes"
                else 1 - receipt.average_fill_price
            )
            debit = receipt.fill_count * (outcome_price + receipt.average_fee_paid)
            if not 0 <= outcome_price <= plan.max_price or not 0 <= debit <= plan.reserve_usd:
                ledger.finish(
                    plan.key,
                    "unknown",
                    order_id=receipt.order_id,
                    message="filled price or debit exceeded the reservation",
                )
                raise ValueError("Kalshi filled debit exceeded the authorized bounds")
            ledger.finish(
                plan.key,
                "accepted",
                order_id=receipt.order_id,
                fill_count=str(receipt.fill_count),
                actual_debit_usd=str(debit),
            )
            log.info(
                "FILLED %s | %s contracts | exchange debit %s USD | order %s",
                plan.pick.label,
                receipt.fill_count,
                debit,
                receipt.order_id,
            )
        else:
            ledger.finish(
                plan.key,
                "unknown",
                order_id=receipt.order_id,
                message="zero-fill acknowledgement needs terminal provider reconciliation",
            )
            log.info(
                "UNCONFIRMED %s | order %s | run kalshi reconcile",
                plan.pick.label,
                receipt.order_id,
            )
    return True
