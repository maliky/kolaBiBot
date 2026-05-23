from __future__ import annotations

import json

from kolabi.bargain import smoke
from kolabi.bargain.smoke import SmokeOrder, build_smoke_orders, extract_min_quantity


def test_extract_min_quantity_falls_back_to_one_contract():
    assert extract_min_quantity({"symbol": "PI_XBTUSD"}) == 1.0


def test_build_smoke_orders_covers_standard_order_sweep():
    orders = build_smoke_orders("kraken", "PI_XBTUSD", quantity=1.0, reference_price=100.0)
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
    assert "Market" in order_types


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

    assert smoke.run_smoke("kraken", "PI_XBTUSD", "demo", 0.0, "INFO") == 1


def test_list_smoke_orders_for_exchange_returns_order_types():
    payload = smoke.list_smoke_orders("kraken")
    assert isinstance(payload, list)
    assert any(row["order_type"] == "Limit" for row in payload)
    assert any(row["order_type"] == "Market" for row in payload)


def test_main_list_orders_exits_without_submitting(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "run_smoke", lambda *args, **kwargs: 99)
    rc = smoke.main(["--exchange", "binance", "--list-orders"])
    assert rc == 0
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["exchange"] == "binance"
    assert any(order["order_type"] == "STOP" for order in parsed["orders"])
