"""Kraken Futures exchange adapter backed by REST and optional private DB state."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence, cast
from urllib.parse import urlencode

import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kolabi.kraken_contract import build_send_order_contract
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.core.runtime_types import OrderQty, Price, StopPrice
from kolabi.shared.core.types import ExchangeABC
from kolabi.shared.kraken_futures import kraken_futures_environment
from kolabi.shared.persistence import (
    Base,
    ExchangeFill,
    ExchangeInstrument,
    ExchangeOrder,
)
from kolabi.tree.account import sign_rest_auth


@dataclass(frozen=True)
class _Ticker:
    bid: float
    ask: float
    mark_price: float
    index_price: float
    last: float


class KrakenFuturesAdapter(ExchangeABC):
    """Adapter exposing a BitMEX-like surface to the legacy runtime."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        symbol: str,
        *,
        environment: str = "demo",
        account_db_url: str | None = None,
        public_db_url: str | None = None,
        timeout: float = 10.0,
        postOnly: bool = False,
        session: requests.Session | None = None,
        **_ignored: Any,
    ) -> None:
        super().__init__(api_key, api_secret, base_url, symbol)
        self.environment = environment
        self.timeout = timeout
        self.post_only = postOnly
        self.session = session or requests.Session()
        self.rest_url = base_url.rstrip("/") + "/derivatives/api/v3"
        env_cfg = kraken_futures_environment(environment)
        self.account_db_url = account_db_url or env_cfg.private_db_url
        self.public_db_url = public_db_url or env_cfg.public_db_url
        self.dummy = False
        self.dummyID = ""
        self._engine = create_engine(self.account_db_url)
        self._public_engine = create_engine(self.public_db_url)
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            class_=Session,
        )
        self._public_sessionmaker = sessionmaker(
            bind=self._public_engine,
            expire_on_commit=False,
            class_=Session,
        )
        Base.metadata.create_all(self._public_engine)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _first(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, list):
            return payload[0] if payload else {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _clean_params(params: Sequence[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """Garde l'ordre des champs tout en supprimant les valeurs vides."""
        return [
            (key, value)
            for key, value in params
            if value is not None and value != ""
        ]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Sequence[tuple[str, Any]] | Dict[str, Any] | None = None,
        auth: bool = False,
        retry_attempts: int = 3,
    ) -> Dict[str, Any]:
        url = f"{self.rest_url}{path}"
        raw_params = list(params.items()) if isinstance(params, dict) else list(params or [])
        payload = self._clean_params(raw_params)
        last_error: RuntimeError | None = None
        for attempt in range(1, retry_attempts + 1):
            headers: Dict[str, str] = {}
            if auth:
                nonce = str(int(time.time() * 1000))
                post_data = urlencode(payload)
                headers = {
                    "APIKey": self.api_key,
                    "Authent": sign_rest_auth(
                        post_data=post_data,
                        nonce=nonce,
                        endpoint_path=f"/api/v3{path}",
                        api_secret=self.api_secret,
                    ),
                    "Nonce": nonce,
                }
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=payload if method.upper() == "GET" else None,
                    data=payload if method.upper() != "GET" else None,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                error = RuntimeError(
                    f"Kraken transport error on {path}: {exc.__class__.__name__}: {exc}"
                )
                if attempt < retry_attempts:
                    time.sleep(0.5 * attempt)
                    last_error = error
                    continue
                raise error from exc
            try:
                data = response.json()
            except ValueError:
                data = {"raw_text": response.text}
            status_code = getattr(response, "status_code", 200)
            if status_code >= 400:
                error = RuntimeError(f"Kraken HTTP {status_code} on {path}: {data}")
                if status_code in {502, 503, 504} and attempt < retry_attempts:
                    time.sleep(0.5 * attempt)
                    last_error = error
                    continue
                raise error
            if not isinstance(data, dict):
                raise RuntimeError(f"Unexpected Kraken payload type: {type(data)!r}")
            if data.get("result") == "error":
                raise RuntimeError(str(data))
            return data
        assert last_error is not None
        raise last_error

    def _ticker(self) -> _Ticker:
        payload = self._request("GET", f"/tickers/{self.symbol}")
        ticker = self._first(payload.get("ticker") or payload.get("result") or payload)
        bid = float(ticker.get("bid", ticker.get("bidPrice", 0.0)) or 0.0)
        ask = float(ticker.get("ask", ticker.get("askPrice", 0.0)) or 0.0)
        mark = float(ticker.get("markPrice", ticker.get("mark_price", bid)) or bid)
        index_price = float(
            ticker.get("indexPrice", ticker.get("index_price", mark)) or mark
        )
        last = float(ticker.get("last", ticker.get("lastPrice", mark)) or mark)
        return _Ticker(bid=bid, ask=ask, mark_price=mark, index_price=index_price, last=last)

    def list_instruments(self) -> list[Dict[str, Any]]:
        """Retourne les instruments Futures exposes par Kraken."""
        payload = self._request("GET", "/instruments")
        instruments = payload.get("instruments")
        if isinstance(instruments, list):
            rows = [item for item in instruments if isinstance(item, dict)]
            self._sync_instrument_cache(rows)
            return rows
        return []

    def _sync_instrument_cache(self, instruments: Sequence[Dict[str, Any]]) -> None:
        """Persist instrument metadata to the public DB for local consultation."""
        now = datetime.now(timezone.utc)
        with self._public_sessionmaker() as session:
            for instrument in instruments:
                symbol = str(instrument.get("symbol") or instrument.get("product_id") or "")
                if not symbol:
                    continue
                row = session.execute(
                    select(ExchangeInstrument).where(
                        ExchangeInstrument.exchange == "kraken",
                        ExchangeInstrument.environment == self.environment,
                        ExchangeInstrument.market_type == "futures",
                        ExchangeInstrument.symbol == symbol,
                    )
                ).scalars().first()
                payload = dict(instrument)
                min_quantity = _extract_min_quantity_from_instrument(payload)
                tick_size = _optional_float(payload.get("tickSize"))
                contract_size = _optional_float(payload.get("contractSize"))
                if row is None:
                    row = ExchangeInstrument(
                        exchange="kraken",
                        environment=self.environment,
                        market_type="futures",
                        symbol=symbol,
                        instrument_type=str(payload.get("type") or payload.get("tag") or ""),
                        tradeable=bool(payload.get("tradeable", True)),
                        tick_size=tick_size,
                        contract_size=contract_size,
                        min_quantity=min_quantity,
                        raw_payload=payload,
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    row.instrument_type = str(payload.get("type") or payload.get("tag") or "")
                    row.tradeable = bool(payload.get("tradeable", True))
                    row.tick_size = tick_size
                    row.contract_size = contract_size
                    row.min_quantity = min_quantity
                    row.raw_payload = payload
                    row.updated_at = now
            session.commit()

    def instrument_rules(self, symbol: str | None = None) -> Dict[str, Any]:
        """Return cached local instrument rules, syncing from Kraken if needed."""
        target_symbol = symbol or self.symbol
        with self._public_sessionmaker() as session:
            row = session.execute(
                select(ExchangeInstrument).where(
                    ExchangeInstrument.exchange == "kraken",
                    ExchangeInstrument.environment == self.environment,
                    ExchangeInstrument.market_type == "futures",
                    ExchangeInstrument.symbol == target_symbol,
                )
            ).scalars().first()
            if row is not None:
                return {
                    "symbol": row.symbol,
                    "tradeable": row.tradeable,
                    "tickSize": row.tick_size,
                    "contractSize": row.contract_size,
                    "minQuantity": row.min_quantity,
                    "type": row.instrument_type,
                    **dict(row.raw_payload or {}),
                }
        instrument = self.validate_symbol(target_symbol)
        return {
            "symbol": str(instrument.get("symbol") or target_symbol),
            "tradeable": bool(instrument.get("tradeable", True)),
            "tickSize": _optional_float(instrument.get("tickSize")),
            "contractSize": _optional_float(instrument.get("contractSize")),
            "minQuantity": _extract_min_quantity_from_instrument(instrument),
            "type": instrument.get("type") or instrument.get("tag"),
            **dict(instrument),
        }

    def minimum_order_quantity(self, symbol: str | None = None) -> float:
        """Return the locally cached minimum order quantity for the instrument."""
        rules = self.instrument_rules(symbol)
        return float(rules.get("minQuantity") or 1.0)

    def validate_symbol(self, symbol: str | None = None) -> Dict[str, Any]:
        """Valide un product id localement contre la liste publique Kraken."""
        target_symbol = symbol or self.symbol
        candidates = self.list_instruments()
        for instrument in candidates:
            if str(instrument.get("symbol") or instrument.get("product_id") or "") == target_symbol:
                return instrument
        hint = ""
        if target_symbol.startswith("PF_"):
            hint = f" Did you mean '{target_symbol.replace('PF_', 'PI_', 1)}'?"
        raise ValueError(f"Unknown Kraken Futures symbol '{target_symbol}'.{hint}")

    def _market_like_limit_price(self, side: str) -> float:
        ticker = self._ticker()
        if side.lower() == "buy":
            return ticker.ask * 1.01
        return ticker.bid * 0.99

    @staticmethod
    def _legacy_ack_from_order(order: Dict[str, Any], *, exec_type: str = "New") -> Dict[str, Any]:
        quantity = float(order.get("qty", order.get("size", order.get("quantity", 0.0))) or 0.0)
        filled = float(order.get("filled", order.get("filled_quantity", 0.0)) or 0.0)
        price = order.get("limit_price", order.get("price", order.get("stop_price")))
        side = "buy" if str(order.get("direction", order.get("side", "buy"))) in {"0", "buy"} else "sell"
        status = _map_order_status_from_payload(order)
        return {
            "orderID": str(order.get("order_id", order.get("orderId", ""))),
            "clOrdID": str(order.get("cli_ord_id", order.get("cliOrdId", ""))),
            "ordStatus": status,
            "execType": exec_type,
            "price": _optional_float(price),
            "orderQty": quantity,
            "cumQty": filled,
            "side": side.capitalize(),
            "transactTime": _parse_ms(order.get("last_update_time") or order.get("time")),
        }

    def _read_orders_from_db(self) -> list[ExchangeOrder]:
        with self._sessionmaker() as session:
            stmt = (
                select(ExchangeOrder)
                .where(
                    ExchangeOrder.exchange == "kraken",
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == "futures",
                    ExchangeOrder.symbol == self.symbol,
                )
                .order_by(ExchangeOrder.local_timestamp.desc(), ExchangeOrder.id.desc())
            )
            return list(session.execute(stmt).scalars())

    def _read_fills_from_db(self) -> list[ExchangeFill]:
        with self._sessionmaker() as session:
            stmt = (
                select(ExchangeFill)
                .join(ExchangeOrder)
                .where(
                    ExchangeOrder.exchange == "kraken",
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == "futures",
                    ExchangeOrder.symbol == self.symbol,
                )
                .order_by(ExchangeFill.local_timestamp.desc(), ExchangeFill.id.desc())
            )
            return list(session.execute(stmt).scalars())

    def place(
        self,
        orderQty: float,
        *,
        side: str,
        asBulk: bool = False,
        ordType: str = "Limit",
        price: float | None = None,
        stopPx: float | None = None,
        execInst: str = "",
        clOrdID: str | None = None,
        text: str | None = None,
        **_opts: Any,
    ) -> Dict[str, Any]:
        del asBulk
        fallback_market_price = (
            self._market_like_limit_price(side)
            if ordType.replace("_", "").strip().lower() == "market"
            else None
        )
        contract = build_send_order_contract(
            ord_type=ordType,
            symbol=self.symbol,
            side=side,
            size=orderQty,
            price=price,
            stop_price=stopPx,
            fallback_market_price=fallback_market_price,
            cli_ord_id=clOrdID,
            reduce_only=_has_exec_flag(execInst, "ReduceOnly"),
            post_only=self.post_only
            or _has_exec_flag(execInst, "ParticipateDoNotInitiate"),
            trigger_signal=_map_trigger_signal(execInst),
            trailing_stop_max_deviation=_opts.get("trailingStopMaxDeviation"),
            trailing_stop_deviation_unit=_opts.get("trailingStopDeviationUnit"),
            tag=text,
        )
        payload = self._request(
            "POST",
            "/sendorder",
            params=contract.as_params(),
            auth=True,
        )
        order = _merge_order_payload_defaults(
            _extract_order_like(payload),
            side=side,
            size=orderQty,
            price=price if price is not None else fallback_market_price,
            stop_price=stopPx,
            reduce_only=_has_exec_flag(execInst, "ReduceOnly"),
            cli_ord_id=clOrdID,
        )
        return self._legacy_ack_from_order(order)

    def amend(self, order: Dict[str, Any] | str, **params: Any) -> Dict[str, Any]:
        order_id = _extract_order_id(order)
        payload = [
            ("order_id", order_id),
            ("orderId", order_id),
            ("limitPrice", params.get("price")),
            ("stopPrice", params.get("stopPx")),
            ("size", params.get("orderQty")),
            ("cliOrdId", params.get("clOrdID")),
        ]
        response = self._request("POST", "/editorder", params=payload, auth=True)
        order_payload = _extract_order_like(response)
        return self._legacy_ack_from_order(order_payload, exec_type="Replaced")

    def cancel(self, order: str | Sequence[str]) -> Dict[str, Any] | list[Dict[str, Any]]:
        if isinstance(order, (list, tuple)):
            replies: list[Dict[str, Any]] = []
            for item in order:
                if not isinstance(item, str):
                    continue
                reply = self.cancel(item)
                if isinstance(reply, dict):
                    replies.append(reply)
            return replies
        payload = self._request(
            "POST",
            "/cancelorder",
            params=[("order_id", order)],
            auth=True,
        )
        ack = _extract_order_like(payload)
        if not ack:
            ack = {"order_id": order, "cli_ord_id": order, "reason": "cancelled_by_user"}
        return self._legacy_ack_from_order(ack, exec_type="Canceled")

    def http_open_orders(self) -> list[Dict[str, Any]]:
        payload = self._request("GET", "/openorders", auth=True)
        return list(payload.get("openOrders", []))

    def live_open_orders(self) -> list[Dict[str, Any]]:
        """Retourne les ordres limites/resting depuis la vue REST fraiche."""
        return [
            _normalize_live_order(order)
            for order in self.http_open_orders()
            if _matches_symbol(order, self.symbol) and not _is_trigger_order(order)
        ]

    def live_trigger_orders(self) -> list[Dict[str, Any]]:
        """Retourne les ordres conditionnels/trigger depuis la vue REST fraiche."""
        return [
            _normalize_live_order(order)
            for order in self.http_open_orders()
            if _matches_symbol(order, self.symbol) and _is_trigger_order(order)
        ]

    def open_orders(self) -> list[Dict[str, Any]]:
        rows = self._read_orders_from_db()
        if rows:
            open_statuses = {"new", "open", "partiallyfilled", "partial_fill"}
            return [
                _db_order_to_legacy(row)
                for row in rows
                if row.status.replace(" ", "").replace("_", "").lower() in open_statuses
            ]
        return [
            self._legacy_ack_from_order(order)
            for order in self.http_open_orders()
            if _matches_symbol(order, self.symbol) and not _is_trigger_order(order)
        ]

    def exec_orders(self) -> list[Dict[str, Any]]:
        rows = self._read_orders_from_db()
        fills = self._read_fills_from_db()
        if rows:
            return build_exec_orders(rows, fills)
        return []

    def position(self, symbol: str | None = None) -> Dict[str, Any]:
        target_symbol = symbol or self.symbol
        payload = self._request("GET", "/openpositions", auth=True)
        positions = payload.get("openPositions", [])
        for position in positions:
            if str(position.get("symbol") or position.get("instrument")) == target_symbol:
                current_qty = float(
                    position.get("size", position.get("balance", position.get("qty", 0.0)))
                )
                side_hint = str(position.get("side") or position.get("direction") or "").lower()
                if current_qty > 0 and side_hint in {"short", "sell", "-1"}:
                    current_qty = -current_qty
                return {
                    "symbol": target_symbol,
                    "currentQty": current_qty,
                    "avgEntryPrice": _optional_float(
                        position.get("entry_price") or position.get("price")
                    ),
                    "leverage": _optional_float(position.get("leverage")),
                    "liquidationPrice": _optional_float(position.get("liquidation_price")),
                }
        return {
            "symbol": target_symbol,
            "currentQty": 0.0,
            "avgEntryPrice": None,
            "leverage": 1.0,
            "liquidationPrice": None,
        }

    def margin(self) -> Dict[str, Any]:
        payload = self._request("GET", "/accounts", auth=True)
        available = _extract_available_margin(payload)
        return {"availableMargin": available}

    def instrument(self, symbol: str) -> Dict[str, Any]:
        metadata = self.instrument_rules(symbol)
        ticker = self._ticker() if symbol == self.symbol else _ticker_from_payload(
            self._request("GET", f"/tickers/{symbol}")
        )
        return {
            "symbol": symbol,
            "tickSize": metadata.get("tickSize"),
            "contractSize": metadata.get("contractSize"),
            "minQuantity": metadata.get("minQuantity"),
            "bidPrice": ticker.bid,
            "askPrice": ticker.ask,
            "markPrice": ticker.mark_price,
            "indicativeSettlePrice": ticker.index_price,
            "lastPrice": ticker.last,
        }

    def recent_trades(self) -> list[Dict[str, Any]]:
        payload = self._request("GET", "/history", params={"symbol": self.symbol})
        return list(payload.get("history", []))

    def place_order(
        self,
        side: str,
        orderQty: OrderQty | float,
        price: Price | float | None = None,
        stopPx: StopPrice | float | None = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        exec_inst = "ReduceOnly" if params.pop("reduceOnly", False) else ""
        response = self.place(
            orderQty=float(orderQty),
            side=side,
            ordType=type_,
            price=float(price) if price is not None else None,
            stopPx=float(stopPx) if stopPx is not None else None,
            execInst=exec_inst,
            **params,
        )
        return _ack_from_legacy(response)

    def amend_order(self, order_id: str, **params: float) -> OrderAck:
        response = self.amend({"orderID": order_id}, **params)
        return _ack_from_legacy(response)

    def cancel_order(self, order_id: str) -> OrderAck:
        response = self.cancel(order_id)
        if isinstance(response, list):
            response = response[0]
        return _ack_from_legacy(response)

    def get_position(self) -> Position:
        position = self.position(self.symbol)
        return Position(
            symbol=self.symbol,
            qty=float(position.get("currentQty", 0.0)),
            entry_price=_optional_float(position.get("avgEntryPrice")),
        )

    def get_balance(self) -> float:
        margin = self.margin()
        return float(margin.get("availableMargin", 0.0))


def _ack_from_legacy(payload: Dict[str, Any]) -> OrderAck:
    return OrderAck(
        order_id=str(payload.get("orderID", "")),
        status=str(payload.get("ordStatus", "")),
        price=_optional_float(payload.get("price")),
        orig_qty=_optional_float(payload.get("orderQty")),
        executed_qty=_optional_float(payload.get("cumQty")),
        side=payload.get("side"),
    )


def _extract_min_quantity_from_instrument(instrument: Dict[str, Any]) -> float:
    for key in (
        "minimumQuantity",
        "minOrderSize",
        "minimumOrderSize",
        "contractSize",
        "lotSize",
        "minQuantity",
    ):
        value = instrument.get(key)
        if value not in (None, ""):
            return max(float(cast(float | str, value)), 1.0)
    return 1.0


def _extract_order_like(payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("sendStatus", "editStatus", "cancelStatus", "order", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            if "order" in value and isinstance(value["order"], dict):
                return value["order"]
            return value
    return payload


def _merge_order_payload_defaults(
    payload: Dict[str, Any],
    *,
    side: str,
    size: float,
    price: float | None,
    stop_price: float | None,
    reduce_only: bool,
    cli_ord_id: str | None,
) -> Dict[str, Any]:
    merged = dict(payload)
    if not any(key in merged for key in ("qty", "size", "quantity", "orderQty")):
        merged["qty"] = size
    if not any(key in merged for key in ("direction", "side")):
        merged["side"] = side
    if cli_ord_id and not any(key in merged for key in ("cli_ord_id", "cliOrdId")):
        merged["cli_ord_id"] = cli_ord_id
    if price is not None and not any(key in merged for key in ("limit_price", "price")):
        merged["price"] = price
    if stop_price is not None and not any(
        key in merged for key in ("stop_price", "stopPrice")
    ):
        merged["stop_price"] = stop_price
    if reduce_only and "reduce_only" not in merged and "reduceOnly" not in merged:
        merged["reduce_only"] = True
    return merged


def _matches_symbol(order: Dict[str, Any], symbol: str) -> bool:
    return str(order.get("instrument") or order.get("symbol") or "") == symbol


def _is_trigger_order(order: Dict[str, Any]) -> bool:
    order_type = str(order.get("type") or order.get("orderType") or "").lower()
    if order_type in {
        "stop",
        "stop_loss",
        "stoploss",
        "stop_limit",
        "stoplosslimit",
        "take_profit",
        "takeprofit",
        "take_profit_limit",
        "takeprofitlimit",
        "trailing_stop",
        "trailingstop",
        "trailing_stop_limit",
        "trailingstoplimit",
        "market_if_touched",
        "limit_if_touched",
    }:
        return True
    for key in (
        "stop_price",
        "stopPrice",
        "triggerPrice",
        "trigger_price",
        "trailingStopMaxDeviation",
        "trailing_stop_max_deviation",
    ):
        value = order.get(key)
        if value not in (None, "", 0, 0.0):
            return True
    return False


def _normalize_live_order(order: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "order_id": str(order.get("order_id") or order.get("orderId") or ""),
        "client_order_id": str(order.get("cli_ord_id") or order.get("cliOrdId") or ""),
        "symbol": str(order.get("instrument") or order.get("symbol") or ""),
        "side": str(order.get("direction") or order.get("side") or ""),
        "order_type": str(order.get("type") or order.get("orderType") or ""),
        "qty": _optional_float(order.get("qty") or order.get("quantity")),
        "filled": _optional_float(order.get("filled") or order.get("filled_quantity")),
        "price": _optional_float(order.get("limit_price") or order.get("price")),
        "stop_price": _optional_float(order.get("stop_price") or order.get("stopPrice")),
        "status": _map_order_status_from_payload(order),
    }


def _extract_order_id(order: Dict[str, Any] | str) -> str:
    if isinstance(order, str):
        return order
    return str(order.get("orderID") or order.get("order_id") or order.get("clOrdID") or "")


def _map_trigger_signal(exec_inst: str) -> str | None:
    normalized = exec_inst.lower()
    if "markprice" in normalized:
        return "mark"
    if "lastprice" in normalized:
        return "last"
    if "indexprice" in normalized:
        return "spot"
    return None


def _has_exec_flag(exec_inst: str, flag: str) -> bool:
    return flag.lower() in (exec_inst or "").lower()


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"cannot convert {type(value)!r} to float")


def _first_float(payload: Dict[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            continue
        if value not in (None, ""):
            if isinstance(value, (int, float, str)):
                return float(value)
            continue
    return default


def _extract_available_margin(payload: Dict[str, Any]) -> float:
    accounts = payload.get("accounts")
    candidates: list[Dict[str, Any]] = []
    if isinstance(accounts, list):
        candidates.extend(item for item in accounts if isinstance(item, dict))
    elif isinstance(accounts, dict):
        candidates.extend(value for value in accounts.values() if isinstance(value, dict))
    if not candidates:
        candidates = [payload]
    for account in candidates:
        available = _first_float(
            account,
            "available",
            "availableMargin",
            "available_margin",
            "availableFunds",
            default=-1.0,
        )
        if available >= 0:
            return available
        auxiliary = account.get("auxiliary")
        if isinstance(auxiliary, dict):
            available = _first_float(
                auxiliary,
                "available",
                "availableMargin",
                "available_margin",
                "availableFunds",
                default=-1.0,
            )
            if available >= 0:
                return available
    return 0.0


def _map_order_status_from_payload(payload: Dict[str, Any]) -> str:
    reason = str(payload.get("reason", "") or "").lower()
    if payload.get("is_cancel") is True:
        if "fill" in reason:
            return "Filled"
        return "Canceled"
    if reason == "partial_fill":
        return "PartiallyFilled"
    if reason in {"new_placed_order_by_user", "", "new"}:
        return "New"
    if reason == "cancelled_by_user":
        return "Canceled"
    if "edit" in reason or "replace" in reason:
        return "New"
    return "New"


def _parse_ms(value: object) -> str:
    if value in (None, ""):
        return KrakenFuturesAdapter._now_iso()
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        return dt.isoformat()
    return str(value)


def _db_order_to_legacy(row: ExchangeOrder) -> Dict[str, Any]:
    return {
        "orderID": row.exchange_order_id or "",
        "clOrdID": row.client_order_id or "",
        "ordStatus": _db_status_to_legacy(row.status),
        "execType": _db_exec_type(row.status),
        "price": row.price,
        "orderQty": row.quantity,
        "cumQty": row.filled_quantity,
        "side": row.side.capitalize(),
        "transactTime": (
            row.source_timestamp or row.local_timestamp or datetime.now(timezone.utc)
        ).isoformat(),
    }


def _db_status_to_legacy(status: str) -> str:
    normalized = status.replace(" ", "").replace("_", "").lower()
    if normalized in {"filled", "fullfill"}:
        return "Filled"
    if normalized in {"canceled", "cancelled"}:
        return "Canceled"
    if normalized in {"partialfill", "partiallyfilled"}:
        return "PartiallyFilled"
    return "New"


def _db_exec_type(status: str) -> str:
    normalized = status.replace(" ", "").replace("_", "").lower()
    if normalized in {"filled", "fullfill", "partialfill", "partiallyfilled"}:
        return "Trade"
    if normalized in {"canceled", "cancelled"}:
        return "Canceled"
    if normalized in {"replaced", "amended", "edited"}:
        return "Replaced"
    return "New"


def build_exec_orders(
    orders: Iterable[ExchangeOrder],
    fills: Iterable[ExchangeFill],
) -> list[Dict[str, Any]]:
    by_order_id = {row.id: row for row in orders}
    rows = [_db_order_to_legacy(order) for order in orders]
    for fill in fills:
        order = by_order_id.get(fill.order_id)
        if order is None:
            continue
        rows.append(
            {
                "orderID": order.exchange_order_id or "",
                "clOrdID": order.client_order_id or "",
                "ordStatus": "Filled"
                if order.filled_quantity >= order.quantity
                else "PartiallyFilled",
                "execType": "Trade",
                "price": fill.price,
                "orderQty": order.quantity,
                "cumQty": order.filled_quantity,
                "side": order.side.capitalize(),
                "transactTime": (
                    fill.source_timestamp or fill.local_timestamp or datetime.now(timezone.utc)
                ).isoformat(),
            }
        )
    return rows


def _ticker_from_payload(payload: Dict[str, Any]) -> _Ticker:
    ticker = KrakenFuturesAdapter._first(payload.get("ticker") or payload.get("result") or payload)
    bid = float(ticker.get("bid", ticker.get("bidPrice", 0.0)) or 0.0)
    ask = float(ticker.get("ask", ticker.get("askPrice", 0.0)) or 0.0)
    mark = float(ticker.get("markPrice", ticker.get("mark_price", bid)) or bid)
    index_price = float(ticker.get("indexPrice", ticker.get("index_price", mark)) or mark)
    last = float(ticker.get("last", ticker.get("lastPrice", mark)) or mark)
    return _Ticker(bid=bid, ask=ask, mark_price=mark, index_price=index_price, last=last)


Adapter = KrakenFuturesAdapter
