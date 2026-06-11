from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, cast

import requests

from kolabi.shared.bitmex_futures import (
    bitmex_futures_critical_db_url,
    bitmex_futures_environment,
    bitmex_futures_private_db_url,
)
from kolabi.shared.config import (
    exchange_credential_env_names,
    first_configured_env_name,
)
from kolabi.shared.exchanges.bitmex_api.auth import APIKeyAuthWithExpires
from kolabi.shared.logging import setup_logging
from kolabi.tree.account import (
    AccountStateStore,
    AccountStreamConfig,
    BalanceWrite,
    KrakenFuturesCredentials,
    OrderWrite,
    PositionWrite,
    critical_private_config,
    run_rest_reconciler_forever,
)

JsonMapT = Mapping[str, Any]


@dataclass(frozen=True)
class BitmexAccountConfig:
    """Configuration for BitMEX private REST DB reconciliation."""

    db_url: str = "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
    critical_db_url: str = "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical"
    exchange: str = "bitmex"
    environment: str = "demo"
    market_type: str = "futures"
    account_scope: str = "default"
    symbol: str = "XBTUSD"
    rest_url: str = "https://testnet.bitmex.com/api/v1"
    api_key_env: str = "BTX_DEMO_API_KEY"
    api_secret_env: str = "BTX_DEMO_API_SECRET"
    rest_reconcile_seconds: float = 10.0
    log_level: str = "INFO"


