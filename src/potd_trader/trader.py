"""Turn today's picks into at most one small BUY each, or a named reason not to.

Every guard is a plain `if`. Read them top to bottom; that is the whole strategy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .config import Settings
from .control import LiveControl
from .ledger import Ledger
from .oxinsider import Pick, Slate
from .polymarket import Account, PublicReads, round_down_to_tick

log = logging.getLogger("potd-trader")


@dataclass(frozen=True)
class Plan:
    pick: Pick
    buy: bool
    reason: str
    # A transient skip (book too thin, price drifted) is worth another look before kickoff.
    transient: bool = False
    quote: Decimal | None = None
    max_price: Decimal | None = None
    stake_usd: Decimal | None = None
    valid_until: datetime | None = None


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator / denominator - 1) * 100


def plan_pick(
    pick: Pick,
    *,
    settings: Settings,
    ledger: Ledger,
    reads: PublicReads,
    now: datetime,
    committed_today: Decimal,
) -> Plan:
    def skip(reason: str, *, transient: bool = False) -> Plan:
        return Plan(pick=pick, buy=False, reason=reason, transient=transient)

    if pick.pick_date != now.astimezone(ZoneInfo("America/New_York")).date().isoformat():
        return skip("pick is not from the current New York product day")
    if pick.is_locked is True or pick.release_at is None or pick.release_at > now:
        return skip("pick is not verifiably released")
    if pick.backed_price is None or not 0 < pick.backed_price < 1:
        return skip("missing or invalid backed price; cannot enforce slippage")
    auth = pick.entry_authorization
    if auth is None:
        return skip("missing entry authorization")
    if auth.token_id != pick.token_id or not auth.issued_at <= now < auth.expires_at:
        return skip("entry authorization mismatches the token or is not current")
    if pick.outcome != "pending":
        return skip(f"already settled ({pick.outcome})")
    if pick.token_id is None:
        return skip("no CLOB token id on this pick")
    if pick.game_started:
        return skip("game already started; the pre-game price is gone")
    if not settings.min_ranks <= pick.pick_rank <= settings.max_ranks:
        return skip(f"rank {pick.pick_rank} is outside MIN_RANKS..MAX_RANKS")
    if ledger.blocks(
        pick.key, pick_date=pick.pick_date, pick_rank=pick.pick_rank, token_id=pick.token_id
    ):
        return skip("pick, slot, or token already reserved in the ledger")

    stake = settings.stake_usd
    if stake <= 0:
        return skip("STAKE_USD is 0")
    if committed_today + stake > settings.daily_cap_usd:
        return skip(
            f"DAILY_CAP_USD {settings.daily_cap_usd} would be exceeded "
            f"({committed_today} committed today)"
        )

    market = reads.market_for_token(pick.token_id)
    if market is None:
        return skip("Polymarket does not list this token")
    if market.closed is not False or market.accepting_orders is not True:
        return skip("market is closed, unavailable, or not explicitly accepting orders")
    if (
        market.condition_id != auth.condition_id
        or market.token_ids[auth.outcome_index] != pick.token_id
    ):
        return skip("Polymarket market identity disagrees with the pick authorization")
    if market.game_start_time is None or market.game_start_time.tzinfo is None:
        return skip("missing verified kickoff; cannot enforce the pre-game restriction")
    buffer = timedelta(minutes=settings.kickoff_buffer_minutes)
    valid_until = min(
        market.game_start_time - buffer, auth.expires_at - buffer, now + timedelta(seconds=30)
    )
    if valid_until <= now:
        return skip("kickoff or authorization expiry is inside KICKOFF_BUFFER_MINUTES")
    if (
        market.tick_size is None
        or not market.tick_size.is_finite()
        or not 0 < market.tick_size < 1
        or market.minimum_order_size is None
        or not market.minimum_order_size.is_finite()
        or market.minimum_order_size <= 0
    ):
        return skip("missing or invalid market tick/minimum order size")
    quote = reads.estimate_buy_price(pick.token_id, stake)
    if quote is None:
        return skip(f"no resting liquidity for a {stake} pUSD buy", transient=True)
    if not quote.is_finite() or not 0 < quote < 1:
        return skip("invalid executable book price")
    if quote > auth.max_entry_price:
        return skip("book price exceeds the entry authorization ceiling", transient=True)
    if quote > settings.max_price:
        return skip(f"book price {quote} is above MAX_PRICE {settings.max_price}", transient=True)
    if pick.backed_price:
        backed = Decimal(str(pick.backed_price))
        drift = _pct(quote, backed)
        if drift > settings.max_slippage_pct:
            return skip(
                f"book price {quote} is {drift:.1f}% above the pick's price {backed} "
                f"(MAX_SLIPPAGE_PCT {settings.max_slippage_pct})",
                transient=True,
            )

    max_price = round_down_to_tick(quote, market.tick_size)
    if max_price <= 0:
        return skip("book price is below the market tick")
    if stake < market.minimum_order_size * max_price:
        return skip(
            f"STAKE_USD {stake} cannot buy the market minimum of "
            f"{market.minimum_order_size} shares at the order ceiling {max_price}"
        )
    return Plan(
        pick=pick,
        buy=True,
        reason=f"book {quote} <= MAX_PRICE {settings.max_price}",
        quote=quote,
        max_price=max_price,
        stake_usd=stake,
        valid_until=valid_until,
    )


def plan_slate(
    slate: Slate, *, settings: Settings, ledger: Ledger, reads: PublicReads
) -> list[Plan]:
    now = datetime.now(UTC)
    ranks = [pick.pick_rank for pick in slate.picks]
    tokens = [pick.token_id for pick in slate.picks if pick.token_id is not None]
    if (
        len(ranks) != len(set(ranks))
        or len(tokens) != len(set(tokens))
        or any(pick.pick_date != slate.pick_date for pick in slate.picks)
    ):
        return [
            Plan(pick=pick, buy=False, reason="duplicate or inconsistent slate identity")
            for pick in slate.picks
        ]
    committed = ledger.spent_today_usd(now)
    plans: list[Plan] = []
    for pick in sorted(slate.picks, key=lambda item: item.pick_rank):
        plan = plan_pick(
            pick,
            settings=settings,
            ledger=ledger,
            reads=reads,
            now=now,
            committed_today=committed,
        )
        if plan.buy and plan.stake_usd is not None:
            committed += plan.stake_usd
        plans.append(plan)
    return plans


def execute(
    plan: Plan,
    *,
    account: Account,
    ledger: Ledger,
    settings: Settings,
    reads: PublicReads,
) -> bool:
    """Revalidate, sign without posting, then atomically authorize/reserve/post once."""
    control = LiveControl(settings.control_env_path)
    if not plan.buy or not control.enabled(settings.is_live):
        log.info("SKIP %s | live trading is stopped or plan declined", plan.pick.label)
        return False
    plan = plan_pick(
        plan.pick,
        settings=settings,
        ledger=ledger,
        reads=reads,
        now=datetime.now(UTC),
        committed_today=ledger.spent_today_usd(),
    )
    if not plan.buy:
        log.info("SKIP %s | submission recheck: %s", plan.pick.label, plan.reason)
        return False
    pick = plan.pick
    if (
        plan.stake_usd is None
        or plan.max_price is None
        or pick.token_id is None
        or plan.valid_until is None
    ):
        raise ValueError("buy plan has incomplete submission fields")
    # SDK 0.10.0 create_market_order only signs; post_order is the separate write. This
    # allows a stop/expiry check AFTER slow metadata/signing work, and avoids the combined
    # helper's automatic allowance transactions and repost path.
    signed = account.prepare_buy(pick.token_id, plan.stake_usd, plan.max_price)
    with control.submission(settings.is_live) as enabled:
        if not enabled or datetime.now(UTC) >= plan.valid_until:
            log.info("SKIP %s | stopped or quote/kickoff expired before submission", pick.label)
            return False
        declined = ledger.reserve(
            pick.key,
            daily_cap=settings.daily_cap_usd,
            stake_usd=plan.stake_usd,
            pick_date=pick.pick_date,
            pick_rank=pick.pick_rank,
            label=pick.label,
            token_id=pick.token_id,
            max_price=str(plan.max_price),
            wallet=account.wallet,
            authorization_id=str(pick.entry_authorization.authorization_id)
            if pick.entry_authorization
            else None,
        )
        if declined:
            log.info("SKIP %s | %s", pick.label, declined)
            return False
        # Recheck after the durable disk write too. No order exists on this branch.
        if not control.enabled(settings.is_live) or datetime.now(UTC) >= plan.valid_until:
            ledger.finish(pick.key, "rejected", message="stopped or expired before post")
            return False
        try:
            response = account.submit_buy(signed)
        except Exception as exc:
            # Raw exception messages may contain headers or signing inputs. Store only type.
            ledger.finish(pick.key, "unknown", error=type(exc).__name__)
            log.error(
                "%s: order result unknown (%s); check Polymarket Activity. "
                "The pick and budget remain reserved.",
                pick.label,
                type(exc).__name__,
            )
            raise
        if response.ok:
            ledger.finish(
                pick.key,
                "accepted",
                order_id=str(response.order_id),
                status=response.status,
                making_amount=str(response.making_amount),
                taking_amount=str(response.taking_amount),
                trade_ids=list(response.trade_ids),
            )
            log.info(
                "%s: %s, order %s, spent %s pUSD for %s shares",
                pick.label,
                response.status,
                response.order_id,
                response.making_amount,
                response.taking_amount,
            )
        else:
            # An unclassified SDK answer is not evidence that the exchange did nothing.
            state = "unknown" if response.code == "unknown" else "rejected"
            ledger.finish(pick.key, state, code=response.code)
            log.warning("%s: exchange %s (%s)", pick.label, state, response.code)
    return True
