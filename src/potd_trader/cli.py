"""`potd-trader` commands: status, setup, run, watch, ledger."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError

from . import __version__
from .config import Settings
from .ledger import Ledger
from .oxinsider import (
    NotModified,
    NotReleased,
    OxinsiderClient,
    OxinsiderError,
    PickResult,
    Slate,
    TryLater,
)
from .polymarket import Account, PublicReads, geoblock
from .trader import Plan, execute, plan_slate

log = logging.getLogger("potd-trader")

MIN_SLEEP = timedelta(seconds=30)


def _settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        raise SystemExit(
            f"Configuration problem: {missing}. Copy .env.example to .env and fill it in."
        ) from exc


def _mode_banner(settings: Settings) -> None:
    if settings.is_live:
        log.warning(
            "LIVE=yes: this run spends real pUSD. STAKE_USD=%s MAX_PRICE=%s DAILY_CAP_USD=%s",
            settings.stake_usd,
            settings.max_price,
            settings.daily_cap_usd,
        )
    else:
        log.info('DRY RUN (LIVE is not "yes"): nothing is bought. Plans are printed only.')


def _fmt_time(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ") if value else "unknown"


def _print_plans(plans: list[Plan]) -> None:
    for plan in plans:
        pick = plan.pick
        price = pick.odds_display or (f"{pick.backed_price:.3f}" if pick.backed_price else "?")
        if plan.buy:
            log.info(
                "BUY  %s | pick price %s | book %s | stake %s pUSD | max %s",
                pick.label,
                price,
                plan.quote,
                plan.stake_usd,
                plan.max_price,
            )
        else:
            log.info("SKIP %s | pick price %s | %s", pick.label, price, plan.reason)


def _preflight(settings: Settings, total_stake: Decimal) -> Account | None:
    """Everything that must be true before the first live order. Returns None to abort."""
    geo = geoblock()
    if geo.blocked:
        log.error(
            "Polymarket blocks trading from this location (%s %s). Not buying.",
            geo.country,
            geo.region,
        )
        return None
    account = Account(settings)
    log.info("Polymarket account %s (%s)", account.wallet, account.wallet_type)
    ready, missing = account.approvals_ready()
    if not ready:
        log.error(
            "Trading approvals are not set for this wallet (%s). Run `potd-trader setup` once "
            "with a Relayer API key, or place one trade on polymarket.com first.",
            missing,
        )
        account.close()
        return None
    balance = account.collateral_balance_usd()
    log.info("pUSD balance %s", balance)
    if balance < total_stake:
        log.error(
            "Balance %s pUSD is below the %s pUSD this run wants to spend. Not buying.",
            balance,
            total_stake,
        )
        account.close()
        return None
    return account


def _handle_result(
    result: PickResult, settings: Settings, ledger: Ledger
) -> tuple[list[Plan], Slate | None]:
    if isinstance(result, NotReleased):
        log.info("No pick released yet. Earliest change: %s", _fmt_time(result.retry_at))
        return [], None
    if isinstance(result, TryLater):
        log.info(
            "0xinsider answered %s (%s). Try again at %s",
            result.status,
            result.reason,
            _fmt_time(result.retry_at),
        )
        return [], None
    if isinstance(result, NotModified):
        log.info("No change since the last read.")
        return [], None
    slate = result
    log.info(
        "Product day %s: %d released pick(s), %d scheduled",
        slate.pick_date,
        len(slate.picks),
        len(slate.scheduled),
    )
    plans = plan_slate(slate, settings=settings, ledger=ledger, reads=PublicReads())
    _print_plans(plans)
    buys = [plan for plan in plans if plan.buy]
    if buys and settings.is_live:
        total = sum((plan.stake_usd or Decimal("0") for plan in buys), Decimal("0"))
        account = _preflight(settings, total)
        if account is not None:
            try:
                for plan in buys:
                    execute(plan, account=account, ledger=ledger)
            finally:
                account.close()
    elif buys:
        log.info("%d pick(s) would be bought. Set LIVE=yes to buy for real.", len(buys))
    if slate.next_release_at:
        log.info("Next scheduled release: %s", _fmt_time(slate.next_release_at))
    return plans, slate


def cmd_run(settings: Settings) -> int:
    _mode_banner(settings)
    ledger = Ledger(settings.ledger_path)
    client = OxinsiderClient(
        settings.oxinsider_api_base, settings.oxinsider_api_key.get_secret_value()
    )
    try:
        _handle_result(client.pick_of_the_day(), settings, ledger)
    finally:
        client.close()
    return 0


def _next_wake(
    result: PickResult, plans: list[Plan], settings: Settings, now: datetime
) -> datetime:
    idle = now + timedelta(minutes=settings.watch_idle_minutes)
    candidates: list[datetime] = []
    if isinstance(result, NotReleased | TryLater) and result.retry_at:
        candidates.append(result.retry_at)
    if isinstance(result, Slate) and result.next_release_at:
        candidates.append(result.next_release_at)
    if any(plan.transient for plan in plans):
        candidates.append(now + timedelta(minutes=5))
    wake = min(candidates) if candidates else idle
    return max(wake, now + MIN_SLEEP)


def cmd_watch(settings: Settings) -> int:
    _mode_banner(settings)
    log.info("Watching. Ctrl-C stops it. Nothing is polled faster than the server asks for.")
    client = OxinsiderClient(
        settings.oxinsider_api_base, settings.oxinsider_api_key.get_secret_value()
    )
    etag: str | None = None
    last_slate: Slate | None = None
    try:
        while True:
            ledger = Ledger(settings.ledger_path)
            result = client.pick_of_the_day(etag=etag)
            if isinstance(result, NotModified) and last_slate is not None:
                # Same picks as before: re-plan them, because the book or the ledger may have moved.
                result = last_slate
            plans, slate = _handle_result(result, settings, ledger)
            if slate is not None:
                etag, last_slate = slate.etag, slate
            elif isinstance(result, NotReleased):
                etag, last_slate = None, None
            now = datetime.now(UTC)
            wake = _next_wake(result, plans, settings, now)
            log.info("Sleeping until %s", _fmt_time(wake))
            time.sleep((wake - now).total_seconds())
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    finally:
        client.close()


def cmd_status(settings: Settings) -> int:
    _mode_banner(settings)
    geo = geoblock()
    log.info(
        "Polymarket geoblock: %s (%s %s)",
        "BLOCKED" if geo.blocked else "ok",
        geo.country,
        geo.region,
    )
    if settings.has_polymarket_credentials:
        account = Account(settings)
        try:
            log.info("Polymarket account %s (%s)", account.wallet, account.wallet_type)
            ready, missing = account.approvals_ready()
            log.info(
                "Trading approvals: %s%s",
                "SET" if ready else "MISSING",
                "" if ready else f" ({missing})",
            )
            log.info("pUSD balance %s", account.collateral_balance_usd())
        finally:
            account.close()
    else:
        log.info("Polymarket credentials not configured; only dry runs are possible.")
    ledger = Ledger(settings.ledger_path)
    entries = ledger.entries()
    log.info("Ledger %s: %d order(s)", settings.ledger_path, len(entries))
    return 0


def cmd_setup(settings: Settings) -> int:
    account = Account(settings)
    try:
        log.info("Polymarket account %s (%s)", account.wallet, account.wallet_type)
        ready, missing = account.approvals_ready()
        if ready:
            log.info("Trading approvals already SET. Nothing to do.")
            return 0
        if not (
            settings.polymarket_relayer_api_key and settings.polymarket_relayer_api_key_address
        ):
            log.error(
                "Trading approvals MISSING (%s). Create a Relayer API key at polymarket.com > "
                "Settings > API Keys, put it in .env, and run setup again. This submits the "
                "approval transactions gaslessly; it never places an order.",
                missing,
            )
            return 1
        log.info("Submitting trading approvals through the relayer...")
        account.setup_approvals()
        ready, missing = account.approvals_ready()
        log.info(
            "Trading approvals: %s%s",
            "SET" if ready else "MISSING",
            "" if ready else f" ({missing})",
        )
        return 0 if ready else 1
    finally:
        account.close()


def cmd_ledger(settings: Settings) -> int:
    ledger = Ledger(settings.ledger_path)
    entries = ledger.entries()
    if not entries:
        log.info("Ledger %s is empty.", settings.ledger_path)
        return 0
    for key, entry in entries:
        log.info(
            "%-10s %s | %s pUSD | order %s | %s",
            entry.get("state"),
            entry.get("label", key),
            entry.get("stake_usd"),
            entry.get("order_id", "-"),
            entry.get("message") or entry.get("error") or entry.get("status") or "",
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="potd-trader",
        description="Buy the 0xinsider Pick of the Day on your own Polymarket account.",
    )
    parser.add_argument("--version", action="version", version=f"potd-trader {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="check region, account, approvals, balance and the ledger")
    sub.add_parser("setup", help="set trading approvals once (needs a Relayer API key)")
    sub.add_parser("run", help="read today's picks and buy each one at most once, then exit")
    sub.add_parser("watch", help="keep running: wake for each release and buy as picks appear")
    sub.add_parser("ledger", help="print every order this tool has placed")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = _settings()
    commands = {
        "status": cmd_status,
        "setup": cmd_setup,
        "run": cmd_run,
        "watch": cmd_watch,
        "ledger": cmd_ledger,
    }
    try:
        return commands[args.command](settings)
    except OxinsiderError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
