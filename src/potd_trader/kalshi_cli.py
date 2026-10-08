"""Local, environment-bound Kalshi commands; setup never enables trading."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from polymarket import PolymarketError
from portalocker.exceptions import BaseLockException
from pydantic import ValidationError

from . import __version__
from .control import LiveControl
from .kalshi import KalshiClient, KalshiError
from .kalshi_config import KalshiConfigError, KalshiSettings
from .kalshi_mapping import Mapping, MappingError, MappingStore, source_facts
from .kalshi_setup import setup
from .kalshi_trader import execute, plan_pick
from .ledger import Ledger, LedgerError
from .oxinsider import NotModified, NotReleased, OxinsiderClient, OxinsiderError, Slate, TryLater
from .storage import atomic_write
from .wizard import prepare_folder, require_tty, write_env

log = logging.getLogger("potd-trader")
REVIEW_PHRASE = "the same event, outcome and settlement rules"


class FeedWait(OxinsiderError):
    def __init__(self, result: TryLater) -> None:
        self.delay = (
            max(60.0, (result.retry_at - datetime.now(UTC)).total_seconds())
            if result.retry_at
            else 60.0
        )
        super().__init__(f"feed unavailable ({result.status}); retry at {result.retry_at}")


def _client(settings: KalshiSettings, *, authenticated: bool = False) -> KalshiClient:
    if authenticated:
        settings.require_kalshi_credentials()
        if not settings.kalshi_api_key_id or not settings.kalshi_private_key_path:
            raise KalshiError("fill KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env.kalshi")
        return KalshiClient(
            settings.environment, settings.kalshi_api_key_id, settings.kalshi_private_key_path
        )
    return KalshiClient(settings.environment)


def _slate(settings: KalshiSettings) -> Slate | None:
    settings.require_feed_credentials()
    client = OxinsiderClient(
        settings.oxinsider_api_base, settings.oxinsider_api_key.get_secret_value()
    )
    try:
        result = client.pick_of_the_day()
    finally:
        client.close()
    if isinstance(result, NotReleased):
        log.info("No pick released. Earliest change: %s", result.retry_at or "not provided")
        return None
    if isinstance(result, TryLater):
        raise FeedWait(result)
    if isinstance(result, NotModified):
        raise OxinsiderError("unexpected uncached 304; no candidate is assumed")
    return result


def cmd_init(directory: Path, environment: str, *, guided: bool = False) -> int:
    folder = prepare_folder(directory)
    # No inherited LIVE or API key is copied. The user enters credentials in the
    # private local file; the command is usable without a terminal or any key.
    write_env(
        folder / ".env.kalshi",
        {
            "KALSHI_ENVIRONMENT": environment,
            "KALSHI_API_KEY_ID": "",
            "KALSHI_PRIVATE_KEY_PATH": '"' + str(folder / "kalshi-key.pem") + '"',
            "OXINSIDER_API_KEY": "",
            "LIVE": "no",
            "STAKE_USD": "5",
            "DAILY_CAP_USD": "25",
            "MAX_PRICE": "0.925",
            "MAX_SLIPPAGE_PCT": "3",
            "KICKOFF_BUFFER_MINUTES": "5",
        },
    )
    atomic_write(folder / "HALT", "Kalshi setup starts stopped\n")
    atomic_write(folder / ".gitignore", ".env*\n*.pem\n*.json*\n.*lock\nHALT\n.kalshi-*\n")
    log.info("Created stopped %s configuration: %s", environment, folder / ".env.kalshi")
    if not guided:
        log.info("Save your matching Kalshi account's PEM key to %s", folder / "kalshi-key.pem")
        log.info("Fill KALSHI_API_KEY_ID and OXINSIDER_API_KEY privately, then cd %s", folder)
        log.info("Run potd-trader kalshi status, then picks, markets, map and run --dry-run.")
    return 0


def cmd_status(settings: KalshiSettings) -> int:
    control = LiveControl(settings.control_env_path)
    log.info(
        "Environment %s | trading %s | configuration %s",
        settings.environment,
        "enabled" if control.enabled(settings.is_live) else "stopped",
        settings.control_env_path,
    )
    log.info(
        "Per-pick maximum reservation %s USD; UTC daily cap %s USD, including fee reserves",
        settings.stake_usd,
        settings.daily_cap_usd,
    )
    log.info(
        "Ledger reserved %s USD; %d reviewed mappings",
        Ledger(settings.ledger_path).spent_today_usd(),
        len(MappingStore(settings.mappings_path).entries()),
    )
    client = _client(settings, authenticated=True)
    try:
        log.info(
            "Available Kalshi balance on shard 0: %s USD; buys check their own shard",
            client.balance(0),
        )
    finally:
        client.close()
    return 0


def cmd_picks(settings: KalshiSettings) -> int:
    slate = _slate(settings)
    if slate is None:
        return 0
    for pick in slate.picks:
        log.info(
            "Slot %s | %s | %s | source token %s",
            pick.pick_rank,
            pick.label,
            pick.outcome,
            pick.token_id or "unavailable",
        )
    log.info(
        "%s account: up to %s daily picks; %d released",
        slate.access.tier,
        slate.access.daily_pick_limit,
        len(slate.picks),
    )
    return 0


def cmd_markets(settings: KalshiSettings, series: str | None) -> int:
    client = _client(settings)
    try:
        rows = client.markets(series)
        for row in rows:
            log.info(
                "%s | %s | YES: %s | status %s",
                row.get("ticker"),
                row.get("title"),
                row.get("yes_sub_title"),
                row.get("status"),
            )
        log.info(
            "%s environment: first %d open market(s); discovery is not a verified mapping",
            settings.environment,
            len(rows),
        )
    finally:
        client.close()
    return 0


def cmd_map(
    settings: KalshiSettings, rank: int, ticker: str, outcome: str, ceiling: Decimal
) -> int:
    require_tty()
    if not ceiling.is_finite() or not 0 < ceiling < 1:
        raise ValueError("mapping price ceiling must be between zero and one")
    control = LiveControl(settings.control_env_path)
    if control.enabled(True):
        raise ValueError("run kalshi live off before reviewing mappings")
    slate = _slate(settings)
    if slate is None:
        return 1
    selected = [pick for pick in slate.picks if pick.pick_rank == rank]
    if len(selected) != 1:
        raise ValueError("slot is not an accessible released pick")
    pick = selected[0]
    if pick.outcome != "pending" or pick.game_started:
        raise ValueError("cannot map a settled or started pick")
    facts = source_facts(pick)
    auth = pick.entry_authorization
    now = datetime.now(UTC)
    if auth is None or not auth.issued_at <= now < auth.expires_at or facts.kickoff <= now:
        raise ValueError("source entry authority or verified kickoff is not current")
    client = _client(settings)
    try:
        bundle = client.bundle(ticker)
        if not client.is_tradable(bundle):
            raise ValueError("destination is not currently accepting trades")
        print(f"\nEnvironment: {settings.environment}")
        print(f"Source proposition: {facts.question}\nSelected source outcome: {facts.label}")
        print(f"Source rules: {facts.description}\nSource resolution: {facts.resolution_source}")
        print(f"Source verified kickoff (hard cutoff): {facts.kickoff.isoformat()}")
        print(f"Kalshi ticker: {ticker}\nBuying {outcome.upper()} at at most {ceiling} USD")
        for name in ("title", "yes_sub_title", "no_sub_title", "rules_primary", "rules_secondary"):
            print(f"{name}: {bundle.market.get(name, '')}")
        for name in ("contract_url", "contract_terms_url", "settlement_sources"):
            print(f"{name}: {bundle.series.get(name, '')}")
        print("Review linked terms too. Check game versus map, overtime, ties, postponement,")
        print("cancellation and settlement. Different or uncertain contracts must be skipped.")
        print(f'If equivalent, type "{REVIEW_PHRASE}"; Enter cancels.')
        if input("> ").strip() != REVIEW_PHRASE:
            log.info("Mapping not saved.")
            return 1
        # A human review can take minutes; re-read both providers before saving.
        current_facts = source_facts(pick)
        current_bundle = client.bundle(ticker)
        if current_facts != facts or current_bundle.fingerprint != bundle.fingerprint:
            raise ValueError("provider facts changed during review; review again")
        now = datetime.now(UTC)
        if facts.kickoff <= now or auth.expires_at <= now or control.enabled(True):
            raise ValueError("review expired or trading was enabled; mapping not saved")
        mapping = Mapping(
            source_token_id=facts.token_id,
            source_condition_id=facts.condition_id,
            source_fingerprint=facts.fingerprint,
            destination_ticker=ticker,
            destination_fingerprint=bundle.fingerprint,
            outcome=outcome,
            max_price=ceiling,
            environment=settings.environment,
            kickoff=facts.kickoff,
            reviewed_at=now,
            expires_at=facts.kickoff,
        )
        with control.submission(False):
            if control.enabled(True) or KalshiSettings.load().model_dump() != settings.model_dump():
                raise ValueError("configuration changed during review; mapping not saved")
            MappingStore(settings.mappings_path).save(mapping)
        log.info("Saved exact reviewed mapping. Run kalshi run --dry-run before enabling orders.")
    finally:
        client.close()
    return 0


def cmd_run(settings: KalshiSettings) -> int:
    slate = _slate(settings)
    if slate is None:
        return 0
    ledger = Ledger(settings.ledger_path)
    store = MappingStore(settings.mappings_path)
    enabled = LiveControl(settings.control_env_path).enabled(settings.is_live)
    log.info(
        "%s | %s | unmatched and changed contracts skip",
        settings.environment,
        "ORDERS ENABLED" if enabled else "DRY RUN",
    )
    client = _client(settings, authenticated=enabled)
    try:
        committed = ledger.spent_today_usd()
        reserved = ledger.reserved_picks(slate.pick_date) if slate.pick_date else 0
        for pick in sorted(
            slate.picks,
            key=lambda item: (
                item.release_at or datetime.max.replace(tzinfo=UTC),
                item.token_id or "",
            ),
        ):
            plan = plan_pick(
                pick,
                slate.access,
                settings=settings,
                ledger=ledger,
                mappings=store,
                client=client,
                committed=committed,
                reserved_picks=reserved,
            )
            if not plan.buy:
                log.info("SKIP %s | %s", pick.label, plan.reason)
                continue
            if plan.mapping is None:
                raise ValueError("missing reviewed mapping on buy plan")
            log.info(
                "BUY %s | %s %s | %d contracts | price ceiling %s | fee reserve %s | "
                "maximum debit %s USD | expires %s",
                pick.label,
                plan.mapping.destination_ticker,
                plan.mapping.outcome,
                plan.count,
                plan.max_price,
                plan.fee_bound,
                plan.reserve_usd,
                plan.valid_until,
            )
            if enabled:
                execute(plan, settings=settings, ledger=ledger, mappings=store, client=client)
                committed = ledger.spent_today_usd()
                reserved = ledger.reserved_picks(pick.pick_date)
            else:
                committed += plan.reserve_usd
                reserved += 1
        if not enabled:
            log.info("Dry run complete. No account order or ledger reservation was created.")
    finally:
        client.close()
    return 0


def cmd_watch(settings: KalshiSettings) -> int:
    log.info(
        "Kalshi watcher starts with %s configuration; Ctrl-C stops this process",
        settings.environment,
    )
    while True:
        try:
            result = cmd_run(settings)
        except FeedWait as exc:
            log.warning("%s; watcher honors the provider retry time", exc)
            time.sleep(exc.delay)
            continue
        if result:
            return result
        time.sleep(60)


def cmd_live(settings: KalshiSettings, state: str) -> int:
    control = LiveControl(settings.control_env_path)
    if state == "off":
        control.set_live(False)
        log.info("Kalshi trading stopped. Already submitted orders are not canceled.")
        return 0
    if state == "status":
        log.info(
            "%s | trading %s",
            settings.environment,
            "enabled" if control.enabled(True) else "stopped",
        )
        return 0
    require_tty()
    settings.require_feed_credentials()
    client = _client(settings, authenticated=True)
    try:
        client.balance(0)
    finally:
        client.close()
    settings.bind_account()
    phrase = "place demo orders" if settings.environment == "demo" else "spend real money on Kalshi"
    print(f"Environment: {settings.environment}. Caps include conservative exchange fee reserves.")
    print("Mappings certify only the contracts you reviewed; unmatched picks skip.")
    if input(f'Type "{phrase}" to enable orders: ').strip() != phrase:
        log.info("Trading remains stopped.")
        return 1
    control.set_live(
        True, before_enable=lambda: KalshiSettings.load().model_dump() == settings.model_dump()
    )
    log.warning(
        "%s orders enabled. The next run/watch may submit mapped picks.", settings.environment
    )
    return 0


def cmd_ledger(settings: KalshiSettings) -> int:
    for _, entry in Ledger(settings.ledger_path).entries():
        log.info(
            "%s | %s %s | reserved %s USD | filled debit %s | order %s | client %s",
            entry["state"],
            entry.get("ticker"),
            entry.get("outcome"),
            entry["stake_usd"],
            entry.get("actual_debit_usd", "unknown"),
            entry.get("order_id", "unknown"),
            entry.get("client_order_id", "unknown"),
        )
    return 0


def cmd_reconcile(settings: KalshiSettings) -> int:
    settings.bind_account()
    ledger = Ledger(settings.ledger_path)
    client = _client(settings, authenticated=True)
    try:
        for key, entry in ledger.entries():
            if entry["state"] not in {"submitting", "unknown"}:
                continue
            if (
                entry.get("provider") != "kalshi"
                or entry.get("environment") != settings.environment
            ):
                raise LedgerError("ledger contains a different provider or environment")
            observation = client.find_order(
                entry["client_order_id"],
                entry["ticker"],
                entry["exchange_index"],
                entry.get("order_id"),
            )
            if observation is None:
                log.warning(
                    "UNRESOLVED %s | no complete matching provider record; reservation kept", key
                )
                continue
            if (
                observation.client_order_id != entry["client_order_id"]
                or observation.ticker != entry["ticker"]
                or observation.outcome_side != entry["outcome"]
                or observation.exchange_index != entry["exchange_index"]
                or observation.initial_count != Decimal(entry["count"])
                or observation.fill_count < 0
                or observation.fill_count > observation.initial_count
                or observation.remaining_count != 0
                or observation.status not in {"executed", "canceled"}
            ):
                log.warning("UNRESOLVED %s | provider identity or terminal state mismatches", key)
                continue
            if observation.limit_price > Decimal(entry["max_price"]):
                log.warning(
                    "UNRESOLVED %s | original provider order limit exceeds the ceiling", key
                )
                continue
            if observation.fill_count > 0 and (
                observation.fill_cost <= 0
                or observation.fill_cost > observation.fill_count * Decimal(entry["max_price"])
                or observation.fees > Decimal(entry["fee_bound"])
            ):
                log.warning("UNRESOLVED %s | provider fill price or fees exceed the bounds", key)
                continue
            if observation.fill_count == 0 and observation.total_cost != 0:
                log.warning("UNRESOLVED %s | zero fills with nonzero debit", key)
                continue
            state = "accepted" if observation.fill_count > 0 else "rejected"
            if state == "accepted" and (
                observation.total_cost is None
                or not 0 <= observation.total_cost <= Decimal(entry["stake_usd"])
            ):
                log.warning("UNRESOLVED %s | complete bounded debit unavailable", key)
                continue
            ledger.reconcile(
                key,
                state,
                client_order_id=entry["client_order_id"],
                order_id=observation.order_id,
                fill_count=str(observation.fill_count),
                actual_debit_usd=str(observation.total_cost)
                if observation.total_cost is not None
                else None,
            )
            log.info(
                "RECONCILED %s | %s | original reservation retained for filled orders", key, state
            )
    finally:
        client.close()
    return 0


def _terminate(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="potd-trader kalshi", description="Buy explicitly reviewed POTD contracts on Kalshi."
    )
    parser.add_argument("--version", action="version", version=f"potd-trader {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a stopped, isolated Kalshi configuration")
    init.add_argument("directory", nargs="?", type=Path)
    init.add_argument("--environment", choices=["demo", "production"], default="demo")
    wizard = sub.add_parser("setup", help="interactive account, contract review and dry-run setup")
    wizard.add_argument("directory", nargs="?", type=Path)
    for name in ("status", "picks", "ledger", "reconcile"):
        sub.add_parser(name)
    markets = sub.add_parser("markets", help="list the first page of open candidate markets")
    markets.add_argument("--series")
    mapping = sub.add_parser("map", help="review source and destination rules in your terminal")
    mapping.add_argument("--rank", type=int, required=True)
    mapping.add_argument("--ticker", required=True)
    mapping.add_argument("--outcome", choices=["yes", "no"], required=True)
    mapping.add_argument("--max-price", type=Decimal, required=True)
    for name in ("run", "watch"):
        command = sub.add_parser(name)
        command.add_argument("--dry-run", action="store_true")
    live = sub.add_parser("live")
    live.add_argument("state", choices=["on", "off", "status"])
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _terminate)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command == "init":
            directory = args.directory or Path(
                "kalshi-potd-demo" if args.environment == "demo" else "kalshi-potd-live"
            )
            return cmd_init(directory, args.environment)
        if args.command == "setup":
            return setup(args.directory)
        settings = KalshiSettings.load()
        if getattr(args, "dry_run", False):
            settings = settings.model_copy(update={"live": "no"})
        if args.command == "markets":
            return cmd_markets(settings, args.series)
        if args.command == "map":
            return cmd_map(settings, args.rank, args.ticker, args.outcome, args.max_price)
        if args.command == "live":
            return cmd_live(settings, args.state)
        if args.command == "watch":
            return cmd_watch(settings)
        commands = {
            "status": cmd_status,
            "picks": cmd_picks,
            "run": cmd_run,
            "ledger": cmd_ledger,
            "reconcile": cmd_reconcile,
        }
        return commands[args.command](settings)
    except KeyboardInterrupt:
        log.info("Stopped. Interrupted submissions remain reserved until provider reconciliation.")
        return 130
    except EOFError:
        log.error("Terminal input ended. Rerun Kalshi setup in your terminal to continue.")
        return 1
    except ValidationError as exc:
        names = ", ".join(".".join(str(p) for p in err["loc"]) for err in exc.errors())
        log.error("Invalid configuration or provider fields: %s; no new order permitted", names)
        return 1
    except (KalshiError, KalshiConfigError, MappingError, LedgerError, OxinsiderError) as exc:
        log.error("%s", exc)
        return 1
    except (httpx.HTTPError, PolymarketError, BaseLockException, OSError, ValueError) as exc:
        log.error(
            "Command stopped (%s); check configuration/network and ledger before retrying",
            type(exc).__name__,
        )
        return 1
