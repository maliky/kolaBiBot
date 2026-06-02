"""Binance USD-M Futures adapter.

This adapter deliberately talks to the `/fapi` Futures API directly instead of
using the legacy spot `python-binance` client. The bot passes platform-specific
symbols in TSV files, so no symbol mapping is attempted here.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, Sequence, cast
from urllib.parse import urlencode
from uuid import uuid4

import requests
from sqlalchemy.exc import OperationalError
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

_REST_AUDIT_SQLITE_BUSY_TIMEOUT_SECONDS = 0.5
_REST_AUDIT_LOCK_RETRIES = 3
_REST_AUDIT_LOCK_SLEEP_SECONDS = 0.15
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
        rest_audit_retention_minutes: int = 1440,
        rest_audit_retention_limit: int = 10000,
        rest_audit_maintenance_seconds: float = _REST_AUDIT_MAINTENANCE_SECONDS,
        **_unused: Any,
    ) -> None:
        super().__init__(api_key, api_secret, base_url.rstrip("/"), symbol)
        self.environment = environment
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
        self._audit_engine = create_persistence_engine(
            self.audit_db_url,
            sqlite_busy_timeout_seconds=_REST_AUDIT_SQLITE_BUSY_TIMEOUT_SECONDS,
        )
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
            for retry in range(_REST_AUDIT_LOCK_RETRIES + 1):
                try:
                    session.add(
                        ExchangeRestCall(
                            local_uuid=str(uuid4()),
                            exchange="binance",
                            environment=self.environment,
                            market_type="futures",
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
                except OperationalError as exc:
                    session.rollback()
                    if _is_sqlite_locked_error(exc) and retry < _REST_AUDIT_LOCK_RETRIES:
                        time.sleep(_REST_AUDIT_LOCK_SLEEP_SECONDS * (retry + 1))
                        continue
                    self._record_rest_audit_failure(
                        method=method,
                        path=path,
                        client_order_id=client_order_id,
                        endpoint_order_id=endpoint_order_id,
                        retry=retry,
                        exc=exc,
                    )
                    return
                except Exception as exc:
                    session.rollback()
                    self._record_rest_audit_failure(
                        method=method,
                        path=path,
                        client_order_id=client_order_id,
                        endpoint_order_id=endpoint_order_id,
                        retry=retry,
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
            market_type="futures",
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
            "/fapi/v1/exchangeInfo",
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
                "quantityPrecision": item.get("quantityPrecision"),
                "pricePrecision": item.get("pricePrecision"),
            }
            self._rules_cache[target_symbol] = rules
            return rules
        raise ValueError(f"Binance Futures symbol not found: {target_symbol}")

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
        binance_type = _map_order_type(type_)
        working_type = _working_type_from_exec_inst(exec_inst)
        time_in_force = _time_in_force_from_exec_inst(exec_inst, binance_type)
        client_order_id = str(params.pop("clOrdID", "") or _generated_client_order_id())
        request = {
            "symbol": self.symbol,
            "side": side.upper(),
            "type": binance_type,
            "quantity": float(orderQty),
            "newClientOrderId": client_order_id,
        }
        if reduce_only:
            request["reduceOnly"] = "true"
        if working_type is not None:
            request["workingType"] = working_type
        if binance_type == "LIMIT":
            if price is None:
                raise ValueError("Binance LIMIT order requires price")
            request["price"] = float(price)
            request["timeInForce"] = time_in_force or "GTC"
        elif binance_type == "MARKET":
            pass
        elif binance_type == "STOP_MARKET":
            if stopPx is None:
                raise ValueError("Binance STOP_MARKET order requires stopPx")
            request["stopPrice"] = float(stopPx)
        else:
            raise ValueError(f"Unsupported Binance order type: {binance_type}")
        normalised = self.validate_order(request)
        payload = self._request(
            "POST",
            "/fapi/v1/order",
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
                "/fapi/v1/order",
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

    def cancel_order(self, order_id: str) -> OrderAck:
        request = {
            "symbol": self.symbol,
            _order_id_param_name(order_id): order_id,
        }
        payload = self._request(
            "DELETE",
            "/fapi/v1/order",
            params=request,
            auth=True,
        )
        ack = _ack_from_payload(payload)
        self._forget_order(order_id)
        self._forget_order(ack_client_id(ack, ""))
        return ack

    def live_open_orders(self) -> list[Dict[str, Any]]:
        payload = self._request(
            "GET",
            "/fapi/v1/openOrders",
            params={"symbol": self.symbol},
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
            "/fapi/v1/openOrders",
            params={"symbol": self.symbol},
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
            "/fapi/v1/ticker/bookTicker",
            params={"symbol": symbol},
            auth=False,
        )
        premium = self._request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            auth=False,
        )
        return {
            "symbol": symbol,
            "tickSize": rules.get("tickSize"),
            "minQuantity": rules.get("minQuantity"),
            "bidPrice": _optional_float(ticker.get("bidPrice")) if isinstance(ticker, dict) else None,
            "askPrice": _optional_float(ticker.get("askPrice")) if isinstance(ticker, dict) else None,
            "markPrice": _optional_float(premium.get("markPrice")) if isinstance(premium, dict) else None,
            "lastPrice": _optional_float(premium.get("markPrice")) if isinstance(premium, dict) else None,
            "indicativeSettlePrice": _optional_float(premium.get("indexPrice")) if isinstance(premium, dict) else None,
        }

    def get_position(self) -> Position:
        payload = self._request(
            "GET",
            "/fapi/v2/positionRisk",
            params={"symbol": self.symbol},
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
        payload = self._request("GET", "/fapi/v2/balance", auth=True)
        rows = payload if isinstance(payload, list) else []
        for item in rows:
            if not isinstance(item, dict):
                continue
            if str(item.get("asset") or "").upper() == "USDT":
                return float(item.get("availableBalance") or item.get("balance") or 0.0)
        return 0.0

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


def _map_order_type(value: object) -> str:
    normalized = str(value or "").replace("_", "").replace("-", "").lower()
    if normalized in {"m", "market"}:
        return "MARKET"
    if normalized in {"l", "limit"}:
        return "LIMIT"
    if normalized in {"s", "stp", "stop", "stopmarket", "stoploss"}:
        return "STOP_MARKET"
    if normalized in {
        "sl",
        "stoplimit",
        "marketiftouched",
        "limitiftouched",
        "mt",
        "lt",
    }:
        raise ValueError(
            f"Unsupported Binance Futures order type '{value}'. "
            "Supported v1 types are M, L, and S/STOP_MARKET."
        )
    raise ValueError(f"Unsupported Binance Futures order type '{value}'")


def _working_type_from_exec_inst(exec_inst: str) -> str | None:
    if _has_exec_flag(exec_inst, "IndexPrice"):
        raise ValueError("Binance USD-M Futures stop triggers do not support IndexPrice")
    if _has_exec_flag(exec_inst, "MarkPrice"):
        return "MARK_PRICE"
    if _has_exec_flag(exec_inst, "LastPrice"):
        return "CONTRACT_PRICE"
    return None


def _time_in_force_from_exec_inst(exec_inst: str, order_type: str) -> str | None:
    if _has_exec_flag(exec_inst, "ParticipateDoNotInitiate"):
        if order_type != "LIMIT":
            raise ValueError("Binance post-only/GTX is only valid for LIMIT orders")
        return "GTX"
    return None


def _exec_inst_from_cached(cached: BinanceOrderRequest) -> str:
    flags: list[str] = []
    if cached.reduce_only:
        flags.append("ReduceOnly")
    if cached.working_type == "MARK_PRICE":
        flags.append("MarkPrice")
    elif cached.working_type == "CONTRACT_PRICE":
        flags.append("LastPrice")
    if cached.time_in_force == "GTX":
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
    )


def ack_client_id(ack: OrderAck, default: str) -> str:
    # OrderAck does not carry client ids. Keep the caller-provided identity.
    return default


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
    if method.upper() != "POST" or path != "/fapi/v1/order":
        return max(1, configured)
    if str(payload.get("newClientOrderId") or "").strip():
        return max(1, configured)
    return 1


def _should_persist_rest_call(*, method: str, path: str) -> bool:
    return path == "/fapi/v1/order" and method.upper() in {"POST", "PUT", "DELETE"}


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


def _is_sqlite_locked_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        if "database is locked" in message or "database table is locked" in message:
            return True
        nested = getattr(current, "orig", None)
        current = nested if isinstance(nested, BaseException) else current.__cause__
    return False


def _compact_error(exc: BaseException) -> str:
    message = str(exc).strip().replace("\n", " ")
    if len(message) <= 280:
        return message
    return f"{message[:277]}..."


Adapter = BinanceAdapter
