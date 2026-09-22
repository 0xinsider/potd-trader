"""Deterministic safety regression cases. No wallet keys and no real exchange calls."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr, ValidationError

from potd_trader import cli
from potd_trader.config import Settings
from potd_trader.control import LiveControl
from potd_trader.ledger import Ledger, LedgerError
from potd_trader.oxinsider import (
    OxinsiderClient,
    OxinsiderError,
    Pick,
    Slate,
    TryLater,
    parse_slate,
)
from potd_trader.polymarket import Account, MarketFacts
from potd_trader.trader import execute, plan_slate

D = Decimal
CONDITION = "0x" + "a" * 64


def pick_data(rank=1, token="123", now=None):
    now = now or datetime.now(UTC)
    return dict(
        pick_date=now.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
        pick_rank=rank,
        token_id=token,
        outcome="pending",
        backed_price="0.50",
        release_at=(now - timedelta(minutes=2)).isoformat(),
        game_started=False,
        entry_authorization=dict(
            version=1,
            authorization_id="00000000-0000-0000-0000-000000000001",
            policy_version=7,
            condition_id=CONDITION,
            token_id=token,
            outcome_index=0,
            max_entry_price="0.53",
            issued_at=(now - timedelta(minutes=2)).isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
        ),
    )


def settings_at(folder, **updates):
    return Settings(
        _env_file=None,
        oxinsider_api_key=SecretStr("fake-test-key"),
        live="yes",
        ledger_path=folder / "ledger.json",
        control_env_path=folder / ".env",
        **updates,
    )


class FakeReads:
    def __init__(self, now=None):
        now = now or datetime.now(UTC)
        self.facts = MarketFacts(
            slug="fake-market",
            accepting_orders=True,
            closed=False,
            game_start_time=now + timedelta(hours=1),
            seconds_delay=0,
            minimum_order_size=D("1"),
            tick_size=D("0.01"),
            condition_id=CONDITION,
            token_ids=("123", "456"),
        )
        self.quote = D("0.51")

    def market_for_token(self, token):
        return replace(self.facts, token_ids=(token, "456"))

    def estimate_buy_price(self, token, stake):
        return self.quote

    def close(self):
        pass


class FakeExchange:
    wallet = "fake-wallet"

    def __init__(self, path=None):
        self.path = path
        self.orders = []
        self.prepare_hook = None
        self.submit_hook = None
        self.response = SimpleNamespace(
            ok=True,
            order_id="fake-order",
            status="matched",
            making_amount=D("5"),
            taking_amount=D("9.8"),
            trade_ids=(),
        )

    def prepare_buy(self, token, stake, ceiling):
        if self.prepare_hook:
            self.prepare_hook()
        return (token, stake, ceiling)

    def submit_buy(self, signed):
        self.orders.append(signed)
        if self.path:
            with self.path.open("a") as handle:
                handle.write(signed[0] + "\n")
        if self.submit_hook:
            self.submit_hook()
        return self.response


def race_worker(folder, rank, token, barrier, cap):
    folder = Path(folder)
    settings = settings_at(folder, daily_cap_usd=D(cap))
    ledger, reads = Ledger(settings.ledger_path), FakeReads()
    pick = Pick.model_validate(pick_data(rank, token))
    plan = plan_slate(
        Slate(pick.pick_date, (pick,), (), None), settings=settings, ledger=ledger, reads=reads
    )[0]
    barrier.wait(timeout=10)
    execute(
        plan, settings=settings, ledger=ledger, reads=reads, account=FakeExchange(folder / "posts")
    )


def crash_worker(folder):
    folder = Path(folder)
    Ledger(folder / "ledger.json").reserve(
        "crashed",
        daily_cap=D("5"),
        stake_usd=D("5"),
        pick_date="2026-09-23",
        pick_rank=1,
        token_id="123",
    )
    os._exit(7)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        (self.folder / ".env").write_text("LIVE=yes\nOXINSIDER_API_KEY=fake-test-key\n")
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # Any unintended real HTTP call fails the test before it reaches a network.
        self.net = patch.object(
            httpx.Client, "send", side_effect=AssertionError("real HTTP forbidden")
        )
        self.net.start()
        self.addCleanup(self.net.stop)
        self.settings = settings_at(self.folder)
        self.ledger = Ledger(self.settings.ledger_path)
        self.reads, self.account = FakeReads(), FakeExchange()
        self.pick = Pick.model_validate(pick_data())

    def plans(self, *picks):
        return plan_slate(
            Slate(self.pick.pick_date, picks or (self.pick,), (), None),
            settings=self.settings,
            ledger=self.ledger,
            reads=self.reads,
        )

    def execute(self, plan=None):
        return execute(
            plan or self.plans()[0],
            account=self.account,
            ledger=self.ledger,
            settings=self.settings,
            reads=self.reads,
        )

    def reserve(self, key="one", **updates):
        fields = dict(
            daily_cap=D("25"),
            stake_usd=D("5"),
            pick_date=self.pick.pick_date,
            pick_rank=1,
            token_id="123",
        )
        fields.update(updates)
        return self.ledger.reserve(key, **fields)

    def test_exact_order_and_duplicate_execution(self):
        plan = self.plans()[0]
        self.assertTrue(self.execute(plan))
        self.assertEqual(self.account.orders, [("123", D("5"), D("0.51"))])
        self.assertFalse(self.execute(plan))
        self.assertEqual(len(self.account.orders), 1)

    def test_two_independent_processes_post_same_pick_only_once(self):
        self.run_race([(1, "123"), (1, "123")], "25", 1)

    def test_competing_processes_reserve_budget_atomically(self):
        self.run_race([(1, "123"), (2, "789")], "5", 1)

    def test_independent_finishes_do_not_lose_other_orders(self):
        self.run_race([(1, "123"), (2, "789")], "25", 2)
        self.assertEqual(len(Ledger(self.settings.ledger_path).entries()), 2)

    def run_race(self, identities, cap, expected):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(len(identities))
        processes = [
            ctx.Process(target=race_worker, args=(str(self.folder), rank, token, barrier, cap))
            for rank, token in identities
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.kill()
                process.join()
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(len((self.folder / "posts").read_text().splitlines()), expected)

    def test_crash_reservation_survives_and_blocks_retry(self):
        ctx = multiprocessing.get_context("spawn")
        process = ctx.Process(target=crash_worker, args=(str(self.folder),))
        process.start()
        process.join(timeout=10)
        self.assertEqual(process.exitcode, 7)
        self.assertFalse(self.execute())
        self.assertEqual(self.account.orders, [])
        self.assertEqual(self.ledger.spent_today_usd(), D("5"))

    def test_unknown_submission_stays_reserved_and_does_not_leak_exception(self):
        self.account.submit_hook = Mock(side_effect=TimeoutError("secret-looking-value"))
        with self.assertRaises(TimeoutError):
            self.execute()
        self.assertEqual(self.ledger.entries()[0][1]["state"], "unknown")
        self.assertNotIn("secret-looking-value", self.ledger.path.read_text())
        self.assertFalse(self.execute())
        self.assertEqual(self.ledger.spent_today_usd(), D("5"))

    def test_live_off_overrides_inherited_live_and_stale_watcher(self):
        plan = self.plans()[0]
        with patch.dict(os.environ, {"LIVE": "yes"}):
            LiveControl(self.settings.control_env_path).set_live(False)
            self.assertFalse(self.execute(plan))
        self.assertEqual(self.account.orders, [])
        self.assertEqual(self.ledger.entries(), [])

    def test_live_off_during_signing_prevents_post(self):
        self.account.prepare_hook = lambda: LiveControl(self.settings.control_env_path).set_live(
            False
        )
        self.assertFalse(self.execute())
        self.assertEqual(self.account.orders, [])
        self.assertEqual(self.ledger.entries(), [])

    def test_stop_between_reservation_and_post_releases_unsubmitted_budget(self):
        reserve = self.ledger.reserve

        def stop_after_reserve(*args, **kwargs):
            result = reserve(*args, **kwargs)
            LiveControl(self.settings.control_env_path).halt_path.touch()
            return result

        with patch.object(self.ledger, "reserve", side_effect=stop_after_reserve):
            self.assertFalse(self.execute())
        self.assertEqual(self.account.orders, [])
        self.assertEqual(self.ledger.spent_today_usd(), 0)

    def test_live_off_acknowledges_after_in_flight_submission(self):
        control = LiveControl(self.settings.control_env_path)
        posted, release, stopped = threading.Event(), threading.Event(), threading.Event()

        def post_in_progress():
            with control.submission(True) as enabled:
                self.assertTrue(enabled)
                posted.set()
                self.assertTrue(release.wait(timeout=5))

        def stop():
            control.set_live(False)
            stopped.set()

        posting = threading.Thread(target=post_in_progress)
        posting.start()
        self.assertTrue(posted.wait(timeout=5))
        stopping = threading.Thread(target=stop)
        stopping.start()
        self.assertFalse(stopped.wait(timeout=0.1))
        release.set()
        posting.join(timeout=5)
        stopping.join(timeout=5)
        self.assertTrue(stopped.is_set())
        self.assertFalse(control.enabled(True))

    def test_deleted_control_file_fails_closed(self):
        self.settings.control_env_path.unlink()
        self.assertFalse(self.execute())

    def test_dry_session_cannot_be_armed(self):
        self.settings = self.settings.model_copy(update={"live": "no"})
        self.assertFalse(self.execute())

    def test_init_forces_dry_run_even_with_inherited_live_yes(self):
        before = Path.cwd()
        self.addCleanup(os.chdir, before)

        def dry_run(settings):
            self.assertFalse(settings.is_live)
            return 0

        with (
            patch.dict(os.environ, {"LIVE": "yes"}),
            patch.object(cli, "collect", return_value=self.folder),
            patch.object(cli, "cmd_status", side_effect=dry_run),
            patch.object(cli, "cmd_run", side_effect=dry_run),
            patch.object(cli, "confirm_live", return_value=False),
        ):
            self.assertEqual(cli.cmd_init(self.folder), 0)

    def test_cli_dry_run_flag_overrides_live(self):
        with (
            patch.object(cli, "_settings", return_value=self.settings),
            patch.object(cli, "cmd_run", return_value=0) as run,
        ):
            self.assertEqual(cli.main(["run", "--dry-run"]), 0)
            self.assertFalse(run.call_args.args[0].is_live)

    def test_spend_uses_submission_timestamp_not_feed_date(self):
        now = datetime(2026, 9, 23, 12, tzinfo=UTC)
        self.reserve(pick_date="1999-01-01", now=now)
        self.ledger.finish("one", "accepted")
        self.assertEqual(self.ledger.spent_today_usd(now), D("5"))
        self.assertEqual(self.ledger.spent_today_usd(now + timedelta(days=1)), 0)

    def test_unknown_old_order_still_reserves_todays_budget(self):
        now = datetime(2026, 9, 23, 12, tzinfo=UTC)
        self.reserve(now=now - timedelta(days=1))
        self.ledger.finish("one", "unknown")
        self.assertEqual(self.ledger.spent_today_usd(now), D("5"))

    def test_same_slot_with_changed_token_and_same_token_changed_date_blocked(self):
        self.reserve()
        self.assertIsNotNone(self.reserve("other", token_id="789"))
        self.assertIsNotNone(self.reserve("later", pick_date="2099-01-01", pick_rank=2))

    def test_duplicate_feed_is_rejected_before_planning(self):
        row = pick_data()
        with self.assertRaisesRegex(OxinsiderError, "duplicate"):
            parse_slate({"pick_date": row["pick_date"], "picks": [row, row]}, None)
        self.assertFalse(any(plan.buy for plan in self.plans(self.pick, self.pick)))

    def test_mismatched_and_stale_dates_do_not_buy(self):
        row = pick_data()
        with self.assertRaisesRegex(OxinsiderError, "inconsistent"):
            parse_slate({"pick_date": "2099-01-01", "picks": [row]}, None)
        stale = self.pick.model_copy(update={"pick_date": "1999-01-01"})
        self.assertFalse(self.plans(stale)[0].buy)

    def test_missing_and_invalid_safety_fields_skip(self):
        changes = [
            dict(backed_price=None),
            dict(backed_price=D("0")),
            dict(entry_authorization=None),
            dict(release_at=None),
            dict(is_locked=True),
            dict(game_started=True),
            dict(outcome="win"),
        ]
        for fields in changes:
            with self.subTest(fields=fields):
                self.assertFalse(self.plans(self.pick.model_copy(update=fields))[0].buy)
        for fields in [
            dict(game_start_time=None),
            dict(accepting_orders=None),
            dict(closed=None),
            dict(tick_size=None),
            dict(minimum_order_size=None),
            dict(condition_id=None),
        ]:
            with self.subTest(fields=fields):
                original = self.reads.facts
                self.reads.facts = replace(original, **fields)
                self.assertFalse(self.plans()[0].buy)
                self.reads.facts = original

    def test_expired_authorization_and_stale_quote_never_submit(self):
        original = self.account.prepare_buy

        def slow_sign(*args):
            result = original(*args)
            future = datetime.now(UTC) + timedelta(minutes=2)
            clock = Mock(wraps=datetime)
            clock.now.return_value = future
            patcher = patch("potd_trader.trader.datetime", clock)
            patcher.start()
            self.addCleanup(patcher.stop)
            return result

        self.account.prepare_buy = slow_sign
        self.assertFalse(self.execute())
        self.assertEqual(self.account.orders, [])

    def test_rechecks_live_price_before_signing(self):
        plan = self.plans()[0]
        self.reads.quote = D("0.90")
        self.assertFalse(self.execute(plan))
        self.assertEqual(self.account.orders, [])

    def test_api_key_destination_and_redirects(self):
        for url in [
            "http://api.0xinsider.com",
            "https://evil.example",
            "https://api.0xinsider.com.evil",
            "https://api.0xinsider.com@evil.example",
            "https://api.0xinsider.com/path",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                OxinsiderClient(url, "fake-test-key")
        client = OxinsiderClient("https://api.0xinsider.com", "fake-test-key")
        self.addCleanup(client.close)
        self.assertFalse(client._client.follow_redirects)
        with patch.object(
            client._client,
            "get",
            return_value=httpx.Response(302, headers={"Location": "https://evil.example"}),
        ):
            with self.assertRaisesRegex(OxinsiderError, "HTTP 302"):
                client.pick_of_the_day()

    def test_nonfinite_unsafe_configuration_refused(self):
        for fields in [
            dict(daily_cap_usd="0"),
            dict(stake_usd="NaN"),
            dict(stake_usd="Infinity"),
            dict(max_slippage_pct="-1"),
            dict(kickoff_buffer_minutes=-1),
            dict(min_ranks=6, max_ranks=1),
            dict(watch_idle_minutes=0),
        ]:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                settings_at(self.folder, **fields)

    def test_corrupt_ledger_and_failed_flush_prevent_order(self):
        self.ledger.path.write_text('{"orders":{"bad":{"state":"accepted"}}}')
        with self.assertRaises(LedgerError):
            self.execute()
        self.ledger.path.unlink()
        with patch.object(self.ledger, "_flush", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.execute()
        self.assertEqual(self.account.orders, [])

    def test_legacy_ledger_is_preserved_and_permissions_private(self):
        self.reserve()
        snapshot = json.loads(self.ledger.path.read_text())
        self.assertEqual(snapshot["format"], "potd-trader.ledger.v1")
        self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Ledger(self.ledger.path).spent_today_usd(), D("5"))

    def test_proof_pending_retry_is_used_and_server_delay_is_respected(self):
        now = datetime.now(UTC)
        retry = now + timedelta(seconds=60)
        row = pick_data(now=now)
        slate = parse_slate(
            dict(
                pick_date=row["pick_date"],
                picks=[row],
                proof_pending_picks=[
                    dict(pick_rank=2, release_at=now.isoformat(), retry_at=retry.isoformat())
                ],
            ),
            None,
        )
        self.assertEqual(slate.next_release_at, retry)
        delayed = TryLater(429, "rate_limited", now + timedelta(hours=2))
        self.assertEqual(cli._next_wake(delayed, [], self.settings, now), delayed.retry_at)

    def test_bad_feed_numbers_and_timestamps_are_refused(self):
        for change in [
            dict(backed_price="NaN"),
            dict(backed_price="Infinity"),
            dict(release_at="2026-09-23T12:00:00"),
            dict(game_started="false"),
            dict(token_id="123/../../bad"),
            dict(pick_date="2026-02-30"),
        ]:
            row = {**pick_data(), **change}
            with self.subTest(change=change), self.assertRaises(OxinsiderError):
                parse_slate(dict(pick_date=row["pick_date"], picks=[row]), None)

    def test_product_day_is_new_york_not_utc(self):
        now = datetime(2026, 9, 23, 1, tzinfo=UTC)
        pick = Pick.model_validate(pick_data(now=now))
        self.assertEqual(pick.pick_date, "2026-09-22")
        with patch("potd_trader.trader.datetime") as clock:
            clock.now.return_value = now
            self.reads = FakeReads(now)
            self.pick = pick
            self.assertTrue(self.plans()[0].buy)

    def test_expired_authorization_and_mismatched_outcome_skip(self):
        for fields in [
            dict(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
            dict(token_id="789"),
            dict(outcome_index=1),
        ]:
            auth = self.pick.entry_authorization.model_copy(update=fields)
            with self.subTest(fields=fields):
                self.assertFalse(
                    self.plans(self.pick.model_copy(update={"entry_authorization": auth}))[0].buy
                )

    def test_known_rejection_releases_budget_but_unclassified_does_not(self):
        self.account.response = SimpleNamespace(ok=False, code="fak_not_filled")
        self.assertTrue(self.execute())
        self.assertEqual(self.ledger.spent_today_usd(), 0)
        self.account.response = SimpleNamespace(ok=False, code="unknown")
        self.assertTrue(self.execute())
        self.assertEqual(self.ledger.spent_today_usd(), D("5"))
        self.assertFalse(self.execute())

    def test_partial_fill_retains_full_reservation(self):
        self.account.response.making_amount = D("1")
        self.execute()
        self.assertEqual(self.ledger.spent_today_usd(), D("5"))

    def test_market_minimum_uses_actual_order_ceiling(self):
        self.settings = self.settings.model_copy(update={"stake_usd": D("3")})
        self.reads.facts = replace(self.reads.facts, minimum_order_size=D("5"))
        self.assertTrue(self.plans()[0].buy)
        self.settings = self.settings.model_copy(update={"stake_usd": D("2")})
        self.assertFalse(self.plans()[0].buy)

    def test_local_configuration_does_not_mix_in_home_secrets(self):
        before = Path.cwd()
        self.addCleanup(os.chdir, before)
        home = self.folder / "home"
        home.mkdir()
        (home / ".env").write_text("POLYMARKET_WALLET_ADDRESS=old-wallet\nLIVE=yes\n")
        os.chdir(self.folder)
        with patch.dict(
            os.environ, {"POTD_TRADER_HOME": str(home), "CONTROL_ENV_PATH": str(home / ".env")}
        ):
            settings = Settings.load()
            self.assertIsNone(settings.polymarket_wallet_address)
            self.assertEqual(settings.control_env_path, (self.folder / ".env").resolve())

    def test_watcher_read_failure_backs_off_without_touching_exchange(self):
        client = Mock()
        client.pick_of_the_day.side_effect = httpx.ConnectError("network unavailable")
        with (
            patch.object(cli, "OxinsiderClient", return_value=client),
            patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt) as sleep,
            patch.object(cli, "_preflight") as preflight,
        ):
            self.assertEqual(cli.cmd_watch(self.settings), 0)
            sleep.assert_called_once_with(30)
            preflight.assert_not_called()
            client.close.assert_called_once()

    def test_sdk_signing_is_separate_from_posting(self):
        account = Account.__new__(Account)
        account._client = Mock()
        account._builder_code = None
        signed = account.prepare_buy("123", D("5"), D("0.51"))
        account._client.create_market_order.assert_called_once_with(
            token_id="123",
            side="BUY",
            amount="5",
            max_price="0.51",
            order_type="FAK",
            builder_code=None,
        )
        account._client.post_order.assert_not_called()
        account.submit_buy(signed)
        account._client.post_order.assert_called_once_with(signed)
        account._client.place_market_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
