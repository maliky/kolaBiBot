from __future__ import annotations

from kolabi.bargain.smoke import build_smoke_orders, extract_min_quantity


def test_extract_min_quantity_falls_back_to_one_contract():
    assert extract_min_quantity({"symbol": "PI_XBTUSD"}) == 1.0


def test_build_smoke_orders_covers_standard_order_sweep():
    orders = build_smoke_orders("PI_XBTUSD", quantity=1.0, reference_price=100.0)
    order_types = {order.order_type for order in orders}

    assert "Limit" in order_types
    assert "Market" in order_types
    assert "StopLoss" in order_types
    assert "StopLossLimit" in order_types
    assert "TakeProfit" in order_types
    assert "TakeProfitLimit" in order_types
    assert "TrailingStop" in order_types
    assert "TrailingStopLimit" in order_types
