from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, cast
from urllib.parse import urlencode

import requests
import websockets

from kolabi.shared.binance_futures import (
    binance_futures_critical_db_url,
    binance_futures_environment,
    binance_futures_private_db_url,
)
from kolabi.shared.logging import setup_logging
from kolabi.tree.account import (
    AccountStateStore,
    AccountStreamConfig,
    stream_kind_uses_critical_db,
)

JsonDictT = dict[str, Any]


@dataclass(frozen=True)
class BinanceAccountConfig:
    """Configuration for Binance USD-M Futures private DB ingestion."""

    db_url: str = "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
    critical_db_url: str = "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical"
    exchange: str = "binance"
    environment: str = "demo"
    market_type: str = "futures"
    account_scope: str = "default"
    ws_url: str = "wss://stream.binancefuture.com/ws"
    rest_url: str = "https://testnet.binancefuture.com"
    api_key_env: str = "BINANCE_FUTURES_DEMO_API_KEY"
    api_secret_env: str = "BINANCE_FUTURES_DEMO_API_SECRET"
    reconnect_seconds: int = 5
    heartbeat_log_seconds: int = 60
    listen_key_keepalive_seconds: int = 1800
    rest_reconcile_seconds: float = 10.0
    balance_write_min_interval_seconds: float = 300.0
    position_write_min_interval_seconds: float = 60.0
    log_level: str = "INFO"


