from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from kolabi.bot.exchange_routes import (
    default_symbol_for_route,
    exchange_supports_market_type,
    unsupported_market_message,
)
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
    parser.add_argument(
        "--market-type",
        choices=("futures", "spot", "margin", "isolated_margin"),
        default="futures",
        help="Exchange market lane to smoke-test.",
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
        help="Environment variable name containing the exchange API key.",
    )
    parser.add_argument(
        "--api-secret-env",
        help="Environment variable name containing the exchange API secret.",
    )
    parser.add_argument(
        "--list-orders",
        action="store_true",
        help="List order types/plans tested for the selected exchange and exit.",
    )
    parser.add_argument(
        "--include-market",
        action="store_true",
        help="Include immediate market orders. Disabled by default for safer smoke tests.",
    )
    parser.add_argument(
        "--only",
        dest="only_orders",
        action="append",
        default=[],
        metavar="ORDER_NAME",
        help=(
            "Run only the named smoke order. Repeat or comma-separate names. "
            "Use --list-orders to see available names."
        ),
    )
    parser.add_argument(
        "--leave-open",
        action="store_true",
        help="Leave accepted smoke orders open instead of cancelling them after each ack.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=2.0, help="Pause between order submissions.")
    parser.add_argument("--log-level", default="INFO", help="Logging verbosity.")
    return parser


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
) -> Any:
    """Build the selected exchange adapter from environment credentials."""
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


def _env_scope_key(account_scope: str) -> str:
    return (account_scope.strip() or "default").upper().replace("-", "_")


def _kolabi_scoped_db_url(lane: str, account_scope: str) -> str | None:
    lane_key = lane.upper()
    if (account_scope.strip() or "default") == "default":
        return os.environ.get(f"KOLABI_{lane_key}_DB_URL")
    return os.environ.get(f"KOLABI_{_env_scope_key(account_scope)}_{lane_key}_DB_URL")


def _decorate_adapter_config(config: Any, *, account_scope: str) -> None:
    """Attach shared DB lanes used by direct smoke-test adapter calls."""

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


def extract_min_quantity(
    instrument: dict[str, Any],
    *,
    market_type: str = "futures",
) -> float:
    """Extract a minimum tradable quantity defensively."""
    for key in (
        "minimumQuantity",
        "minQuantity",
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
            quantity = float(value)
            if quantity <= 0:
                continue
            if market_type == "futures":
                return max(quantity, 1.0)
            return quantity
    if market_type == "futures":
        return 1.0
    raise RuntimeError(
        "Could not extract a safe minimum quantity for "
        f"market_type={market_type} symbol={instrument.get('symbol', '-')}"
    )


def smoke_quantity(
    instrument: dict[str, Any],
    reference_price: float,
    *,
    market_type: str = "futures",
    min_price_factor: float = 0.8,
) -> float:
    """Return a smoke quantity that satisfies min quantity and min notional."""

    quantity = extract_min_quantity(instrument, market_type=market_type)
    min_notional = _as_float(instrument.get("minNotional"))
    reference_floor = reference_price * min_price_factor
    if min_notional and reference_floor > 0:
        quantity = max(quantity, min_notional / reference_floor)
    step = _as_float(instrument.get("stepSize"))
    if step and step > 0:
        quantity = math.ceil(quantity / step) * step
    return quantity


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
    *,
    market_type: str = "futures",
    include_market: bool = False,
) -> list[SmokeOrder]:
    """Build a compact sweep of standard order kinds by exchange."""
    if not exchange_supports_market_type(exchange, market_type):
        reason = unsupported_market_message(exchange, market_type)
        raise ValueError(f"Market type '{market_type}' {reason} for exchange '{exchange}'")
    del symbol
    below = round(reference_price * 0.8, 2)
    above = round(reference_price * 1.2, 2)
    above_limit = round(reference_price * 1.205, 2)
    below_limit = round(reference_price * 0.795, 2)
    base_orders = [
        SmokeOrder("limit_below", "Limit", "buy", quantity, price=below),
    ]
    if include_market:
        base_orders.append(SmokeOrder("market_now", "Market", "buy", quantity))
    if exchange == "binance":
        orders = base_orders + [
            SmokeOrder("stop_market_above", "STOP", "buy", quantity, stop_price=above),
        ]
        if market_type != "futures":
            orders.append(
                SmokeOrder(
                    "stop_limit_above",
                    "SL",
                    "buy",
                    quantity,
                    price=above_limit,
                    stop_price=above,
                )
            )
        return orders
    if exchange == "bitmex":
        if market_type == "spot":
            return base_orders
        return base_orders + [
            SmokeOrder("stop_market_above", "Stop", "buy", quantity, stop_price=above),
            SmokeOrder(
                "stop_limit_above",
                "StopLimit",
                "buy",
                quantity,
                price=above_limit,
                stop_price=above,
            ),
            SmokeOrder(
                "market_if_touched_below",
                "MarketIfTouched",
                "buy",
                quantity,
                stop_price=below,
            ),
            SmokeOrder(
                "limit_if_touched_below",
                "LimitIfTouched",
                "buy",
                quantity,
                price=below_limit,
                stop_price=below,
            ),
        ]
    if exchange == "kraken" and market_type != "futures":
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


def selected_order_names(values: Sequence[str] | None) -> tuple[str, ...]:
    """Normalize repeated/comma-separated smoke order names."""

    if not values:
        return ()
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        for raw_name in str(value).split(","):
            name = raw_name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
    return tuple(names)


