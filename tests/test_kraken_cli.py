from __future__ import annotations

import json
from typing import Any

import pytest
from kolabi.bargain.cli import (
    _close_position,
    build_adapter,
    build_parser,
    main,
    permission_status_payload,
    route_matrix_payload,
)
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


class DirectCloseAdapter:
    def __init__(self, qty: float) -> None:
        self._position = Position(symbol="PI_XBTUSD", qty=qty, entry_price=1.0)
        self.placed_orders: list[dict[str, Any]] = []

    def get_position(self) -> Position:
        return self._position

    def place_order(self, side: str, orderQty: float, **params: Any) -> OrderAck:
        self.placed_orders.append({"side": side, "orderQty": orderQty, **params})
        self._position = Position(symbol="PI_XBTUSD", qty=0.0, entry_price=None)
        return OrderAck(order_id="OID-CLOSE", status="New", side=side, orig_qty=orderQty)


class DummyBotService:
    def __init__(
        self,
        *,
        cancelled: list[OrderAck] | None = None,
        close_ack: OrderAck | None = None,
        before_qty: float = 0.0,
        after_qty: float = 0.0,
        audit_errors: list[str] | None = None,
        cancel_errors: list[dict[str, str]] | None = None,
    ) -> None:
        self._cancelled = cancelled or []
        self._close_ack = close_ack
        self._before = Position(symbol="PI_XBTUSD", qty=before_qty, entry_price=1.0)
        self._after = Position(symbol="PI_XBTUSD", qty=after_qty, entry_price=None if after_qty == 0.0 else 1.0)
        self._audit_errors = audit_errors or []
        self._cancel_errors = cancel_errors or []

    def cancel_all_orders(self) -> list[OrderAck]:
        return list(self._cancelled)

    def close_all_orders(self) -> dict[str, object]:
        return {
            "cancelled": list(self._cancelled),
            "cancel_errors": list(self._cancel_errors),
            "close_ack": self._close_ack,
            "close_action": (
                "skipped_no_position"
                if float(self._before.qty) == 0.0
                else "submitted_reduce_only_market"
            ),
            "close_skipped_reason": (
                "no_position" if float(self._before.qty) == 0.0 else None
            ),
            "position_before": self._before,
            "position_after": self._after,
            "closed": float(self._after.qty) == 0.0,
            "audit_persistence_ok": not self._audit_errors,
            "audit_persistence_errors": list(self._audit_errors),
        }


def test_balance_command_prints_available_margin(monkeypatch, capsys):
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: DummyAdapter(),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "balance"])

    assert exit_code == 0
    assert '"availableMargin": 42.5' in capsys.readouterr().out


