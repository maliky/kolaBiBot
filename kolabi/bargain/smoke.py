from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from kolabi.shared.config import load_exchange_config
from kolabi.shared.exchanges import get_adapter


@dataclass(frozen=True)
class SmokeOrder:
    """One smoke-order plan row."""

    name: str
    order_type: str
    side: str
    quantity: float
    price: float | None = None
    stop_price: float | None = None
    trailing_deviation: float | None = None
    trailing_unit: str | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build the smoke-test CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.bargain.smoke",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--exchange", choices=("kraken", "binance", "bitmex"), default="kraken", help="Target exchange adapter.")
    parser.add_argument("--symbol", default="PI_XBTUSD", help="Trading symbol / instrument id.")
    parser.add_argument("--environment", choices=("demo", "live"), default="demo", help="API environment.")
    parser.add_argument(
        "--list-orders",
        action="store_true",
        help="List order types/plans tested for the selected exchange and exit.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=2.0, help="Pause between order submissions.")
    parser.add_argument("--log-level", default="INFO", help="Logging verbosity.")
    return parser


def build_adapter(exchange: str, symbol: str, environment: str) -> Any:
    """Build the selected exchange adapter from environment credentials."""
    config = load_exchange_config(exchange, symbol=symbol, environment=environment)
    adapter_cls = get_adapter(exchange)
    return adapter_cls(
        api_key=config.api_key,
        api_secret=config.api_secret,
        base_url=config.base_url,
        symbol=config.symbol,
        **config.adapter_kwargs,
    )


def extract_min_quantity(instrument: dict[str, Any]) -> float:
    """Extract a minimum tradable quantity defensively."""
    for key in (
        "minimumQuantity",
        "minOrderSize",
        "minimumOrderSize",
        "minQty",
        "contractSize",
        "lotSize",
    ):
        value = instrument.get(key)
        if value not in (None, ""):
            if not isinstance(value, (int, float, str)):
                raise RuntimeError(f"Unsupported minimum quantity type for {key}: {type(value)!r}")
            return max(float(value), 1.0)
    return 1.0


def extract_reference_price(adapter: Any, symbol: str) -> float:
    """Use mark price first, then last price, then bid/ask midpoint."""
    instrument = adapter_instrument(adapter, symbol)
    mark = instrument.get("markPrice")
    if mark not in (None, ""):
        if not isinstance(mark, (int, float, str)):
            raise RuntimeError(f"Unsupported markPrice type: {type(mark)!r}")
        return float(mark)
    last = instrument.get("lastPrice")
    if last not in (None, ""):
        if not isinstance(last, (int, float, str)):
            raise RuntimeError(f"Unsupported lastPrice type: {type(last)!r}")
        return float(last)
    bid = _as_float(instrument.get("bidPrice")) or 0.0
    ask = _as_float(instrument.get("askPrice")) or 0.0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    raise RuntimeError(f"Could not extract a usable price for {symbol}")


def build_smoke_orders(
    exchange: str,
    symbol: str,
    quantity: float,
    reference_price: float,
) -> list[SmokeOrder]:
    """Build a compact sweep of standard order kinds by exchange."""
    del symbol
    below = round(reference_price * 0.8, 2)
    above = round(reference_price * 1.2, 2)
    above_limit = round(reference_price * 1.205, 2)
    below_limit = round(reference_price * 0.795, 2)
    base_orders = [
        SmokeOrder("limit_below", "Limit", "buy", quantity, price=below),
        SmokeOrder("market_now", "Market", "buy", quantity),
    ]
    if exchange == "binance":
        return base_orders + [
            SmokeOrder("stop_market_above", "STOP", "buy", quantity, stop_price=above),
            SmokeOrder(
                "stop_limit_above",
                "STOP",
                "buy",
                quantity,
                price=above_limit,
                stop_price=above,
            ),
        ]
    return base_orders + [
        SmokeOrder(
            "stop_loss_market_above", "StopLoss", "buy", quantity, stop_price=above
        ),
        SmokeOrder(
            "stop_loss_limit_above",
            "StopLossLimit",
            "buy",
            quantity,
            price=above_limit,
            stop_price=above,
        ),
        SmokeOrder(
            "take_profit_market_below", "TakeProfit", "buy", quantity, stop_price=below
        ),
        SmokeOrder(
            "take_profit_limit_below",
            "TakeProfitLimit",
            "buy",
            quantity,
            price=below_limit,
            stop_price=below,
        ),
        SmokeOrder(
            "trailing_stop_percent",
            "TrailingStop",
            "buy",
            quantity,
            trailing_deviation=20.0,
            trailing_unit="PERCENT",
        ),
        SmokeOrder(
            "trailing_stop_limit_percent",
            "TrailingStopLimit",
            "buy",
            quantity,
            price=above_limit,
            trailing_deviation=20.0,
            trailing_unit="PERCENT",
        ),
    ]


def submit_one(adapter: Any, order: SmokeOrder) -> dict[str, Any]:
    """Submit one smoke order and return a printable payload."""
    ack = adapter.place_order(
        side=order.side,
        orderQty=order.quantity,
        price=order.price,
        stopPx=order.stop_price,
        type_=order.order_type,
        trailingStopMaxDeviation=order.trailing_deviation,
        trailingStopDeviationUnit=order.trailing_unit,
    )
    payload = asdict(ack)
    payload["name"] = order.name
    payload["order_type"] = order.order_type
    return payload


def _as_float(value: object | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    return None


def adapter_instrument(adapter: Any, symbol: str) -> dict[str, object]:
    if hasattr(adapter, "instrument"):
        return dict(adapter.instrument(symbol))
    if hasattr(adapter, "validate_symbol"):
        return dict(adapter.validate_symbol(symbol))
    if hasattr(adapter, "filters"):
        return {
            "symbol": symbol,
            "minQty": getattr(adapter, "filters", {}).get("minQty"),
            "markPrice": _as_float(getattr(adapter, "client").get_symbol_ticker(symbol=symbol).get("price")),
        }
    raise RuntimeError(f"Adapter does not expose symbol metadata for {symbol}")


def run_smoke(
    exchange: str,
    symbol: str,
    environment: str,
    sleep_seconds: float,
    log_level: str,
) -> int:
    """Run a compact smoke test against the selected exchange adapter."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("exchange_smoke")
    adapter = build_adapter(exchange, symbol, environment)
    instrument = adapter_instrument(adapter, symbol)
    quantity = extract_min_quantity(instrument)
    reference_price = extract_reference_price(adapter, symbol)
    logger.info(
        "smoke_start exchange=%s symbol=%s environment=%s reference_price=%.4f quantity=%.4f",
        exchange,
        symbol,
        environment,
        reference_price,
        quantity,
    )
    orders = build_smoke_orders(exchange, symbol, quantity, reference_price)
    had_error = False
    for order in orders:
        logger.info(
            "submit name=%s type=%s side=%s qty=%.4f price=%s stop=%s trailing=%s/%s",
            order.name,
            order.order_type,
            order.side,
            order.quantity,
            order.price,
            order.stop_price,
            order.trailing_deviation,
            order.trailing_unit,
        )
        try:
            payload = submit_one(adapter, order)
            logger.info("ack %s", payload)
        except Exception as exc:
            logger.error("error name=%s detail=%s", order.name, exc)
            had_error = True
        time.sleep(max(sleep_seconds, 0.0))
    logger.info("smoke_done symbol=%s had_error=%s", symbol, had_error)
    return 1 if had_error else 0


def list_smoke_orders(exchange: str) -> list[dict[str, object]]:
    """Return a deterministic order-plan view for CLI listing."""
    orders = build_smoke_orders(exchange=exchange, symbol="PI_XBTUSD", quantity=1.0, reference_price=100.0)
    return [
        {
            "name": order.name,
            "order_type": order.order_type,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "stop_price": order.stop_price,
            "trailing_deviation": order.trailing_deviation,
            "trailing_unit": order.trailing_unit,
        }
        for order in orders
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the smoke-test runner."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_orders:
        print(
            json.dumps(
                {
                    "exchange": args.exchange,
                    "orders": list_smoke_orders(args.exchange),
                },
                sort_keys=True,
            )
        )
        return 0
    return run_smoke(
        exchange=args.exchange,
        symbol=args.symbol,
        environment=args.environment,
        sleep_seconds=args.sleep_seconds,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    raise SystemExit(main())
