from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from typing import Any, Sequence

from kolabi.shared.config import load_exchange_config
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.exchanges import get_adapter

EXCHANGES = ("kraken", "binance", "bitmex")


def build_parser() -> argparse.ArgumentParser:
    """Build a small exchange CLI for direct adapter operations."""
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.bargain.cli",
        usage="python -m kolabi.bargain.cli [--exchange EXCHANGE] [--symbol SYMBOL] [--environment {demo,live}] <command> [<args>]",
        description=(
            "Direct exchange adapter CLI for instrument checks, account reads, and order actions."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--exchange", choices=EXCHANGES, default="kraken", help="Target exchange adapter.")
    parser.add_argument("--symbol", default="PI_XBTUSD", help="Trading symbol / instrument id.")
    parser.add_argument("--environment", choices=("demo", "live"), default="demo", help="API environment.")
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
    add_command("close-all", "Cancel all orders and close position.", "Cancel all orders and close current position with reduce-only market logic.")

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


def build_adapter(exchange: str, symbol: str, environment: str):
    """Build the adapter for the selected exchange from environment credentials."""
    config = load_exchange_config(exchange, symbol=symbol, environment=environment)
    adapter_cls = get_adapter(exchange)
    return adapter_cls(
        api_key=config.api_key,
        api_secret=config.api_secret,
        base_url=config.base_url,
        symbol=config.symbol,
        **config.adapter_kwargs,
    )


def print_json(payload: object) -> None:
    """Imprimer un resultat compact pour usage shell."""
    print(json.dumps(payload, sort_keys=True))


def ack_to_payload(ack: OrderAck) -> dict[str, object]:
    """Convertir un dataclass ack vers JSON stable."""
    return asdict(ack)


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
    live_orders = list(adapter.live_open_orders()) + list(adapter.live_trigger_orders())
    for order in live_orders:
        order_id = (
            order.get("orderID")
            or order.get("order_id")
            or order.get("clOrdID")
            or order.get("cli_ord_id")
        )
        if not order_id:
            continue
        payloads.append(ack_to_payload(adapter.cancel_order(str(order_id))))
    return payloads


def _close_position(adapter: Any) -> dict[str, object] | None:
    """Fermer la position existante via un ordre market reduce-only."""
    position_before = adapter.get_position()
    qty = float(position_before.qty)
    if qty == 0:
        return None
    side = "sell" if qty > 0 else "buy"
    attempts = 1
    ack = adapter.place_order(
        side=side,
        orderQty=abs(qty),
        type_="MARKET",
        reduceOnly=True,
    )
    position_after = position_before
    verification_error: str | None = None
    for _ in range(10):
        try:
            position_after = adapter.get_position()
        except Exception as exc:
            verification_error = str(exc)
            break
        if float(position_after.qty) == 0.0:
            break
        time.sleep(0.2)
    if float(position_after.qty) != 0.0 and hasattr(adapter, "instrument"):
        # Pour Kraken Futures, un close IOC peut rater si le prix de protection
        # implicite n'est pas assez agressif. On retente avec un prix explicite.
        for multiplier in (1.05, 1.10, 1.20):
            try:
                instrument = adapter.instrument(position_after.symbol)
            except Exception as exc:
                verification_error = str(exc)
                break
            bid = float(instrument.get("bidPrice") or 0.0)
            ask = float(instrument.get("askPrice") or 0.0)
            aggressive_price = ask * multiplier if side == "buy" else bid / multiplier
            attempts += 1
            ack = adapter.place_order(
                side=side,
                orderQty=abs(float(position_after.qty)),
                price=aggressive_price,
                type_="MARKET",
                reduceOnly=True,
            )
            for _ in range(10):
                try:
                    position_after = adapter.get_position()
                except Exception as exc:
                    verification_error = str(exc)
                    break
                if float(position_after.qty) == 0.0:
                    break
                time.sleep(0.2)
            if verification_error is not None:
                break
            if float(position_after.qty) == 0.0:
                break
    payload = ack_to_payload(ack)
    payload["attempts"] = attempts
    payload["closed"] = float(position_after.qty) == 0.0
    payload["position_before"] = position_to_payload(position_before)
    payload["position_after"] = position_to_payload(position_after)
    payload["verification_error"] = verification_error
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """Point d'entree CLI pour tests de communication directs."""
    parser = build_parser()
    args = parser.parse_args(argv)
    adapter = build_adapter(args.exchange, args.symbol, args.environment)

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
        print_json(adapter.validate_symbol(args.symbol))
        return 0

    if args.command == "balance":
        print_json(
            {
                "environment": args.environment,
                "symbol": args.symbol,
                "availableMargin": adapter.get_balance(),
            }
        )
        return 0
    if args.command == "position":
        print_json(position_to_payload(adapter.get_position()))
        return 0
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
                "symbol": args.symbol,
                "orders": adapter.live_open_orders(),
            }
        )
        return 0
    if args.command == "trigger-orders":
        print_json(
            {
                "environment": args.environment,
                "symbol": args.symbol,
                "orders": adapter.live_trigger_orders(),
            }
        )
        return 0
    if args.command == "cancel-all":
        print_json(
            {
                "environment": args.environment,
                "symbol": args.symbol,
                "cancelled": _cancel_all_orders(adapter),
            }
        )
        return 0
    if args.command == "close-all":
        print_json(
            {
                "environment": args.environment,
                "symbol": args.symbol,
                "cancelled": _cancel_all_orders(adapter),
                "close_order": _close_position(adapter),
            }
        )
        return 0
    if args.command == "limit":
        adapter.validate_symbol(args.symbol)
        print_json(
            ack_to_payload(
                adapter.place_order(
                    side=args.side,
                    orderQty=args.qty,
                    price=args.price,
                    type_="LIMIT",
                )
            )
        )
        return 0
    if args.command == "market":
        adapter.validate_symbol(args.symbol)
        initial_position = adapter.get_position()
        ack = adapter.place_order(
            side=args.side,
            orderQty=args.qty,
            type_="MARKET",
        )
        payload = ack_to_payload(ack)
        if (
            str(payload.get("status", "")).lower() == "filled"
            or float(payload.get("executed_qty") or 0.0) > 0.0
        ):
            payload["verification"] = {"filled": True, "reason": "ack_execution"}
            print_json(payload)
            return 0
        verification = _verify_market_submission(
            adapter,
            order_id=str(payload.get("order_id") or ""),
            initial_qty=float(initial_position.qty),
            side=args.side,
            quantity=float(args.qty),
        )
        payload["verification"] = verification
        print_json(payload)
        return 0 if bool(verification.get("filled")) else 2
    if args.command == "trailing":
        adapter.validate_symbol(args.symbol)
        print_json(
            ack_to_payload(
                adapter.place_order(
                    side=args.side,
                    orderQty=args.qty,
                    type_="TrailingStop",
                    trailingStopMaxDeviation=args.deviation,
                    trailingStopDeviationUnit=args.unit,
                )
            )
        )
        return 0
    if args.command == "trailing-limit":
        adapter.validate_symbol(args.symbol)
        print_json(
            ack_to_payload(
                adapter.place_order(
                    side=args.side,
                    orderQty=args.qty,
                    price=args.price,
                    type_="TrailingStopLimit",
                    trailingStopMaxDeviation=args.deviation,
                    trailingStopDeviationUnit=args.unit,
                )
            )
        )
        return 0
    raise ValueError(f"Unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
