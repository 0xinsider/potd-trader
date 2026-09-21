"""`potd-trader init`: one terminal session from nothing to a checked dry run.

Keys are typed with echo off and written straight to the `.env` file at mode 0600. Nothing here
prints a key, and nothing here sets `LIVE=yes` without the person typing the confirmation phrase.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import active_env_file, home_dir

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
        if stake > 0:
            return str(stake)
        say("     a positive number, like 5. Try again.")


def write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(path, 0o600)


def set_live(path: Path, live: bool) -> None:
    """Rewrite the LIVE line in place, keeping every other line byte for byte."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if not line.startswith("LIVE=")]
    kept.append(f"LIVE={'yes' if live else 'no'}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(kept) + "\n")


def confirm_live() -> bool:
    say(f'Type "{LIVE_PHRASE}" to set LIVE=yes, or press Enter to stay in dry run.')
    return input("     > ").strip() == LIVE_PHRASE


def collect() -> Path:
    """Run the prompts and write the file. Returns the path written."""
    require_tty()
    target = active_env_file()
    say("potd-trader setup. Four questions, then a dry run. Nothing is bought.")
    say(f"Writes {target} (mode 600). Your keys stay in that file and nowhere else.")
    say()
    if target.exists():
        answer = input(f"     {target} exists. Overwrite it? [y/N]: ").strip().lower()
        if answer != "y":
            raise SystemExit("kept the existing file. Edit it by hand, or delete it and rerun.")
        say()
    values = {
        "OXINSIDER_API_KEY": ask_oxinsider_key(),
        "POLYMARKET_PRIVATE_KEY": ask_private_key(),
        "POLYMARKET_WALLET_ADDRESS": ask_wallet_address(),
        "STAKE_USD": ask_stake(),
        "LIVE": "no",
    }
    write_env(target, values)
    (home_dir()).mkdir(parents=True, exist_ok=True)
    say()
    say(f"Written: {target}")
    say()
    return target
