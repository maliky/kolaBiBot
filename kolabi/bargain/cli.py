from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from typing import Any, Mapping, Sequence, cast

from kolabi.bargain.smoke import smoke_client_order_id
from kolabi.bot.exchange_routes import (
    EXCHANGE_MARKET_TYPES,
    default_symbol_for_route,
    route_codes_for_market,
)
from kolabi.shared.config import (
    exchange_credential_env_names,
    exchange_requires_explicit_base_url,
    load_exchange_config,
)
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.exchanges import get_adapter

EXCHANGES = ("kraken", "binance", "bitmex")


def build_parser() -> argparse.ArgumentParser:
    """Build a small exchange CLI for direct adapter operations."""
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.bargain.cli",
        usage=(
            "python -m kolabi.bargain.cli [--exchange EXCHANGE] "
            "[--market-type MARKET_TYPE] [--symbol SYMBOL] "
            "[--environment {demo,live}] [--account-scope ACCOUNT_SCOPE] "
            "[--base-url BASE_URL] "
            "[--api-key-env API_KEY_ENV] [--api-secret-env API_SECRET_ENV] "
            "<command> [<args>]"
        ),
        description=(
            "Direct exchange adapter CLI for instrument checks, account reads, and order actions."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--exchange", choices=EXCHANGES, default="kraken", help="Target exchange adapter.")
    parser.add_argument(
        "--market-type",
        choices=("futures", "spot", "margin", "isolated_margin"),
        default="futures",
        help="Exchange market lane for direct adapter operations.",
    )
    parser.add_argument(
        "--symbol",
        default=argparse.SUPPRESS,
        help="Trading symbol / instrument id. Default follows --exchange and --market-type.",
    )
    parser.add_argument("--environment", choices=("demo", "live"), default="demo", help="API environment.")
    parser.add_argument(
        "--base-url",
        "--rest-url",
        dest="base_url",
        help="REST base URL override for demo/live bring-up.",
    )
    parser.add_argument(
        "--account-scope",
        default="default",
        help="Logical account/persona label used for account-scoped persistence lanes.",
    )
    parser.add_argument(
        "--api-key-env",
        help="Environment variable name containing the API key.",
    )
    parser.add_argument(
        "--api-secret-env",
        help="Environment variable name containing the API secret.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="<command>",
    )

    def add_command(name: str, help_text: str, description: str | None = None) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, help=help_text, description=description or help_text)

    add_command("balance", "Show available margin.", "Show available margin for the selected exchange account.")
    add_command("position", "Show current position.", "Show current position for the selected symbol.")
    add_command(
        "permissions",
        "Check API key order-write permission.",
        "Check API key order-write permission without placing an order when the adapter supports it.",
    )
    add_command(
        "routes",
        "List supported route codes.",
        "List exchange/market route codes, defaults, and credential environment names without connecting.",
    )

    instruments = add_command(
        "instruments",
        "List available instruments.",
        "List exchange instruments; optionally filter symbols by substring.",
    )
    instruments.add_argument(
        "--contains",
        help="Filter instruments whose symbol contains this text (case-insensitive).",
    )

    add_command(
        "check-symbol",
        "Validate selected symbol.",
        "Validate the selected symbol on the target exchange and return metadata.",
    )

    cancel = add_command(
        "cancel",
        "Cancel one order.",
        "Cancel one order by exchange order id.",
    )
    cancel.add_argument(
        "--order-id",
        required=True,
        help="Exchange order id to cancel.",
    )
    amend = add_command(
        "amend",
        "Amend one order.",
        "Amend one order by id using optional price and quantity updates.",
    )
    amend.add_argument(
        "--order-id",
        required=True,
        help="Exchange order id to amend.",
    )
    amend.add_argument(
        "--price",
        type=float,
        help="New limit price for the order.",
    )
    amend.add_argument(
        "--qty",
        type=float,
        help="New order quantity.",
    )
    add_command("open-orders", "Show live resting orders.", "Show live resting orders for the selected symbol.")
    add_command("trigger-orders", "Show live trigger orders.", "Show live trigger orders for the selected symbol.")
    add_command("cancel-all", "Cancel all open and trigger orders.", "Cancel all open and trigger orders for the selected symbol.")
    add_command("close-all", "Cancel all orders and close position.", "Cancel all orders and close current position with market-type-aware close logic.")

    for command in ("limit", "market"):
        cmd = add_command(
            command,
            "Submit one limit order." if command == "limit" else "Submit one market order.",
            "Submit one limit order." if command == "limit" else "Submit one market order.",
        )
        cmd.add_argument(
            "--side",
            choices=("buy", "sell"),
            required=True,
            help="Order side.",
        )
        cmd.add_argument(
            "--qty",
            type=float,
            required=True,
            help="Order quantity.",
        )
        if command == "limit":
            cmd.add_argument(
                "--price",
                type=float,
                required=True,
                help="Limit price.",
            )

    trailing = add_command(
        "trailing",
        "Submit one trailing-stop order.",
        "Submit one native trailing-stop order.",
    )
    trailing.add_argument(
        "--side",
        choices=("buy", "sell"),
        required=True,
        help="Order side.",
    )
    trailing.add_argument(
        "--qty",
        type=float,
        required=True,
        help="Order quantity.",
    )
    trailing.add_argument(
        "--deviation",
        type=float,
        required=True,
        help="Trailing stop deviation value.",
    )
    trailing.add_argument(
        "--unit",
        choices=("PERCENT", "QUOTE_CURRENCY"),
        default="PERCENT",
        help="Deviation unit for trailing stop.",
    )

    trailing_limit = add_command(
        "trailing-limit",
        "Submit one trailing-stop-limit order.",
        "Submit one native trailing-stop-limit order.",
    )
    trailing_limit.add_argument(
        "--side",
        choices=("buy", "sell"),
        required=True,
        help="Order side.",
    )
    trailing_limit.add_argument(
        "--qty",
        type=float,
        required=True,
        help="Order quantity.",
    )
    trailing_limit.add_argument(
        "--price",
        type=float,
        required=True,
        help="Limit price for trailing-stop-limit order.",
    )
    trailing_limit.add_argument(
        "--deviation",
        type=float,
        required=True,
        help="Trailing stop deviation value.",
    )
    trailing_limit.add_argument(
        "--unit",
        choices=("PERCENT", "QUOTE_CURRENCY"),
        default="PERCENT",
        help="Deviation unit for trailing stop.",
    )
    return parser


