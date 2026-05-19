from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from typing import Any, Sequence

from kolabi.shared.config import load_exchange_config
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.exchanges import get_adapter


def build_parser() -> argparse.ArgumentParser:
    """Construire une petite CLI pour tester le canal Futures Kraken."""
    parser = argparse.ArgumentParser(prog="python -m kolabi.bargain.cli")
    parser.add_argument("--symbol", default="PI_XBTUSD")
    parser.add_argument("--environment", choices=("demo", "live"), default="demo")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("balance")
    subparsers.add_parser("position")

    instruments = subparsers.add_parser("instruments")
    instruments.add_argument("--contains")

    subparsers.add_parser("check-symbol")

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)
    amend = subparsers.add_parser("amend")
    amend.add_argument("--order-id", required=True)
    amend.add_argument("--price", type=float)
    amend.add_argument("--qty", type=float)
    subparsers.add_parser("open-orders")
    subparsers.add_parser("trigger-orders")
    subparsers.add_parser("cancel-all")
    subparsers.add_parser("close-all")

    for command in ("limit", "market"):
        cmd = subparsers.add_parser(command)
        cmd.add_argument("--side", choices=("buy", "sell"), required=True)
        cmd.add_argument("--qty", type=float, required=True)
        if command == "limit":
            cmd.add_argument("--price", type=float, required=True)

    trailing = subparsers.add_parser("trailing")
    trailing.add_argument("--side", choices=("buy", "sell"), required=True)
    trailing.add_argument("--qty", type=float, required=True)
    trailing.add_argument("--deviation", type=float, required=True)
    trailing.add_argument(
        "--unit",
        choices=("PERCENT", "QUOTE_CURRENCY"),
        default="PERCENT",
    )

    trailing_limit = subparsers.add_parser("trailing-limit")
    trailing_limit.add_argument("--side", choices=("buy", "sell"), required=True)
    trailing_limit.add_argument("--qty", type=float, required=True)
    trailing_limit.add_argument("--price", type=float, required=True)
    trailing_limit.add_argument("--deviation", type=float, required=True)
    trailing_limit.add_argument(
        "--unit",
        choices=("PERCENT", "QUOTE_CURRENCY"),
        default="PERCENT",
    )
    return parser


def build_adapter(symbol: str, environment: str):
    """Construire l'adapter Kraken a partir des variables d'environnement."""
    config = load_exchange_config("kraken", symbol=symbol, environment=environment)
    adapter_cls = get_adapter("kraken")
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
    adapter = build_adapter(args.symbol, args.environment)

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
        print_json(
            ack_to_payload(
                adapter.place_order(
                    side=args.side,
                    orderQty=args.qty,
                    type_="MARKET",
                )
            )
        )
        return 0
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
