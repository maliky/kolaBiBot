from __future__ import annotations

from typing import Any, cast

import pytest
import requests
from kolabi.bargain.cli import build_parser, main
from kolabi.shared.core.models import OrderAck, Position


class DummyAdapter:
    def __init__(self) -> None:
        self.placed_orders: list[dict[str, Any]] = []
        self._position = Position(symbol="PI_XBTUSD", qty=0.0, entry_price=None)
        self._instrument = {"bidPrice": 100.0, "askPrice": 101.0}
        self._recent_trades: list[dict[str, object]] = []

    def list_instruments(self):
        return [
            {"symbol": "PI_XBTUSD", "tradeable": True},
            {"symbol": "PI_ADAUSD", "tradeable": True},
        ]

    def validate_symbol(self, symbol: str):
        if symbol == "PF_ADAUSD":
            raise ValueError(
                "Unknown Kraken Futures symbol 'PF_ADAUSD'. Did you mean 'PI_ADAUSD'?"
            )
        return {"symbol": symbol, "tradeable": True}

    def instrument(self, symbol: str):
        _ = symbol
        return self._instrument

    def get_balance(self) -> float:
        return 42.5

    def get_position(self) -> Position:
        return self._position

    def cancel_order(self, order_id: str) -> OrderAck:
        return OrderAck(order_id=order_id, status="Canceled")

    def amend_order(self, order_id: str, **params: float) -> OrderAck:
        self.placed_orders.append({"order_id": order_id, "type_": "AMEND", **params})
        return OrderAck(order_id=order_id, status="Replaced", price=params.get("price"))

    def open_orders(self):
        return [
            {"orderID": "OID-1", "symbol": "PI_XBTUSD"},
            {"orderID": "OID-2", "symbol": "PI_XBTUSD"},
        ]

    def live_open_orders(self):
        return [
            {"order_id": "OID-1", "symbol": "PI_XBTUSD", "order_type": "lmt"},
        ]

    def live_trigger_orders(self):
        return [
            {
                "order_id": "OID-2",
                "symbol": "PI_XBTUSD",
                "order_type": "stop",
                "stop_price": 75000.0,
            },
        ]

    def place_order(
        self,
        side: str,
        orderQty: float,
        price: float | None = None,
        stopPx: float | None = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        self.placed_orders.append(
            {
                "side": side,
                "orderQty": orderQty,
                "price": price,
                "stopPx": stopPx,
                "type_": type_,
                **params,
            }
        )
        if params.get("reduceOnly") and type_.upper() == "MARKET":
            current_qty = float(self._position.qty)
            signed_delta = -abs(orderQty) if side == "sell" else abs(orderQty)
            new_qty = current_qty + signed_delta
            if current_qty > 0 and side == "sell":
                new_qty = max(0.0, new_qty)
            elif current_qty < 0 and side == "buy":
                new_qty = min(0.0, new_qty)
            self._position = Position(
                symbol=self._position.symbol,
                qty=new_qty,
                entry_price=None if new_qty == 0.0 else self._position.entry_price,
            )
        return OrderAck(
            order_id="OID-1",
            status="New",
            price=price,
            orig_qty=orderQty,
            side=side,
        )

    def recent_trades(self):
        return list(self._recent_trades)


def test_balance_command_prints_available_margin(monkeypatch, capsys):
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: DummyAdapter(),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "balance"])

    assert exit_code == 0
    assert '"availableMargin": 42.5' in capsys.readouterr().out


