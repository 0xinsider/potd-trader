"""Pinned Kalshi REST reads and one non-retried V2 IOC order submission.

Contracts: docs.kalshi.com/api-reference/orders/create-order-v2,
getting_started/{api_keys,order_direction,fixed_point_migration,fee_rounding},
and kalshi.com/docs/kalshi-fee-schedule.pdf (effective July 7, 2026).
All V2 order prices use the YES leg; buying NO submits an ask at 1-NO price.
Only binary, one-dollar, default-settlement event contracts are supported.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from pydantic import SecretStr

Outcome = Literal["yes", "no"]
Environment = Literal["demo", "production"]
_ORIGINS = {
    "demo": "https://external-api.demo.kalshi.co",
    "production": "https://external-api.kalshi.com",
}
_API_PATH = "/trade-api/v2"
_ONE = Decimal("1")
_QUADRATIC_FEES = frozenset(
    {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}
)


class KalshiError(RuntimeError):
    """Sanitized provider, credential, or contract failure; submission must stop."""


class KalshiHTTPError(KalshiError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Kalshi returned HTTP {status}; no automatic order retry")


class KalshiNotSubmitted(KalshiError):
    """A failure before the order POST began; no exchange order was submitted."""


@dataclass(frozen=True)
class MarketBundle:
    market: dict[str, Any]
    event: dict[str, Any]
    series: dict[str, Any]
    fingerprint: str
    ticker: str
    exchange_index: int
    fractional: bool
    fee_per_contract: Decimal


@dataclass(frozen=True)
class OrderReceipt:
    order_id: str
    client_order_id: str | None
    fill_count: Decimal
    remaining_count: Decimal
    # This is the provider's YES-book average, including for a NO purchase.
    average_fill_price: Decimal | None
    average_fee_paid: Decimal | None


@dataclass(frozen=True)
class OrderObservation:
    order_id: str
    client_order_id: str
    ticker: str
    outcome_side: Outcome
    status: str
    fill_count: Decimal
    remaining_count: Decimal
    initial_count: Decimal
    limit_price: Decimal
    fill_cost: Decimal
    fees: Decimal
    total_cost: Decimal | None
    exchange_index: int


def _reject_constant(_value: str) -> None:
    raise KalshiError("Kalshi response contains a nonfinite JSON number")


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise KalshiError(f"Kalshi {field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise KalshiError(f"Kalshi {field} must be a nonempty string")
    if any(ord(char) < 32 for char in value):
        raise KalshiError(f"Kalshi {field} contains control characters")
    return value


def _integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise KalshiError(f"Kalshi {field} must be a nonnegative integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise KalshiError(f"Kalshi {field} must be a boolean")
    return value


def _decimal(value: Any, field: str, *, places: int = 6) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise KalshiError(f"Kalshi {field} must be a fixed-point decimal string")
    if len(value) > 40:
        raise KalshiError(f"Kalshi {field} exceeds supported precision")
    result = Decimal(value)
    exponent = result.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -places:
        raise KalshiError(f"Kalshi {field} exceeds supported decimal places")
    return result


def _instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise KalshiError(f"Kalshi {field} must be an aware timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise KalshiError(f"Kalshi {field} has an invalid timestamp") from None
    if result.tzinfo is None:
        raise KalshiError(f"Kalshi {field} has no timezone")
    return result


def _fee_multiplier(value: Any) -> Decimal:
    # OpenAPI specifies this field as a JSON number, unlike money/count fields.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise KalshiError("Kalshi fee multiplier must be a finite nonnegative number")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise KalshiError("Kalshi fee multiplier is invalid") from None
    if not result.is_finite() or result < 0:
        raise KalshiError("Kalshi fee multiplier must be finite and nonnegative")
    exponent = result.as_tuple().exponent
    if result > 100 or not isinstance(exponent, int) or exponent < -6:
        raise KalshiError("Kalshi fee multiplier exceeds the supported exact spending model")
    return result


def _fee_model(fee_type: Any, multiplier: Any) -> Decimal:
    if not isinstance(fee_type, str) or fee_type not in _QUADRATIC_FEES:
        raise KalshiError("Kalshi fee model is unsupported; cannot bound all-in spending")
    return _fee_multiplier(multiplier)


def _ranges(market: dict[str, Any]) -> list[tuple[Decimal, Decimal, Decimal]]:
    values = market.get("price_ranges")
    if not isinstance(values, list) or not values or len(values) > 100:
        raise KalshiError("Kalshi price ranges are unavailable or invalid")
    result = []
    for value in values:
        band = _object(value, "price range")
        start = _decimal(band.get("start"), "price range start", places=4)
        end = _decimal(band.get("end"), "price range end", places=4)
        step = _decimal(band.get("step"), "price range step", places=4)
        if not 0 <= start < end <= _ONE or not 0 < step <= end - start:
            raise KalshiError("Kalshi price range is outside the binary price domain")
        result.append((start, end, step))
    return result


def _settlement_sources(owner: dict[str, Any], field: str) -> None:
    sources = owner.get("settlement_sources")
    if not isinstance(sources, list) or not sources or len(sources) > 100:
        raise KalshiError(f"Kalshi {field} settlement sources are unavailable or invalid")
    for raw in sources:
        source = _object(raw, "settlement source")
        _string(source.get("name"), "settlement source name")
        url = _string(source.get("url"), "settlement source URL")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise KalshiError("Kalshi settlement source URL is invalid") from None
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise KalshiError("Kalshi settlement source URL must identify an HTTPS source")


def _fingerprint(
    market: dict[str, Any],
    event: dict[str, Any],
    series: dict[str, Any],
    documents: dict[str, str],
) -> str:
    owners = (
        (
            market,
            (
                "ticker",
                "event_ticker",
                "market_type",
                "yes_sub_title",
                "no_sub_title",
                "notional_value_dollars",
                "rules_primary",
                "rules_secondary",
                "strike_type",
                "floor_strike",
                "cap_strike",
                "functional_strike",
                "custom_strike",
                "settlement_bounds_type",
                "settlement_floor_dollars",
                "mve_collection_ticker",
                "mve_selected_legs",
                "primary_participant_key",
            ),
        ),
        (
            event,
            (
                "event_ticker",
                "series_ticker",
                "title",
                "sub_title",
                "settlement_sources",
                "strike_date",
                "strike_period",
                "collateral_return_type",
                "mutually_exclusive",
            ),
        ),
        (
            series,
            (
                "ticker",
                "contract_url",
                "contract_terms_url",
                "settlement_sources",
                "additional_prohibitions",
            ),
        ),
    )
    payload = [{key: owner.get(key) for key in keys} for owner, keys in owners]
    payload.append({"contract_document_sha256": documents})

    def encode_number(value: Any) -> str:
        if isinstance(value, Decimal) and value.is_finite():
            return str(value)
        raise KalshiError("Kalshi settlement metadata contains an unsupported value")

    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=encode_number
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class KalshiClient:
    def __init__(
        self,
        environment: Environment,
        api_key_id: SecretStr | None = None,
        private_key_path: Path | None = None,
    ) -> None:
        if environment not in _ORIGINS:
            raise KalshiError("Kalshi environment must be demo or production")
        if (api_key_id is None) != (private_key_path is None):
            raise KalshiError("Kalshi authentication needs both key ID and private key file")
        self._key_id = api_key_id
        self._private_key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | None = None
        if private_key_path is not None:
            try:
                parsed = serialization.load_pem_private_key(
                    private_key_path.expanduser().read_bytes(), password=None
                )
            except (OSError, ValueError, TypeError, UnsupportedAlgorithm) as exc:
                raise KalshiError(
                    f"Kalshi private key cannot be loaded ({type(exc).__name__})"
                ) from None
            if not isinstance(parsed, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
                raise KalshiError("Kalshi private key must be RSA or Ed25519")
            if isinstance(parsed, rsa.RSAPrivateKey) and parsed.key_size < 2048:
                raise KalshiError("Kalshi RSA private key must have at least 2048 bits")
            self._private_key = parsed
        self._client = httpx.Client(
            base_url=_ORIGINS[environment],
            timeout=15.0,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "potd-trader/kalshi"},
        )

    def close(self) -> None:
        self._client.close()

    def _headers(self, method: str, full_path: str, authenticated: bool) -> dict[str, str]:
        headers = {}
        if authenticated:
            if self._private_key is None or self._key_id is None:
                raise KalshiError("Kalshi account credentials are not configured")
            timestamp = str(time.time_ns() // 1_000_000)
            message = (timestamp + method + full_path).encode("utf-8")
            try:
                if isinstance(self._private_key, ed25519.Ed25519PrivateKey):
                    signature = self._private_key.sign(message)
                else:
                    signature = self._private_key.sign(
                        message,
                        padding.PSS(
                            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
                        ),
                        hashes.SHA256(),
                    )
            except (ValueError, TypeError) as exc:
                raise KalshiError(f"Kalshi request signing failed ({type(exc).__name__})") from None
            headers = {
                "KALSHI-ACCESS-KEY": self._key_id.get_secret_value(),
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
            }
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = False,
        params: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
        before_post: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "?" in path or "#" in path:
            if method == "POST":
                raise KalshiNotSubmitted("Kalshi request path is invalid; order not submitted")
            raise KalshiError("Kalshi request path is invalid")
        full_path = _API_PATH + path
        if method == "POST":
            try:
                headers = self._headers(method, full_path, authenticated)
                if before_post is None or before_post() is not True:
                    raise KalshiNotSubmitted(
                        "Kalshi order stopped or expired immediately before POST"
                    )
            except KalshiNotSubmitted:
                raise
            except KalshiError as exc:
                raise KalshiNotSubmitted(str(exc)) from None
            except Exception as exc:
                raise KalshiNotSubmitted(
                    f"Kalshi order pre-submission control failed ({type(exc).__name__})"
                ) from None
        else:
            headers = self._headers(method, full_path, authenticated)
        try:
            response = self._client.request(
                method, full_path, params=params, json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise KalshiError(f"Kalshi transport failed ({type(exc).__name__})") from None
        if response.status_code not in ({201} if method == "POST" else {200}):
            raise KalshiHTTPError(response.status_code)
        try:
            value = json.loads(
                response.content, parse_float=Decimal, parse_constant=_reject_constant
            )
        except (ValueError, UnicodeError):
            raise KalshiError("Kalshi response is not valid JSON") from None
        return _object(value, "response")

    def _contract_document_hash(self, value: Any) -> tuple[str, str]:
        url = _string(value, "contract document URL")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "assets.kalshi.com"
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(
                r"/(?:contract_terms|regulatory/product-certifications)/[A-Za-z0-9_-]+\.pdf",
                parsed.path,
            )
        ):
            raise KalshiError("Kalshi contract document URL is outside the pinned asset paths")
        digest = hashlib.sha256()
        size = 0
        prefix = b""
        try:
            with self._client.stream(
                "GET", url, headers={"Accept": "application/pdf", "Cache-Control": "no-cache"}
            ) as response:
                if response.status_code != 200:
                    raise KalshiHTTPError(response.status_code)
                if (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/pdf"
                ):
                    raise KalshiError("Kalshi contract document is not a PDF")
                for chunk in response.iter_bytes(chunk_size=8192):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise KalshiError("Kalshi contract document exceeds the two-megabyte bound")
                    if len(prefix) < 5:
                        prefix += chunk[: 5 - len(prefix)]
                    digest.update(chunk)
        except httpx.HTTPError as exc:
            raise KalshiError(
                f"Kalshi contract document read failed ({type(exc).__name__})"
            ) from None
        if size == 0 or prefix != b"%PDF-":
            raise KalshiError("Kalshi contract document has invalid PDF content")
        return url, digest.hexdigest()

    def markets(self, series_ticker: str | None = None) -> list[dict[str, Any]]:
        """Read one bounded page for discovery, never choose or map a trade."""
        params: dict[str, str | int] = {"status": "open", "limit": 50}
        if series_ticker is not None:
            params["series_ticker"] = _string(series_ticker, "series ticker")
        entries = self._request("GET", "/markets", params=params).get("markets")
        if not isinstance(entries, list) or len(entries) > 50:
            raise KalshiError("Kalshi market discovery response is invalid")
        return [_object(entry, "market") for entry in entries]

    def bundle(self, ticker: str) -> MarketBundle:
        ticker = _string(ticker, "ticker")
        market = _object(
            self._request("GET", f"/markets/{quote(ticker, safe='')}").get("market"), "market"
        )
        if _string(market.get("ticker"), "market ticker") != ticker:
            raise KalshiError("Kalshi returned a different market ticker")
        if (
            market.get("market_type") != "binary"
            or market.get("settlement_bounds_type") != "default"
        ):
            raise KalshiError("Kalshi market must be binary with default settlement bounds")
        if _decimal(market.get("notional_value_dollars"), "contract notional") != _ONE:
            raise KalshiError("Kalshi market must settle on a one-dollar notional")
        if market.get("mve_collection_ticker") or market.get("mve_selected_legs"):
            raise KalshiError("Kalshi combo markets are unsupported")
        for field in ("rules_primary", "rules_secondary", "yes_sub_title", "no_sub_title"):
            value = market.get(field)
            if not isinstance(value, str) or (field == "rules_primary" and not value.strip()):
                raise KalshiError(f"Kalshi {field} is unavailable")
        rules = (market["rules_primary"] + "\n" + market["rules_secondary"]).casefold()
        if any(
            phrase in rules
            for phrase in (
                "no real event settlement",
                "demo-only title validation fixture",
            )
        ):
            raise KalshiError("Kalshi demo title-validation fixture has no real event settlement")
        _ranges(market)
        _instant(market.get("open_time"), "market open time")
        _instant(market.get("close_time"), "market close time")
        _string(market.get("status"), "market status")
        index = _integer(market.get("exchange_index"), "market exchange index")
        event_ticker = _string(market.get("event_ticker"), "event ticker")
        event = _object(
            self._request("GET", f"/events/{quote(event_ticker, safe='')}").get("event"), "event"
        )
        if _string(event.get("event_ticker"), "event ticker") != event_ticker:
            raise KalshiError("Kalshi market and event identities disagree")
        series_ticker = _string(event.get("series_ticker"), "series ticker")
        series = _object(
            self._request("GET", f"/series/{quote(series_ticker, safe='')}").get("series"), "series"
        )
        if _string(series.get("ticker"), "series ticker") != series_ticker:
            raise KalshiError("Kalshi event and series identities disagree")
        _settlement_sources(event, "event")
        _settlement_sources(series, "series")
        if (
            _integer(event.get("exchange_index"), "event exchange index") != index
            or _integer(series.get("exchange_index"), "series exchange index") != index
        ):
            raise KalshiError("Kalshi market, event and series exchange indexes disagree")
        multiplier = self._maximum_fee_multiplier(event, series)
        documents = dict(
            self._contract_document_hash(series.get(field))
            for field in (
                "contract_url",
                "contract_terms_url",
            )
        )
        # Fractional fills can occur even for integer orders (official fixed-point guide).
        # For C contracts there are at most 100*C fills. Every fill adds <=$0.000001
        # model rounding and <$0.01 balance rounding at the coarser member precision.
        # Ignore rebates: 0.07*M*P*(1-P) <=0.0175*M, plus 100*(.000001+.01).
        # This bounds exchange fees for both precision classes; external FCM commissions
        # are not supported. It deliberately over-reserves instead of assuming rebates.
        fee = Decimal("0.0175") * multiplier + Decimal("1.0001")
        return MarketBundle(
            market,
            event,
            series,
            _fingerprint(market, event, series, documents),
            ticker,
            index,
            True,
            fee,
        )

    def _maximum_fee_multiplier(self, event: dict[str, Any], series: dict[str, Any]) -> Decimal:
        multipliers = [_fee_model(series.get("fee_type"), series.get("fee_multiplier"))]
        override_type = event.get("fee_type_override")
        override_multiplier = event.get("fee_multiplier_override")
        if (override_type is None) != (override_multiplier is None):
            raise KalshiError("Kalshi event fee override is incomplete")
        if override_type is not None:
            multipliers.append(_fee_model(override_type, override_multiplier))
        changes = self._request(
            "GET",
            "/series/fee_changes",
            params={"series_ticker": _string(series.get("ticker"), "series ticker")},
        ).get("series_fee_change_arr")
        if not isinstance(changes, list) or len(changes) > 10_000:
            raise KalshiError("Kalshi scheduled series fees are unavailable or unbounded")
        now = datetime.now(UTC)
        for raw in changes:
            change = _object(raw, "series fee change")
            if change.get("series_ticker") != series["ticker"]:
                raise KalshiError("Kalshi series fee change has a different identity")
            if _instant(change.get("scheduled_ts"), "fee change time") > now:
                multipliers.append(_fee_model(change.get("fee_type"), change.get("fee_multiplier")))
        cursor: str | None = None
        for _page in range(10):
            params: dict[str, str | int] = {"event_ticker": event["event_ticker"], "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = self._request("GET", "/events/fee_changes", params=params)
            entries = payload.get("event_fee_changes")
            if not isinstance(entries, list) or len(entries) > 1000:
                raise KalshiError("Kalshi scheduled event fees are unavailable or invalid")
            for raw in entries:
                change = _object(raw, "event fee change")
                if (
                    change.get("event_ticker") != event["event_ticker"]
                    or change.get("series_ticker") != series["ticker"]
                ):
                    raise KalshiError("Kalshi event fee change has a different identity")
                if _instant(change.get("scheduled_ts"), "fee change time") <= now:
                    continue
                fee_type, fee_multiplier = (
                    change.get("fee_type_override"),
                    change.get("fee_multiplier_override"),
                )
                if (fee_type is None) != (fee_multiplier is None):
                    raise KalshiError("Kalshi scheduled event fee override is incomplete")
                if fee_type is not None:
                    multipliers.append(_fee_model(fee_type, fee_multiplier))
            next_cursor = payload.get("cursor")
            if not isinstance(next_cursor, str):
                raise KalshiError("Kalshi fee changes cursor is invalid")
            if not next_cursor:
                return max(multipliers)
            if next_cursor == cursor:
                raise KalshiError("Kalshi fee changes cursor failed to advance")
            cursor = next_cursor
        raise KalshiError("Kalshi scheduled fee changes exceeded the bounded read")

    def snap_price(self, bundle: MarketBundle, outcome: Outcome, max_price: Decimal) -> Decimal:
        """Return a valid YES price that preserves the chosen-outcome cost ceiling."""
        if outcome not in ("yes", "no") or not max_price.is_finite() or not 0 < max_price < 1:
            raise KalshiError("Kalshi outcome or price ceiling is invalid")
        target = max_price if outcome == "yes" else _ONE - max_price
        candidates = []
        for start, end, step in _ranges(bundle.market):
            if outcome == "yes" and target >= start:
                units = ((min(target, end) - start) / step).to_integral_value(rounding=ROUND_FLOOR)
            elif outcome == "no" and target <= end:
                units = ((max(target, start) - start) / step).to_integral_value(
                    rounding=ROUND_CEILING
                )
            else:
                continue
            price = start + units * step
            if start <= price <= end and 0 < price < 1:
                candidates.append(price)
        if not candidates:
            raise KalshiError("Kalshi price ceiling has no valid provider price level")
        return max(candidates) if outcome == "yes" else min(candidates)

    def offers(self, bundle: MarketBundle, outcome: Outcome) -> list[tuple[Decimal, Decimal]]:
        if outcome not in ("yes", "no"):
            raise KalshiError("Kalshi outcome is invalid")
        payload = self._request(
            "GET", f"/markets/{quote(bundle.ticker, safe='')}/orderbook", params={"depth": 100}
        )
        book = _object(payload.get("orderbook_fp"), "orderbook")
        levels = book.get("no_dollars" if outcome == "yes" else "yes_dollars")
        if not isinstance(levels, list) or len(levels) > 100:
            raise KalshiError("Kalshi orderbook levels are invalid")
        result = []
        prices: set[Decimal] = set()
        for level in levels:
            if not isinstance(level, list) or len(level) != 2:
                raise KalshiError("Kalshi orderbook level must contain price and quantity")
            bid = _decimal(level[0], "book price", places=4)
            quantity = _decimal(level[1], "book quantity", places=2)
            if not 0 < bid < 1 or quantity <= 0 or bid in prices:
                raise KalshiError("Kalshi orderbook price or quantity is invalid")
            prices.add(bid)
            selected_price = _ONE - bid
            yes_price = selected_price if outcome == "yes" else bid
            if self.snap_price(bundle, "yes", yes_price) != yes_price:
                raise KalshiError("Kalshi orderbook price is off the provider grid")
            result.append((selected_price, quantity))
        return sorted(result)

    def is_tradable(self, bundle: MarketBundle) -> bool:
        now = datetime.now(UTC)
        if bundle.market["status"] != "active" or not _instant(
            bundle.market["open_time"], "market open time"
        ) <= now < _instant(bundle.market["close_time"], "market close time"):
            return False
        status = self._request("GET", "/exchange/status")
        indexes = status.get("exchange_index_statuses")
        if indexes is None and bundle.exchange_index == 0:
            return _boolean(status.get("exchange_active"), "exchange active") and _boolean(
                status.get("trading_active"), "trading active"
            )
        if not isinstance(indexes, list) or len(indexes) > 100:
            raise KalshiError("Kalshi exchange shard status is unavailable")
        matches = []
        for item in indexes:
            shard = _object(item, "exchange status")
            if (
                _integer(shard.get("exchange_index"), "exchange status index")
                == bundle.exchange_index
            ):
                matches.append(shard)
        if len(matches) != 1:
            raise KalshiError("Kalshi exchange shard status is missing or duplicated")
        return _boolean(matches[0].get("exchange_active"), "exchange active") and _boolean(
            matches[0].get("trading_active"), "trading active"
        )

    def balance(self, exchange_index: int) -> Decimal:
        index = _integer(exchange_index, "exchange index")
        payload = self._request(
            "GET",
            "/portfolio/balance",
            authenticated=True,
            params={"exchange_index": index, "subaccount": 0},
        )
        _integer(payload.get("updated_ts"), "balance timestamp")
        return _decimal(payload.get("balance_dollars"), "available balance")

    def _rows(
        self,
        path: str,
        params: dict[str, str | int],
        field: str,
    ) -> Iterator[dict[str, Any]]:
        cursor = ""
        seen: set[str] = set()
        for _page in range(10):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            payload = self._request("GET", path, authenticated=True, params=query)
            rows = payload.get(field)
            if not isinstance(rows, list) or len(rows) > 1000:
                raise KalshiError("Kalshi portfolio rows are invalid")
            for raw in rows:
                yield _object(raw, "portfolio row")
            # GetOrdersResponse requires cursor; GetPositionsResponse makes it optional.
            next_cursor = (
                payload.get("cursor") if path == "/portfolio/orders" else payload.get("cursor", "")
            )
            if not isinstance(next_cursor, str):
                raise KalshiError("Kalshi portfolio cursor is invalid")
            if not next_cursor:
                return
            if next_cursor in seen:
                raise KalshiError("Kalshi portfolio cursor failed to advance")
            seen.add(next_cursor)
            cursor = next_cursor
        raise KalshiError("Kalshi exposure lookup exceeded the bounded read")

    def has_exposure(self, bundle: MarketBundle) -> bool:
        """Refuse to offset existing YES/NO positions or cross unmanaged resting orders."""
        query: dict[str, str | int] = {
            "ticker": bundle.ticker,
            "exchange_index": bundle.exchange_index,
            "subaccount": 0,
            "limit": 1000,
        }
        for position in self._rows(
            "/portfolio/positions", {**query, "settlement_status": "unsettled"}, "market_positions"
        ):
            if (
                position.get("ticker") != bundle.ticker
                or _integer(position.get("exchange_index"), "position exchange index")
                != bundle.exchange_index
            ):
                raise KalshiError("Kalshi position response has a different market identity")
            value = position.get("position_fp")
            if not isinstance(value, str):
                raise KalshiError("Kalshi position must be a signed fixed-point count string")
            magnitude = _decimal(
                value[1:] if value.startswith("-") else value, "position count", places=2
            )
            exposure = _decimal(position.get("market_exposure_dollars"), "position exposure")
            if magnitude != 0 or exposure != 0:
                return True
        for row in self._rows("/portfolio/orders", {**query, "status": "resting"}, "orders"):
            order = self._observation(row)
            if (
                order.ticker != bundle.ticker
                or order.exchange_index != bundle.exchange_index
                or order.status != "resting"
            ):
                raise KalshiError("Kalshi resting-order response has a different market identity")
            return True
        return False

    def submit_buy(
        self,
        bundle: MarketBundle,
        outcome: Outcome,
        count: int,
        max_price: Decimal,
        client_order_id: str,
        *,
        before_post: Callable[[], bool],
    ) -> OrderReceipt:
        try:
            if type(count) is not int or count < 1:
                raise KalshiError("Kalshi order quantity must be a positive whole contract count")
            client_id = _string(client_order_id, "client order ID")
            current = self.bundle(bundle.ticker)
            if (
                current.fingerprint != bundle.fingerprint
                or current.exchange_index != bundle.exchange_index
            ):
                raise KalshiError("Kalshi market identity changed before submission")
            if current.fee_per_contract > bundle.fee_per_contract:
                raise KalshiError("Kalshi fee bound increased before submission")
            if not self.is_tradable(current):
                raise KalshiError("Kalshi market or exchange is paused before submission")
            if self.has_exposure(current):
                raise KalshiError("Kalshi market already has an account position or resting order")
            price = self.snap_price(current, outcome, max_price)
        except KalshiError as exc:
            raise KalshiNotSubmitted(str(exc)) from None
        except Exception as exc:
            raise KalshiNotSubmitted(
                f"Kalshi order pre-submission validation failed ({type(exc).__name__})"
            ) from None
        payload = self._request(
            "POST",
            "/portfolio/events/orders",
            authenticated=True,
            before_post=before_post,
            body={
                "ticker": current.ticker,
                "client_order_id": client_id,
                "side": "bid" if outcome == "yes" else "ask",
                "count": f"{count}.00",
                "price": format(price, ".4f"),
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "post_only": False,
                "cancel_order_on_pause": True,
                "reduce_only": False,
                "subaccount": 0,
                "exchange_index": current.exchange_index,
            },
        )
        order_id = _string(payload.get("order_id"), "order ID")
        echoed = payload.get("client_order_id")
        if echoed is not None and _string(echoed, "client order ID") != client_id:
            raise KalshiError("Kalshi order response has a different client order ID")
        filled = _decimal(payload.get("fill_count"), "filled count", places=2)
        remaining = _decimal(payload.get("remaining_count"), "remaining count", places=2)
        if filled > count or remaining != 0:
            raise KalshiError("Kalshi IOC response has an invalid filled or resting quantity")
        _integer(payload.get("ts_ms"), "order timestamp")
        average = None
        fee = None
        if filled > 0:
            average = _decimal(payload.get("average_fill_price"), "average fill price")
            fee = _decimal(payload.get("average_fee_paid"), "average fee paid")
            selected = average if outcome == "yes" else _ONE - average
            if not 0 < selected <= max_price or fee > bundle.fee_per_contract:
                raise KalshiError("Kalshi fill exceeds its reserved price or fee ceiling")
        return OrderReceipt(order_id, echoed, filled, remaining, average, fee)

    def _observation(self, value: Any) -> OrderObservation:
        order = _object(value, "order")
        side = order.get("outcome_side")
        if side not in ("yes", "no"):
            raise KalshiError("Kalshi order outcome side is invalid")
        if order.get("book_side") != ("bid" if side == "yes" else "ask"):
            raise KalshiError("Kalshi order direction fields disagree")
        status = order.get("status")
        if status not in ("resting", "canceled", "executed"):
            raise KalshiError("Kalshi order status is invalid")
        filled = _decimal(order.get("fill_count_fp"), "filled count", places=2)
        remaining = _decimal(order.get("remaining_count_fp"), "remaining count", places=2)
        initial = _decimal(order.get("initial_count_fp"), "initial count", places=2)
        if initial <= 0 or filled + remaining > initial:
            raise KalshiError("Kalshi order quantities are inconsistent")
        yes_price = _decimal(order.get("yes_price_dollars"), "YES order limit price")
        no_price = _decimal(order.get("no_price_dollars"), "NO order limit price")
        if not 0 < yes_price < 1 or not 0 < no_price < 1 or yes_price + no_price != _ONE:
            raise KalshiError("Kalshi order limit prices are inconsistent")
        limit_price = yes_price if side == "yes" else no_price
        fill_cost = _decimal(order.get("taker_fill_cost_dollars"), "taker fill cost") + _decimal(
            order.get("maker_fill_cost_dollars"), "maker fill cost"
        )
        fees = _decimal(order.get("taker_fees_dollars"), "taker fees") + _decimal(
            order.get("maker_fees_dollars"), "maker fees"
        )
        if (filled > 0 and not 0 < fill_cost <= filled * limit_price) or (
            filled == 0 and (fill_cost != 0 or fees != 0)
        ):
            raise KalshiError(
                "Kalshi filled order principal is inconsistent with its outcome limit"
            )
        subaccount = order.get("subaccount_number")
        if subaccount is not None and _integer(subaccount, "order subaccount") != 0:
            raise KalshiError("Kalshi order belongs to a different subaccount")
        return OrderObservation(
            _string(order.get("order_id"), "order ID"),
            _string(order.get("client_order_id"), "client order ID"),
            _string(order.get("ticker"), "market ticker"),
            cast(Outcome, side),
            status,
            filled,
            remaining,
            initial,
            limit_price,
            fill_cost,
            fees,
            fill_cost + fees,
            _integer(order.get("exchange_index"), "exchange index"),
        )

    def find_order(
        self,
        client_order_id: str,
        ticker: str,
        exchange_index: int,
        order_id: str | None = None,
    ) -> OrderObservation | None:
        client_id = _string(client_order_id, "client order ID")
        ticker = _string(ticker, "market ticker")
        index = _integer(exchange_index, "exchange index")
        if order_id is not None:
            order_id = _string(order_id, "order ID")
            try:
                payload = self._request(
                    "GET", f"/portfolio/orders/{quote(order_id, safe='')}", authenticated=True
                )
            except KalshiHTTPError as exc:
                if exc.status == 404:
                    return None
                raise
            observed = self._observation(payload.get("order"))
            if (
                observed.order_id != order_id
                or observed.client_order_id != client_id
                or observed.ticker != ticker
                or observed.exchange_index != index
            ):
                raise KalshiError("Kalshi order observation has a different identity")
            return observed
        cursor: str | None = None
        for _page in range(10):
            params: dict[str, str | int] = {
                "ticker": ticker,
                "exchange_index": index,
                "subaccount": 0,
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._request("GET", "/portfolio/orders", authenticated=True, params=params)
            orders = payload.get("orders")
            if not isinstance(orders, list) or len(orders) > 1000:
                raise KalshiError("Kalshi order observations are invalid")
            matches = []
            for raw in orders:
                value = _object(raw, "order")
                if value.get("client_order_id") == client_id:
                    observed = self._observation(value)
                    if observed.ticker != ticker or observed.exchange_index != index:
                        raise KalshiError("Kalshi order observation has a different identity")
                    matches.append(observed)
            if len(matches) > 1:
                raise KalshiError("Kalshi client order ID matches multiple orders")
            if matches:
                return matches[0]
            next_cursor = payload.get("cursor")
            if not isinstance(next_cursor, str):
                raise KalshiError("Kalshi orders cursor is invalid")
            if not next_cursor:
                return None
            if next_cursor == cursor:
                raise KalshiError("Kalshi orders cursor failed to advance")
            cursor = next_cursor
        raise KalshiError("Kalshi order lookup exceeded the bounded read; reservation stays held")