def filter_smoke_orders(
    orders: Sequence[SmokeOrder],
    only_orders: Sequence[str] | None,
) -> list[SmokeOrder]:
    """Return the requested smoke orders, preserving plan order."""

    requested = selected_order_names(only_orders)
    if not requested:
        return list(orders)
    available = {order.name for order in orders}
    missing = [name for name in requested if name not in available]
    if missing:
        raise ValueError(
            "Unknown smoke order(s) "
            f"{', '.join(missing)}. Available: {', '.join(sorted(available))}"
        )
    requested_set = set(requested)
    return [order for order in orders if order.name in requested_set]


def submit_one(
    adapter: Any,
    order: SmokeOrder,
    *,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Submit one smoke order and return a printable payload."""
    clordid = client_order_id or smoke_client_order_id(order.name)
    ack = adapter.place_order(
        side=order.side,
        orderQty=order.quantity,
        price=order.price,
        stopPx=order.stop_price,
        type_=order.order_type,
        clOrdID=clordid,
        trailingStopMaxDeviation=order.trailing_deviation,
        trailingStopDeviationUnit=order.trailing_unit,
    )
    payload = asdict(ack)
    payload["client_order_id"] = payload.get("client_order_id") or clordid
    payload["name"] = order.name
    payload["order_type"] = order.order_type
    return payload


def smoke_client_order_id(
    order_name: str,
    *,
    at: datetime | None = None,
) -> str:
    """Return a compact exchange-safe client id for smoke submissions."""

    stamp = (at or datetime.now(timezone.utc)).strftime("%y%m%d%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9]+", "", order_name).lower() or "order"
    if not slug[0].isalpha():
        slug = f"o{slug}"
    return f"H1sm{slug[:16]}-{stamp}"


def cancel_submitted_order(adapter: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Cancel a smoke order by the strongest identity returned by the adapter."""

    order_id = _payload_order_identity(payload)
    if order_id is None:
        raise RuntimeError(f"Cannot cancel smoke order without order identity: {payload}")
    ack = adapter.cancel_order(order_id)
    return asdict(ack)


def _payload_order_identity(payload: dict[str, Any]) -> str | None:
    for key in ("order_id", "client_order_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


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
    *,
    market_type: str = "futures",
    account_scope: str = "default",
    api_key_env: str | None = None,
    api_secret_env: str | None = None,
    base_url: str | None = None,
    include_market: bool = False,
    only_orders: Sequence[str] | None = None,
    cancel_after_submit: bool = True,
) -> int:
    """Run a compact smoke test against the selected exchange adapter."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("exchange_smoke")
    adapter = build_adapter(
        exchange,
        symbol,
        environment,
        market_type=market_type,
        account_scope=account_scope,
        api_key_env=api_key_env,
        api_secret_env=api_secret_env,
        base_url=base_url,
    )
    instrument = adapter_instrument(adapter, symbol)
    reference_price = extract_reference_price(adapter, symbol)
    quantity = smoke_quantity(
        instrument,
        reference_price,
        market_type=market_type,
    )
    logger.info(
        "smoke_start exchange=%s market_type=%s symbol=%s environment=%s account_scope=%s reference_price=%.4f quantity=%.4f",
        exchange,
        market_type,
        symbol,
        environment,
        account_scope,
        reference_price,
        quantity,
    )
    orders = build_smoke_orders(
        exchange,
        symbol,
        quantity,
        reference_price,
        market_type=market_type,
        include_market=include_market,
    )
    orders = filter_smoke_orders(orders, only_orders)
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
            if cancel_after_submit:
                try:
                    cancel_payload = cancel_submitted_order(adapter, payload)
                    logger.info("cancel_ack %s", cancel_payload)
                except Exception as exc:
                    logger.error("cancel_error name=%s detail=%s", order.name, exc)
                    had_error = True
        except Exception as exc:
            logger.error("error name=%s detail=%s", order.name, exc)
            had_error = True
        time.sleep(max(sleep_seconds, 0.0))
    logger.info("smoke_done symbol=%s had_error=%s", symbol, had_error)
    return 1 if had_error else 0


def list_smoke_orders(
    exchange: str,
    *,
    market_type: str = "futures",
    include_market: bool = False,
    only_orders: Sequence[str] | None = None,
) -> list[dict[str, object]]:
    """Return a deterministic order-plan view for CLI listing."""
    orders = build_smoke_orders(
        exchange=exchange,
        symbol="PI_XBTUSD",
        quantity=1.0,
        reference_price=100.0,
        market_type=market_type,
        include_market=include_market,
    )
    orders = filter_smoke_orders(orders, only_orders)
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
    symbol = getattr(args, "symbol", None) or default_symbol_for_route(
        args.exchange,
        args.market_type,
    )
    if args.list_orders:
        print(
            json.dumps(
                {
                    "exchange": args.exchange,
                    "market_type": args.market_type,
                    "symbol": symbol,
                    "account_scope": args.account_scope,
                    "include_market": args.include_market,
                    "only_orders": list(selected_order_names(args.only_orders)),
                    "cancel_after_submit": not args.leave_open,
                    "orders": list_smoke_orders(
                        args.exchange,
                        market_type=args.market_type,
                        include_market=args.include_market,
                        only_orders=args.only_orders,
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    return run_smoke(
        exchange=args.exchange,
        symbol=symbol,
        environment=args.environment,
        sleep_seconds=args.sleep_seconds,
        log_level=args.log_level,
        market_type=args.market_type,
        account_scope=args.account_scope,
        api_key_env=args.api_key_env,
        api_secret_env=args.api_secret_env,
        base_url=args.base_url,
        include_market=args.include_market,
        only_orders=args.only_orders,
        cancel_after_submit=not args.leave_open,
    )


if __name__ == "__main__":
    raise SystemExit(main())
