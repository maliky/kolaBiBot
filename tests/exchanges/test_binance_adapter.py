import json
from typing import Any

import pytest
import responses

from kola.exchanges.binance_adapter import BinanceAdapter

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.000001", "minQty": "0.000001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
            ],
        }
    ]
}


@responses.activate
def test_order_round_trip() -> None:
    base = "https://test"
    responses.add(
        responses.GET,
        f"{base}/v3/exchangeInfo",
        json=EXCHANGE_INFO,
    )

    # initial place order
    responses.add(
        responses.POST,
        f"{base}/v3/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 1,
            "status": "NEW",
            "price": "10000.12",
            "origQty": "0.001234",
            "executedQty": "0.0",
            "side": "BUY",
        },
    )

    # cancel on amend
    responses.add(
        responses.DELETE,
        f"{base}/v3/order",
        json={"symbol": "BTCUSDT", "orderId": 1},
    )
    # new order after amend
    responses.add(
        responses.POST,
        f"{base}/v3/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 2,
            "status": "NEW",
            "price": "10100.00",
            "origQty": "0.002000",
            "executedQty": "0.0",
            "side": "BUY",
        },
    )
    # final cancel
    responses.add(
        responses.DELETE,
        f"{base}/v3/order",
        json={"symbol": "BTCUSDT", "orderId": 2},
    )

    # avoid real network call during client initialisation
    from binance.client import Client
    Client.ping = lambda self: {}

    adapter = BinanceAdapter("k", "s", base, "BTCUSDT")
    adapter._throttle = lambda *a, **k: None  # disable sleep in tests

    ack1 = adapter.place_order("BUY", 0.0012345, price=10000.123)
    assert ack1.order_id == "1"
    assert ack1.price == 10000.12
    assert ack1.orig_qty == 0.001234

    ack2 = adapter.amend_order("1", side="BUY", orderQty=0.002, price=10100)
    assert ack2.order_id == "2"
    adapter.cancel_order("2")

    # verify request payload rounding
    from urllib.parse import parse_qs

    first_call = responses.calls[1].request  # POST order
    q = parse_qs(first_call.body)
    assert float(q["quantity"][0]) == pytest.approx(0.001234)
    assert float(q["price"][0]) == pytest.approx(10000.12)


@responses.activate
def test_validate_precision_edge() -> None:
    base = "https://test"
    responses.add(responses.GET, f"{base}/v3/exchangeInfo", json=EXCHANGE_INFO)
    adapter = BinanceAdapter("k", "s", base, "BTCUSDT")
    adapter._throttle = lambda *a, **k: None
    data = adapter.validate_order({"quantity": 0.0023456, "price": 10000.1234})
    assert data["quantity"] == 0.002345
    assert data["price"] == 10000.12
    with pytest.raises(ValueError):
        adapter.validate_order({"quantity": 0.000001, "price": 1})
