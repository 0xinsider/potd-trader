"""Turn today's picks into at most one small BUY each, or a named reason not to.

Every guard is a plain `if`. Read them top to bottom; that is the whole strategy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .config import Settings
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

    if pick.outcome != "pending":
        return skip(f"already settled ({pick.outcome})")
    if pick.token_id is None:
        return skip("no CLOB token id on this pick")
    if pick.game_started:
        return skip("game already started; the pre-game price is gone")
    if not settings.min_ranks <= pick.pick_rank <= settings.max_ranks:
        return skip(f"rank {pick.pick_rank} is outside MIN_RANKS..MAX_RANKS")
    if ledger.blocks(pick.key):
        entry = ledger.get(pick.key) or {}
        return skip(f"already in the ledger ({entry.get('state')}, order {entry.get('order_id')})")

    stake = settings.stake_usd
    if stake <= 0:
        return skip("STAKE_USD is 0")
    if settings.daily_cap_usd > 0 and committed_today + stake > settings.daily_cap_usd:
        return skip(
            f"DAILY_CAP_USD {settings.daily_cap_usd} would be exceeded "
            f"({committed_today} committed today)"
        )

    market = reads.market_for_token(pick.token_id)
    if market is None:
        return skip("Polymarket does not list this token")
    if market.closed or market.accepting_orders is False:
        return skip("market is closed or not accepting orders")
    if market.game_start_time is not None:
        buffer = timedelta(minutes=settings.kickoff_buffer_minutes)
        if market.game_start_time <= now + buffer:
            return skip(
                f"kickoff {market.game_start_time.isoformat()} is within "
                f"KICKOFF_BUFFER_MINUTES ({settings.kickoff_buffer_minutes})"
            )
    if market.minimum_order_size is not None and settings.max_price > 0:
        min_stake = market.minimum_order_size * settings.max_price
        if stake < min_stake:
            return skip(
                f"STAKE_USD {stake} cannot buy the market minimum of "
                f"{market.minimum_order_size} shares at MAX_PRICE {settings.max_price}"
            )

    quote = reads.estimate_buy_price(pick.token_id, stake)
    if quote is None:
        return skip(f"no resting liquidity for a {stake} pUSD buy", transient=True)
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
        max_price = quote
    return Plan(
        pick=pick,
        buy=True,
        reason=f"book {quote} <= MAX_PRICE {settings.max_price}",
        quote=quote,
        max_price=max_price,
        stake_usd=stake,
    )


def plan_slate(
    slate: Slate, *, settings: Settings, ledger: Ledger, reads: PublicReads
) -> list[Plan]:
    now = datetime.now(UTC)
    committed = ledger.spent_today_usd(slate.pick_date or "")
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


def execute(plan: Plan, *, account: Account, ledger: Ledger) -> None:
    """Post ONE order for a plan that says buy. The ledger entry precedes the post."""
    pick = plan.pick
    assert plan.buy and plan.stake_usd is not None and plan.max_price is not None
    assert pick.token_id is not None
    ledger.start(
        pick.key,
        pick_date=pick.pick_date,
        pick_rank=pick.pick_rank,
        label=pick.label,
        token_id=pick.token_id,
        stake_usd=str(plan.stake_usd),
        max_price=str(plan.max_price),
        wallet=account.wallet,
    )
    try:
        response = account.buy(pick.token_id, plan.stake_usd, plan.max_price)
    except Exception as exc:
        # The post may or may not have reached the exchange. Keep the pick blocked and say so.
        ledger.finish(pick.key, "unknown", error=f"{type(exc).__name__}: {exc}")
        log.error(
            "%s: order result unknown (%s). Check Polymarket > Activity before retrying; "
            "the ledger keeps this pick blocked until you remove the entry.",
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
        if response.status == "delayed":
            log.info(
                "%s: the market delays matching; check Polymarket > Activity for the fill",
                pick.label,
            )
    else:
        ledger.finish(pick.key, "rejected", code=response.code, message=response.message)
        log.warning(
            "%s: rejected by the exchange (%s: %s)", pick.label, response.code, response.message
        )
