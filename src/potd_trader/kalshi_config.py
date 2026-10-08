"""File-only Kalshi settings, isolated from the existing Polymarket setup."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .config import Settings
from .storage import atomic_write, file_lock

_ENV_FIELDS = {
    "KALSHI_ENVIRONMENT": "environment",
    "KALSHI_API_KEY_ID": "kalshi_api_key_id",
    "KALSHI_PRIVATE_KEY_PATH": "kalshi_private_key_path",
    "OXINSIDER_API_KEY": "oxinsider_api_key",
    "OXINSIDER_API_BASE": "oxinsider_api_base",
    "LIVE": "live",
    "STAKE_USD": "stake_usd",
    "MAX_PRICE": "max_price",
    "MAX_SLIPPAGE_PCT": "max_slippage_pct",
    "DAILY_CAP_USD": "daily_cap_usd",
    "KICKOFF_BUFFER_MINUTES": "kickoff_buffer_minutes",
    "WATCH_IDLE_MINUTES": "watch_idle_minutes",
}


class KalshiConfigError(ValueError):
    """A configuration error whose message never includes a supplied value."""


class KalshiSettings(Settings):
    model_config = SettingsConfigDict(extra="forbid", allow_inf_nan=False, env_file=None)

    # An empty feed key permits local status; run and mapping must explicitly require it.
    oxinsider_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    environment: Literal["demo", "production"] = "demo"
    kalshi_api_key_id: SecretStr | None = None
    kalshi_private_key_path: Path | None = None
    live: Literal["no", "yes"] = "no"
    mappings_path: Path

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Do not inherit LIVE, keys, home directories, paths, or sizing from the process.
        return (init_settings,)

    @classmethod
    def load(cls) -> KalshiSettings:
        local = Path.cwd() / ".env.kalshi"
        if local.is_symlink() or not local.is_file():
            raise KalshiConfigError("Create a private local .env.kalshi before this command")
        path = local.resolve()
        try:
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise KalshiConfigError(".env.kalshi must have private permissions (chmod 600)")
            raw = dotenv_values(path, encoding="utf-8", interpolate=False)
        except OSError as exc:
            raise KalshiConfigError("Cannot read .env.kalshi") from exc
        unknown = set(raw) - set(_ENV_FIELDS)
        if unknown:
            raise KalshiConfigError("Unsupported .env.kalshi fields: " + ", ".join(sorted(unknown)))
        values: dict[str, Any] = {
            _ENV_FIELDS[name]: value
            for name, value in raw.items()
            if value is not None and value != ""
        }
        values.update(
            control_env_path=path,
            ledger_path=path.parent / "kalshi-ledger.json",
            mappings_path=path.parent / "kalshi-mappings.json",
        )
        private_key = values.get("kalshi_private_key_path")
        if isinstance(private_key, str):
            key_path = Path(private_key).expanduser()
            if not key_path.is_absolute():
                key_path = path.parent / key_path
            values["kalshi_private_key_path"] = key_path.resolve()
        try:
            settings = cls(**values)
        except ValidationError as exc:
            names = sorted(
                {str(error["loc"][0]).upper() for error in exc.errors(include_input=False)}
            )
            # Pydantic's standard error includes raw inputs; expose field names only.
            raise KalshiConfigError("Invalid .env.kalshi fields: " + ", ".join(names)) from None
        if settings.kalshi_api_key_id is not None or settings.ledger_path.exists():
            settings.bind_account()
        return settings

    def require_feed_credentials(self) -> None:
        if not self.oxinsider_api_key.get_secret_value().strip():
            raise KalshiConfigError("OXINSIDER_API_KEY is required in .env.kalshi")

    def require_kalshi_credentials(self) -> None:
        if self.kalshi_api_key_id is None or not self.kalshi_api_key_id.get_secret_value().strip():
            raise KalshiConfigError("KALSHI_API_KEY_ID is required in .env.kalshi")
        if self.kalshi_private_key_path is None:
            raise KalshiConfigError("KALSHI_PRIVATE_KEY_PATH is required in .env.kalshi")
        try:
            path = self.kalshi_private_key_path
            if not path.is_file():
                raise KalshiConfigError("KALSHI_PRIVATE_KEY_PATH must name a private key file")
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise KalshiConfigError("KALSHI_PRIVATE_KEY_PATH must have private permissions")
        except OSError as exc:
            raise KalshiConfigError("Cannot read KALSHI_PRIVATE_KEY_PATH") from exc

    def bind_account(self, account_id: str | None = None) -> None:
        """Bind the environment and credential identity before any local ledger is created.

        Switching a bound account requires a new stopped configuration folder. Keys never
        appear in the binding file; credential identity is a one-way digest of the key ID.
        """
        folder = self.control_env_path.parent
        path = folder / "kalshi-binding.json"
        key_id = (
            self.kalshi_api_key_id.get_secret_value() if self.kalshi_api_key_id is not None else ""
        )
        identity: dict[str, str | None] = {
            "format": "potd-trader.kalshi-binding.v1",
            "environment": self.environment,
            "credential_fingerprint": hashlib.sha256(key_id.encode()).hexdigest()
            if key_id
            else None,
            "control_env_path": str(self.control_env_path),
            "ledger_path": str(self.ledger_path),
            "mappings_path": str(self.mappings_path),
            "account_id": account_id,
        }
        with file_lock(folder / "kalshi-binding.lock"):
            previous: dict[str, str | None] | None = None
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict) or set(loaded) != set(identity):
                        raise ValueError("invalid binding shape")
                    if not all(
                        isinstance(value, str) or value is None for value in loaded.values()
                    ):
                        raise ValueError("invalid binding values")
                    previous = loaded
                except (OSError, ValueError) as exc:
                    raise KalshiConfigError(
                        "Invalid Kalshi safety binding; trading is stopped"
                    ) from exc
                if account_id is None:
                    identity["account_id"] = previous["account_id"]
            elif self.ledger_path.exists():
                raise KalshiConfigError(
                    "Existing Kalshi ledger has no account binding; trading is stopped"
                )
            if self.ledger_path.exists() and previous != identity:
                raise KalshiConfigError(
                    "Kalshi environment, account, or paths differ from the ledger binding; "
                    "restore the original stopped configuration"
                )
            if previous != identity:
                atomic_write(path, json.dumps(identity, indent=2, sort_keys=True) + "\n")