def test_limit_command_prints_order_ack(monkeypatch, capsys):
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: DummyAdapter(),
    )

    exit_code = main(
        [
            "--symbol",
            "PI_XBTUSD",
            "--environment",
            "demo",
            "limit",
            "--side",
            "buy",
            "--qty",
            "2",
            "--price",
            "80000",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-1"' in output
    assert '"price": 80000.0' in output


def test_market_command_exits_nonzero_when_fill_not_observed(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(
        [
            "--symbol",
            "PI_XBTUSD",
            "--environment",
            "demo",
            "market",
            "--side",
            "buy",
            "--qty",
            "1",
        ]
    )

    assert exit_code == 2
    output = capsys.readouterr().out
    assert '"filled": false' in output
    assert '"reason": "no_fill_observed"' in output


def test_market_command_succeeds_when_recent_trade_matches_order_id(monkeypatch, capsys):
    adapter = DummyAdapter()
    adapter._recent_trades = [{"order_id": "OID-1", "size": 1.0}]
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(
        [
            "--symbol",
            "PI_XBTUSD",
            "--environment",
            "demo",
            "market",
            "--side",
            "buy",
            "--qty",
            "1",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"filled": true' in output
    assert '"reason": "recent_trades_match"' in output


def test_check_symbol_command_prints_validation(monkeypatch, capsys):
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: DummyAdapter(),
    )

    exit_code = main(["--symbol", "PI_ADAUSD", "--environment", "demo", "check-symbol"])

    assert exit_code == 0
    assert '"symbol": "PI_ADAUSD"' in capsys.readouterr().out


def test_amend_command_supports_price_and_quantity(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(
        [
            "--symbol",
            "PI_XBTUSD",
            "--environment",
            "demo",
            "amend",
            "--order-id",
            "OID-1",
            "--price",
            "80123",
            "--qty",
            "3",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"status": "Replaced"' in output
    assert adapter.placed_orders[-1]["price"] == 80123.0
    assert adapter.placed_orders[-1]["orderQty"] == 3.0


def test_cancel_all_command_cancels_open_orders(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "cancel-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-1"' in output
    assert '"order_id": "OID-2"' in output


def test_cancel_all_command_accepts_client_order_id_only(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        adapter,
        "live_open_orders",
        lambda: [{"client_order_id": "CID-1", "symbol": "PI_XBTUSD"}],
    )
    monkeypatch.setattr(
        adapter,
        "live_trigger_orders",
        lambda: [{"cliOrdId": "CID-2", "symbol": "PI_XBTUSD"}],
    )
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "cancel-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "CID-1"' in output
    assert '"order_id": "CID-2"' in output


def test_open_orders_command_prints_live_resting_orders(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "open-orders"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-1"' in output
    assert '"order_type": "lmt"' in output


def test_trigger_orders_command_prints_live_trigger_orders(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "trigger-orders"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-2"' in output
    assert '"stop_price": 75000.0' in output


def test_close_all_command_cancels_orders_and_closes_long_position(monkeypatch, capsys):
    adapter = DummyAdapter()
    adapter._position = Position(symbol="PI_XBTUSD", qty=3.0, entry_price=1.0)
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-1"' in output
    assert '"closed": true' in output
    assert adapter.placed_orders[-1]["type_"] == "MARKET"
    assert adapter.placed_orders[-1]["side"] == "sell"
    assert adapter.placed_orders[-1]["reduceOnly"] is True


def test_close_all_retries_with_reduce_only_market_when_still_open(
    monkeypatch, capsys
):
    adapter = DummyAdapter()
    adapter._position = Position(symbol="PI_XBTUSD", qty=-1.0, entry_price=1.0)
    first_close_attempt = True

    original_place_order = adapter.place_order

    def flaky_close(**kwargs):
        nonlocal first_close_attempt
        ack = original_place_order(**kwargs)
        if (
            kwargs.get("reduceOnly")
            and kwargs.get("type_") == "MARKET"
            and first_close_attempt
        ):
            first_close_attempt = False
            adapter._position = Position(symbol="PI_XBTUSD", qty=-1.0, entry_price=1.0)
        return ack

    monkeypatch.setattr(cast(Any, adapter), "place_order", flaky_close)
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"closed": true' in output
    assert '"attempts": 2' in output
    assert all(order["type_"] == "MARKET" for order in adapter.placed_orders)
    assert all(order["reduceOnly"] is True for order in adapter.placed_orders)
    assert all(order["price"] is None for order in adapter.placed_orders)


def test_close_all_reports_verification_timeout_without_traceback(monkeypatch, capsys):
    adapter = DummyAdapter()
    adapter._position = Position(symbol="PI_XBTUSD", qty=-1.0, entry_price=1.0)
    first_call = True

    def timeout_after_close() -> Position:
        nonlocal first_call
        if first_call:
            first_call = False
            return adapter._position
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(cast(Any, adapter), "get_position", timeout_after_close)
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment: adapter,
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"verification_error":' in output
    assert '"closed": false' in output


def test_top_level_help_lists_subcommand_descriptions():
    help_text = build_parser().format_help()
    assert "Show available margin." in help_text
    assert "Submit one limit order." in help_text
    assert "Cancel one order." in help_text


def test_subcommand_help_shows_description(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["balance", "--help"])
    output = capsys.readouterr().out
    assert "Show available margin for the selected exchange account." in output


def test_exchange_option_is_forwarded_to_build_adapter(monkeypatch):
    observed: dict[str, str] = {}

    def _build_adapter(exchange: str, symbol: str, environment: str):
        observed["exchange"] = exchange
        observed["symbol"] = symbol
        observed["environment"] = environment
        return DummyAdapter()

    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", _build_adapter)
    exit_code = main(
        [
            "--exchange",
            "binance",
            "--symbol",
            "BTCUSDT",
            "--environment",
            "demo",
            "balance",
        ]
    )
    assert exit_code == 0
    assert observed == {"exchange": "binance", "symbol": "BTCUSDT", "environment": "demo"}