def _env_scope_key(account_scope: str) -> str:
    return (account_scope.strip() or "default").upper().replace("-", "_")


def _kolabi_scoped_db_url(lane: str, account_scope: str) -> str | None:
    lane_key = lane.upper()
    if (account_scope.strip() or "default") == "default":
        return os.environ.get(f"KOLABI_{lane_key}_DB_URL")
    return os.environ.get(f"KOLABI_{_env_scope_key(account_scope)}_{lane_key}_DB_URL")


def build_adapter(
    exchange: str,
    symbol: str,
    environment: str,
    *,
    market_type: str = "futures",
    account_scope: str = "default",
    api_key_env: str | None = None,
    api_secret_env: str | None = None,
    base_url: str | None = None,
):
    """Build the adapter for the selected exchange from environment credentials."""
    config = load_exchange_config(
        exchange,
        symbol=symbol,
        environment=environment,
        market_type=market_type,
        api_key_env=api_key_env,
        api_secret_env=api_secret_env,
        base_url=base_url,
    )
    _decorate_adapter_config(config, account_scope=account_scope)
    adapter_cls = get_adapter(exchange, market_type)
    return adapter_cls(
        api_key=config.api_key,
        api_secret=config.api_secret,
        base_url=config.base_url,
        symbol=config.symbol,
        **config.adapter_kwargs,
    )


def _decorate_adapter_config(
    config,
    *,
    account_scope: str,
) -> None:
    """Attach shared DB lanes used by direct operator adapter commands."""

    config.adapter_kwargs["account_scope"] = account_scope
    public_db_url = os.environ.get("KOLABI_MARKET_DB_URL")
    account_db_url = _kolabi_scoped_db_url("ACCOUNT", account_scope)
    audit_db_url = _kolabi_scoped_db_url("AUDIT", account_scope)
    if public_db_url is not None:
        config.adapter_kwargs["public_db_url"] = public_db_url
    if account_db_url is not None:
        config.adapter_kwargs["account_db_url"] = account_db_url
    if audit_db_url is not None:
        config.adapter_kwargs["audit_db_url"] = audit_db_url


def build_bot_service(
    exchange: str,
    symbol: str,
    environment: str,
    *,
    market_type: str = "futures",
    account_scope: str = "default",
    api_key_env: str | None = None,
    api_secret_env: str | None = None,
):
    """Build BotService for admin actions that must flow through the bot path."""
    from kolabi.bot.service import BotConfig, BotService

    return BotService(
        BotConfig(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            environment=environment,
            account_scope=account_scope,
            api_key_env=api_key_env,
            api_secret_env=api_secret_env,
            require_ready=False,
            log_level="INFO",
        )
    )


