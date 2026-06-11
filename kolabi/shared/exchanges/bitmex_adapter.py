"""BitMEX exchange adapter backed by the legacy client."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict
from uuid import uuid4

from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.core.runtime_types import OrderQty, Price, StopPrice
from kolabi.shared.core.types import ExchangeABC
from kolabi.shared.exchanges.bitmex_api.custom_api import BitMEX
from kolabi.shared.persistence import (
    Base,
    ExchangeRestCall,
    create_persistence_engine,
    prune_exchange_rest_calls,
)
from kolabi.shared.pruning import DEFAULT_PRUNING
from sqlalchemy.orm import Session, sessionmaker


_LOGGER = logging.getLogger("kola")
_KOLABI_CLIENT_ORDER_ID_RE = re.compile(
    r"^[HT][1-9][0-9]*[A-Za-z][A-Za-z0-9-]*-\d{12}$"
)


class BitmexAdapter(ExchangeABC):
    """Adapter that reuses the legacy BitMEX client."""

    _ORDER_TYPE_MAP = {
        "LIMIT": "Limit",
        "MARKET": "Market",
        "STOP": "Stop",
        "STOPLIMIT": "StopLimit",
        "MARKETIFTOUCHED": "MarketIfTouched",
        "LIMITIFTOUCHED": "LimitIfTouched",
        "MIT": "MarketIfTouched",
        "LIT": "LimitIfTouched",
    }

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        symbol: str,
        *,
        client_factory: Callable[..., Any] | None = None,
        market_type: str = "futures",
        **client_kwargs: Any,
    ) -> None:
        super().__init__(api_key, api_secret, base_url, symbol)
        self.market_type = market_type
        self.environment = str(client_kwargs.pop("environment", "demo") or "demo")
        self.audit_db_url = client_kwargs.pop("audit_db_url", None)
        self.account_scope = str(
            client_kwargs.pop("account_scope", "default") or "default"
        )
        self.rest_audit_retention_minutes = max(
            0,
            int(
                client_kwargs.pop(
                    "rest_audit_retention_minutes",
                    DEFAULT_PRUNING.rest_audit.retention_minutes,
                )
            ),
        )
        self.rest_audit_retention_limit = max(
            0,
            int(
                client_kwargs.pop(
                    "rest_audit_retention_limit",
                    DEFAULT_PRUNING.rest_audit.retention_limit,
                )
            ),
        )
        self.rest_audit_maintenance_seconds = max(
            1.0,
            float(
                client_kwargs.pop(
                    "rest_audit_maintenance_seconds",
                    DEFAULT_PRUNING.rest_audit.maintenance_seconds,
                )
            ),
        )
        self.rest_audit_errors: list[str] = []
        self._last_rest_audit_prune_monotonic = 0.0
        self._audit_engine = (
            create_persistence_engine(self.audit_db_url) if self.audit_db_url else None
        )
        self._audit_sessionmaker = (
            sessionmaker(
                bind=self._audit_engine,
                expire_on_commit=False,
                class_=Session,
            )
            if self._audit_engine is not None
            else None
        )
        if self._audit_engine is not None:
            Base.metadata.create_all(self._audit_engine)
        for ignored in (
            "public_db_url",
            "account_db_url",
            "critical_account_db_url",
        ):
            client_kwargs.pop(ignored, None)
        factory = client_factory or BitMEX
        kwargs = dict(client_kwargs)
        kwargs.setdefault("orderIDPrefix", "mlk_")
        self.order_id_prefix = str(kwargs.get("orderIDPrefix") or "")
        kwargs.setdefault("postOnly", False)
        kwargs.setdefault("timeout", 12)
        kwargs.setdefault("useWebsocket", False)
        try:
            self.client = factory(
                base_url=base_url,
                symbol=symbol,
                apiKey=api_key,
                apiSecret=api_secret,
                **kwargs,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to initialise BitMEX client: {exc}") from exc
        self._order_write_permission_checked = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @classmethod
    def _normalize_type(cls, type_: str) -> str:
        key = type_.replace("_", "").upper()
        return cls._ORDER_TYPE_MAP.get(key, type_)

    @staticmethod
    def _first(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, list) and payload:
            first = payload[0]
            return first if isinstance(first, dict) else {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _build_ack(data: Dict[str, Any]) -> OrderAck:
        return OrderAck(
            order_id=str(data.get("orderID", "")),
            status=data.get("ordStatus", ""),
            price=data.get("price"),
            orig_qty=data.get("orderQty"),
            executed_qty=data.get("cumQty"),
            side=data.get("side"),
            client_order_id=data.get("clOrdID"),
        )

    def _ensure_order_write_permission(self) -> None:
        """Fail before submitting when a BitMEX key cannot create orders."""

        if self._order_write_permission_checked:
            return
        curl = getattr(self.client, "_curl_bitmex", None)
        if not callable(curl):
            self._order_write_permission_checked = True
            return
        try:
            payload = curl(path="apiKey", verb="GET")
        except Exception as exc:
            raise RuntimeError(
                "Could not verify BitMEX order permission before placing order: "
                f"{_compact_error(exc)}"
            ) from exc
        rows = _bitmex_payload_rows(payload)
        candidates = _bitmex_api_key_candidates(rows, api_key=self.api_key)
        enabled_candidates = [
            row for row in candidates if _bitmex_api_key_enabled(row)
        ]
        if any(
            _has_bitmex_order_write_permission(row.get("permissions"))
            for row in enabled_candidates
        ):
            self._order_write_permission_checked = True
            return
        permissions = _bitmex_permissions_repr(candidates or rows)
        raise RuntimeError(
            "BitMEX API key cannot place orders; "
            f"permissions={permissions}; enable order/write permission on the demo key."
        )

    def permission_status(self) -> dict[str, object]:
        """Return no-order API key permission evidence for operator preflight."""

        curl = getattr(self.client, "_curl_bitmex", None)
        if not callable(curl):
            return {
                "exchange": "bitmex",
                "market_type": self.market_type,
                "symbol": self.symbol,
                "permission_probe": "unavailable",
                "can_place_orders": None,
                "reason": "client does not expose the apiKey endpoint",
            }
        try:
            payload = curl(path="apiKey", verb="GET")
        except Exception as exc:
            return {
                "exchange": "bitmex",
                "market_type": self.market_type,
                "symbol": self.symbol,
                "permission_probe": "apiKey",
                "can_place_orders": False,
                "reason": "permission_probe_failed",
                "error": _compact_error(exc),
            }
        rows = _bitmex_payload_rows(payload)
        candidates = _bitmex_api_key_candidates(rows, api_key=self.api_key)
        enabled_candidates = [
            row for row in candidates if _bitmex_api_key_enabled(row)
        ]
        permissions = _bitmex_permission_list(candidates or rows)
        can_place_orders = any(
            _has_bitmex_order_write_permission(row.get("permissions"))
            for row in enabled_candidates
        )
        reason = "ok"
        if not candidates:
            reason = "api_key_not_found"
        elif not enabled_candidates:
            reason = "api_key_disabled"
        elif not can_place_orders:
            reason = "missing_order_write_permission"
        return {
            "exchange": "bitmex",
            "market_type": self.market_type,
            "symbol": self.symbol,
            "permission_probe": "apiKey",
            "can_place_orders": can_place_orders,
            "api_key_enabled": bool(enabled_candidates),
            "permissions": permissions,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # ExchangeABC API
    # ------------------------------------------------------------------
    def place_order(
        self,
        side: str,
        orderQty: OrderQty | float,
        price: Price | float | None = None,
        stopPx: StopPrice | float | None = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        self._ensure_order_write_permission()
        ord_type = self._normalize_type(type_)
        reduce_only = _truthy(params.pop("reduceOnly", False))
        exec_inst = params.pop("execInst", None)
        if reduce_only:
            exec_inst = _merge_exec_inst_flag(exec_inst, "ReduceOnly")
        opts: Dict[str, Any] = {"ordType": ord_type}
        if exec_inst not in (None, ""):
            opts["execInst"] = exec_inst
        if self.market_type == "spot":
            _reject_spot_order_type(ord_type)
            if reduce_only:
                _reject_spot_exec_flags("ReduceOnly")
            _reject_spot_exec_flags(exec_inst)
        if price is not None:
            opts["price"] = float(price)
        if stopPx is not None:
            opts["stopPx"] = float(stopPx)
        opts.update(params)
        request = {
            "symbol": self.symbol,
            "side": _bitmex_side(side),
            "orderQty": float(orderQty),
            **opts,
        }
        try:
            response = self._submit_order_request(
                request,
                order_qty=float(orderQty),
                side=side.lower(),
                opts=opts,
            )
        except Exception as exc:
            self._record_rest_call(
                method="POST",
                path="/order",
                request_params=request,
                response_payload={},
                result_kind="transport_error",
                error_text=str(exc),
            )
            raise
        data = self._first(response)
        self._record_rest_call(
            method="POST",
            path="/order",
            request_params=request,
            response_payload=data,
            result_kind="ok",
            error_text=None,
        )
        return self._build_ack(data)

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        request = {"orderID": order_id, **params}
        try:
            response = self._amend_order_request(request)
        except Exception as exc:
            self._record_rest_call(
                method="PUT",
                path="/order",
                request_params=request,
                response_payload={},
                result_kind="transport_error",
                error_text=str(exc),
            )
            raise
        data = self._first(response)
        self._record_rest_call(
            method="PUT",
            path="/order",
            request_params=request,
            response_payload=data,
            result_kind="ok",
            error_text=None,
        )
        return self._build_ack(data)

    def _submit_order_request(
        self,
        request: Dict[str, Any],
        *,
        order_qty: float,
        side: str,
        opts: Dict[str, Any],
    ) -> Any:
        if self.market_type == "spot" and hasattr(self.client, "_curl_bitmex"):
            return self.client._curl_bitmex(
                path="order",
                postdict=request,
                verb="POST",
            )
        return self.client.place(
            order_qty,
            side=side,
            asBulk=False,
            **opts,
        )

    def _amend_order_request(self, request: Dict[str, Any]) -> Any:
        if hasattr(self.client, "_curl_bitmex"):
            return self.client._curl_bitmex(
                path="order",
                postdict=request,
                verb="PUT",
            )
        order_id = str(request["orderID"])
        params = {key: value for key, value in request.items() if key != "orderID"}
        return self.client.amend({"orderID": order_id}, **params)

    def cancel_order(self, order_id: str) -> OrderAck:
        request = self._cancel_request(order_id)
        try:
            response = self._cancel_order_request(request)
        except Exception as exc:
            self._record_rest_call(
                method="DELETE",
                path="/order",
                request_params=request,
                response_payload={},
                result_kind="transport_error",
                error_text=str(exc),
            )
            raise
        data = self._first(response)
        if isinstance(data, dict):
            data.setdefault("ordStatus", data.get("ordStatus", "Canceled"))
        self._record_rest_call(
            method="DELETE",
            path="/order",
            request_params=request,
            response_payload=data,
            result_kind="ok",
            error_text=None,
        )
        return self._build_ack(data)

    def _cancel_request(self, order_id: str) -> Dict[str, Any]:
        identity = str(order_id)
        if _looks_like_client_order_id(identity, prefix=self.order_id_prefix):
            return {"clOrdID": identity}
        return {"orderID": identity}

    def _cancel_order_request(self, request: Dict[str, Any]) -> Any:
        if hasattr(self.client, "_curl_bitmex"):
            return self.client._curl_bitmex(
                path="order",
                postdict=request,
                verb="DELETE",
            )
        if "clOrdID" in request:
            return self.client.cancel({"clIDList": [request["clOrdID"]]})
        return self.client.cancel(str(request["orderID"]))

    def _record_rest_call(
        self,
        *,
        method: str,
        path: str,
        request_params: Dict[str, Any],
        response_payload: Dict[str, Any],
        result_kind: str,
        error_text: str | None,
        attempt_count: int = 1,
        http_status: int | None = None,
    ) -> None:
        if self._audit_sessionmaker is None:
            return
        request_payload = _json_safe_dict(request_params)
        payload = _json_safe_dict(response_payload)
        exchange_order_id = _optional_str(
            payload.get("orderID") or request_payload.get("orderID")
        )
        client_order_id = _optional_str(
            payload.get("clOrdID")
            or request_payload.get("clOrdID")
            or request_payload.get("origClOrdID")
        )
        ack_status = _optional_str(payload.get("ordStatus"))
        if ack_status is None and result_kind != "ok":
            ack_status = "Rejected"
        with self._audit_sessionmaker() as session:
            try:
                session.add(
                    ExchangeRestCall(
                        local_uuid=str(uuid4()),
                        exchange="bitmex",
                        environment=self.environment,
                        market_type=self.market_type,
                        account_scope=self.account_scope,
                        symbol=self.symbol,
                        method=method.upper(),
                        path=path,
                        request_params=request_payload,
                        attempt_count=attempt_count,
                        http_status=http_status,
                        result_kind=result_kind,
                        response_payload=payload,
                        error_text=error_text,
                        client_order_id=client_order_id,
                        exchange_order_id=exchange_order_id,
                        endpoint_order_id=exchange_order_id or client_order_id,
                        correlation_id=client_order_id or exchange_order_id,
                        ack_status=ack_status,
                        ack_order_id=exchange_order_id,
                        ack_client_order_id=client_order_id,
                    )
                )
                session.commit()
                self._prune_rest_audit_if_due(session)
            except Exception as exc:
                session.rollback()
                self._record_rest_audit_failure(
                    method=method,
                    path=path,
                    client_order_id=client_order_id,
                    endpoint_order_id=exchange_order_id or client_order_id,
                    exc=exc,
                )

    def _record_rest_audit_failure(
        self,
        *,
        method: str,
        path: str,
        client_order_id: str | None,
        endpoint_order_id: str | None,
        exc: BaseException,
    ) -> None:
        error = _compact_error(exc)
        self.rest_audit_errors.append(
            (
                f"method={method.upper()} path={path} "
                f"clOrdID={client_order_id or '-'} orderID={endpoint_order_id or '-'} "
                f"error={error}"
            )
        )
        _LOGGER.warning(
            "rest call audit persistence failed method=%s path=%s clOrdID=%s orderID=%s error=%s",
            method.upper(),
            path,
            client_order_id or "-",
            endpoint_order_id or "-",
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
        try:
            prune_exchange_rest_calls(
                session,
                exchange="bitmex",
                environment=self.environment,
                market_type=self.market_type,
                account_scope=self.account_scope,
                retention_minutes=self.rest_audit_retention_minutes,
                retention_limit=self.rest_audit_retention_limit,
                now=datetime.now(timezone.utc),
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            _LOGGER.warning("rest call audit pruning skipped error=%s", _compact_error(exc))

    def instrument_rules(self, symbol: str | None = None) -> dict[str, object]:
        target_symbol = symbol or self.symbol
        instrument = self.client.instrument(target_symbol) or {}
        tick_size = _optional_float(instrument.get("tickSize"))
        min_quantity = (
            _optional_float(instrument.get("lotSize"))
            or _optional_float(instrument.get("minOrderQty"))
            or 1.0
        )
        return {
            "symbol": target_symbol,
            "status": instrument.get("state"),
            "tickSize": tick_size,
            "minQuantity": min_quantity,
            "bidPrice": _optional_float(instrument.get("bidPrice")),
            "askPrice": _optional_float(instrument.get("askPrice")),
            "markPrice": _optional_float(
                instrument.get("markPrice") or instrument.get("fairPrice")
            ),
            "indicativeSettlePrice": _optional_float(
                instrument.get("indicativeSettlePrice")
            ),
            "lastPrice": _optional_float(instrument.get("lastPrice")),
            "contractSize": _optional_float(
                instrument.get("contractSize") or instrument.get("multiplier")
            ),
        }

    def instrument(self, symbol: str) -> Dict[str, Any]:
        """Return compact BitMEX instrument metadata for operator tools."""

        return dict(self.instrument_rules(symbol))

    def list_instruments(self) -> list[Dict[str, Any]]:
        """Return BitMEX instruments when the legacy client exposes them."""

        method = getattr(self.client, "instruments", None)
        rows: object
        if callable(method):
            rows = method()
        else:
            rows = [self.client.instrument(self.symbol)]
        if isinstance(rows, dict):
            rows = [rows]
        instruments: list[Dict[str, Any]] = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "")
            if not symbol:
                continue
            state = item.get("state")
            instruments.append(
                {
                    "symbol": symbol,
                    "product_id": symbol,
                    "type": self.market_type,
                    "status": state,
                    "tradeable": str(state or "").lower() in {"open", "trading"},
                    **item,
                }
            )
        return instruments

    def validate_symbol(self, symbol: str | None = None) -> Dict[str, Any]:
        """Validate a BitMEX symbol and return operator-facing metadata."""

        target_symbol = symbol or self.symbol
        rules = self.instrument_rules(target_symbol)
        return {
            "symbol": target_symbol,
            "product_id": target_symbol,
            "type": self.market_type,
            "tradeable": str(rules.get("status") or "").lower() in {"open", "trading"},
            **rules,
        }

    def live_open_orders(self) -> list[Dict[str, Any]]:
        return [
            _normalize_live_order(order)
            for order in self._open_orders()
            if not _is_trigger_order(order)
        ]

    def live_trigger_orders(self) -> list[Dict[str, Any]]:
        return [
            _normalize_live_order(order)
            for order in self._open_orders()
            if _is_trigger_order(order)
        ]

    def live_trigger_orders_db(self) -> list[Dict[str, Any]]:
        return []

    def open_orders(self) -> list[Dict[str, Any]]:
        return [
            *_legacy_from_normalized(self.live_open_orders()),
            *_legacy_from_normalized(self.live_trigger_orders()),
        ]

    def _open_orders(self) -> list[Dict[str, Any]]:
        for method_name in ("open_orders", "http_open_orders"):
            method = getattr(self.client, method_name, None)
            if not callable(method):
                continue
            rows = method()
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    def get_position(self) -> Position:
        if self.market_type == "spot":
            payload = self._margin_payload(currency="all")
            return Position(
                symbol=self.symbol,
                qty=_bitmex_spot_balance_quantity(
                    payload,
                    asset=_bitmex_base_asset(self.symbol),
                    available=False,
                ),
                entry_price=None,
            )
        data = self.client.position(self.symbol) or {}
        qty = float(data.get("currentQty") or 0)
        entry = data.get("avgEntryPrice")
        entry_price = float(entry) if entry is not None else None
        return Position(symbol=self.symbol, qty=qty, entry_price=entry_price)

    def get_balance(self) -> float:
        if self.market_type == "spot":
            payload = self._margin_payload(currency="all")
            return _bitmex_spot_balance_quantity(
                payload,
                asset=_bitmex_quote_asset(self.symbol),
                available=True,
            )
        margin = self._margin_payload()
        return float(self._first(margin).get("availableMargin", 0))

    def _margin_payload(self, *, currency: str = "XBt") -> Any:
        if hasattr(self.client, "_curl_bitmex"):
            return self.client._curl_bitmex(
                path="user/margin",
                query={"currency": currency},
                verb="GET",
            )
        method = getattr(self.client, "margin")
        try:
            return method(currency)
        except TypeError:
            return method()


def _json_safe_dict(payload: Dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _json_safe_value(value)
        for key, value in payload.items()
        if value is not None
    }


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


def _optional_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def _compact_error(exc: BaseException) -> str:
    message = str(exc).strip().replace("\n", " ")
    if len(message) <= 280:
        return message
    return f"{message[:277]}..."


def _reject_spot_exec_flags(exec_inst: object) -> None:
    text = str(exec_inst or "").lower()
    blocked = (
        "indexprice",
        "reduceonly",
        "close",
        "lastwithinmark",
    )
    if any(flag in text for flag in blocked):
        raise ValueError(
            "BitMEX spot orders do not support IndexPrice, ReduceOnly, Close, "
            "or LastWithinMark execution instructions"
        )


def _reject_spot_order_type(ord_type: str) -> None:
    if ord_type not in {"Limit", "Market"}:
        raise ValueError(
            "BitMEX spot orders only support Limit and Market order types"
        )


def _bitmex_side(side: str) -> str:
    return "Buy" if side.lower() == "buy" else "Sell"


def _merge_exec_inst_flag(exec_inst: object, flag: str) -> str:
    parts = [
        part.strip()
        for part in str(exec_inst or "").split(",")
        if part.strip()
    ]
    if not any(part.lower() == flag.lower() for part in parts):
        parts.append(flag)
    return ",".join(parts)


def _looks_like_client_order_id(value: object, *, prefix: str = "") -> bool:
    text = str(value or "")
    return bool(_KOLABI_CLIENT_ORDER_ID_RE.match(text)) or bool(
        prefix and text.startswith(prefix)
    )


_BITMEX_ORDER_WRITE_PERMISSIONS = {
    "order",
    "orders",
    "ordercreate",
    "ordermanage",
    "orderwrite",
    "trade",
    "write",
}


def _bitmex_api_key_candidates(
    rows: list[Dict[str, Any]],
    *,
    api_key: str,
) -> list[Dict[str, Any]]:
    matching = [
        row
        for row in rows
        if str(row.get("id") or row.get("apiKey") or row.get("key") or "") == api_key
    ]
    if matching:
        return matching
    if len(rows) == 1:
        return rows
    enabled = [row for row in rows if _bitmex_api_key_enabled(row)]
    return enabled or rows


def _bitmex_api_key_enabled(row: Dict[str, Any]) -> bool:
    value = row.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "disabled"}
    return bool(value)


def _has_bitmex_order_write_permission(permissions: object) -> bool:
    return any(
        _normalise_bitmex_permission(permission) in _BITMEX_ORDER_WRITE_PERMISSIONS
        for permission in _bitmex_permission_tokens(permissions)
    )


def _bitmex_permissions_repr(rows: list[Dict[str, Any]]) -> str:
    return repr(_bitmex_permission_list(rows))


def _bitmex_permission_list(rows: list[Dict[str, Any]]) -> list[str]:
    permissions: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for permission in _bitmex_permission_tokens(row.get("permissions")):
            text = str(permission)
            key = _normalise_bitmex_permission(text)
            if key in seen:
                continue
            seen.add(key)
            permissions.append(text)
    return permissions


def _bitmex_permission_tokens(permissions: object) -> list[object]:
    if permissions in (None, ""):
        return []
    if isinstance(permissions, str):
        return [
            token
            for token in re.split(r"[\s,]+", permissions.strip())
            if token
        ]
    if isinstance(permissions, (list, tuple, set)):
        return list(permissions)
    return [permissions]


def _normalise_bitmex_permission(permission: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(permission).lower())


_BITMEX_QUOTE_ASSETS = ("USDT", "USDC", "USD", "XBT", "ETH", "BMEX")


def _bitmex_normalise_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace("_", "").replace("-", "").upper()


def _bitmex_quote_asset(symbol: str) -> str:
    compact = _bitmex_normalise_symbol(symbol)
    for quote in _BITMEX_QUOTE_ASSETS:
        if compact.endswith(quote):
            return quote
    return "USDT"


def _bitmex_base_asset(symbol: str) -> str:
    compact = _bitmex_normalise_symbol(symbol)
    quote = _bitmex_quote_asset(compact)
    if quote and compact.endswith(quote):
        return compact[: -len(quote)]
    return compact


def _bitmex_asset_key(value: object) -> str:
    return str(value or "").replace("_", "").replace("-", "").upper()


def _bitmex_spot_balance_quantity(
    payload: object,
    *,
    asset: str,
    available: bool,
) -> float:
    target = _bitmex_asset_key(asset)
    fields = (
        ("availableMargin", "availableBalance", "available")
        if available
        else ("marginBalance", "walletBalance", "total", "amount")
    )
    for row in _bitmex_payload_rows(payload):
        currency = _bitmex_asset_key(row.get("currency") or row.get("asset"))
        if currency != target:
            continue
        for field in fields:
            value = row.get(field)
            if value not in (None, ""):
                return float(value)
        return 0.0
    return 0.0


def _bitmex_payload_rows(payload: object) -> list[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _normalize_live_order(order: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "order_id": str(order.get("orderID") or ""),
        "client_order_id": str(order.get("clOrdID") or ""),
        "symbol": str(order.get("symbol") or ""),
        "side": str(order.get("side") or "").lower(),
        "order_type": str(order.get("ordType") or ""),
        "qty": _optional_float(order.get("orderQty")),
        "filled": _optional_float(order.get("cumQty")),
        "price": _optional_float(order.get("price")),
        "stop_price": _optional_float(order.get("stopPx")),
        "trigger_signal": str(order.get("execInst") or ""),
        "reduce_only": "reduceonly" in str(order.get("execInst") or "").lower(),
        "status": str(order.get("ordStatus") or ""),
    }


def _legacy_from_normalized(orders: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
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
    order_type = str(order.get("ordType") or "").lower()
    if any(token in order_type for token in ("stop", "touched")):
        return True
    return order.get("stopPx") not in (None, "", 0, 0.0)


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    return None


BitmexFuturesAdapter = BitmexAdapter
BitmexSpotAdapter = BitmexAdapter

# Alias expected by get_adapter
Adapter = BitmexAdapter
