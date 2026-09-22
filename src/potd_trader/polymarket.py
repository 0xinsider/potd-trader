"""Polymarket reads and the one write: a market BUY, through the official SDK.

The SDK signs each order with `POLYMARKET_PRIVATE_KEY` inside this process and posts the
signed order to clob.polymarket.com. The key itself is never transmitted.

SDK docs: https://docs.polymarket.com/getting-started/python
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

import httpx
from polymarket import (
    AcceptedOrder,
    InsufficientLiquidityError,
    PublicClient,
    RejectedOrder,
    RelayerApiKey,
    SecureClient,
    SignedOrder,
)

from .config import Settings

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
COLLATERAL_BASE_UNIT = Decimal("0.000001")  # pUSD has six decimals


@dataclass(frozen=True)
class Geoblock:
    blocked: bool
    country: str
    region: str


def geoblock(timeout: float = 10.0) -> Geoblock:
    """Polymarket's own answer to "may this IP trade?". A missing answer is never a yes."""
    response = httpx.get(GEOBLOCK_URL, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("blocked"), bool):
        raise RuntimeError("geoblock: malformed response; refusing to assume this region may trade")
    return Geoblock(
        blocked=body["blocked"],
        country=str(body.get("country", "")),
        region=str(body.get("region", "")),
    )


@dataclass(frozen=True)
class MarketFacts:
    slug: str | None
    accepting_orders: bool | None
    closed: bool | None
    game_start_time: datetime | None
    seconds_delay: int | None
    minimum_order_size: Decimal | None
    tick_size: Decimal | None
    condition_id: str | None
    token_ids: tuple[str | None, str | None]


class PublicReads:
    """Order book and market facts. No credential."""

    def __init__(self) -> None:
        self._client = PublicClient()

    def market_for_token(self, token_id: str) -> MarketFacts | None:
        page = self._client.list_markets(clob_token_ids=token_id, page_size=1).first_page()
        if not page.items:
            return None
        market = page.items[0]
        return MarketFacts(
            slug=market.slug,
            accepting_orders=market.state.accepting_orders,
            closed=market.state.closed,
            game_start_time=market.sports.game_start_time if market.sports else None,
            seconds_delay=market.trading.seconds_delay if market.trading else None,
            minimum_order_size=market.trading.minimum_order_size if market.trading else None,
            tick_size=market.trading.minimum_tick_size if market.trading else None,
            condition_id=str(market.condition_id) if market.condition_id else None,
            token_ids=(
                str(market.outcomes.yes.token_id) if market.outcomes.yes.token_id else None,
                str(market.outcomes.no.token_id) if market.outcomes.no.token_id else None,
            ),
        )

    def close(self) -> None:
        self._client.close()

    def estimate_buy_price(self, token_id: str, stake_usd: Decimal) -> Decimal | None:
        """The price level a FAK BUY of `stake_usd` reaches on the current book. None: no depth."""
        try:
            return self._client.estimate_market_price(
                token_id=token_id, side="BUY", amount=str(stake_usd), order_type="FAK"
            )
        except InsufficientLiquidityError:
            return None


def round_down_to_tick(price: Decimal, tick: Decimal | None) -> Decimal:
    if tick is None or tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


class Account:
    """The Polymarket account that buys. Built only when a command needs it."""

    def __init__(self, settings: Settings) -> None:
        settings.require_polymarket_credentials()
        assert settings.polymarket_private_key is not None
        kwargs: dict[str, Any] = {
            "private_key": settings.polymarket_private_key.get_secret_value(),
            "wallet": settings.polymarket_wallet_address,
        }
        if settings.polymarket_relayer_api_key and settings.polymarket_relayer_api_key_address:
            kwargs["api_key"] = RelayerApiKey(
                key=settings.polymarket_relayer_api_key.get_secret_value(),
                address=settings.polymarket_relayer_api_key_address,
            )
        self._client = SecureClient.create(**kwargs)
        self._builder_code = settings.polymarket_builder_code

    @property
    def wallet(self) -> str:
        return str(self._client.wallet)

    @property
    def wallet_type(self) -> str:
        return str(self._client.wallet_type)

    def collateral_balance_usd(self) -> Decimal:
        balance = self._client.get_balance_allowance(asset_type="COLLATERAL")
        return Decimal(balance.balance) * COLLATERAL_BASE_UNIT

    def approvals_ready(self) -> tuple[bool, str]:
        """(all set, a short count of what is missing) for the wallet's exchange approvals."""
        state = self._client.get_trading_approvals_state()
        missing = state.missing
        erc20 = len(getattr(missing, "erc20", ()) or ())
        erc1155 = len(getattr(missing, "erc1155", ()) or ())
        return bool(state.is_fully_approved), f"{erc20} ERC-20 and {erc1155} ERC-1155 approvals"

    def setup_approvals(self) -> None:
        """Gasless, idempotent, and only meaningful with a Relayer API key configured."""
        self._client.setup_trading_approvals()

    def prepare_buy(self, token_id: str, stake_usd: Decimal, max_price: Decimal) -> SignedOrder:
        """Sign a Fill-and-Kill BUY without sending it to the exchange."""
        return self._client.create_market_order(
            token_id=token_id,
            side="BUY",
            amount=str(stake_usd),
            max_price=str(max_price),
            order_type="FAK",
            builder_code=self._builder_code,
        )

    def submit_buy(self, signed: SignedOrder) -> AcceptedOrder | RejectedOrder:
        """Submit once; never auto-approve or retry an ambiguous response."""
        return self._client.post_order(signed)

    def close(self) -> None:
        self._client.close()
