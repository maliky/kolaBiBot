"""Binance REST adapters.

The bot passes platform-specific symbols in TSV files, so no symbol mapping is
attempted here. USD-M Futures remains the default adapter; Spot and Margin share
the same signing, rounding, audit, and runtime-facing order contract where the
Binance APIs overlap.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any, Dict, Iterable, Sequence, cast
from urllib.parse import urlencode
from uuid import uuid4

import requests
from sqlalchemy.orm import Session, sessionmaker

from kolabi.shared.binance_futures import binance_futures_audit_db_url
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.core.runtime_types import OrderQty, Price, StopPrice
from kolabi.shared.core.types import ExchangeABC
from kolabi.shared.persistence import (
    Base,
    ExchangeRestCall,
    create_persistence_engine,
    prune_exchange_rest_calls,
)

_LOGGER = logging.getLogger("kola")

_REST_AUDIT_MAINTENANCE_SECONDS = 60.0


@dataclass(frozen=True)
class BinanceOrderRequest:
    """Cached order request needed for Binance cancel-replace amendments."""

    side: str
    quantity: float
    kolabi_type: str
    binance_type: str
    price: float | None
    stop_price: float | None
    client_order_id: str
    reduce_only: bool
    working_type: str | None
    time_in_force: str | None


class BinanceAdapter(ExchangeABC):
    """REST adapter for Binance USD-M Futures."""

    market_type = "futures"
    order_path = "/fapi/v1/order"
    test_order_path: str | None = "/fapi/v1/order/test"
    open_orders_path = "/fapi/v1/openOrders"
    exchange_info_path = "/fapi/v1/exchangeInfo"
    book_ticker_path = "/fapi/v1/ticker/bookTicker"
    premium_index_path = "/fapi/v1/premiumIndex"
    position_path = "/fapi/v2/positionRisk"
    balance_path = "/fapi/v2/balance"
    supports_reduce_only = True
    supports_working_type = True
    supports_native_limit_amend = True
    uses_margin_order_params = False

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        symbol: str,
        *,
        environment: str = "demo",
        timeout: float = 10.0,
        audit_db_url: str | None = None,
        account_scope: str = "default",
        market_type: str | None = None,
        side_effect_type: str | None = None,
        is_isolated: bool = False,
        auto_repay_at_cancel: bool | None = None,
        rest_audit_retention_minutes: int = 1440,
        rest_audit_retention_limit: int = 10000,
        rest_audit_maintenance_seconds: float = _REST_AUDIT_MAINTENANCE_SECONDS,
        **_unused: Any,
    ) -> None:
        super().__init__(api_key, api_secret, base_url.rstrip("/"), symbol)
        self.environment = environment
        self.market_type = market_type or self.market_type
        self.side_effect_type = side_effect_type
        self.is_isolated = _truthy(is_isolated)
        self.auto_repay_at_cancel = auto_repay_at_cancel
        self.timeout = float(timeout)
        self.account_scope = account_scope
        self.rest_audit_retention_minutes = rest_audit_retention_minutes
        self.rest_audit_retention_limit = rest_audit_retention_limit
        self.rest_audit_maintenance_seconds = rest_audit_maintenance_seconds
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self.audit_db_url = audit_db_url or binance_futures_audit_db_url(
            environment,
            account_scope,
        )
        self._audit_engine = create_persistence_engine(self.audit_db_url)
        Base.metadata.create_all(self._audit_engine)
        self._audit_sessionmaker = sessionmaker(
            bind=self._audit_engine,
            expire_on_commit=False,
            class_=Session,
        )
        self.rest_audit_errors: list[str] = []
        self._last_rest_audit_prune_monotonic = 0.0
        self._rules_cache: dict[str, dict[str, object]] = {}
        self._orders_by_order_id: dict[str, BinanceOrderRequest] = {}
        self._orders_by_client_id: dict[str, BinanceOrderRequest] = {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | Sequence[tuple[str, Any]] | None = None,
        auth: bool = False,
        retry_attempts: int = 3,
    ) -> Any:
        raw_params = dict(params or {}) if not isinstance(params, list) else dict(params)
        payload = _clean_params(raw_params)
        max_attempts = _effective_retry_attempts(
            method=method,
            path=path,
            payload=payload,
            configured=retry_attempts,
        )
        last_error: RuntimeError | None = None
        for attempt in range(1, max_attempts + 1):
            request_payload = dict(payload)
            if auth:
                request_payload.setdefault("recvWindow", 5000)
                request_payload["timestamp"] = int(time.time() * 1000)
                request_payload["signature"] = _sign_params(
                    request_payload,
                    self.api_secret,
                )
            try:
                response = self.session.request(
                    method.upper(),
                    f"{self.base_url}{path}",
                    params=request_payload if method.upper() in {"GET", "DELETE"} else None,
                    data=request_payload if method.upper() not in {"GET", "DELETE"} else None,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                error = RuntimeError(
                    f"Binance transport error on {path}: {exc.__class__.__name__}: {exc}"
                )
                if attempt < max_attempts:
                    time.sleep(0.25 * attempt)
                    last_error = error
                    continue
                self._record_rest_call(
                    method=method,
                    path=path,
                    request_params=request_payload,
                    attempt_count=attempt,
                    http_status=None,
                    response_payload={},
                    result_kind="transport_error",
                    error_text=str(error),
                )
                raise error from exc
            try:
                data = response.json()
            except ValueError:
                data = {"raw_text": response.text}
            status_code = getattr(response, "status_code", 200)
            if status_code >= 400:
                error = RuntimeError(f"Binance HTTP {status_code} on {path}: {data}")
                if status_code in {502, 503, 504} and attempt < max_attempts:
                    time.sleep(0.25 * attempt)
                    last_error = error
                    continue
                self._record_rest_call(
                    method=method,
                    path=path,
                    request_params=request_payload,
                    attempt_count=attempt,
                    http_status=status_code,
                    response_payload=data if isinstance(data, dict) else {"payload": data},
                    result_kind="http_error",
                    error_text=str(error),
                )
                raise error
            self._record_rest_call(
                method=method,
                path=path,
                request_params=request_payload,
                attempt_count=attempt,
                http_status=status_code,
                response_payload=data if isinstance(data, dict) else {"payload": data},
                result_kind="ok",
                error_text=None,
            )
            return data
        assert last_error is not None
        raise last_error

    def _record_rest_call(
        self,
        *,
        method: str,
        path: str,
        request_params: Dict[str, Any],
        attempt_count: int,
        http_status: int | None,
        response_payload: Dict[str, Any],
        result_kind: str,
        error_text: str | None,
    ) -> None:
        if not _should_persist_rest_call(method=method, path=path):
            return
        request_payload = _redact_signature(request_params)
        payload = cast(Dict[str, Any], _json_safe_value(response_payload))
        order_like = _extract_order_like(payload)
        request_order_id = optional_str(
            request_payload.get("orderId") or request_payload.get("origClientOrderId")
        )
        response_order_id = optional_str(order_like.get("orderId"))
        endpoint_order_id = response_order_id or request_order_id
        client_order_id = optional_str(
            order_like.get("clientOrderId")
            or request_payload.get("newClientOrderId")
            or request_payload.get("origClientOrderId")
        )
        correlation_id = client_order_id or endpoint_order_id or request_order_id
        with self._audit_sessionmaker() as session:
            try:
                session.add(
                    ExchangeRestCall(
                        local_uuid=str(uuid4()),
                        exchange="binance",
                        environment=self.environment,
                        market_type=self.market_type,
                        account_scope=self.account_scope,
                        symbol=optional_str(
                            order_like.get("symbol") or request_payload.get("symbol")
                        ),
                        method=method.upper(),
                        path=path,
                        request_params=request_payload,
                        attempt_count=attempt_count,
                        http_status=http_status,
                        result_kind=result_kind,
                        response_payload=payload,
                        error_text=error_text,
                        client_order_id=client_order_id,
                        exchange_order_id=endpoint_order_id,
                        endpoint_order_id=endpoint_order_id,
                        correlation_id=correlation_id,
                        ack_status=optional_str(order_like.get("status")),
                        ack_order_id=endpoint_order_id,
                        ack_client_order_id=client_order_id,
                    )
                )
                session.commit()
                self._prune_rest_audit_if_due(session)
                return
            except Exception as exc:
                session.rollback()
                self._record_rest_audit_failure(
                    method=method,
                    path=path,
                    client_order_id=client_order_id,
                    endpoint_order_id=endpoint_order_id,
                    retry=0,
                    exc=exc,
                )
                return

    def _record_rest_audit_failure(
        self,
        *,
        method: str,
        path: str,
        client_order_id: str | None,
        endpoint_order_id: str | None,
        retry: int,
        exc: BaseException,
    ) -> None:
        error = _compact_error(exc)
        self.rest_audit_errors.append(
            (
                f"method={method.upper()} path={path} "
                f"clOrdID={client_order_id or '-'} orderID={endpoint_order_id or '-'} "
                f"retry={retry} error={error}"
            )
        )
        _LOGGER.warning(
            "rest call audit persistence failed method=%s path=%s clOrdID=%s orderID=%s retry=%s error=%s",
            method.upper(),
            path,
            client_order_id or "-",
            endpoint_order_id or "-",
            retry,
            error,
        )

    def _prune_rest_audit_if_due(self, session: Session) -> None:
        now_monotonic = time.monotonic()
        if (
            now_monotonic - self._last_rest_audit_prune_monotonic
            < self.rest_audit_maintenance_seconds
        ):
            return
        self._last_rest_audit_prune_monotonic = now_monotonic
        prune_exchange_rest_calls(
            session,
            exchange="binance",
            environment=self.environment,
            market_type=self.market_type,
            account_scope=self.account_scope,
            retention_minutes=self.rest_audit_retention_minutes,
            retention_limit=self.rest_audit_retention_limit,
            now=datetime.now(timezone.utc),
        )
        session.commit()

    def instrument_rules(self, symbol: str | None = None) -> dict[str, object]:
        """Return compact symbol filters needed by preflight and rounding."""

        target_symbol = symbol or self.symbol
        if target_symbol in self._rules_cache:
            return self._rules_cache[target_symbol]
        payload = self._request(
            "GET",
            self.exchange_info_path,
            params={"symbol": target_symbol},
            auth=False,
        )
        symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
        for item in symbols:
            if not isinstance(item, dict) or item.get("symbol") != target_symbol:
                continue
            filters = {
                str(entry.get("filterType")): entry
                for entry in item.get("filters", [])
                if isinstance(entry, dict)
            }
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            market_lot_filter = filters.get("MARKET_LOT_SIZE", {})
            min_notional_filter = filters.get("MIN_NOTIONAL", {})
            notional_filter = filters.get("NOTIONAL", {})
            rules: dict[str, object] = {
                "symbol": target_symbol,
                "status": item.get("status"),
                "tickSize": _optional_float(price_filter.get("tickSize")),
                "minPrice": _optional_float(price_filter.get("minPrice")),
                "stepSize": _optional_float(
                    lot_filter.get("stepSize")
                    or market_lot_filter.get("stepSize")
                ),
                "minQuantity": _optional_float(
                    lot_filter.get("minQty")
                    or market_lot_filter.get("minQty")
                ),
                "minNotional": _optional_float(
                    min_notional_filter.get("notional")
                    or min_notional_filter.get("minNotional")
                    or notional_filter.get("minNotional")
                ),
                "quantityPrecision": item.get("quantityPrecision"),
                "pricePrecision": item.get("pricePrecision"),
            }
            self._rules_cache[target_symbol] = rules
            return rules
        raise ValueError(f"Binance {self.market_type} symbol not found: {target_symbol}")

    def list_instruments(self) -> list[Dict[str, Any]]:
        """Return Binance symbols from the selected market exchange-info endpoint."""

        payload = self._request(
            "GET",
            self.exchange_info_path,
            auth=False,
        )
        symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
        rows: list[Dict[str, Any]] = []
        for item in symbols:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "")
            if not symbol:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "product_id": symbol,
                    "type": self.market_type,
                    "status": item.get("status"),
                    "tradeable": str(item.get("status") or "").upper() == "TRADING",
                    **item,
                }
            )
        return rows

    def validate_symbol(self, symbol: str | None = None) -> Dict[str, Any]:
        """Validate a Binance symbol and return operator-facing metadata."""

        target_symbol = symbol or self.symbol
        rules = self.instrument_rules(target_symbol)
        return {
            "symbol": target_symbol,
            "product_id": target_symbol,
            "type": self.market_type,
            "tradeable": str(rules.get("status") or "").upper() == "TRADING",
            **rules,
        }

    def validate_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Round quantity/price fields against Binance symbol filters."""

        rules = self.instrument_rules(str(order.get("symbol") or self.symbol))
        step = _optional_float(rules.get("stepSize"))
        tick = _optional_float(rules.get("tickSize"))
        min_qty = _optional_float(rules.get("minQuantity")) or 0.0
        normalised = dict(order)
        if step and "quantity" in normalised:
            quantity = _round_decimal(normalised["quantity"], step, ROUND_DOWN)
            if quantity < min_qty:
                raise ValueError(
                    f"Binance order quantity {quantity:g} is below minimum {min_qty:g} "
                    f"for {normalised.get('symbol') or self.symbol}"
                )
            normalised["quantity"] = _format_decimal(quantity)
        for key in ("price", "stopPrice"):
            if tick and key in normalised:
                normalised[key] = _format_decimal(
                    _round_decimal(normalised[key], tick, ROUND_HALF_UP)
                )
        return normalised

    def place_order(
        self,
        side: str,
        orderQty: OrderQty | float,
        price: Price | float | None = None,
        stopPx: StopPrice | float | None = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        exec_inst = str(params.pop("execInst", "") or "")
        reduce_only = _truthy(params.pop("reduceOnly", False)) or _has_exec_flag(
            exec_inst,
            "ReduceOnly",
        )
        binance_type = _map_order_type(type_, self.market_type)
        if reduce_only and not self.supports_reduce_only:
            raise ValueError(
                f"Binance {self.market_type} orders do not support ReduceOnly"
            )
        working_type = _working_type_from_exec_inst(exec_inst)
        if working_type is not None and not self.supports_working_type:
            raise ValueError(
                f"Binance {self.market_type} stop triggers do not support MarkPrice/LastPrice"
            )
        binance_type, time_in_force = _apply_post_only(
            exec_inst,
            binance_type,
            self.market_type,
        )
        client_order_id = str(params.pop("clOrdID", "") or _generated_client_order_id())
        request = {
            "symbol": self.symbol,
            "side": side.upper(),
            "type": binance_type,
            "quantity": float(orderQty),
            "newClientOrderId": client_order_id,
        }
        if reduce_only and self.supports_reduce_only:
            request["reduceOnly"] = "true"
        if working_type is not None and self.supports_working_type:
            request["workingType"] = working_type
        if binance_type in {"LIMIT", "LIMIT_MAKER"}:
            if price is None:
                raise ValueError("Binance LIMIT order requires price")
            request["price"] = float(price)
            if binance_type == "LIMIT":
                request["timeInForce"] = time_in_force or "GTC"
        elif binance_type == "MARKET":
            pass
        elif binance_type in {"STOP_MARKET", "STOP_LOSS"}:
            if stopPx is None:
                raise ValueError(f"Binance {binance_type} order requires stopPx")
            request["stopPrice"] = float(stopPx)
        elif binance_type == "STOP_LOSS_LIMIT":
            if stopPx is None:
                raise ValueError("Binance STOP_LOSS_LIMIT order requires stopPx")
            if price is None:
                raise ValueError("Binance STOP_LOSS_LIMIT order requires price")
            request["stopPrice"] = float(stopPx)
            request["price"] = float(price)
            request["timeInForce"] = time_in_force or "GTC"
        else:
            raise ValueError(f"Unsupported Binance order type: {binance_type}")
        normalised = self.validate_order(
            self._with_route_params(request, include_side_effect=True)
        )
        payload = self._request(
            "POST",
            self.order_path,
            params=normalised,
            auth=True,
        )
        ack = _ack_from_payload(payload)
        self._cache_order(
            ack,
            BinanceOrderRequest(
                side=side.upper(),
                quantity=float(normalised["quantity"]),
                kolabi_type=str(type_),
                binance_type=binance_type,
                price=_optional_float(normalised.get("price")),
                stop_price=_optional_float(normalised.get("stopPrice")),
                client_order_id=client_order_id,
                reduce_only=reduce_only,
                working_type=working_type,
                time_in_force=optional_str(normalised.get("timeInForce")),
            ),
        )
        return ack

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        cached = self._lookup_cached_order(order_id, params.get("clOrdID"))
        if cached is None:
            raise ValueError(
                "Binance amend requires a cached original order from this process; "
                f"order_id={order_id}"
            )
        if not self.supports_native_limit_amend:
            return self._amend_by_cancel_replace(order_id, cached, params)
        if cached.binance_type == "LIMIT":
            quantity = float(params.get("orderQty") or cached.quantity)
            price = params.get("price")
            if price is None:
                price = params.get("stopPx")
            if price is None:
                raise ValueError("Binance LIMIT amend requires price")
            request = self.validate_order(
                {
                    "symbol": self.symbol,
                    "side": cached.side,
                    "quantity": quantity,
                    "price": float(price),
                    _order_id_param_name(order_id): order_id,
                }
            )
            payload = self._request(
                "PUT",
                self.order_path,
                params=request,
                auth=True,
            )
            ack = _ack_from_payload(payload)
            self._cache_order(
                ack,
                BinanceOrderRequest(
                    side=cached.side,
                    quantity=float(request["quantity"]),
                    kolabi_type=cached.kolabi_type,
                    binance_type=cached.binance_type,
                    price=_optional_float(request.get("price")),
                    stop_price=cached.stop_price,
                    client_order_id=ack_client_id(ack, cached.client_order_id),
                    reduce_only=cached.reduce_only,
                    working_type=cached.working_type,
                    time_in_force=cached.time_in_force,
                ),
            )
            return ack
        # Binance modify only supports LIMIT orders. Stop-market tails are
        # therefore replaced atomically from the runtime point of view:
        # cancel old trigger, then submit the new trigger with the same client id.
        self.cancel_order(order_id)
        return self.place_order(
            side=cached.side,
            orderQty=float(params.get("orderQty") or cached.quantity),
            price=cached.price,
            stopPx=params.get("stopPx") or cached.stop_price,
            type_=cached.kolabi_type,
            clOrdID=params.get("clOrdID") or cached.client_order_id,
            reduceOnly=cached.reduce_only,
            execInst=_exec_inst_from_cached(cached),
        )

    def _amend_by_cancel_replace(
        self,
        order_id: str,
        cached: BinanceOrderRequest,
        params: dict[str, Any],
    ) -> OrderAck:
        self.cancel_order(order_id)
        return self.place_order(
            side=cached.side,
            orderQty=float(params.get("orderQty") or cached.quantity),
            price=params.get("price", cached.price),
            stopPx=params.get("stopPx", cached.stop_price),
            type_=cached.kolabi_type,
            clOrdID=str(
                params.get("newClOrdID")
                or params.get("replaceClOrdID")
                or _replacement_client_order_id(cached.client_order_id)
            ),
            reduceOnly=cached.reduce_only,
            execInst=_exec_inst_from_cached(cached),
        )

    def cancel_order(self, order_id: str) -> OrderAck:
        request = {
            "symbol": self.symbol,
            _order_id_param_name(order_id): order_id,
        }
        payload = self._request(
            "DELETE",
            self.order_path,
            params=self._with_route_params(request, include_auto_repay=True),
            auth=True,
        )
        ack = _ack_from_payload(payload)
        self._forget_order(order_id)
        self._forget_order(ack_client_id(ack, ""))
        return ack

    def live_open_orders(self) -> list[Dict[str, Any]]:
        payload = self._request(
            "GET",
            self.open_orders_path,
            params=self._with_route_params({"symbol": self.symbol}),
            auth=True,
        )
        rows = payload if isinstance(payload, list) else []
        return [
            _normalize_live_order(cast(Dict[str, Any], row))
            for row in rows
            if isinstance(row, dict) and not _is_trigger_order(row)
        ]

    def live_trigger_orders(self) -> list[Dict[str, Any]]:
        payload = self._request(
            "GET",
            self.open_orders_path,
            params=self._with_route_params({"symbol": self.symbol}),
            auth=True,
        )
        rows = payload if isinstance(payload, list) else []
        return [
            _normalize_live_order(cast(Dict[str, Any], row))
            for row in rows
            if isinstance(row, dict) and _is_trigger_order(row)
        ]

    def live_trigger_orders_db(self) -> list[Dict[str, Any]]:
        return []

    def open_orders(self) -> list[Dict[str, Any]]:
        return [*_legacy_from_normalized(self.live_open_orders()), *_legacy_from_normalized(self.live_trigger_orders())]

    def instrument(self, symbol: str) -> Dict[str, object]:
        rules = self.instrument_rules(symbol)
        ticker = self._request(
            "GET",
            self.book_ticker_path,
            params={"symbol": symbol},
            auth=False,
        )
        premium = {}
        if self.premium_index_path:
            premium = self._request(
                "GET",
                self.premium_index_path,
                params={"symbol": symbol},
                auth=False,
            )
        mark_price = (
            _optional_float(premium.get("markPrice"))
            if isinstance(premium, dict)
            else None
        )
        last_price = mark_price
        if not self.premium_index_path and isinstance(ticker, dict):
            last_price = _optional_float(ticker.get("lastPrice"))
        return {
            "symbol": symbol,
            "tickSize": rules.get("tickSize"),
            "minQuantity": rules.get("minQuantity"),
            "minNotional": rules.get("minNotional"),
            "stepSize": rules.get("stepSize"),
            "bidPrice": _optional_float(ticker.get("bidPrice")) if isinstance(ticker, dict) else None,
            "askPrice": _optional_float(ticker.get("askPrice")) if isinstance(ticker, dict) else None,
            "markPrice": mark_price,
            "lastPrice": last_price,
            "indicativeSettlePrice": _optional_float(premium.get("indexPrice")) if isinstance(premium, dict) else None,
        }

    def get_position(self) -> Position:
        if not self.position_path:
            payload = self._request(
                "GET",
                self.balance_path,
                params=self._balance_params(),
                auth=True,
            )
            return Position(
                symbol=self.symbol,
                qty=_spot_position_qty(
                    payload,
                    self.symbol,
                    market_type=self.market_type,
                    is_isolated=self.is_isolated,
                ),
                entry_price=None,
            )
        payload = self._request(
            "GET",
            self.position_path,
            params=self._with_route_params({"symbol": self.symbol}),
            auth=True,
        )
        rows = payload if isinstance(payload, list) else [payload]
        for item in rows:
            if not isinstance(item, dict) or item.get("symbol") != self.symbol:
                continue
            return Position(
                symbol=self.symbol,
                qty=float(item.get("positionAmt") or 0.0),
                entry_price=_optional_float(item.get("entryPrice")),
            )
        return Position(symbol=self.symbol, qty=0.0, entry_price=None)

    def get_balance(self) -> float:
        payload = self._request("GET", self.balance_path, params=self._balance_params(), auth=True)
        if isinstance(payload, dict):
            if "balances" in payload:
                return _balance_from_asset_rows(payload.get("balances"), self.symbol)
            if "userAssets" in payload:
                return _margin_balance_from_assets(payload.get("userAssets"), self.symbol)
            if "assets" in payload:
                return _isolated_margin_balance_from_assets(payload.get("assets"), self.symbol)
        rows = payload if isinstance(payload, list) else []
        for item in rows:
            if not isinstance(item, dict):
                continue
            if str(item.get("asset") or "").upper() == "USDT":
                return float(item.get("availableBalance") or item.get("balance") or 0.0)
        return 0.0

    def permission_status(self) -> dict[str, object]:
        """Validate order-write permission without creating an order when possible."""

        if self.test_order_path is None:
            return {
                "exchange": "binance",
                "market_type": self.market_type,
                "symbol": self.symbol,
                "permission_probe": "not_supported",
                "can_place_orders": None,
                "reason": "adapter does not expose a no-order permission probe",
            }
        try:
            request = self._permission_test_order()
            self._request(
                "POST",
                self.test_order_path,
                params=request,
                auth=True,
            )
        except Exception as exc:
            return {
                "exchange": "binance",
                "market_type": self.market_type,
                "symbol": self.symbol,
                "permission_probe": "test_order",
                "test_order_path": self.test_order_path,
                "can_place_orders": False,
                "reason": "test_order_failed",
                "error": _compact_error(exc),
            }
        return {
            "exchange": "binance",
            "market_type": self.market_type,
            "symbol": self.symbol,
            "permission_probe": "test_order",
            "test_order_path": self.test_order_path,
            "can_place_orders": True,
            "reason": "ok",
        }

    def _permission_test_order(self) -> Dict[str, Any]:
        rules = self.instrument_rules(self.symbol)
        reference_price = self._permission_reference_price(rules)
        quantity = _permission_test_quantity(rules, reference_price)
        request = {
            "symbol": self.symbol,
            "side": "BUY",
            "type": "LIMIT",
            "quantity": quantity,
            "price": reference_price,
            "timeInForce": "GTC",
            "newClientOrderId": _generated_client_order_id(),
        }
        return self.validate_order(
            self._with_route_params(request, include_side_effect=False)
        )

    def _permission_reference_price(self, rules: dict[str, object]) -> float:
        ticker = self._request(
            "GET",
            self.book_ticker_path,
            params={"symbol": self.symbol},
            auth=False,
        )
        if isinstance(ticker, dict):
            for key in ("bidPrice", "askPrice", "lastPrice"):
                value = _optional_float(ticker.get(key))
                if value and value > 0:
                    return value
        min_price = _optional_float(rules.get("minPrice")) or 1.0
        tick = _optional_float(rules.get("tickSize")) or 1.0
        return max(min_price, tick)

    def _with_route_params(
        self,
        params: Dict[str, Any],
        *,
        include_side_effect: bool = False,
        include_auto_repay: bool = False,
    ) -> Dict[str, Any]:
        payload = dict(params)
        if not self.uses_margin_order_params:
            return payload
        if self.is_isolated:
            payload["isIsolated"] = "TRUE"
        if include_side_effect and self.side_effect_type:
            payload["sideEffectType"] = self.side_effect_type
        if include_auto_repay and self.auto_repay_at_cancel is not None:
            payload["autoRepayAtCancel"] = (
                "TRUE" if self.auto_repay_at_cancel else "FALSE"
            )
        return payload

    def _balance_params(self) -> Dict[str, Any]:
        if self.uses_margin_order_params and self.is_isolated:
            return {"symbols": self.symbol}
        return {}

    def _lookup_cached_order(
        self,
        order_id: str,
        client_order_id: object | None = None,
    ) -> BinanceOrderRequest | None:
        if order_id in self._orders_by_order_id:
            return self._orders_by_order_id[order_id]
        if order_id in self._orders_by_client_id:
            return self._orders_by_client_id[order_id]
        if client_order_id is not None and str(client_order_id) in self._orders_by_client_id:
            return self._orders_by_client_id[str(client_order_id)]
        return None

    def _cache_order(self, ack: OrderAck, request: BinanceOrderRequest) -> None:
        if ack.order_id:
            self._orders_by_order_id[str(ack.order_id)] = request
        self._orders_by_client_id[request.client_order_id] = request

    def _forget_order(self, identity: str) -> None:
        cached = self._orders_by_order_id.pop(identity, None)
        if cached is not None:
            self._orders_by_client_id.pop(cached.client_order_id, None)
            return
        self._orders_by_client_id.pop(identity, None)


def _map_order_type(value: object, market_type: str) -> str:
    normalized = str(value or "").replace("_", "").replace("-", "").lower()
    if normalized in {"m", "market"}:
        return "MARKET"
    if normalized in {"l", "limit"}:
        return "LIMIT"
    if normalized in {"s", "stp", "stop", "stopmarket", "stoploss"}:
        return "STOP_MARKET" if market_type == "futures" else "STOP_LOSS"
    if normalized in {"sl", "stoplimit", "stoplosslimit"}:
        if market_type == "futures":
            raise ValueError(
                f"Unsupported Binance Futures order type '{value}'. "
                "Supported v1 types are M, L, and S/STOP_MARKET."
            )
        return "STOP_LOSS_LIMIT"
    if normalized in {"marketiftouched", "limitiftouched", "mt", "lt"}:
        raise ValueError(
            f"Unsupported Binance {market_type} order type '{value}'. "
            "Supported v1 types are M, L, S, and spot/margin SL."
        )
    raise ValueError(f"Unsupported Binance {market_type} order type '{value}'")


def _working_type_from_exec_inst(exec_inst: str) -> str | None:
    if _has_exec_flag(exec_inst, "IndexPrice"):
        raise ValueError("Binance USD-M Futures stop triggers do not support IndexPrice")
    if _has_exec_flag(exec_inst, "MarkPrice"):
        return "MARK_PRICE"
    if _has_exec_flag(exec_inst, "LastPrice"):
        return "CONTRACT_PRICE"
    return None


def _apply_post_only(
    exec_inst: str,
    order_type: str,
    market_type: str,
) -> tuple[str, str | None]:
    if _has_exec_flag(exec_inst, "ParticipateDoNotInitiate"):
        if order_type != "LIMIT":
            raise ValueError("Binance post-only/GTX is only valid for LIMIT orders")
        if market_type == "futures":
            return order_type, "GTX"
        return "LIMIT_MAKER", None
    return order_type, None


def _exec_inst_from_cached(cached: BinanceOrderRequest) -> str:
    flags: list[str] = []
    if cached.reduce_only:
        flags.append("ReduceOnly")
    if cached.working_type == "MARK_PRICE":
        flags.append("MarkPrice")
    elif cached.working_type == "CONTRACT_PRICE":
        flags.append("LastPrice")
    if cached.time_in_force == "GTX" or cached.binance_type == "LIMIT_MAKER":
        flags.append("ParticipateDoNotInitiate")
    return ",".join(flags)


def _ack_from_payload(payload: Any) -> OrderAck:
    order = _extract_order_like(payload if isinstance(payload, dict) else {})
    return OrderAck(
        order_id=str(order.get("orderId") or ""),
        status=str(order.get("status") or ""),
        price=_optional_float(order.get("price") or order.get("stopPrice")),
        orig_qty=_optional_float(order.get("origQty")),
        executed_qty=_optional_float(order.get("executedQty") or order.get("cumQty")),
        side=optional_str(order.get("side")),
        client_order_id=optional_str(order.get("clientOrderId")),
    )


def ack_client_id(ack: OrderAck, default: str) -> str:
    return ack.client_order_id or default


def _normalize_live_order(order: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "order_id": str(order.get("orderId") or ""),
        "client_order_id": str(order.get("clientOrderId") or ""),
        "symbol": str(order.get("symbol") or ""),
        "side": str(order.get("side") or "").lower(),
        "order_type": str(order.get("type") or order.get("origType") or ""),
        "qty": _optional_float(order.get("origQty")),
        "filled": _optional_float(order.get("executedQty") or order.get("cumQty")),
        "price": _optional_float(order.get("price")),
        "stop_price": _optional_float(order.get("stopPrice")),
        "trigger_signal": str(order.get("workingType") or ""),
        "reduce_only": _truthy(order.get("reduceOnly")),
        "status": str(order.get("status") or ""),
    }


def _legacy_from_normalized(orders: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [
        {
            "orderID": order.get("order_id", ""),
            "clOrdID": order.get("client_order_id", ""),
            "ordStatus": order.get("status", ""),
            "price": order.get("price") or order.get("stop_price"),
            "orderQty": order.get("qty"),
            "cumQty": order.get("filled"),
            "side": str(order.get("side") or "").capitalize(),
        }
        for order in orders
    ]


def _is_trigger_order(order: Dict[str, Any]) -> bool:
    order_type = str(order.get("type") or order.get("origType") or "").upper()
    if order_type in {
        "STOP",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
        "STOP_MARKET",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    }:
        return True
    return _optional_float(order.get("stopPrice")) not in (None, 0.0)


def _order_id_param_name(value: object) -> str:
    text = str(value)
    return "orderId" if text.isdigit() else "origClientOrderId"


def _effective_retry_attempts(
    *,
    method: str,
    path: str,
    payload: Dict[str, Any],
    configured: int,
) -> int:
    if method.upper() != "POST" or not path.endswith("/order"):
        return max(1, configured)
    if str(payload.get("newClientOrderId") or "").strip():
        return max(1, configured)
    return 1


def _should_persist_rest_call(*, method: str, path: str) -> bool:
    return path.endswith("/order") and method.upper() in {"POST", "PUT", "DELETE"}


def _sign_params(params: Dict[str, Any], api_secret: str) -> str:
    encoded = urlencode(_clean_params(params), doseq=True)
    return hmac.new(api_secret.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()


def _clean_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {str(key): value for key, value in params.items() if value not in (None, "")}


def _redact_signature(params: Dict[str, Any]) -> Dict[str, Any]:
    redacted = dict(params)
    if "signature" in redacted:
        redacted["signature"] = "<redacted>"
    return cast(Dict[str, Any], _json_safe_value(redacted))


def _json_safe_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return str(value)


def _extract_order_like(payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("order", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    return None


def optional_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _round_decimal(value: object, step: float, rounding: str) -> float:
    number = Decimal(str(value))
    quantum = Decimal(str(step))
    return float((number / quantum).to_integral_value(rounding=rounding) * quantum)


def _round_up_decimal(value: object, step: float) -> float:
    number = Decimal(str(value))
    quantum = Decimal(str(step))
    return float((number / quantum).to_integral_value(rounding=ROUND_UP) * quantum)


def _permission_test_quantity(rules: dict[str, object], reference_price: float) -> float:
    quantity = _optional_float(rules.get("minQuantity")) or 1.0
    min_notional = _optional_float(rules.get("minNotional"))
    if min_notional and reference_price > 0:
        quantity = max(quantity, min_notional / reference_price)
    step = _optional_float(rules.get("stepSize"))
    if step and step > 0:
        quantity = max(step, _round_up_decimal(quantity, step))
    return quantity


def _format_decimal(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def _has_exec_flag(exec_inst: str, flag: str) -> bool:
    return flag.lower() in (exec_inst or "").lower()


def _generated_client_order_id() -> str:
    return f"b-{uuid4().hex[:30]}"


def _replacement_client_order_id(previous: str) -> str:
    prefix = (previous or "b")[:24].rstrip("-")
    return f"{prefix}-r{uuid4().hex[:9]}"[:36]


_QUOTE_ASSETS = (
    "USDT",
    "USDC",
    "BUSD",
    "FDUSD",
    "TUSD",
    "BTC",
    "ETH",
    "BNB",
    "USD",
)


def _normalise_symbol_text(symbol: str) -> str:
    return symbol.replace("/", "").replace("_", "").replace("-", "").upper()


def _quote_asset_from_symbol(symbol: str) -> str:
    upper = _normalise_symbol_text(symbol)
    for quote in _QUOTE_ASSETS:
        if upper.endswith(quote):
            return quote
    return "USDT"


def _base_asset_from_symbol(symbol: str) -> str:
    upper = _normalise_symbol_text(symbol)
    quote = _quote_asset_from_symbol(upper)
    if quote and upper.endswith(quote):
        return upper[: -len(quote)]
    return upper


def _balance_from_asset_rows(rows: object, symbol: str) -> float:
    quote = _quote_asset_from_symbol(symbol)
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("asset") or "").upper() == quote:
            return float(item.get("free") or item.get("availableBalance") or 0.0)
    return 0.0


def _margin_balance_from_assets(rows: object, symbol: str) -> float:
    quote = _quote_asset_from_symbol(symbol)
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("asset") or "").upper() == quote:
            return float(item.get("free") or item.get("netAsset") or 0.0)
    return 0.0


def _isolated_margin_balance_from_assets(rows: object, symbol: str) -> float:
    quote = _quote_asset_from_symbol(symbol)
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        quote_asset = item.get("quoteAsset")
        if (
            isinstance(quote_asset, dict)
            and str(quote_asset.get("asset") or "").upper() == quote
        ):
            return float(quote_asset.get("free") or quote_asset.get("netAsset") or 0.0)
    return 0.0


def _spot_position_qty(
    payload: object,
    symbol: str,
    *,
    market_type: str,
    is_isolated: bool,
) -> float:
    if not isinstance(payload, dict):
        return 0.0
    if market_type == "spot":
        return _spot_base_quantity_from_balances(payload.get("balances"), symbol)
    if is_isolated:
        return _isolated_margin_base_quantity(payload.get("assets"), symbol)
    return _margin_base_quantity_from_assets(payload.get("userAssets"), symbol)


def _spot_base_quantity_from_balances(rows: object, symbol: str) -> float:
    base = _base_asset_from_symbol(symbol)
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("asset") or "").upper() != base:
            continue
        return _numeric_sum(item.get("free"), item.get("locked"))
    return 0.0


def _margin_base_quantity_from_assets(rows: object, symbol: str) -> float:
    base = _base_asset_from_symbol(symbol)
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("asset") or "").upper() != base:
            continue
        return _first_numeric(item.get("netAsset"), item.get("free"))
    return 0.0


def _isolated_margin_base_quantity(rows: object, symbol: str) -> float:
    base = _base_asset_from_symbol(symbol)
    normalised_symbol = _normalise_symbol_text(symbol)
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        item_symbol = str(item.get("symbol") or "").upper()
        if item_symbol and _normalise_symbol_text(item_symbol) != normalised_symbol:
            continue
        base_asset = item.get("baseAsset")
        if not isinstance(base_asset, dict):
            continue
        if str(base_asset.get("asset") or "").upper() != base:
            continue
        return _first_numeric(base_asset.get("netAsset"), base_asset.get("free"))
    return 0.0


def _first_numeric(*values: object) -> float:
    for value in values:
        if value not in (None, ""):
            return float(value)
    return 0.0


def _numeric_sum(*values: object) -> float:
    return sum(float(value) for value in values if value not in (None, ""))


def _compact_error(exc: BaseException) -> str:
    message = str(exc).strip().replace("\n", " ")
    if len(message) <= 280:
        return message
    return f"{message[:277]}..."


BinanceFuturesAdapter = BinanceAdapter


class BinanceSpotAdapter(BinanceAdapter):
    """REST adapter for Binance Spot."""

    market_type = "spot"
    order_path = "/api/v3/order"
    test_order_path = "/api/v3/order/test"
    open_orders_path = "/api/v3/openOrders"
    exchange_info_path = "/api/v3/exchangeInfo"
    book_ticker_path = "/api/v3/ticker/bookTicker"
    premium_index_path = ""
    position_path = ""
    balance_path = "/api/v3/account"
    supports_reduce_only = False
    supports_working_type = False
    supports_native_limit_amend = False


class BinanceMarginAdapter(BinanceSpotAdapter):
    """REST adapter for Binance cross and isolated margin."""

    market_type = "margin"
    order_path = "/sapi/v1/margin/order"
    test_order_path = None
    open_orders_path = "/sapi/v1/margin/openOrders"
    balance_path = "/sapi/v1/margin/account"
    uses_margin_order_params = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        is_isolated = _truthy(kwargs.get("is_isolated", False))
        if is_isolated:
            kwargs.setdefault("market_type", "isolated_margin")
            self.balance_path = "/sapi/v1/margin/isolated/account"
        else:
            kwargs.setdefault("market_type", "margin")
        kwargs.setdefault("side_effect_type", "NO_SIDE_EFFECT")
        super().__init__(*args, **kwargs)


Adapter = BinanceAdapter
