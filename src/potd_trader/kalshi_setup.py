"""One terminal session for private Kalshi configuration and reviewed dry runs."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError

from .control import LiveControl
from .kalshi import Environment, KalshiClient, KalshiError
from .kalshi_config import KalshiConfigError, KalshiSettings
from .kalshi_mapping import MappingError, MappingStore
from .storage import atomic_write, file_lock
from .wizard import require_tty, say, write_env


def _ask(prompt: str, default: str | None = None, *, optional: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        if optional:
            return ""
        say("Enter a value, or press Ctrl-C to stop.")


def _yes(prompt: str) -> bool:
    while True:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True
        say("Enter y or n.")


def _secret(prompt: str) -> str:
    while True:
        answer = getpass.getpass(f"{prompt} (input hidden): ").strip()
        if answer:
            return answer
        say("A value is needed. Press Ctrl-C to stop; never paste keys into chat.")


def _amount(prompt: str, default: str, *, price: bool = False) -> str:
    while True:
        answer = _ask(prompt, default)
        try:
            amount = Decimal(answer)
        except InvalidOperation:
            say("Enter a number, such as 5 or 0.50.")
            continue
        if amount.is_finite() and amount > 0 and (not price or amount < 1):
            return str(amount)
        say("Enter a price between 0 and 1." if price else "Enter a positive amount.")


def _key_file(folder: Path, environment: Environment, key_id: str, previous: str | None) -> Path:
    say("Create the API key in your matching Kalshi account and download its PEM file.")
    say("Demo: https://demo.kalshi.co  Production: https://kalshi.com")
    say("Enter its downloaded file path. The wizard makes a private local copy.")
    while True:
        source = Path(_ask("Downloaded PEM file", previous)).expanduser()
        try:
            if not source.is_file() or source.stat().st_size > 131_072:
                say("Choose a readable PEM file smaller than 128 KiB.")
                continue
            contents = source.read_text(encoding="utf-8")
            target = folder / f"kalshi-key-{uuid4().hex}.pem"
            atomic_write(target, contents)
            # Reuse the adapter's exact supported key contract without a network request.
            try:
                client = KalshiClient(environment, SecretStr(key_id), target)
            except KalshiError:
                target.unlink()
                say("This is not a supported unencrypted Kalshi private key. Choose its PEM file.")
                continue
            client.close()
            return target
        except (OSError, UnicodeError):
            say("Cannot read or privately save that PEM file. Choose a readable file.")


def _save(folder: Path, original: bytes, values: dict[str, str]) -> KalshiSettings:
    path = folder / ".env.kalshi"
    control = LiveControl(path)
    with control.submission(False):
        if path.read_bytes() != original or control.enabled(True):
            raise KalshiConfigError("Configuration changed during setup; rerun while stopped")
        old = dotenv_values(path, interpolate=False)
        if (folder / "kalshi-ledger.json").exists() and (
            old.get("KALSHI_API_KEY_ID") != values.get("KALSHI_API_KEY_ID")
            or old.get("KALSHI_ENVIRONMENT") != values.get("KALSHI_ENVIRONMENT")
        ):
            raise KalshiConfigError("This ledger belongs to its saved account; choose a new folder")
        # Quote all values so spaces, #, $, quotes and backslashes never become dotenv syntax.
        write_env(
            path, {name: json.dumps(value, ensure_ascii=False) for name, value in values.items()}
        )
    return KalshiSettings.load()


def _launcher(folder: Path) -> None:
    if os.name == "nt":
        executable = sys.executable.replace("%", "%%")
        directory = str(folder).replace("%", "%%")
        atomic_write(
            folder / "trader.cmd",
            f'@echo off\ncd /d "{directory}"\n"{executable}" -I -m potd_trader.cli kalshi %*\n',
        )
    else:
        atomic_write(
            folder / "trader",
            "#!/bin/sh\nset -eu\n"
            f"cd {shlex.quote(str(folder))}\n"
            f'exec {shlex.quote(sys.executable)} -I -m potd_trader.cli kalshi "$@"\n',
        )
        (folder / "trader").chmod(0o700)


def _collect(folder: Path, *, new: bool) -> KalshiSettings:
    path = folder / ".env.kalshi"
    control = LiveControl(path)
    with file_lock(folder / ".kalshi-setup.lock"):
        settings = KalshiSettings.load()
        original = path.read_bytes()
        raw = dotenv_values(path, interpolate=False)
        if raw.get("LIVE") != "no" or control.enabled(True):
            raise KalshiConfigError("Setup requires stopped trading; use your launcher's live off")
        values = {name: value or "" for name, value in raw.items()}
        has_credentials = bool(values.get("KALSHI_API_KEY_ID") or values.get("OXINSIDER_API_KEY"))
        update = (
            not new
            and has_credentials
            and _yes("Update saved credentials? Otherwise only missing values are asked")
        )
        if update or not values.get("KALSHI_API_KEY_ID"):
            say(f"Use the API key ID from your Kalshi {settings.environment} account.")
            values["KALSHI_API_KEY_ID"] = _secret("Kalshi API key ID")
        key = settings.kalshi_private_key_path
        usable_key = key is not None and key.is_file()
        if usable_key and os.name != "nt" and key is not None:
            usable_key = not bool(key.stat().st_mode & 0o077)
        if update or not usable_key:
            values["KALSHI_PRIVATE_KEY_PATH"] = str(
                _key_file(
                    folder,
                    settings.environment,
                    values["KALSHI_API_KEY_ID"],
                    str(key) if key else None,
                )
            )
        if update or not values.get("OXINSIDER_API_KEY"):
            say("Get a Pro or Max API key from https://0xinsider.com/developers.")
            values["OXINSIDER_API_KEY"] = _secret("0xinsider API key")
        if new or _yes("Change your saved per-pick and daily limits?"):
            say("Both limits include conservative Kalshi exchange fee reserves.")
            values["STAKE_USD"] = _amount(
                "Maximum USD reserved per pick", values.get("STAKE_USD") or "5"
            )
            values["DAILY_CAP_USD"] = _amount(
                "Maximum USD reserved per UTC day", values.get("DAILY_CAP_USD") or "25"
            )
        settings = _save(folder, original, values)
        _launcher(folder)
        say(f"Saved privately in {folder}. Trading remains stopped.")
        return settings


def setup(directory: Path | None) -> int:
    # Local imports avoid changing the existing CLI's import/dispatch contract.
    from . import kalshi_cli

    require_tty()
    say("Kalshi setup: private keys, account check, contract review, then a dry run.")
    say("Nothing is bought unless you later type the environment's order confirmation.")
    default = (
        Path.cwd() if (Path.cwd() / ".env.kalshi").is_file() else Path.home() / "kalshi-potd-demo"
    )
    chosen = directory or Path(_ask("Setup folder", str(default)))
    chosen = chosen.expanduser().absolute()
    if chosen.is_symlink():
        raise KalshiConfigError("Choose a private setup folder without a symbolic link")
    new = not chosen.exists()
    if new:
        while True:
            environment = _ask("Kalshi account: demo or production", "demo").lower()
            if environment in ("demo", "production"):
                break
            say("Enter demo or production. Demo uses a separate account and virtual funds.")
        kalshi_cli.cmd_init(chosen, environment, guided=True)
    elif not chosen.is_dir() or not (chosen / ".env.kalshi").is_file():
        raise KalshiConfigError("That folder is not a Kalshi setup; choose a new private folder")
    folder = chosen.resolve()
    if os.name != "nt" and folder.stat().st_mode & 0o077:
        raise KalshiConfigError(
            "The setup folder must be private (chmod 700), or choose a new folder"
        )
    previous_cwd = Path.cwd()
    os.chdir(folder)
    try:
        settings = _collect(folder, new=new)
        say("Checking your Kalshi account and current 0xinsider picks...")
        kalshi_cli.cmd_status(settings)
        slate = kalshi_cli._slate(settings)
        if slate is None:
            say(
                "No pick is released yet. Your setup is saved; "
                "rerun this command when one is available."
            )
            return 0
        store = MappingStore(settings.mappings_path)
        for pick in slate.picks:
            say(f"\nPick {pick.pick_rank}: {pick.label}")
            if pick.outcome != "pending" or pick.game_started or not pick.token_id:
                say("This pick is started, settled or missing its source token; it skips.")
                continue
            saved = store.get(pick.token_id, settings.environment)
            if saved is not None and saved.expires_at > datetime.now(UTC):
                say(
                    f"Existing reviewed contract: {saved.destination_ticker}, "
                    f"{saved.outcome.upper()}."
                )
                say("The dry run rechecks its current rules and trading guards.")
                if not _yes("Review this saved contract again?"):
                    continue
            say("Find the equivalent event contract in your matching Kalshi account.")
            if saved is None:
                say("Copy its ticker from the market page. Enter skips an uncertain match.")
            else:
                say("Enter keeps the saved ticker. Cancel at the rules review if it is uncertain.")
            ticker = _ask(
                "Kalshi contract ticker",
                saved.destination_ticker if saved is not None else None,
                optional=True,
            )
            if not ticker:
                say("No mapping saved; this pick skips.")
                continue
            while True:
                outcome = _ask(
                    "Buy YES or NO", saved.outcome if saved is not None else "yes"
                ).lower()
                if outcome in ("yes", "no"):
                    break
                say("Enter yes or no.")
            ceiling = Decimal(
                _amount(
                    "Maximum contract price in USD",
                    str(saved.max_price if saved is not None else settings.max_price),
                    price=True,
                )
            )
            try:
                kalshi_cli.cmd_map(settings, pick.pick_rank, ticker, outcome, ceiling)
            except ValidationError:
                say("Contract review received invalid provider fields; no mapping was saved.")
            except (MappingError, KalshiError, ValueError) as exc:
                say(f"Contract review did not save a mapping: {exc}. This pick skips.")
        say("\nRunning the dry run. No orders or ledger reservations are submitted.")
        result = kalshi_cli.cmd_run(settings.model_copy(update={"live": "no"}))
        if result:
            return result
        say(f"Setup saved in {folder}. Rerun the same setup command whenever needed.")
        say("You can also use the local trader launcher for status, live off and watch.")
        tokens = {pick.token_id for pick in slate.picks}
        if not any(
            mapping.source_token_id in tokens
            and mapping.environment == settings.environment
            and mapping.expires_at > datetime.now(UTC)
            for mapping in store.entries()
        ):
            say("No current pick has a reviewed contract. Trading remains stopped.")
            return 0
        if not _yes(f"Enable {settings.environment} orders and start watching in this terminal?"):
            say("Finished. Trading remains stopped.")
            return 0
        try:
            if kalshi_cli.cmd_live(settings, "on"):
                return 0
            say("Watching in this terminal. Ctrl-C stops this watcher and disables new orders.")
            return kalshi_cli.cmd_watch(KalshiSettings.load())
        finally:
            LiveControl(settings.control_env_path).set_live(False)
            say("Watcher stopped. New orders are disabled; submitted orders remain in the ledger.")
    finally:
        os.chdir(previous_cwd)