def test_limit_command_prints_order_ack(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: adapter,
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
    assert '"client_order_id": "H1smclilimit-' in output
    assert '"price": 80000.0' in output
    assert str(adapter.placed_orders[-1]["clOrdID"]).startswith("H1smclilimit-")


def test_market_command_exits_nonzero_when_fill_not_observed(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: adapter,
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
        lambda exchange, symbol, environment, **kwargs: adapter,
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
        lambda exchange, symbol, environment, **kwargs: DummyAdapter(),
    )

    exit_code = main(["--symbol", "PI_ADAUSD", "--environment", "demo", "check-symbol"])

    assert exit_code == 0
    assert '"symbol": "PI_ADAUSD"' in capsys.readouterr().out


def test_amend_command_supports_price_and_quantity(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: adapter,
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
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            cancelled=[OrderAck(order_id="OID-1", status="Canceled"), OrderAck(order_id="OID-2", status="Canceled")]
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "cancel-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-1"' in output
    assert '"order_id": "OID-2"' in output


def test_cancel_all_command_accepts_client_order_id_only(monkeypatch, capsys):
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            cancelled=[OrderAck(order_id="CID-1", status="Canceled"), OrderAck(order_id="CID-2", status="Canceled")]
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "cancel-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "CID-1"' in output
    assert '"order_id": "CID-2"' in output


def test_cancel_all_survives_live_open_orders_503_with_fallback(monkeypatch, capsys):
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            cancelled=[OrderAck(order_id="OID-FALLBACK", status="Canceled")]
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "cancel-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-FALLBACK"' in output


def test_open_orders_command_prints_live_resting_orders(monkeypatch, capsys):
    adapter = DummyAdapter()
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: adapter,
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
        lambda exchange, symbol, environment, **kwargs: adapter,
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "trigger-orders"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-2"' in output
    assert '"stop_price": 75000.0' in output


def test_direct_close_position_uses_reduce_only_for_futures() -> None:
    adapter = DirectCloseAdapter(qty=2.0)

    result = _close_position(adapter, market_type="futures")

    assert result is not None
    assert result["closed"] is True
    assert len(adapter.placed_orders) == 1
    assert adapter.placed_orders[0]["side"] == "sell"
    assert adapter.placed_orders[0]["orderQty"] == 2.0
    assert adapter.placed_orders[0]["type_"] == "MARKET"
    assert adapter.placed_orders[0]["reduceOnly"] is True
    assert str(adapter.placed_orders[0]["clOrdID"]).startswith("H1smcliclose-")


def test_direct_close_position_omits_reduce_only_for_margin() -> None:
    adapter = DirectCloseAdapter(qty=-3.0)

    result = _close_position(adapter, market_type="margin")

    assert result is not None
    assert result["closed"] is True
    assert len(adapter.placed_orders) == 1
    assert adapter.placed_orders[0]["side"] == "buy"
    assert adapter.placed_orders[0]["orderQty"] == 3.0
    assert adapter.placed_orders[0]["type_"] == "MARKET"
    assert str(adapter.placed_orders[0]["clOrdID"]).startswith("H1smcliclose-")
    assert "reduceOnly" not in adapter.placed_orders[0]


def test_close_all_command_cancels_orders_and_closes_long_position(monkeypatch, capsys):
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            cancelled=[OrderAck(order_id="OID-1", status="Canceled")],
            close_ack=OrderAck(order_id="OID-CLOSE", status="New", side="sell"),
            before_qty=3.0,
            after_qty=0.0,
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"order_id": "OID-1"' in output
    assert '"closed": true' in output
    assert '"order_id": "OID-CLOSE"' in output


def test_close_all_retries_with_reduce_only_market_when_still_open(
    monkeypatch, capsys
):
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            close_ack=OrderAck(order_id="OID-CLOSE", status="New", side="buy"),
            before_qty=-1.0,
            after_qty=0.0,
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"closed": true' in output
    assert '"closed": true' in output


def test_close_all_reports_verification_timeout_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            before_qty=-1.0,
            after_qty=-1.0,
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"closed": false' in output


def test_close_all_reports_no_position_as_explicit_skip(monkeypatch, capsys):
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            cancelled=[OrderAck(order_id="OID-1", status="Canceled")],
            before_qty=0.0,
            after_qty=0.0,
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"closed": true' in output
    assert '"close_action": "skipped_no_position"' in output
    assert '"close_skipped_reason": "no_position"' in output
    assert '"close_order": null' in output


def test_close_all_reports_audit_persistence_error_separately(monkeypatch, capsys):
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            cancelled=[OrderAck(order_id="OID-1", status="Canceled")],
            before_qty=0.0,
            after_qty=0.0,
            audit_errors=["audit write failed"],
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"closed": true' in output
    assert '"audit_persistence_ok": false' in output
    assert '"audit write failed"' in output


def test_close_all_survives_cancel_fetch_503_and_still_closes_position(monkeypatch, capsys):
    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", lambda exchange, symbol, environment, **kwargs: DummyAdapter())
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_bot_service",
        lambda exchange, symbol, environment, **kwargs: DummyBotService(
            cancelled=[],
            close_ack=OrderAck(order_id="OID-CLOSE", status="New", side="sell"),
            before_qty=2.0,
            after_qty=0.0,
        ),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "close-all"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"cancelled": []' in output
    assert '"closed": true' in output


def test_bargain_runtime_account_flags_parse_before_subcommand() -> None:
    args = build_parser().parse_args(
        [
            "--market-type",
            "spot",
            "--account-scope",
            "advers",
            "--api-key-env",
            "KRKF_DEMO2_API_KEY",
            "--api-secret-env",
            "KRKF_DEMO2_API_SECRET",
            "close-all",
        ]
    )

    assert args.market_type == "spot"
    assert args.account_scope == "advers"
    assert args.api_key_env == "KRKF_DEMO2_API_KEY"
    assert args.api_secret_env == "KRKF_DEMO2_API_SECRET"
    assert args.command == "close-all"


@pytest.mark.parametrize(
    ("exchange", "market_type", "expected_symbol"),
    [
        ("kraken", "futures", "PI_XBTUSD"),
        ("kraken", "spot", "XBT/USD"),
        ("binance", "spot", "BTCUSDT"),
        ("binance", "isolated_margin", "BTCUSDT"),
        ("bitmex", "futures", "XBTUSD"),
        ("bitmex", "spot", "XBT_USDT"),
    ],
)
def test_direct_cli_defaults_symbol_from_exchange_market_route(
    monkeypatch,
    capsys,
    exchange: str,
    market_type: str,
    expected_symbol: str,
) -> None:
    observed: dict[str, object] = {}

    def _build_adapter(
        exchange_name: str,
        symbol: str,
        environment: str,
        **kwargs: object,
    ) -> DummyAdapter:
        observed["exchange"] = exchange_name
        observed["symbol"] = symbol
        observed["environment"] = environment
        observed["kwargs"] = kwargs
        return DummyAdapter()

    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", _build_adapter)

    exit_code = main(
        [
            "--exchange",
            exchange,
            "--market-type",
            market_type,
            "--environment",
            "demo",
            "check-symbol",
        ]
    )

    assert exit_code == 0
    assert observed["exchange"] == exchange
    assert observed["symbol"] == expected_symbol
    assert observed["environment"] == "demo"
    assert observed["kwargs"] == {
        "market_type": market_type,
        "account_scope": "default",
        "api_key_env": None,
        "api_secret_env": None,
        "base_url": None,
    }
    assert f'"symbol": "{expected_symbol}"' in capsys.readouterr().out


def test_close_all_forwards_account_scope_and_key_envs_to_service(monkeypatch, capsys):
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: DummyAdapter(),
    )

    def _build_bot_service(exchange: str, symbol: str, environment: str, **kwargs):
        observed["exchange"] = exchange
        observed["symbol"] = symbol
        observed["environment"] = environment
        observed["kwargs"] = kwargs
        return DummyBotService(
            cancelled=[OrderAck(order_id="OID-ADV", status="Canceled")],
            before_qty=0.0,
            after_qty=0.0,
        )

    monkeypatch.setattr("kolabi.bargain.cli.build_bot_service", _build_bot_service)

    exit_code = main(
        [
            "--market-type",
            "spot",
            "--symbol",
            "PI_XBTUSD",
            "--environment",
            "demo",
            "--account-scope",
            "advers",
            "--api-key-env",
            "KRKF_DEMO2_API_KEY",
            "--api-secret-env",
            "KRKF_DEMO2_API_SECRET",
            "close-all",
        ]
    )

    assert exit_code == 0
    assert observed == {
        "exchange": "kraken",
        "symbol": "PI_XBTUSD",
        "environment": "demo",
        "kwargs": {
            "market_type": "spot",
            "account_scope": "advers",
            "api_key_env": "KRKF_DEMO2_API_KEY",
            "api_secret_env": "KRKF_DEMO2_API_SECRET",
        },
    }
    output = capsys.readouterr().out
    assert '"account_scope": "advers"' in output
    assert '"market_type": "spot"' in output
    assert '"order_id": "OID-ADV"' in output


def test_build_adapter_uses_scoped_kraken_credentials_and_db_lanes(monkeypatch):
    built: dict[str, object] = {}

    class CapturingAdapter:
        def __init__(self, **kwargs) -> None:
            built.update(kwargs)

    monkeypatch.setenv("KRKF_DEMO2_API_KEY", "adv-key")
    monkeypatch.setenv("KRKF_DEMO2_API_SECRET", "adv-secret")
    monkeypatch.setenv("KOLABI_MARKET_DB_URL", "postgresql://market")
    monkeypatch.setenv("KOLABI_ADVERS_ACCOUNT_DB_URL", "postgresql://account-advers")
    monkeypatch.setenv("KOLABI_ADVERS_AUDIT_DB_URL", "postgresql://audit-advers")
    observed_loader: dict[str, str] = {}

    def _get_adapter(exchange: str, market_type: str):
        observed_loader["exchange"] = exchange
        observed_loader["market_type"] = market_type
        return CapturingAdapter

    monkeypatch.setattr("kolabi.bargain.cli.get_adapter", _get_adapter)

    build_adapter(
        "kraken",
        "PI_XBTUSD",
        "demo",
        market_type="futures",
        account_scope="advers",
        api_key_env="KRKF_DEMO2_API_KEY",
        api_secret_env="KRKF_DEMO2_API_SECRET",
    )

    assert observed_loader == {"exchange": "kraken", "market_type": "futures"}
    assert built["api_key"] == "adv-key"
    assert built["api_secret"] == "adv-secret"
    assert built["account_scope"] == "advers"
    assert built["public_db_url"] == "postgresql://market"
    assert built["account_db_url"] == "postgresql://account-advers"
    assert built["audit_db_url"] == "postgresql://audit-advers"


def test_top_level_help_lists_subcommand_descriptions():
    help_text = build_parser().format_help()
    assert "Show available margin." in help_text
    assert "Submit one limit order." in help_text
    assert "Cancel one order." in help_text
    assert "Check API key order-write permission." in help_text
    assert "List supported route codes." in help_text


def test_subcommand_help_shows_description(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["balance", "--help"])
    output = capsys.readouterr().out
    assert "Show available margin for the selected exchange account." in output


def test_routes_command_prints_route_matrix_without_building_adapter(
    monkeypatch,
    capsys,
) -> None:
    for name in (
        "BINS_DEMO_API_KEY",
        "BINS_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    def _fail_build_adapter(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("routes command must not build an exchange adapter")

    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", _fail_build_adapter)

    exit_code = main(["--environment", "demo", "routes"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    routes = {
        (row["exchange"], row["market_type"]): row
        for row in payload["routes"]
    }
    assert payload["environment"] == "demo"
    assert routes[("binance", "spot")]["codes"] == ["BINS"]
    assert routes[("binance", "spot")]["api_key_env"] == [
        "BINS_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_KEY",
    ]
    assert routes[("binance", "spot")]["api_key_present"] is False
    assert routes[("binance", "spot")]["api_secret_present"] is False
    assert routes[("binance", "spot")]["credentials_present"] is False
    assert routes[("binance", "spot")]["api_key_source"] is None
    assert routes[("binance", "spot")]["api_secret_source"] is None
    assert routes[("binance", "spot")]["permission_probe"] == "test_order"
    assert routes[("binance", "spot")]["order_write_probe"] is True
    assert routes[("binance", "spot")]["demo_requires_base_url_override"] is False
    assert routes[("binance", "margin")]["permission_probe"] == "not_supported"
    assert routes[("binance", "margin")]["order_write_probe"] is False
    assert routes[("binance", "margin")]["demo_requires_base_url_override"] is True
    assert routes[("kraken", "margin")]["codes"] == ["KRKM"]
    assert routes[("kraken", "margin")]["permission_probe"] == "not_supported"
    assert routes[("kraken", "margin")]["demo_requires_base_url_override"] is True
    assert routes[("bitmex", "futures")]["codes"] == ["BMX", "BMXF", "BTX", "BTXF"]
    assert routes[("bitmex", "futures")]["permission_probe"] == "apiKey"
    assert routes[("bitmex", "futures")]["order_write_probe"] is True
    assert routes[("bitmex", "spot")]["default_symbol"] == "XBT_USDT"
    assert routes[("bitmex", "spot")]["api_secret_env"] == [
        "BTX_DEMO_API_SECRET",
        "BITMEX_TEST_SECRET",
    ]


def test_route_matrix_payload_uses_live_env_names() -> None:
    routes = {
        (row["exchange"], row["market_type"]): row
        for row in route_matrix_payload("live", env={})["routes"]
    }

    assert routes[("kraken", "futures")]["api_key_env"] == [
        "KRKF_API_KEY",
        "KRAKEN_FUTURE_API_KEY",
    ]
    assert routes[("binance", "isolated_margin")]["api_secret_env"] == [
        "BINI_API_SECRET",
        "BINM_API_SECRET",
        "BINANCE_MARGIN_API_SECRET",
    ]


def test_route_matrix_payload_marks_present_credentials_without_values() -> None:
    payload = route_matrix_payload(
        "demo",
        env={
            "BINS_DEMO_API_KEY": "spot-key",
            "BINS_DEMO_API_SECRET": "spot-secret",
            "BTX_DEMO_API_KEY": "cancel-key",
        },
    )
    routes = {
        (row["exchange"], row["market_type"]): row
        for row in payload["routes"]
    }

    binance_spot = routes[("binance", "spot")]
    assert binance_spot["api_key_present"] is True
    assert binance_spot["api_secret_present"] is True
    assert binance_spot["credentials_present"] is True
    assert binance_spot["api_key_source"] == "BINS_DEMO_API_KEY"
    assert binance_spot["api_secret_source"] == "BINS_DEMO_API_SECRET"
    assert "spot-key" not in str(payload)
    assert "spot-secret" not in str(payload)

    bitmex_futures = routes[("bitmex", "futures")]
    assert bitmex_futures["api_key_present"] is True
    assert bitmex_futures["api_secret_present"] is False
    assert bitmex_futures["credentials_present"] is False
    assert bitmex_futures["api_key_source"] == "BTX_DEMO_API_KEY"
    assert bitmex_futures["api_secret_source"] is None


def test_check_symbol_command_prints_adapter_metadata(monkeypatch, capsys):
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: DummyAdapter(),
    )

    exit_code = main(["--symbol", "PI_XBTUSD", "--environment", "demo", "check-symbol"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"symbol": "PI_XBTUSD"' in output
    assert '"tradeable": true' in output


def test_permissions_command_prints_supported_adapter_status(
    monkeypatch,
    capsys,
) -> None:
    class PermissionAdapter:
        def permission_status(self) -> dict[str, object]:
            return {
                "exchange": "bitmex",
                "market_type": "futures",
                "symbol": "XBTUSD",
                "permission_probe": "apiKey",
                "can_place_orders": False,
                "permissions": ["orderCancel"],
                "reason": "missing_order_write_permission",
            }

    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: PermissionAdapter(),
    )

    exit_code = main(
        [
            "--exchange",
            "bitmex",
            "--market-type",
            "futures",
            "--environment",
            "demo",
            "permissions",
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert '"can_place_orders": false' in output
    assert '"permissions": ["orderCancel"]' in output
    assert '"environment": "demo"' in output


def test_permissions_command_returns_zero_when_probe_allows_orders(
    monkeypatch,
    capsys,
) -> None:
    class PermissionAdapter:
        def permission_status(self) -> dict[str, object]:
            return {
                "exchange": "binance",
                "market_type": "spot",
                "symbol": "BTCUSDT",
                "permission_probe": "test_order",
                "can_place_orders": True,
                "reason": "ok",
            }

    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: PermissionAdapter(),
    )

    exit_code = main(
        [
            "--exchange",
            "binance",
            "--market-type",
            "spot",
            "--environment",
            "demo",
            "permissions",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"can_place_orders": true' in output
    assert '"permission_probe": "test_order"' in output


@pytest.mark.parametrize(
    ("exchange", "market_type", "symbol"),
    [
        ("kraken", "futures", "PI_XBTUSD"),
        ("kraken", "spot", "XBT/USD"),
        ("binance", "margin", "BTCUSDT"),
        ("binance", "isolated_margin", "BTCUSDT"),
    ],
)
def test_permissions_command_reports_unsupported_route_without_building_adapter(
    monkeypatch,
    capsys,
    exchange: str,
    market_type: str,
    symbol: str,
) -> None:
    def _fail_build_adapter(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unsupported permissions probe must not build an adapter")

    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", _fail_build_adapter)

    exit_code = main(
        [
            "--exchange",
            exchange,
            "--market-type",
            market_type,
            "--environment",
            "demo",
            "permissions",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "exchange": exchange,
        "market_type": market_type,
        "symbol": symbol,
        "permission_probe": "not_supported",
        "can_place_orders": None,
        "reason": "adapter does not expose a no-order permission probe",
        "environment": "demo",
    }


def test_permission_status_payload_reports_unsupported_probe() -> None:
    payload = permission_status_payload(
        DummyAdapter(),
        exchange="kraken",
        market_type="futures",
        symbol="PI_XBTUSD",
        environment="demo",
    )

    assert payload == {
        "exchange": "kraken",
        "market_type": "futures",
        "symbol": "PI_XBTUSD",
        "permission_probe": "not_supported",
        "can_place_orders": None,
        "reason": "adapter does not expose a no-order permission probe",
        "environment": "demo",
    }


def test_instruments_command_prints_filtered_adapter_symbols(monkeypatch, capsys):
    monkeypatch.setattr(
        "kolabi.bargain.cli.build_adapter",
        lambda exchange, symbol, environment, **kwargs: DummyAdapter(),
    )

    exit_code = main(
        [
            "--symbol",
            "PI_XBTUSD",
            "--environment",
            "demo",
            "instruments",
            "--contains",
            "ADA",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "PI_ADAUSD" in output
    assert "PI_XBTUSD" not in output


def test_exchange_and_market_type_options_are_forwarded_to_build_adapter(monkeypatch):
    observed: dict[str, str] = {}

    def _build_adapter(exchange: str, symbol: str, environment: str, **kwargs):
        observed["exchange"] = exchange
        observed["symbol"] = symbol
        observed["environment"] = environment
        observed["market_type"] = str(kwargs.get("market_type"))
        observed["base_url"] = str(kwargs.get("base_url"))
        return DummyAdapter()

    monkeypatch.setattr("kolabi.bargain.cli.build_adapter", _build_adapter)
    exit_code = main(
        [
            "--exchange",
            "binance",
            "--market-type",
            "spot",
            "--symbol",
            "BTCUSDT",
            "--environment",
            "demo",
            "--base-url",
            "https://spot-demo.example.test",
            "balance",
        ]
    )
    assert exit_code == 0
    assert observed == {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "environment": "demo",
        "market_type": "spot",
        "base_url": "https://spot-demo.example.test",
    }


def test_bargain_market_type_reaches_adapter_loader(monkeypatch) -> None:
    built: dict[str, object] = {}
    observed_loader: dict[str, str] = {}

    class CapturingAdapter:
        def __init__(self, **kwargs) -> None:
            built.update(kwargs)

    def _get_adapter(exchange: str, market_type: str):
        observed_loader["exchange"] = exchange
        observed_loader["market_type"] = market_type
        return CapturingAdapter

    monkeypatch.setenv("BINS_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINS_DEMO_API_SECRET", "spot-secret")
    monkeypatch.setattr("kolabi.bargain.cli.get_adapter", _get_adapter)

    build_adapter("binance", "BTCUSDT", "demo", market_type="spot")

    assert observed_loader == {"exchange": "binance", "market_type": "spot"}
    assert built["api_key"] == "spot-key"
    assert built["api_secret"] == "spot-secret"
    assert built["symbol"] == "BTCUSDT"
    assert built["market_type"] == "spot"


def test_build_adapter_accepts_base_url_override_for_binance_margin_demo(
    monkeypatch,
) -> None:
    built: dict[str, object] = {}

    class CapturingAdapter:
        def __init__(self, **kwargs) -> None:
            built.update(kwargs)

    monkeypatch.setenv("BINM_DEMO_API_KEY", "margin-key")
    monkeypatch.setenv("BINM_DEMO_API_SECRET", "margin-secret")
    monkeypatch.setattr(
        "kolabi.bargain.cli.get_adapter",
        lambda _exchange, _market_type: CapturingAdapter,
    )

    build_adapter(
        "binance",
        "BTCUSDT",
        "demo",
        market_type="margin",
        base_url="https://margin-demo.example.test",
    )

    assert built["api_key"] == "margin-key"
    assert built["api_secret"] == "margin-secret"
    assert built["base_url"] == "https://margin-demo.example.test"
    assert built["market_type"] == "margin"


@pytest.mark.parametrize(
    ("exchange", "market_type", "symbol", "key_env", "secret_env"),
    [
        (
            "binance",
            "spot",
            "BTCUSDT",
            "BINS_DEMO_API_KEY",
            "BINS_DEMO_API_SECRET",
        ),
        (
            "bitmex",
            "futures",
            "XBTUSD",
            "BTX_DEMO_API_KEY",
            "BTX_DEMO_API_SECRET",
        ),
    ],
)
def test_build_adapter_decorates_non_kraken_db_lanes(
    monkeypatch,
    exchange: str,
    market_type: str,
    symbol: str,
    key_env: str,
    secret_env: str,
) -> None:
    built: dict[str, object] = {}

    class CapturingAdapter:
        def __init__(self, **kwargs) -> None:
            built.update(kwargs)

    monkeypatch.setenv(key_env, "api-key")
    monkeypatch.setenv(secret_env, "api-secret")
    monkeypatch.setenv("KOLABI_MARKET_DB_URL", "postgresql://market")
    monkeypatch.setenv("KOLABI_ADVERS_ACCOUNT_DB_URL", "postgresql://account-advers")
    monkeypatch.setenv("KOLABI_ADVERS_AUDIT_DB_URL", "postgresql://audit-advers")
    monkeypatch.setattr(
        "kolabi.bargain.cli.get_adapter",
        lambda _exchange, _market_type: CapturingAdapter,
    )

    build_adapter(
        exchange,
        symbol,
        "demo",
        market_type=market_type,
        account_scope="advers",
    )

    assert built["api_key"] == "api-key"
    assert built["api_secret"] == "api-secret"
    assert built["symbol"] == symbol
    assert built["account_scope"] == "advers"
    assert built["public_db_url"] == "postgresql://market"
    assert built["account_db_url"] == "postgresql://account-advers"
    assert built["audit_db_url"] == "postgresql://audit-advers"
