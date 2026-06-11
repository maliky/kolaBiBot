from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kolabi.bargain import smoke
from kolabi.bargain.smoke import (
    SmokeOrder,
    build_adapter,
    build_smoke_orders,
    extract_min_quantity,
    filter_smoke_orders,
    smoke_client_order_id,
    smoke_quantity,
    submit_one,
)
from kolabi.shared.core.models import OrderAck


def test_extract_min_quantity_falls_back_to_one_contract():
    assert extract_min_quantity({"symbol": "PI_XBTUSD"}) == 1.0


def test_extract_min_quantity_keeps_spot_subunit_minimum():
    assert (
        extract_min_quantity(
            {"symbol": "XBT/USD", "minQuantity": "0.0001"},
            market_type="spot",
        )
        == 0.0001
    )


def test_extract_min_quantity_keeps_margin_subunit_minimum():
    assert (
        extract_min_quantity(
            {"symbol": "XBT/USD", "minQty": "0.0002"},
            market_type="margin",
        )
        == 0.0002
    )


def test_extract_min_quantity_refuses_missing_spot_minimum():
    try:
        extract_min_quantity({"symbol": "XBT/USD"}, market_type="spot")
    except RuntimeError as exc:
        assert "safe minimum quantity" in str(exc)
    else:
        raise AssertionError("expected missing spot minimum to fail")


def test_smoke_quantity_uses_min_notional_and_step_size():
    assert (
        smoke_quantity(
            {
                "symbol": "BTCUSDT",
                "minQuantity": "0.00001",
                "minNotional": "5",
                "stepSize": "0.00001",
            },
            reference_price=62500.0,
            market_type="spot",
        )
        == 0.0001
    )


def test_smoke_client_order_id_is_exchange_safe() -> None:
    clordid = smoke_client_order_id(
        "trailing_stop_limit_percent",
        at=datetime(2026, 6, 10, 12, 0, 1, tzinfo=timezone.utc),
    )

    assert clordid == "H1smtrailingstoplimi-260610120001"
    assert len(clordid) <= 36


def test_submit_one_forwards_client_order_id_to_adapter() -> None:
    class FakeAdapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def place_order(self, **kwargs):
            self.calls.append(dict(kwargs))
            return OrderAck(
                order_id="",
                client_order_id=kwargs.get("clOrdID"),
                status="New",
            )

    adapter = FakeAdapter()
    order = SmokeOrder("limit_below", "Limit", "buy", 1.0, price=90.0)

    payload = submit_one(
        adapter,
        order,
        client_order_id="H1smlimitbelow-260610120000",
    )

    assert adapter.calls == [
        {
            "side": "buy",
            "orderQty": 1.0,
            "price": 90.0,
            "stopPx": None,
            "type_": "Limit",
            "clOrdID": "H1smlimitbelow-260610120000",
            "trailingStopMaxDeviation": None,
            "trailingStopDeviationUnit": None,
        }
    ]
    assert payload["client_order_id"] == "H1smlimitbelow-260610120000"
    assert payload["name"] == "limit_below"


def test_build_smoke_orders_covers_standard_order_sweep():
    orders = build_smoke_orders(
        "kraken",
        "PI_XBTUSD",
        quantity=1.0,
        reference_price=100.0,
        include_market=True,
    )
    order_types = {order.order_type for order in orders}

    assert "Limit" in order_types
    assert "Market" in order_types
    assert "StopLoss" in order_types
    assert "StopLossLimit" in order_types
    assert "TakeProfit" in order_types
    assert "TakeProfitLimit" in order_types
    assert "TrailingStop" in order_types
    assert "TrailingStopLimit" in order_types


def test_build_smoke_orders_binance_uses_exchange_safe_subset():
    orders = build_smoke_orders("binance", "BTCUSDT", quantity=1.0, reference_price=100.0)
    order_types = {order.order_type for order in orders}
    assert "Limit" in order_types
    assert "Market" not in order_types
    assert "STOP" in order_types
    assert "SL" not in order_types