class BinancePrivateStream:
    """Binance listen-key websocket translated into AccountStateStore messages."""

    def __init__(
        self,
        config: BinanceAccountConfig,
        account_store: AccountStateStore,
        critical_store: AccountStateStore,
        api_key: str,
        api_secret: str,
    ) -> None:
        self.config = config
        self.account_store = account_store
        self.critical_store = critical_store
        self.api_key = api_key
        self.api_secret = api_secret
        self.logger = setup_logging(config.log_level)
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self._running = True
        self._listen_key: str | None = None

    async def run(self) -> None:
        self.logger.info(
            "binance_account starting env=%s account_db=%s critical_db=%s ws=%s rest=%s",
            self.config.environment,
            self.config.db_url,
            self.config.critical_db_url,
            self.config.ws_url,
            self.config.rest_url,
        )
        reconciler = asyncio.create_task(self._reconcile_loop())
        try:
            while self._running:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._record_status("private_ws", "reconnecting", str(exc), critical=True)
                    self._record_status("private_ws_account", "reconnecting", str(exc))
                    self.logger.warning(
                        "binance_account reconnecting in %ss after error: %s",
                        self.config.reconnect_seconds,
                        exc,
                    )
                    await asyncio.sleep(self.config.reconnect_seconds)
        finally:
            reconciler.cancel()
            try:
                await reconciler
            except asyncio.CancelledError:
                pass

    async def run_once(self) -> None:
        listen_key = self._create_listen_key()
        self._listen_key = listen_key
        self._record_status("private_ws", "subscribed", critical=True)
        self._record_status("private_ws_critical", "subscribed", critical=True)
        self._record_status("private_ws_account", "subscribed")
        keepalive = asyncio.create_task(self._keepalive_loop())
        url = f"{self.config.ws_url.rstrip('/')}/{listen_key}"
        self.logger.info("binance_account subscribed env=%s ws=%s", self.config.environment, url)
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                last_heartbeat_log = time.monotonic()
                while self._running:
                    now = time.monotonic()
                    if now - last_heartbeat_log >= self.config.heartbeat_log_seconds:
                        self.logger.info(
                            "binance_account heartbeat env=%s db=%s stream=private_ws_critical",
                            self.config.environment,
                            self.config.critical_db_url,
                        )
                        self._record_status("private_ws", "healthy", critical=True)
                        self._record_status("private_ws_critical", "healthy", critical=True)
                        self._record_status("private_ws_account", "healthy")
                        last_heartbeat_log = now
                    try:
                        raw_message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    payload = json.loads(raw_message)
                    self.handle_message(payload)
        finally:
            keepalive.cancel()
            with suppress(asyncio.CancelledError):
                await keepalive

    def stop(self) -> None:
        self._running = False

    def handle_message(self, payload: Mapping[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        messages = normalise_binance_private_event(dict(payload))
        for message, critical in messages:
            store = self.critical_store if critical else self.account_store
            stream_kind = "private_ws_critical" if critical else "private_ws_account"
            result = store.ingest_message(
                message,
                stream_kind=stream_kind,
                is_critical=critical,
                received_at=now,
                prune_raw=False,
            )
            self._log_ingest_result(result)
        if any(critical for _message, critical in messages):
            self._record_status("private_ws", "healthy", critical=True)
            self._record_status("private_ws_critical", "healthy", critical=True)
        if any(not critical for _message, critical in messages):
            self._record_status("private_ws_account", "healthy")

    async def _keepalive_loop(self) -> None:
        while self._running:
            await asyncio.sleep(max(60, self.config.listen_key_keepalive_seconds))
            if self._listen_key:
                self._request("PUT", "/fapi/v1/listenKey", {"listenKey": self._listen_key})

    async def _reconcile_loop(self) -> None:
        if self.config.rest_reconcile_seconds <= 0:
            return
        while self._running:
            try:
                await asyncio.sleep(self.config.rest_reconcile_seconds)
                stats = self.reconcile_once()
                self.account_store.record_connection_status("rest_reconciler", "healthy")
                self.logger.info(
                    "binance_account rest_reconcile orders=%s positions=%s balances=%s interval=%.1fs",
                    stats["orders"],
                    stats["positions"],
                    stats["balances"],
                    self.config.rest_reconcile_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.account_store.record_connection_status(
                    "rest_reconciler",
                    "error",
                    last_error=str(exc),
                )
                self.logger.warning(
                    "binance_account rest_reconcile failed interval=%.1fs error=%s",
                    self.config.rest_reconcile_seconds,
                    exc,
                )

    def reconcile_once(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        orders = self._signed_request("GET", "/fapi/v1/openOrders")
        positions = self._signed_request("GET", "/fapi/v2/positionRisk")
        balances = self._signed_request("GET", "/fapi/v2/balance")
        order_message = {
            "feed": "open_orders_snapshot",
            "event": "rest_reconcile",
            "orders": [normalise_rest_order(item) for item in _list_payload(orders)],
        }
        position_message = {
            "feed": "open_positions_snapshot",
            "event": "rest_reconcile",
            "positions": [normalise_rest_position(item) for item in _list_payload(positions)],
        }
        balance_message = {
            "feed": "balances_snapshot",
            "event": "rest_reconcile",
            "flex_futures": {
                "currencies": {
                    str(item.get("asset")): {
                        "available_balance": item.get("availableBalance"),
                        "balance_value": item.get("balance"),
                    }
                    for item in _list_payload(balances)
                    if isinstance(item, dict) and item.get("asset")
                }
            },
        }
        self.critical_store.ingest_message(
            order_message,
            stream_kind="private_ws_critical",
            is_critical=True,
            received_at=now,
            prune_raw=False,
        )
        self.account_store.ingest_message(
            position_message,
            stream_kind="private_ws_account",
            is_critical=False,
            received_at=now,
            prune_raw=False,
        )
        self.account_store.ingest_message(
            balance_message,
            stream_kind="private_ws_account",
            is_critical=False,
            received_at=now,
            prune_raw=False,
        )
        return {
            "orders": len(_list_payload(orders)),
            "positions": len(_list_payload(positions)),
            "balances": len(_list_payload(balances)),
        }

    def _create_listen_key(self) -> str:
        payload = self._request("POST", "/fapi/v1/listenKey", {})
        listen_key = str(payload.get("listenKey") or "")
        if not listen_key:
            raise RuntimeError(f"Binance listenKey missing: {payload}")
        return listen_key

    def _request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        response = self.session.request(
            method,
            f"{self.config.rest_url.rstrip('/')}{path}",
            data=params if method.upper() in {"POST", "PUT"} else None,
            params=params if method.upper() not in {"POST", "PUT"} else None,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def _signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        payload.setdefault("recvWindow", 5000)
        payload["timestamp"] = int(time.time() * 1000)
        payload["signature"] = sign_payload(payload, self.api_secret)
        return self._request(method, path, payload)

    def _record_status(
        self,
        stream_kind: str,
        status: str,
        error: str | None = None,
        *,
        critical: bool = False,
    ) -> None:
        store = self.critical_store if critical else self.account_store
        store.record_connection_status(stream_kind, status, last_error=error)

    def _log_ingest_result(self, result: dict[str, object]) -> None:
        feed = str(result.get("feed") or "")
        if feed.startswith("open_orders"):
            for row in cast(Sequence[Any], result.get("orders", ())):
                self.logger.info(
                    "binance_account order_event feed=%s symbol=%s order_id=%s client_id=%s side=%s type=%s status=%s qty=%.8f filled=%.8f price=%s reduce_only=%s",
                    feed,
                    getattr(row, "symbol", "-"),
                    getattr(row, "exchange_order_id", "-"),
                    getattr(row, "client_order_id", "-"),
                    getattr(row, "side", "-"),
                    getattr(row, "order_type", "-"),
                    getattr(row, "status", "-"),
                    float(getattr(row, "quantity", 0.0) or 0.0),
                    float(getattr(row, "filled_quantity", 0.0) or 0.0),
                    getattr(row, "price", None),
                    bool(getattr(row, "reduce_only", False)),
                )
        elif feed.startswith("fills"):
            for event, _row in cast(Sequence[tuple[Any, Any]], result.get("fills", ())):
                self.logger.info(
                    "binance_account fill_event feed=%s symbol=%s order_id=%s fill_id=%s side=%s type=%s qty=%.8f price=%.8f",
                    feed,
                    event.symbol,
                    event.exchange_order_id,
                    event.exchange_fill_id,
                    event.side,
                    event.order_type,
                    event.quantity,
                    event.price,
                )


def normalise_binance_private_event(payload: JsonDictT) -> list[tuple[JsonDictT, bool]]:
    event_type = str(payload.get("e") or "")
    if event_type == "ORDER_TRADE_UPDATE":
        order = payload.get("o")
        if not isinstance(order, Mapping):
            return []
        order_message = {
            "feed": "open_orders",
            "event": event_type,
            "order": normalise_order_update(order),
        }
        messages: list[tuple[JsonDictT, bool]] = [(order_message, True)]
        if str(order.get("x") or "").upper() == "TRADE" or _float(order.get("l")) > 0:
            messages.append(
                (
                    {
                        "feed": "fills",
                        "event": event_type,
                        "fill": normalise_fill_update(order),
                    },
                    True,
                )
            )
        return messages
    if event_type == "ACCOUNT_UPDATE":
        account = payload.get("a")
        if not isinstance(account, Mapping):
            return []
        return [
            (
                {
                    "feed": "balances",
                    "event": event_type,
                    "flex_futures": {
                        "currencies": normalise_account_balances(account),
                    },
                },
                False,
            ),
            (
                {
                    "feed": "open_positions",
                    "event": event_type,
                    "positions": normalise_account_positions(account),
                },
                False,
            ),
        ]
    return [({"feed": "unknown", "event": event_type or "unknown", **payload}, False)]


def normalise_order_update(order: Mapping[str, Any]) -> JsonDictT:
    status = str(order.get("X") or "")
    return {
        "symbol": order.get("s"),
        "orderId": order.get("i"),
        "cliOrdId": order.get("c"),
        "side": order.get("S"),
        "type": order.get("o"),
        "status": normalise_order_status(status),
        "quantity": order.get("q"),
        "filled": order.get("z"),
        "price": binance_order_price(order),
        "stop_price": non_zero_or_none(order.get("sp")),
        "reduceOnly": order.get("R"),
        "lastUpdateTime": order.get("T"),
        "reason": str(order.get("x") or ""),
        "is_cancel": status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"},
    }


def normalise_fill_update(order: Mapping[str, Any]) -> JsonDictT:
    return {
        "symbol": order.get("s"),
        "orderId": order.get("i"),
        "cliOrdId": order.get("c"),
        "fillId": order.get("t"),
        "side": order.get("S"),
        "type": order.get("o"),
        "quantity": order.get("l") or order.get("z"),
        "price": first_non_zero(order.get("L"), order.get("ap")),
        "fee": order.get("n"),
        "feeCurrency": order.get("N"),
        "liquidity": order.get("m"),
        "time": order.get("T"),
    }


def normalise_rest_order(order: Mapping[str, Any]) -> JsonDictT:
    return {
        "symbol": order.get("symbol"),
        "orderId": order.get("orderId"),
        "cliOrdId": order.get("clientOrderId"),
        "side": order.get("side"),
        "type": order.get("type") or order.get("origType"),
        "status": normalise_order_status(str(order.get("status") or "")),
        "quantity": order.get("origQty"),
        "filled": order.get("executedQty"),
        "price": binance_order_price(order),
        "stop_price": non_zero_or_none(order.get("stopPrice")),
        "reduceOnly": order.get("reduceOnly"),
        "lastUpdateTime": first_non_empty(order.get("updateTime"), order.get("time")),
    }


def normalise_rest_position(position: Mapping[str, Any]) -> JsonDictT:
    size = _float(first_non_empty(position.get("positionAmt"), position.get("pa")))
    return {
        "symbol": first_non_empty(position.get("symbol"), position.get("s")),
        "side": "long" if size >= 0 else "short",
        "size": size,
        "entryPrice": first_non_empty(position.get("entryPrice"), position.get("ep")),
        "liquidationPrice": position.get("liquidationPrice"),
        "leverage": position.get("leverage"),
        "time": position.get("updateTime"),
    }


def normalise_account_balances(account: Mapping[str, Any]) -> dict[str, JsonDictT]:
    rows = account.get("B")
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("a")): {
            "available_balance": row.get("cw") or row.get("wb"),
            "balance_value": row.get("wb"),
        }
        for row in rows
        if isinstance(row, Mapping) and row.get("a")
    }


def normalise_account_positions(account: Mapping[str, Any]) -> list[JsonDictT]:
    rows = account.get("P")
    if not isinstance(rows, list):
        return []
    return [normalise_rest_position(row) for row in rows if isinstance(row, Mapping)]


def normalise_order_status(status: str) -> str:
    normalized = status.upper()
    if normalized in {"NEW"}:
        return "open"
    if normalized in {"PARTIALLY_FILLED"}:
        return "partial_fill"
    if normalized in {"FILLED"}:
        return "filled"
    if normalized in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        return "canceled"
    if normalized in {"REJECTED"}:
        return "rejected"
    return normalized.lower() or "open"


def first_non_empty(*values: object) -> object | None:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def first_non_zero(*values: object) -> object | None:
    for value in values:
        if value in (None, ""):
            continue
        if _float(value) == 0.0:
            continue
        return value
    return None


def non_zero_or_none(value: object) -> object | None:
    return first_non_zero(value)


def binance_order_price(order: Mapping[str, Any]) -> object | None:
    order_type = str(order.get("o") or order.get("type") or order.get("origType") or "")
    if "STOP" in order_type.upper() or "TAKE_PROFIT" in order_type.upper():
        return first_non_zero(order.get("sp"), order.get("stopPrice"), order.get("p"), order.get("price"))
    return first_non_zero(order.get("p"), order.get("price"), order.get("ap"), order.get("avgPrice"))


def _list_payload(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        return [payload]
    return []


def _float(value: object) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float, str)):
        return float(value)
    return 0.0


def sign_payload(params: Mapping[str, Any], api_secret: str) -> str:
    encoded = urlencode({key: value for key, value in params.items() if value != ""})
    return hmac.new(api_secret.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()

def account_config(config: BinanceAccountConfig, *, critical: bool = False) -> AccountStreamConfig:
    return AccountStreamConfig(
        db_url=config.critical_db_url if critical else config.db_url,
        critical_db_url=config.critical_db_url,
        exchange=config.exchange,
        environment=config.environment,
        market_type=config.market_type,
        account_scope=config.account_scope,
        ws_url=config.ws_url,
        rest_url=config.rest_url,
        api_key_env=config.api_key_env,
        api_secret_env=config.api_secret_env,
        balance_write_min_interval_seconds=config.balance_write_min_interval_seconds,
        position_write_min_interval_seconds=config.position_write_min_interval_seconds,
        log_level=config.log_level,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.tree.binance_account",
        description="Private account/order DB service for Binance USD-M Futures.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "status", "reconcile"):
        cmd = subparsers.add_parser(command, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        cmd.add_argument("--environment", choices=("demo", "live"), default=BinanceAccountConfig.environment)
        cmd.add_argument("--account-scope", default=BinanceAccountConfig.account_scope)
        cmd.add_argument("--account-db-url", "--db-url", dest="db_url")
        cmd.add_argument("--critical-db-url")
        cmd.add_argument("--ws-url")
        cmd.add_argument("--rest-url")
        cmd.add_argument("--api-key-env")
        cmd.add_argument("--api-secret-env")
        cmd.add_argument("--log-level", default=BinanceAccountConfig.log_level)
        cmd.add_argument("--rest-reconcile-seconds", type=float, default=BinanceAccountConfig.rest_reconcile_seconds)
        cmd.add_argument("--stream-kind", default="private_ws", help="Status stream kind.")
    return parser


def config_from_args(args: argparse.Namespace) -> BinanceAccountConfig:
    env_cfg = binance_futures_environment(args.environment)
    return BinanceAccountConfig(
        db_url=args.db_url
        or binance_futures_private_db_url(args.environment, args.account_scope),
        critical_db_url=args.critical_db_url
        or binance_futures_critical_db_url(args.environment, args.account_scope),
        environment=args.environment,
        account_scope=args.account_scope,
        ws_url=args.ws_url or env_cfg.private_ws_url,
        rest_url=args.rest_url or env_cfg.rest_url,
        api_key_env=args.api_key_env or env_cfg.api_key_env,
        api_secret_env=args.api_secret_env or env_cfg.api_secret_env,
        rest_reconcile_seconds=max(0.0, args.rest_reconcile_seconds),
        log_level=args.log_level,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args)
    account_store = AccountStateStore(account_config(config))
    critical_store = AccountStateStore(account_config(config, critical=True))
    if args.command == "status":
        store = critical_store if stream_kind_uses_critical_db(args.stream_kind) else account_store
        print(json.dumps(store.latest_status(args.stream_kind), sort_keys=True))
        return 0
    key = os.environ.get(config.api_key_env, "")
    secret = os.environ.get(config.api_secret_env, "")
    if not key or not secret:
        raise RuntimeError(
            f"Missing Binance credentials: set {config.api_key_env} and {config.api_secret_env}"
        )
    stream = BinancePrivateStream(config, account_store, critical_store, key, secret)
    if args.command == "reconcile":
        print(json.dumps(stream.reconcile_once(), sort_keys=True))
        return 0
    try:
        asyncio.run(stream.run())
    except KeyboardInterrupt:
        stream.stop()
        critical_store.record_connection_status(
            "private_ws",
            "stopped",
            last_error="stopped by operator",
        )
        critical_store.record_connection_status(
            "private_ws_critical",
            "stopped",
            last_error="stopped by operator",
        )
        account_store.record_connection_status(
            "private_ws_account",
            "stopped",
            last_error="stopped by operator",
        )
        print("private account stream stopped by operator")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