class BitmexRestReconciler:
    """BitMEX REST reconciler writing the shared private account schema."""

    def __init__(
        self,
        config: BitmexAccountConfig,
        store: AccountStateStore,
        credentials: KrakenFuturesCredentials,
        *,
        critical_store: AccountStateStore | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.critical_store = critical_store
        self.credentials = credentials
        self.session = session or requests.Session()

    def reconcile_once(self) -> dict[str, int]:
        stats = {"orders": 0, "positions": 0, "balances": 0}
        open_orders_payload = self.get_json(
            "/order",
            params={
                "filter": json.dumps(
                    {"ordStatus.isTerminated": False, "symbol": self.config.symbol}
                ),
                "count": 100,
            },
        )
        open_orders = [
            map_bitmex_order(order)
            for order in _list_payload(open_orders_payload)
            if order_matches_symbol(order, self.config.symbol)
        ]
        snapshot_payload = {
            "feed": "open_orders_snapshot",
            "orders": [dict(order) for order in _list_payload(open_orders_payload)],
        }
        self.store.record_order_snapshot(open_orders, raw_payload=snapshot_payload)
        if self.critical_store is not None:
            self.critical_store.record_order_snapshot(
                open_orders,
                raw_payload=snapshot_payload,
            )
            self.critical_store.record_connection_status("private_ws", "healthy")
            self.critical_store.record_connection_status(
                "private_ws_critical",
                "healthy",
            )
        stats["orders"] += len(open_orders)

        positions_payload: Any = []
        if bitmex_market_has_positions(self.config.market_type):
            positions_payload = self.get_json(
                "/position",
                params={"filter": json.dumps({"symbol": self.config.symbol})},
            )
        positions = [
            map_bitmex_position(position)
            for position in _list_payload(positions_payload)
            if order_matches_symbol(position, self.config.symbol)
        ]
        position_rows = self.store.record_position_snapshot(
            positions,
            raw_payload={
                "feed": "open_positions_snapshot",
                "positions": [dict(position) for position in _list_payload(positions_payload)],
            },
        )
        stats["positions"] += len(position_rows)

        margin_payload = self.get_json("/user/margin", params={"currency": "all"})
        for balance in map_bitmex_balances(margin_payload):
            self.store.record_balance(balance)
            stats["balances"] += 1
        self.store.record_connection_status("rest_reconciler", "healthy")
        self.store.record_connection_status("private_ws_account", "healthy")
        return stats

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        response = self.session.get(
            url=f"{self.config.rest_url.rstrip('/')}{path}",
            params=dict(params or {}),
            auth=APIKeyAuthWithExpires(
                self.credentials.api_key,
                self.credentials.api_secret,
            ),
            timeout=10,
        )
        response.raise_for_status()
        return response.json()


def map_bitmex_order(payload: JsonMapT) -> OrderWrite:
    return OrderWrite(
        symbol=str(payload.get("symbol") or "unknown"),
        side=str(payload.get("side") or "unknown").lower(),
        order_type=str(payload.get("ordType") or "unknown"),
        status=map_bitmex_order_status(payload.get("ordStatus")),
        quantity=_float(payload.get("orderQty")),
        exchange_order_id=optional_str(payload.get("orderID")),
        client_order_id=optional_str(payload.get("clOrdID")),
        price=first_float(payload, "price", "stopPx", "avgPx"),
        filled_quantity=_float(payload.get("cumQty")),
        reduce_only="reduceonly" in str(payload.get("execInst") or "").lower(),
        raw_payload=dict(payload),
        source_timestamp=parse_time(
            payload.get("transactTime") or payload.get("timestamp")
        ),
    )


def map_bitmex_position(payload: JsonMapT) -> PositionWrite:
    size = _float(payload.get("currentQty"))
    return PositionWrite(
        symbol=str(payload.get("symbol") or "unknown"),
        side="long" if size >= 0 else "short",
        size=size,
        entry_price=first_float(payload, "avgEntryPrice", "avgCostPrice"),
        leverage=first_float(payload, "leverage"),
        liquidation_price=first_float(payload, "liquidationPrice"),
        available_margin=first_float(payload, "availableMargin"),
        maintenance_margin=first_float(payload, "maintMargin"),
        raw_payload=dict(payload),
        source_timestamp=parse_time(payload.get("timestamp")),
    )


def map_bitmex_balances(payload: Any) -> list[BalanceWrite]:
    rows = _list_payload(payload)
    if isinstance(payload, Mapping) and not rows:
        rows = [payload]
    balances: list[BalanceWrite] = []
    for row in rows:
        asset = str(row.get("currency") or "XBt").upper()
        total = _float(row.get("marginBalance") or row.get("walletBalance"))
        available = _float(row.get("availableMargin") or total)
        locked = max(total - available, 0.0)
        balances.append(
            BalanceWrite(
                asset=asset,
                available=available,
                locked=locked,
                total=total,
                raw_payload=dict(row),
                source_timestamp=parse_time(row.get("timestamp")),
            )
        )
    return balances


def order_matches_symbol(payload: JsonMapT, symbol: str) -> bool:
    row_symbol = str(payload.get("symbol") or "")
    return row_symbol in {"", symbol}


def bitmex_market_has_positions(market_type: str) -> bool:
    return (market_type or "futures").strip().lower() == "futures"


def map_bitmex_order_status(value: object) -> str:
    status = str(value or "").replace(" ", "").lower()
    if status in {"new", "open"}:
        return "open"
    if status in {"partiallyfilled", "partialfill"}:
        return "partial_fill"
    if status == "filled":
        return "filled"
    if status in {"canceled", "cancelled", "expired"}:
        return "canceled"
    if status == "rejected":
        return "rejected"
    return status or "open"


def first_float(payload: JsonMapT, *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return _float(value)
    return None


def _float(value: object) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float, str)):
        return float(value)
    return 0.0


def optional_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def parse_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc)
    if isinstance(value, str):
        if value.isdigit():
            return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _list_payload(payload: Any) -> list[JsonMapT]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        return [payload]
    return []


