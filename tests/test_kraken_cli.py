from __future__ import annotations

from typing import Any

import pytest
from kolabi.bargain.cli import build_adapter, build_parser, main
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
            "--account-scope",
            "advers",
            "--api-key-env",
            "KRAKEN_FUTURE_DEMO2_API_KEY",
            "--api-secret-env",
            "KRAKEN_FUTURE_DEMO2_API_SECRET",
            "close-all",
        ]
    )

    assert args.account_scope == "advers"
    assert args.api_key_env == "KRAKEN_FUTURE_DEMO2_API_KEY"
    assert args.api_secret_env == "KRAKEN_FUTURE_DEMO2_API_SECRET"
    assert args.command == "close-all"


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
            "--symbol",
            "PI_XBTUSD",
            "--environment",
            "demo",
            "--account-scope",
            "advers",
            "--api-key-env",
            "KRAKEN_FUTURE_DEMO2_API_KEY",
            "--api-secret-env",
            "KRAKEN_FUTURE_DEMO2_API_SECRET",
            "close-all",
        ]
    )

    assert exit_code == 0
    assert observed == {
        "exchange": "kraken",
        "symbol": "PI_XBTUSD",
        "environment": "demo",
        "kwargs": {
            "account_scope": "advers",
            "api_key_env": "KRAKEN_FUTURE_DEMO2_API_KEY",
            "api_secret_env": "KRAKEN_FUTURE_DEMO2_API_SECRET",
        },
    }
    output = capsys.readouterr().out
    assert '"account_scope": "advers"' in output
    assert '"order_id": "OID-ADV"' in output


def test_build_adapter_uses_scoped_kraken_credentials_and_db_lanes(monkeypatch):
    built: dict[str, object] = {}

    class CapturingAdapter:
        def __init__(self, **kwargs) -> None:
            built.update(kwargs)

    monkeypatch.setenv("KRAKEN_FUTURE_DEMO2_API_KEY", "adv-key")
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO2_API_SECRET", "adv-secret")
    monkeypatch.setenv("KOLABI_MARKET_DB_URL", "postgresql://market")
    monkeypatch.setenv("KOLABI_ADVERS_ACCOUNT_DB_URL", "postgresql://account-advers")
    monkeypatch.setenv("KOLABI_ADVERS_AUDIT_DB_URL", "postgresql://audit-advers")
    monkeypatch.setattr("kolabi.bargain.cli.get_adapter", lambda exchange: CapturingAdapter)

    build_adapter(
        "kraken",
        "PI_XBTUSD",
        "demo",
        account_scope="advers",
        api_key_env="KRAKEN_FUTURE_DEMO2_API_KEY",
        api_secret_env="KRAKEN_FUTURE_DEMO2_API_SECRET",
    )

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


def test_subcommand_help_shows_description(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["balance", "--help"])
    output = capsys.readouterr().out
    assert "Show available margin for the selected exchange account." in output


def test_exchange_option_is_forwarded_to_build_adapter(monkeypatch):
    observed: dict[str, str] = {}

    def _build_adapter(exchange: str, symbol: str, environment: str, **kwargs):
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