def test_build_smoke_orders_binance_spot_includes_stop_limit():
    orders = build_smoke_orders(
        "binance",
        "BTCUSDT",
        quantity=1.0,
        reference_price=100.0,
        market_type="spot",
    )
    order_types = {order.order_type for order in orders}
    assert {"Limit", "STOP", "SL"} <= order_types
    assert "Market" not in order_types


def test_build_smoke_orders_kraken_spot_excludes_futures_only_orders():
    orders = build_smoke_orders(
        "kraken",
        "XBT/USD",
        quantity=1.0,
        reference_price=100.0,
        market_type="spot",
    )
    order_types = {order.order_type for order in orders}
    assert {"Limit", "StopLoss", "StopLossLimit"} <= order_types
    assert "Market" not in order_types
    assert "TakeProfit" not in order_types
    assert "TrailingStop" not in order_types


def test_build_smoke_orders_bitmex_futures_uses_native_adapter_names():
    orders = build_smoke_orders(
        "bitmex",
        "XBTUSD",
        quantity=1.0,
        reference_price=100.0,
    )
    order_types = {order.order_type for order in orders}
    assert {"Limit", "Stop", "StopLimit"} <= order_types
    assert "Market" not in order_types
    assert "StopLoss" not in order_types


def test_build_smoke_orders_bitmex_spot_uses_resting_subset():
    orders = build_smoke_orders(
        "bitmex",
        "BMEXUSDT",
        quantity=1.0,
        reference_price=100.0,
        market_type="spot",
    )
    order_types = {order.order_type for order in orders}
    assert order_types == {"Limit"}


def test_build_smoke_orders_can_include_market_orders_explicitly():
    orders = build_smoke_orders(
        "binance",
        "BTCUSDT",
        quantity=1.0,
        reference_price=100.0,
        market_type="spot",
        include_market=True,
    )
    order_types = {order.order_type for order in orders}
    assert "Market" in order_types


@pytest.mark.parametrize(
    ("exchange", "market_type", "expected_types"),
    [
        ("kraken", "futures", {"Limit", "StopLoss", "StopLossLimit"}),
        ("kraken", "spot", {"Limit", "StopLoss", "StopLossLimit"}),
        ("kraken", "margin", {"Limit", "StopLoss", "StopLossLimit"}),
        ("binance", "futures", {"Limit", "STOP"}),
        ("binance", "spot", {"Limit", "STOP", "SL"}),
        ("binance", "margin", {"Limit", "STOP", "SL"}),
        ("binance", "isolated_margin", {"Limit", "STOP", "SL"}),
        ("bitmex", "futures", {"Limit", "Stop", "StopLimit"}),
        ("bitmex", "spot", {"Limit"}),
    ],
)
def test_build_smoke_orders_covers_supported_route_matrix(
    exchange: str,
    market_type: str,
    expected_types: set[str],
) -> None:
    orders = build_smoke_orders(
        exchange,
        "BTCUSDT",
        quantity=1.0,
        reference_price=100.0,
        market_type=market_type,
    )

    order_types = {order.order_type for order in orders}
    assert expected_types <= order_types
    assert "Market" not in order_types


def test_build_smoke_orders_rejects_unsupported_market_type():
    try:
        build_smoke_orders(
            "bitmex",
            "XBTUSD",
            quantity=1.0,
            reference_price=100.0,
            market_type="margin",
    )
    except ValueError as exc:
        assert "only supported for Binance or Kraken" in str(exc)
    else:
        raise AssertionError("expected unsupported market type to fail")


def test_filter_smoke_orders_accepts_repeated_and_comma_names() -> None:
    orders = [
        SmokeOrder("limit_below", "Limit", "buy", 1.0),
        SmokeOrder("stop_above", "Stop", "buy", 1.0),
        SmokeOrder("other", "Limit", "sell", 1.0),
    ]

    filtered = filter_smoke_orders(
        orders,
        ["stop_above,limit_below", "limit_below"],
    )

    assert [order.name for order in filtered] == ["limit_below", "stop_above"]


