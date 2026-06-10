from urllib.parse import parse_qs

import pytest
import responses
from kolabi.shared.exchanges.binance_adapter import BinanceAdapter

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.10"},
            ],
        }
    ]
}


def _body_query(call: responses.Call) -> dict[str, list[str]]:
    body = call.request.body
    if isinstance(body, bytes):
        return parse_qs(body.decode("utf-8"))
    if isinstance(body, str):
        return parse_qs(body)
    return {}


@responses.activate
def test_order_round_trip_uses_futures_api_and_client_id(postgres_url_factory) -> None:
    base = "https://test-fapi"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/fapi/v1/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.POST,
        f"{base}/fapi/v1/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 1,
            "clientOrderId": "H1unit-260601000000",
            "status": "NEW",
            "price": "10000.10",
            "origQty": "0.123",
            "executedQty": "0",
            "side": "BUY",
        },
    )
    responses.add(
        responses.PUT,
        f"{base}/fapi/v1/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 1,
            "clientOrderId": "H1unit-260601000000",
            "status": "NEW",
            "price": "10010.00",
            "origQty": "0.200",
            "executedQty": "0",
            "side": "BUY",
        },
    )
    responses.add(
        responses.DELETE,
        f"{base}/fapi/v1/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 1,
            "clientOrderId": "H1unit-260601000000",
            "status": "CANCELED",
        },
    )

    adapter = BinanceAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)

    ack1 = adapter.place_order(
        "buy",
        0.12345,
        price=10000.123,
        type_="Limit",
        clOrdID="H1unit-260601000000",
    )
    assert ack1.order_id == "1"
    assert ack1.status == "NEW"
    assert ack1.price == 10000.10
    assert ack1.orig_qty == 0.123

    ack2 = adapter.amend_order("1", orderQty=0.2, price=10010.0)
    assert ack2.order_id == "1"
    adapter.cancel_order("1")

    post_qs = _body_query(responses.calls[1])
    assert post_qs["symbol"] == ["BTCUSDT"]
    assert post_qs["type"] == ["LIMIT"]
    assert post_qs["newClientOrderId"] == ["H1unit-260601000000"]
    assert post_qs["quantity"] == ["0.123"]
    assert post_qs["price"] == ["10000.1"]
    assert "signature" in post_qs


@responses.activate
def test_stop_market_maps_mark_price_and_reduce_only(postgres_url_factory) -> None:
    base = "https://test-fapi"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/fapi/v1/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.POST,
        f"{base}/fapi/v1/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 2,
            "clientOrderId": "T1tail-260601000001",
            "status": "NEW",
            "stopPrice": "9999.90",
            "origQty": "0.500",
            "executedQty": "0",
            "side": "SELL",
        },
    )
    adapter = BinanceAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)

    ack = adapter.place_order(
        "sell",
        0.5,
        stopPx=9999.94,
        type_="Stop",
        clOrdID="T1tail-260601000001",
        execInst="ReduceOnly,MarkPrice",
    )

    assert ack.order_id == "2"
    post_qs = _body_query(responses.calls[1])
    assert post_qs["type"] == ["STOP_MARKET"]
    assert post_qs["stopPrice"] == ["9999.9"]
    assert post_qs["reduceOnly"] == ["true"]
    assert post_qs["workingType"] == ["MARK_PRICE"]


def test_unsupported_index_price_fails_before_request(postgres_url_factory) -> None:
    audit = postgres_url_factory("audit")
    adapter = BinanceAdapter("key", "secret", "https://test-fapi", "BTCUSDT", audit_db_url=audit)
    with pytest.raises(ValueError, match="IndexPrice"):
        adapter.place_order(
            "sell",
            1,
            stopPx=100,
            type_="Stop",
            clOrdID="T",
            execInst="IndexPrice",
        )