def print_json(payload: object) -> None:
    """Imprimer un resultat compact pour usage shell."""
    print(json.dumps(payload, sort_keys=True))


_MARKET_TYPE_ORDER = ("futures", "spot", "margin", "isolated_margin")
_ADAPTER_PERMISSION_PROBES: dict[tuple[str, str], str] = {
    ("binance", "futures"): "test_order",
    ("binance", "spot"): "test_order",
    ("bitmex", "futures"): "apiKey",
    ("bitmex", "spot"): "apiKey",
}
def _first_present_env_name(
    names: Sequence[str],
    env: Mapping[str, str],
) -> str | None:
    for name in names:
        if env.get(name):
            return name
    return None


def route_matrix_payload(
    environment: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return the operator-facing route matrix without reading secret values."""

    routes: list[dict[str, object]] = []
    normalized_environment = str(environment or "demo").strip().lower()
    env_mapping = os.environ if env is None else env
    for exchange in sorted(EXCHANGE_MARKET_TYPES):
        market_types = EXCHANGE_MARKET_TYPES[exchange]
        ordered_market_types = [
            market_type
            for market_type in _MARKET_TYPE_ORDER
            if market_type in market_types
        ]
        for market_type in ordered_market_types:
            api_key_env = list(
                exchange_credential_env_names(
                    exchange,
                    market_type,
                    normalized_environment,
                )
            )
            api_secret_env = list(
                exchange_credential_env_names(
                    exchange,
                    market_type,
                    normalized_environment,
                    secret=True,
                )
            )
            api_key_source = _first_present_env_name(api_key_env, env_mapping)
            api_secret_source = _first_present_env_name(api_secret_env, env_mapping)
            routes.append(
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "codes": list(route_codes_for_market(exchange, market_type)),
                    "default_symbol": default_symbol_for_route(exchange, market_type),
                    "api_key_env": api_key_env,
                    "api_secret_env": api_secret_env,
                    "api_key_present": api_key_source is not None,
                    "api_secret_present": api_secret_source is not None,
                    "credentials_present": (
                        api_key_source is not None and api_secret_source is not None
                    ),
                    "api_key_source": api_key_source,
                    "api_secret_source": api_secret_source,
                    "permission_probe": _permission_probe_name(
                        exchange,
                        market_type,
                    ),
                    "order_write_probe": _permission_probe_can_prove_order_write(
                        exchange,
                        market_type,
                    ),
                    "demo_requires_base_url_override": (
                        exchange_requires_explicit_base_url(
                            exchange,
                            market_type,
                            normalized_environment,
                        )
                    ),
                }
            )
    return {"environment": normalized_environment, "routes": routes}


def permission_status_payload(
    adapter: Any,
    *,
    exchange: str,
    market_type: str,
    symbol: str,
    environment: str,
) -> dict[str, object]:
    """Return an operator permission payload without submitting an order."""

    status = getattr(adapter, "permission_status", None)
    if callable(status):
        payload = dict(status())
    else:
        payload = {
            "exchange": exchange,
            "market_type": market_type,
            "symbol": symbol,
            "permission_probe": "not_supported",
            "can_place_orders": None,
            "reason": "adapter does not expose a no-order permission probe",
        }
    payload.setdefault("exchange", exchange)
    payload.setdefault("market_type", market_type)
    payload.setdefault("symbol", symbol)
    payload["environment"] = environment
    return payload


def _permission_probe_needs_adapter(exchange: str, market_type: str) -> bool:
    """Return whether permissions can run a real no-order adapter probe."""

    return _permission_probe_name(exchange, market_type) != "not_supported"


def _permission_probe_name(exchange: str, market_type: str) -> str:
    """Return the static permission probe type for one direct route."""

    route = (
        str(exchange or "").strip().lower(),
        str(market_type or "futures").strip().lower(),
    )
    return _ADAPTER_PERMISSION_PROBES.get(route, "not_supported")


def _permission_probe_can_prove_order_write(exchange: str, market_type: str) -> bool:
    """Return whether the probe confirms order-write capability."""

    return _permission_probe_name(exchange, market_type) != "not_supported"


def ack_to_payload(ack: OrderAck) -> dict[str, object]:
    """Convertir un dataclass ack vers JSON stable."""
    return asdict(ack)


def _ack_payload_with_client_id(
    ack: OrderAck,
    client_order_id: str,
) -> dict[str, object]:
    payload = ack_to_payload(ack)
    payload["client_order_id"] = payload.get("client_order_id") or client_order_id
    return payload


def _client_order_id_for_command(command: str) -> str:
    return smoke_client_order_id(f"cli_{command}")


def position_to_payload(position: Position) -> dict[str, object]:
    """Convertir une position simple vers JSON stable."""
    return asdict(position)


def _safe_float(value: object | None) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    return None


def _extract_trade_order_id(trade: dict[str, object]) -> str:
    for key in ("order_id", "orderId", "orderID", "cli_ord_id", "cliOrdId"):
        value = trade.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _extract_trade_fill_qty(trade: dict[str, object]) -> float:
    for key in ("filled", "filled_qty", "filledQty", "size", "qty", "quantity"):
        value = _safe_float(trade.get(key))
        if value is not None:
            return abs(value)
    return 0.0


def _verify_market_submission(
    adapter: Any,
    *,
    order_id: str,
    initial_qty: float,
    side: str,
    quantity: float,
    timeout_seconds: float = 2.5,
) -> dict[str, object]:
    """Verify that a market order has visible execution evidence shortly after submit."""
    deadline = time.time() + timeout_seconds
    observed_position = adapter.get_position()
    fill_qty = 0.0
    matched_trade = False
    verification_error: str | None = None
    polls = 0
    while time.time() < deadline:
        polls += 1
        try:
            observed_position = adapter.get_position()
        except Exception as exc:
            verification_error = f"position_check_failed: {exc}"
            break
        if float(observed_position.qty) != float(initial_qty):
            return {
                "filled": True,
                "reason": "position_changed",
                "position_before": initial_qty,
                "position_after": float(observed_position.qty),
                "polls": polls,
            }
        if hasattr(adapter, "recent_trades"):
            try:
                trades = adapter.recent_trades()
            except Exception as exc:
                verification_error = f"recent_trades_failed: {exc}"
                break
            for trade in trades:
                trade_id = _extract_trade_order_id(trade)
                if order_id and trade_id and trade_id == order_id:
                    matched_trade = True
                    fill_qty += _extract_trade_fill_qty(trade)
            if fill_qty > 0:
                return {
                    "filled": True,
                    "reason": "recent_trades_match",
                    "position_before": initial_qty,
                    "position_after": float(observed_position.qty),
                    "filled_qty": fill_qty,
                    "polls": polls,
                }
        time.sleep(0.25)
    return {
        "filled": False,
        "reason": "no_fill_observed",
        "position_before": initial_qty,
        "position_after": float(observed_position.qty),
        "requested_side": side,
        "requested_qty": quantity,
        "matched_trade_without_qty": matched_trade,
        "verification_error": verification_error,
        "polls": polls,
    }


def _cancel_all_orders(adapter: Any) -> list[dict[str, object]]:
    """Annuler tous les ordres ouverts et triggers pour le symbole configure."""
    payloads: list[dict[str, object]] = []
    cancelled_ids: set[str] = set()
    for order in _safe_cancel_order_candidates(adapter):
        order_id = _extract_cancelable_order_id(order)
        if not order_id:
            continue
        order_key = str(order_id)
        if order_key in cancelled_ids:
            continue
        try:
            payloads.append(ack_to_payload(adapter.cancel_order(order_key)))
            cancelled_ids.add(order_key)
        except Exception:
            continue
    return payloads


def _extract_cancelable_order_id(order: dict[str, object]) -> object | None:
    """Return any exchange/client identity accepted by adapter cancellation."""
    for key in (
        "orderID",
        "orderId",
        "order_id",
        "id",
        "clOrdID",
        "cliOrdId",
        "cli_ord_id",
        "client_order_id",
    ):
        value = order.get(key)
        if value:
            return value
    return None


def _safe_cancel_order_candidates(adapter: Any) -> list[dict[str, object]]:
    """Fetch cancel candidates from preferred live sources with defensive fallbacks."""
    candidates: list[dict[str, object]] = []
    for source_name in (
        "live_open_orders",
        "live_trigger_orders",
        "open_orders",
        "live_trigger_orders_db",
    ):
        source = getattr(adapter, source_name, None)
        if not callable(source):
            continue
        try:
            rows = source()
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        candidates.extend([row for row in rows if isinstance(row, dict)])
    return candidates


def _close_position(
    adapter: Any,
    *,
    market_type: str = "futures",
) -> dict[str, object] | None:
    """Fermer la position existante via un ordre market adapte au marche."""
    position_before = adapter.get_position()
    initial_qty = float(position_before.qty)
    if initial_qty == 0:
        return None
    side = "sell" if initial_qty > 0 else "buy"
    attempts = 0
    max_attempts = 3
    ack: OrderAck | None = None
    position_after = position_before
    verification_error: str | None = None
    verification_reason = "position_still_open"
    while attempts < max_attempts:
        current_qty = float(position_after.qty)
        if current_qty == 0.0:
            verification_reason = "position_closed"
            break
        attempts += 1
        close_params = _market_close_order_params(market_type)
        client_order_id = _client_order_id_for_command("close")
        ack = adapter.place_order(
            side=side,
            orderQty=abs(current_qty),
            type_="MARKET",
            clOrdID=client_order_id,
            **close_params,
        )
        for _ in range(10):
            try:
                position_after = adapter.get_position()
            except Exception as exc:
                verification_error = str(exc)
                verification_reason = "position_check_failed"
                break
            if float(position_after.qty) == 0.0:
                verification_reason = "position_closed"
                break
            time.sleep(0.2)
            if verification_error is not None:
                break
        if verification_error is not None or float(position_after.qty) == 0.0:
            break
    payload = ack_to_payload(ack) if ack is not None else {}
    payload["attempts"] = attempts
    payload["closed"] = float(position_after.qty) == 0.0
    payload["verification_reason"] = verification_reason
    payload["position_before"] = position_to_payload(position_before)
    payload["position_after"] = position_to_payload(position_after)
    payload["verification_error"] = verification_error
    return payload


def _market_close_order_params(market_type: str) -> dict[str, object]:
    if (market_type or "futures").strip().lower() == "futures":
        return {"reduceOnly": True}
    return {}


def main(argv: Sequence[str] | None = None) -> int:
    """Point d'entree CLI pour tests de communication directs."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "routes":
        print_json(route_matrix_payload(args.environment))
        return 0
    symbol = getattr(args, "symbol", None) or default_symbol_for_route(
        args.exchange,
        args.market_type,
    )
    if (
        args.command == "permissions"
        and not _permission_probe_needs_adapter(args.exchange, args.market_type)
    ):
        print_json(
            permission_status_payload(
                object(),
                exchange=args.exchange,
                market_type=args.market_type,
                symbol=symbol,
                environment=args.environment,
            )
        )
        return 0
    adapter = build_adapter(
        args.exchange,
        symbol,
        args.environment,
        market_type=args.market_type,
        account_scope=args.account_scope,
        api_key_env=args.api_key_env,
        api_secret_env=args.api_secret_env,
        base_url=args.base_url,
    )

    if args.command == "instruments":
        instruments = adapter.list_instruments()
        contains = (args.contains or "").upper()
        if contains:
            instruments = [
                item
                for item in instruments
                if contains
                in str(item.get("symbol") or item.get("product_id") or "").upper()
            ]
        print_json(
            [
                {
                    "symbol": item.get("symbol") or item.get("product_id"),
                    "type": item.get("tag") or item.get("type"),
                    "tradeable": item.get("tradeable"),
                }
                for item in instruments
            ]
        )
        return 0
    if args.command == "check-symbol":
        print_json(adapter.validate_symbol(symbol))
        return 0

    if args.command == "balance":
        print_json(
            {
                "environment": args.environment,
                "symbol": symbol,
                "availableMargin": adapter.get_balance(),
            }
        )
        return 0
    if args.command == "position":
        print_json(position_to_payload(adapter.get_position()))
        return 0
    if args.command == "permissions":
        payload = permission_status_payload(
            adapter,
            exchange=args.exchange,
            market_type=args.market_type,
            symbol=symbol,
            environment=args.environment,
        )
        print_json(payload)
        can_place_orders = payload.get("can_place_orders")
        return 1 if can_place_orders is False else 0
    if args.command == "cancel":
        print_json(ack_to_payload(adapter.cancel_order(args.order_id)))
        return 0
    if args.command == "amend":
        params: dict[str, float] = {}
        if args.price is not None:
            params["price"] = args.price
        if args.qty is not None:
            params["orderQty"] = args.qty
        print_json(ack_to_payload(adapter.amend_order(args.order_id, **params)))
        return 0
    if args.command == "open-orders":
        print_json(
            {
                "environment": args.environment,
                "symbol": symbol,
                "orders": adapter.live_open_orders(),
            }
        )
        return 0
    if args.command == "trigger-orders":
        print_json(
            {
                "environment": args.environment,
                "symbol": symbol,
                "orders": adapter.live_trigger_orders(),
            }
        )
        return 0
    if args.command == "cancel-all":
        service = build_bot_service(
            args.exchange,
            symbol,
            args.environment,
            market_type=args.market_type,
            account_scope=args.account_scope,
            api_key_env=args.api_key_env,
            api_secret_env=args.api_secret_env,
        )
        print_json(
            {
                "environment": args.environment,
                "market_type": args.market_type,
                "account_scope": args.account_scope,
                "symbol": symbol,
                "cancelled": [ack_to_payload(ack) for ack in service.cancel_all_orders()],
            }
        )
        return 0
    if args.command == "close-all":
        service = build_bot_service(
            args.exchange,
            symbol,
            args.environment,
            market_type=args.market_type,
            account_scope=args.account_scope,
            api_key_env=args.api_key_env,
            api_secret_env=args.api_secret_env,
        )
        result = service.close_all_orders()
        print_json(
            {
                "environment": args.environment,
                "market_type": args.market_type,
                "account_scope": args.account_scope,
                "symbol": symbol,
                "cancelled": [ack_to_payload(ack) for ack in result["cancelled"]],
                "close_order": (
                    None
                    if result["close_ack"] is None
                    else ack_to_payload(cast(OrderAck, result["close_ack"]))
                ),
                "close_action": result.get("close_action"),
                "close_skipped_reason": result.get("close_skipped_reason"),
                "closed": bool(result["closed"]),
                "cancel_errors": result.get("cancel_errors", []),
                "audit_persistence_ok": result.get("audit_persistence_ok", True),
                "audit_persistence_errors": result.get("audit_persistence_errors", []),
                "position_before": position_to_payload(cast(Position, result["position_before"])),
                "position_after": position_to_payload(cast(Position, result["position_after"])),
            }
        )
        return 0
    if args.command == "limit":
        adapter.validate_symbol(symbol)
        client_order_id = _client_order_id_for_command("limit")
        print_json(
            _ack_payload_with_client_id(
                adapter.place_order(
                    side=args.side,
                    orderQty=args.qty,
                    price=args.price,
                    type_="LIMIT",
                    clOrdID=client_order_id,
                ),
                client_order_id,
            )
        )
        return 0
    if args.command == "market":
        adapter.validate_symbol(symbol)
        initial_position = adapter.get_position()
        client_order_id = _client_order_id_for_command("market")
        ack = adapter.place_order(
            side=args.side,
            orderQty=args.qty,
            type_="MARKET",
            clOrdID=client_order_id,
        )
        payload = _ack_payload_with_client_id(ack, client_order_id)
        executed_qty = _safe_float(payload.get("executed_qty")) or 0.0
        if (
            str(payload.get("status", "")).lower() == "filled"
            or executed_qty > 0.0
        ):
            payload["verification"] = {"filled": True, "reason": "ack_execution"}
            print_json(payload)
            return 0
        verification = _verify_market_submission(
            adapter,
            order_id=str(payload.get("order_id") or payload.get("client_order_id") or ""),
            initial_qty=float(initial_position.qty),
            side=args.side,
            quantity=float(args.qty),
        )
        payload["verification"] = verification
        print_json(payload)
        return 0 if bool(verification.get("filled")) else 2
    if args.command == "trailing":
        adapter.validate_symbol(symbol)
        client_order_id = _client_order_id_for_command("trailing")
        print_json(
            _ack_payload_with_client_id(
                adapter.place_order(
                    side=args.side,
                    orderQty=args.qty,
                    type_="TrailingStop",
                    clOrdID=client_order_id,
                    trailingStopMaxDeviation=args.deviation,
                    trailingStopDeviationUnit=args.unit,
                ),
                client_order_id,
            )
        )
        return 0
    if args.command == "trailing-limit":
        adapter.validate_symbol(symbol)
        client_order_id = _client_order_id_for_command("trailing_limit")
        print_json(
            _ack_payload_with_client_id(
                adapter.place_order(
                    side=args.side,
                    orderQty=args.qty,
                    price=args.price,
                    type_="TrailingStopLimit",
                    clOrdID=client_order_id,
                    trailingStopMaxDeviation=args.deviation,
                    trailingStopDeviationUnit=args.unit,
                ),
                client_order_id,
            )
        )
        return 0
    raise ValueError(f"Unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
