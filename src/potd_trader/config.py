"""Settings, read from the environment and an optional `.env` file.

Two secrets exist and both stay on this machine:

- `OXINSIDER_API_KEY` is sent to api.0xinsider.com, and nowhere else, to read the pick.
- `POLYMARKET_PRIVATE_KEY` signs Polymarket orders inside this process. It is never sent
  anywhere. 0xinsider has no endpoint that accepts a wallet key.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 0xinsider (read-only: the pick).
    oxinsider_api_key: SecretStr = Field(description="Pro API key, oxi_sk_live_...")
    oxinsider_api_base: str = "https://api.0xinsider.com"

    # Polymarket (the account that buys).
    polymarket_private_key: SecretStr | None = None
    polymarket_wallet_address: str | None = None
    polymarket_relayer_api_key: SecretStr | None = None
    polymarket_relayer_api_key_address: str | None = None
    polymarket_builder_code: str | None = None

    # The one switch that spends money. Anything but the exact string "yes" is a dry run.
    live: str = "no"

    # Sizing and guards.
    stake_usd: Decimal = Decimal("5")
    max_price: Decimal = Decimal("0.925")
    max_slippage_pct: Decimal = Decimal("3")
    daily_cap_usd: Decimal = Decimal("25")
    kickoff_buffer_minutes: int = 5
    min_ranks: int = 1
    max_ranks: int = 6

    # Files and cadence.
    ledger_path: Path = Path("data/ledger.json")
    watch_idle_minutes: int = 30

    @field_validator("stake_usd", "daily_cap_usd")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("must be zero or more")
        return value

    @field_validator("max_price")
    @classmethod
    def _price_in_range(cls, value: Decimal) -> Decimal:
        if not Decimal("0") < value < Decimal("1"):
            raise ValueError("MAX_PRICE must be between 0 and 1 (a probability)")
        return value

    @property
    def is_live(self) -> bool:
        return self.live.strip().lower() == "yes"

    @property
    def has_polymarket_credentials(self) -> bool:
        return self.polymarket_private_key is not None and bool(self.polymarket_wallet_address)

    def require_polymarket_credentials(self) -> None:
        if not self.has_polymarket_credentials:
            raise SystemExit(
                "POLYMARKET_PRIVATE_KEY and POLYMARKET_WALLET_ADDRESS are both required for "
                "this command. Put them in .env (see .env.example) and chmod 600 .env."
            )