def account_config(config: BitmexAccountConfig, *, critical: bool = False) -> AccountStreamConfig:
    return AccountStreamConfig(
        db_url=config.critical_db_url if critical else config.db_url,
        critical_db_url=config.critical_db_url,
        exchange=config.exchange,
        environment=config.environment,
        market_type=config.market_type,
        account_scope=config.account_scope,
        rest_url=config.rest_url,
        api_key_env=config.api_key_env,
        api_secret_env=config.api_secret_env,
        rest_reconcile_seconds=config.rest_reconcile_seconds,
        log_level=config.log_level,
    )


def credentials_from_env(config: BitmexAccountConfig) -> KrakenFuturesCredentials:
    api_key = os.environ.get(config.api_key_env)
    api_secret = os.environ.get(config.api_secret_env)
    if not api_key or not api_secret:
        raise RuntimeError(f"Missing BitMEX credentials: set {config.api_key_env} and {config.api_secret_env}")
    return KrakenFuturesCredentials(api_key=api_key, api_secret=api_secret)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.tree.bitmex_account",
        description="Private account/order DB service for BitMEX REST reconciliation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "status", "reconcile"):
        cmd = subparsers.add_parser(command, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        cmd.add_argument("--environment", choices=("demo", "live"), default=BitmexAccountConfig.environment)
        cmd.add_argument("--market-type", choices=("futures", "spot"), default=BitmexAccountConfig.market_type)
        cmd.add_argument("--symbol", "--pair", dest="symbol", default=BitmexAccountConfig.symbol)
        cmd.add_argument("--account-scope", default=BitmexAccountConfig.account_scope)
        cmd.add_argument("--account-db-url", "--db-url", dest="db_url")
        cmd.add_argument("--critical-db-url")
        cmd.add_argument("--rest-url")
        cmd.add_argument("--api-key-env")
        cmd.add_argument("--api-secret-env")
        cmd.add_argument("--log-level", default=BitmexAccountConfig.log_level)
        cmd.add_argument("--rest-reconcile-seconds", type=float, default=BitmexAccountConfig.rest_reconcile_seconds)
        cmd.add_argument("--stream-kind", default="private_ws", help="Status stream kind.")
    return parser


def config_from_args(args: argparse.Namespace) -> BitmexAccountConfig:
    env_cfg = bitmex_futures_environment(args.environment)
    key_names = exchange_credential_env_names(
        "bitmex",
        args.market_type,
        args.environment,
    )
    secret_names = exchange_credential_env_names(
        "bitmex",
        args.market_type,
        args.environment,
        secret=True,
    )
    return BitmexAccountConfig(
        db_url=args.db_url
        or bitmex_futures_private_db_url(args.environment, args.account_scope),
        critical_db_url=args.critical_db_url
        or bitmex_futures_critical_db_url(args.environment, args.account_scope),
        environment=args.environment,
        market_type=args.market_type,
        account_scope=args.account_scope,
        symbol=args.symbol,
        rest_url=args.rest_url or env_cfg.rest_url,
        api_key_env=args.api_key_env or first_configured_env_name(key_names),
        api_secret_env=args.api_secret_env
        or first_configured_env_name(secret_names),
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
        store = critical_store if args.stream_kind in {"private_ws", "private_ws_critical"} else account_store
        print(json.dumps(store.latest_status(args.stream_kind), sort_keys=True))
        return 0
    credentials = credentials_from_env(config)
    reconciler = BitmexRestReconciler(
        config,
        account_store,
        credentials,
        critical_store=critical_store,
    )
    if args.command == "reconcile":
        print(json.dumps(reconciler.reconcile_once(), sort_keys=True))
        return 0
    logger = setup_logging(config.log_level)
    try:
        run_rest_reconciler_forever(
            reconciler,
            rest_reconcile_seconds=config.rest_reconcile_seconds,
            logger=logger,
        )
    except KeyboardInterrupt:
        account_store.record_connection_status(
            "rest_reconciler",
            "stopped",
            last_error="stopped by operator",
        )
        critical_store.record_connection_status(
            "private_ws",
            "stopped",
            last_error="stopped by operator",
        )
        print("private account stream stopped by operator")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
