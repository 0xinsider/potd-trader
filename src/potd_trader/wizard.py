"""`potd-trader init`: one terminal session from nothing to a checked dry run.

It creates a new folder and never writes into an existing one. Keys are typed with echo off and
written straight to the folder's `.env` at mode 0600. Nothing here prints a key, and nothing here
sets `LIVE=yes` without the person typing the confirmation phrase.
"""

from __future__ import annotations

import getpass
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .control import LiveControl
from .storage import atomic_write

LIVE_PHRASE = "spend real money"
_HEX_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def say(text: str = "") -> None:
    print(text, file=sys.stderr)


def require_tty() -> None:
    if not sys.stdin.isatty():
        raise SystemExit(
            "potd-trader init is interactive: run it in a terminal. An agent setting this up "
            "for someone follows AGENTS.md instead."
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


def ask_stake() -> str:
    say("4/4  pUSD per pick. Start small.")
    while True:
        value = _ask("     STAKE_USD", secret=False, default="5")
        try:
            stake = Decimal(value)
        except InvalidOperation:
            stake = Decimal("-1")
        if stake.is_finite() and stake > 0:
            return str(stake)
        say("     a positive number, like 5. Try again.")


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
    say("potd-trader setup. Four questions, then a dry run. Nothing is bought.")
    say(f"New folder: {folder}")
    say("Your keys go in its .env (mode 600) and nowhere else.")
    say()
    values = {
        "OXINSIDER_API_KEY": ask_oxinsider_key(),
        "POLYMARKET_PRIVATE_KEY": ask_private_key(),
        "POLYMARKET_WALLET_ADDRESS": ask_wallet_address(),
        "STAKE_USD": ask_stake(),
        "LIVE": "no",
    }
    write_env(target, values)
    say()
    say(f"Written: {target}")
    say()
    return folder
