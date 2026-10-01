"""`potd-trader init`: one terminal session from nothing to a checked dry run.

It creates a new folder and never writes into an existing one. Keys are typed with echo off and
written straight to the folder's `.env` in a private folder. Nothing here prints a key or
sets `LIVE=yes` without the person typing the confirmation phrase.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import dotenv_values

from .config import DEFAULT_CAP_PICK_COUNT, MAX_DAILY_PICKS
from .control import LiveControl
from .storage import atomic_write, file_lock

LIVE_PHRASE = "spend real money"
_HEX_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def say(text: str = "") -> None:
    print(text, file=sys.stderr)


def require_tty() -> None:
    if not sys.stdin.isatty():
        raise SystemExit(
            "This command is interactive: run it in a terminal. An agent setting this up for "
            "someone follows AGENTS.md instead."
        )


def _ask(prompt: str, *, secret: bool, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value:
            return value
        say("  a value is needed.")


def ask_oxinsider_key() -> str:
    say("1/4  0xinsider API key. A live key from https://0xinsider.com/developers")
    say("     (Pro; the pick endpoint answers a free key with 402). Typing is hidden.")
    while True:
        value = _ask("     OXINSIDER_API_KEY", secret=True)
        if value.startswith("oxi_sk_live_"):
            return value
        if value.startswith("oxi_sk_test_"):
            say("     that is a sandbox key; it cannot read the real pick. Kept anyway.")
            return value
        say("     a 0xinsider key starts with oxi_sk_live_. Try again.")


def ask_private_key() -> str:
    say("2/4  Polymarket signer private key. Email or Google login: polymarket.com,")
    say("     Settings, Export private key. MetaMask or Rabby: the key in that wallet app.")
    say("     It signs orders inside this program and is never sent anywhere. Typing is hidden.")
    while True:
        value = _ask("     POLYMARKET_PRIVATE_KEY", secret=True)
        if _HEX_KEY.match(value):
            return value if value.startswith("0x") else f"0x{value}"
        say("     a private key is 64 hex characters, with or without 0x. Try again.")


def ask_wallet_address() -> str:
    say("3/4  Polymarket wallet address: the address in your polymarket.com profile menu.")
    say("     It holds the pUSD. It is not the signer address.")
    while True:
        value = _ask("     POLYMARKET_WALLET_ADDRESS", secret=False)
        if _ADDRESS.match(value):
            return value
        say("     an address is 0x followed by 40 hex characters. Try again.")


def ask_stake(default: str = "5", step: str = "4/5") -> str:
    say(f"{step}  Unit size in pUSD per pick. Every eligible pick uses this amount.")
    while True:
        value = _ask("     STAKE_USD", secret=False, default=default)
        try:
            stake = Decimal(value)
        except InvalidOperation:
            stake = Decimal("-1")
        if stake.is_finite() and stake > 0:
            return str(stake)
        say("     a positive number, like 5. Try again.")


def ask_daily_cap(stake: str, default: str | None = None, step: str = "5/5") -> str:
    full_day = Decimal(stake) * MAX_DAILY_PICKS
    suggested_cap = Decimal(stake) * DEFAULT_CAP_PICK_COUNT
    say(f"{step}  Daily principal limit.")
    say(f"     {MAX_DAILY_PICKS} picks at {stake} pUSD need {full_day} pUSD.")
    say("     Fees are additional. A lower cap skips picks once its budget is used.")
    while True:
        value = _ask("     DAILY_CAP_USD", secret=False, default=default or str(suggested_cap))
        try:
            cap = Decimal(value)
        except InvalidOperation:
            cap = Decimal("-1")
        if cap.is_finite() and cap > 0:
            capacity = int(cap // Decimal(stake))
            say(f"     At this unit size, the cap covers up to {capacity} pick(s).")
            return str(cap)
        say("     a positive number, like 50. Try again.")


def write_env(path: Path, values: dict[str, str]) -> None:
    body = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    atomic_write(path, body)


def set_live(path: Path, live: bool) -> None:
    LiveControl(path).set_live(live)


GITIGNORE = (
    "# Written by potd-trader init. The key file and the order ledger never leave this folder.\n"
    ".env\n"
    "ledger.json*\n"
    ".ledger.json-*\n"
    ".live.lock\n"
    "HALT\n"
)


def refuse_existing(directory: Path) -> Path:
    """An existing folder is never written into, whatever it holds."""
    directory = directory.expanduser()
    if directory.exists():
        raise SystemExit(
            f"{directory} already exists; init never writes into an existing folder. Either cd "
            f"into it and use it as it is, or pick a new name: potd-trader init <name>"
        )
    return directory


def prepare_folder(directory: Path) -> Path:
    """Create the folder (mode 700) with a .gitignore for the two files that must stay in it."""
    directory = refuse_existing(directory)
    directory.mkdir(parents=True, mode=0o700)
    (directory / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    return directory.resolve()


def confirm_live() -> bool:
    say(f'Type "{LIVE_PHRASE}" to set LIVE=yes, or press Enter to stay in dry run.')
    return input("     > ").strip() == LIVE_PHRASE


def collect(directory: Path) -> Path:
    """Create the folder, run the prompts, write its `.env`. Returns the folder."""
    refuse_existing(directory)
    require_tty()
    folder = prepare_folder(directory)
    target = folder / ".env"
    say("potd-trader setup. Five questions, then a dry run. Nothing is bought.")
    say(f"New folder: {folder}")
    if os.name == "nt":
        say("Your keys stay in its .env. Keep this folder in your private Windows user folder.")
    else:
        say("Your keys go in its .env (mode 600) and nowhere else.")
    say()
    values = {
        "OXINSIDER_API_KEY": ask_oxinsider_key(),
        "POLYMARKET_PRIVATE_KEY": ask_private_key(),
        "POLYMARKET_WALLET_ADDRESS": ask_wallet_address(),
        "STAKE_USD": ask_stake(),
    }
    values["DAILY_CAP_USD"] = ask_daily_cap(values["STAKE_USD"])
    values["LIVE"] = "no"
    write_env(target, values)
    say()
    say(f"Written: {target}")
    say()
    return folder


def configure_size(path: Path, stake: str, cap: str) -> None:
    """Change only sizing fields, preserving secrets and all other settings."""
    control = LiveControl(path)
    with file_lock(control.lock_path):
        if not path.is_file() or dotenv_values(path, interpolate=False).get("LIVE") != "no":
            raise ValueError("run `potd-trader live off` before changing sizing")
        lines = path.read_text(encoding="utf-8").splitlines()
        removed = {"STAKE_USD", "DAILY_CAP_USD", "MIN_RANKS", "MAX_RANKS"}
        kept = [
            line
            for line in lines
            if line.split("=", 1)[0].strip().removeprefix("export ").strip() not in removed
        ]
        kept.extend((f"STAKE_USD={stake}", f"DAILY_CAP_USD={cap}"))
        atomic_write(path, "\n".join(kept) + "\n")