def test_filter_smoke_orders_rejects_unknown_name() -> None:
    orders = [SmokeOrder("limit_below", "Limit", "buy", 1.0)]

    try:
        filter_smoke_orders(orders, ["missing"])
    except ValueError as exc:
        assert "Unknown smoke order(s) missing" in str(exc)
        assert "limit_below" in str(exc)
    else:
        raise AssertionError("expected unknown smoke order to fail")


def test_run_smoke_returns_nonzero_on_submission_error(monkeypatch):
    monkeypatch.setattr(smoke, "build_adapter", lambda *args, **kwargs: object())
    monkeypatch.setattr(smoke, "adapter_instrument", lambda *args, **kwargs: {"minQuantity": 1})
    monkeypatch.setattr(smoke, "extract_reference_price", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(
        smoke,
        "build_smoke_orders",
        lambda *args, **kwargs: [
            SmokeOrder("ok", "Limit", "buy", 1.0, price=90.0),
            SmokeOrder("bad", "Market", "buy", 1.0),
        ],
    )

    def _submit(_adapter, order):
        if order.name == "bad":
            raise RuntimeError("boom")
        return {"name": order.name}

    monkeypatch.setattr(smoke, "submit_one", _submit)
    monkeypatch.setattr(smoke.time, "sleep", lambda *_: None)

    assert (
        smoke.run_smoke(
            "kraken",
            "PI_XBTUSD",
            "demo",
            0.0,
            "INFO",
            cancel_after_submit=False,
        )
        == 1
    )


def test_run_smoke_passes_market_type_to_quantity_extraction(monkeypatch):
    observed: dict[str, str] = {}
    monkeypatch.setattr(smoke, "build_adapter", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        smoke,
        "adapter_instrument",
        lambda *args, **kwargs: {"symbol": "XBT/USD", "minQuantity": "0.0001"},
    )
    monkeypatch.setattr(smoke, "extract_reference_price", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(
        smoke,
        "build_smoke_orders",
        lambda *args, **kwargs: [SmokeOrder("ok", "Limit", "buy", 0.0001, price=90.0)],
    )
    monkeypatch.setattr(smoke, "submit_one", lambda _adapter, order: {"name": order.name})
    monkeypatch.setattr(smoke.time, "sleep", lambda *_: None)

    def _extract_quantity(_instrument, *, market_type: str):
        observed["market_type"] = market_type
        return 0.0001

    monkeypatch.setattr(smoke, "extract_min_quantity", _extract_quantity)

    assert (
        smoke.run_smoke(
            "kraken",
            "XBT/USD",
            "live",
            0.0,
            "INFO",
            market_type="spot",
            cancel_after_submit=False,
        )
        == 0
    )
    assert observed == {"market_type": "spot"}


def test_run_smoke_cancels_accepted_orders_by_default(monkeypatch):
    class FakeAdapter:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

    adapter = FakeAdapter()
    monkeypatch.setattr(smoke, "build_adapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(smoke, "adapter_instrument", lambda *args, **kwargs: {"minQuantity": 1})
    monkeypatch.setattr(smoke, "extract_reference_price", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(
        smoke,
        "build_smoke_orders",
        lambda *args, **kwargs: [SmokeOrder("ok", "Limit", "buy", 1.0, price=90.0)],
    )
    monkeypatch.setattr(
        smoke,
        "submit_one",
        lambda _adapter, order: {"name": order.name, "order_id": "OID-1"},
    )
    monkeypatch.setattr(smoke.time, "sleep", lambda *_: None)

    assert smoke.run_smoke("kraken", "PI_XBTUSD", "demo", 0.0, "INFO") == 0
    assert adapter.cancelled == ["OID-1"]


def test_run_smoke_filters_to_named_order(monkeypatch):
    submitted: list[str] = []
    monkeypatch.setattr(smoke, "build_adapter", lambda *args, **kwargs: object())
    monkeypatch.setattr(smoke, "adapter_instrument", lambda *args, **kwargs: {"minQuantity": 1})
    monkeypatch.setattr(smoke, "extract_reference_price", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(
        smoke,
        "build_smoke_orders",
        lambda *args, **kwargs: [
            SmokeOrder("limit_below", "Limit", "buy", 1.0, price=90.0),
            SmokeOrder("stop_above", "Stop", "buy", 1.0, stop_price=110.0),
        ],
    )

    def _submit(_adapter, order):
        submitted.append(order.name)
        return {"name": order.name, "order_id": f"OID-{order.name}"}

    monkeypatch.setattr(smoke, "submit_one", _submit)
    monkeypatch.setattr(smoke.time, "sleep", lambda *_: None)

    assert (
        smoke.run_smoke(
            "kraken",
            "PI_XBTUSD",
            "demo",
            0.0,
            "INFO",
            only_orders=["stop_above"],
            cancel_after_submit=False,
        )
        == 0
    )
    assert submitted == ["stop_above"]


def test_run_smoke_can_leave_orders_open(monkeypatch):
    class FakeAdapter:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

    adapter = FakeAdapter()
    monkeypatch.setattr(smoke, "build_adapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(smoke, "adapter_instrument", lambda *args, **kwargs: {"minQuantity": 1})
    monkeypatch.setattr(smoke, "extract_reference_price", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(
        smoke,
        "build_smoke_orders",
        lambda *args, **kwargs: [SmokeOrder("ok", "Limit", "buy", 1.0, price=90.0)],
    )
    monkeypatch.setattr(
        smoke,
        "submit_one",
        lambda _adapter, order: {"name": order.name, "order_id": "OID-1"},
    )
    monkeypatch.setattr(smoke.time, "sleep", lambda *_: None)

    assert (
        smoke.run_smoke(
            "kraken",
            "PI_XBTUSD",
            "demo",
            0.0,
            "INFO",
            cancel_after_submit=False,
        )
        == 0
    )
    assert adapter.cancelled == []


def test_list_smoke_orders_for_exchange_returns_order_types():
    payload = smoke.list_smoke_orders("kraken")
    assert isinstance(payload, list)
    assert any(row["order_type"] == "Limit" for row in payload)
    assert not any(row["order_type"] == "Market" for row in payload)


def test_list_smoke_orders_honours_market_type():
    payload = smoke.list_smoke_orders("binance", market_type="spot", include_market=True)
    order_types = {row["order_type"] for row in payload}
    assert "Market" in order_types
    assert "SL" in order_types


def test_list_smoke_orders_honours_only_filter():
    payload = smoke.list_smoke_orders(
        "kraken",
        market_type="futures",
        only_orders=["limit_below"],
    )

    assert [row["name"] for row in payload] == ["limit_below"]


def test_main_list_orders_exits_without_submitting(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "run_smoke", lambda *args, **kwargs: 99)
    rc = smoke.main(
        [
            "--exchange",
            "binance",
            "--market-type",
            "spot",
            "--account-scope",
            "advers",
            "--include-market",
            "--leave-open",
            "--only",
            "limit_below,stop_limit_above",
            "--list-orders",
        ]
    )
    assert rc == 0
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["exchange"] == "binance"
    assert parsed["market_type"] == "spot"
    assert parsed["symbol"] == "BTCUSDT"
    assert parsed["account_scope"] == "advers"
    assert parsed["include_market"] is True
    assert parsed["only_orders"] == ["limit_below", "stop_limit_above"]
    assert parsed["cancel_after_submit"] is False
    assert [order["name"] for order in parsed["orders"]] == [
        "limit_below",
        "stop_limit_above",
    ]


def test_main_list_orders_uses_bitmex_spot_default_symbol(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "run_smoke", lambda *args, **kwargs: 99)

    rc = smoke.main(
        [
            "--exchange",
            "bitmex",
            "--market-type",
            "spot",
            "--list-orders",
        ]
    )

    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["exchange"] == "bitmex"
    assert parsed["market_type"] == "spot"
    assert parsed["symbol"] == "XBT_USDT"


def test_build_adapter_forwards_market_type(monkeypatch):
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
    monkeypatch.setattr("kolabi.bargain.smoke.get_adapter", _get_adapter)

    build_adapter("binance", "BTCUSDT", "demo", market_type="spot")

    assert observed_loader == {"exchange": "binance", "market_type": "spot"}
    assert built["api_key"] == "spot-key"
    assert built["api_secret"] == "spot-secret"
    assert built["market_type"] == "spot"


def test_build_adapter_honours_account_scope_and_key_env_overrides(monkeypatch):
    built: dict[str, object] = {}

    class CapturingAdapter:
        def __init__(self, **kwargs) -> None:
            built.update(kwargs)

    monkeypatch.setenv("CUSTOM_BINANCE_KEY", "custom-key")
    monkeypatch.setenv("CUSTOM_BINANCE_SECRET", "custom-secret")
    monkeypatch.setenv(
        "KOLABI_MARKET_DB_URL",
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market",
    )
    monkeypatch.setenv(
        "KOLABI_ADVERS_ACCOUNT_DB_URL",
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers",
    )
    monkeypatch.setenv(
        "KOLABI_ADVERS_AUDIT_DB_URL",
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_audit_advers",
    )
    monkeypatch.setattr(
        "kolabi.bargain.smoke.get_adapter",
        lambda _exchange, _market_type: CapturingAdapter,
    )

    build_adapter(
        "binance",
        "BTCUSDT",
        "demo",
        market_type="spot",
        account_scope="advers",
        api_key_env="CUSTOM_BINANCE_KEY",
        api_secret_env="CUSTOM_BINANCE_SECRET",
    )

    assert built["api_key"] == "custom-key"
    assert built["api_secret"] == "custom-secret"
    assert built["account_scope"] == "advers"
    assert built["public_db_url"] == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    )
    assert built["account_db_url"] == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers"
    )
    assert built["audit_db_url"] == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_audit_advers"
    )


def test_main_forwards_account_scope_and_key_envs(monkeypatch):
    observed: dict[str, object] = {}

    def _run_smoke(*args, **kwargs):
        observed["args"] = args
        observed.update(kwargs)
        return 0

    monkeypatch.setattr(smoke, "run_smoke", _run_smoke)

    rc = smoke.main(
        [
            "--exchange",
            "binance",
            "--market-type",
            "spot",
            "--symbol",
            "BTCUSDT",
            "--environment",
            "demo",
            "--account-scope",
            "advers",
            "--api-key-env",
            "CUSTOM_BINANCE_KEY",
            "--api-secret-env",
            "CUSTOM_BINANCE_SECRET",
            "--only",
            "limit_below",
        ]
    )

    assert rc == 0
    assert observed["account_scope"] == "advers"
    assert observed["api_key_env"] == "CUSTOM_BINANCE_KEY"
    assert observed["api_secret_env"] == "CUSTOM_BINANCE_SECRET"
    assert observed["market_type"] == "spot"
    assert observed["only_orders"] == ["limit_below"]


def test_main_resolves_default_symbol_before_running_smoke(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def _run_smoke(*args, **kwargs):
        observed["args"] = args
        observed.update(kwargs)
        return 0

    monkeypatch.setattr(smoke, "run_smoke", _run_smoke)

    rc = smoke.main(
        [
            "--exchange",
            "bitmex",
            "--market-type",
            "spot",
            "--environment",
            "demo",
        ]
    )

    assert rc == 0
    assert observed["exchange"] == "bitmex"
    assert observed["symbol"] == "XBT_USDT"
    assert observed["environment"] == "demo"
    assert observed["market_type"] == "spot"
