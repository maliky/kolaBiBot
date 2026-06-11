from urllib.parse import parse_qs, urlparse

import pytest
import responses
from kolabi.shared.exchanges import get_adapter
from kolabi.shared.exchanges.binance_adapter import (
    BinanceAdapter,
    BinanceMarginAdapter,
    BinanceSpotAdapter,
)

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.10"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.00"},
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


def _url_query(call: responses.Call) -> dict[str, list[str]]:
    return parse_qs(urlparse(call.request.url).query)


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


def test_adapter_loader_selects_binance_market_types() -> None:
    assert get_adapter("binance", "futures") is BinanceAdapter
    assert get_adapter("binance", "spot") is BinanceSpotAdapter
    assert get_adapter("binance", "margin") is BinanceMarginAdapter
    assert get_adapter("binance", "isolated_margin") is BinanceMarginAdapter


@responses.activate
def test_binance_spot_adapter_lists_and_validates_symbols(postgres_url_factory) -> None:
    base = "https://test-spot"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/api/v3/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(responses.GET, f"{base}/api/v3/exchangeInfo", json=EXCHANGE_INFO)
    adapter = BinanceSpotAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)

    instruments = adapter.list_instruments()
    metadata = adapter.validate_symbol("BTCUSDT")

    assert instruments[0]["symbol"] == "BTCUSDT"
    assert instruments[0]["tradeable"] is True
    assert metadata["symbol"] == "BTCUSDT"
    assert metadata["type"] == "spot"
    assert metadata["minQuantity"] == 0.001
    assert metadata["minNotional"] == 5.0


@responses.activate
def test_spot_stop_order_uses_spot_api_and_stop_loss(postgres_url_factory) -> None:
    base = "https://test-spot"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/api/v3/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.POST,
        f"{base}/api/v3/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 3,
            "clientOrderId": "T1spot-260601000001",
            "status": "NEW",
            "stopPrice": "9999.90",
            "origQty": "0.500",
            "executedQty": "0",
            "side": "SELL",
        },
    )
    adapter = BinanceSpotAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)

    ack = adapter.place_order(
        "sell",
        0.5,
        stopPx=9999.94,
        type_="Stop",
        clOrdID="T1spot-260601000001",
    )

    assert ack.order_id == "3"
    assert ack.client_order_id == "T1spot-260601000001"
    post_qs = _body_query(responses.calls[1])
    assert post_qs["type"] == ["STOP_LOSS"]
    assert post_qs["stopPrice"] == ["9999.9"]
    assert "reduceOnly" not in post_qs
    assert "workingType" not in post_qs


@responses.activate
def test_spot_amend_replaces_with_fresh_client_id(postgres_url_factory) -> None:
    base = "https://test-spot"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/api/v3/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.POST,
        f"{base}/api/v3/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 4,
            "clientOrderId": "H1spot-260601000001",
            "status": "NEW",
            "price": "10000.10",
            "origQty": "0.500",
            "executedQty": "0",
            "side": "BUY",
        },
    )
    responses.add(
        responses.DELETE,
        f"{base}/api/v3/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 4,
            "clientOrderId": "H1spot-260601000001",
            "status": "CANCELED",
        },
    )
    responses.add(
        responses.POST,
        f"{base}/api/v3/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 5,
            "clientOrderId": "replacement-client",
            "status": "NEW",
            "price": "10010.00",
            "origQty": "0.500",
            "executedQty": "0",
            "side": "BUY",
        },
    )
    adapter = BinanceSpotAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)
    adapter.place_order(
        "buy",
        0.5,
        price=10000.1,
        type_="Limit",
        clOrdID="H1spot-260601000001",
    )

    ack = adapter.amend_order("4", price=10010.0)

    assert ack.order_id == "5"
    assert ack.client_order_id == "replacement-client"
    replace_qs = _body_query(responses.calls[3])
    assert replace_qs["newClientOrderId"][0] != "H1spot-260601000001"
    assert replace_qs["price"] == ["10010"]


@responses.activate
def test_isolated_margin_order_adds_margin_flags(postgres_url_factory) -> None:
    base = "https://test-margin"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/api/v3/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.POST,
        f"{base}/sapi/v1/margin/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 6,
            "clientOrderId": "T1margin-260601000001",
            "status": "NEW",
            "stopPrice": "9999.90",
            "origQty": "0.500",
            "executedQty": "0",
            "side": "SELL",
        },
    )
    adapter = BinanceMarginAdapter(
        "key",
        "secret",
        base,
        "BTCUSDT",
        audit_db_url=audit,
        is_isolated=True,
    )

    adapter.place_order(
        "sell",
        0.5,
        stopPx=9999.94,
        type_="Stop",
        clOrdID="T1margin-260601000001",
    )

    post_qs = _body_query(responses.calls[1])
    assert post_qs["type"] == ["STOP_LOSS"]
    assert post_qs["isIsolated"] == ["TRUE"]
    assert post_qs["sideEffectType"] == ["NO_SIDE_EFFECT"]


@responses.activate
def test_cross_margin_order_uses_margin_endpoint_without_isolated_flag(
    postgres_url_factory,
) -> None:
    base = "https://test-margin"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/api/v3/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.POST,
        f"{base}/sapi/v1/margin/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 9,
            "clientOrderId": "H1crossmargin-260601000001",
            "status": "NEW",
            "price": "10000.00",
            "origQty": "0.500",
            "executedQty": "0",
            "side": "BUY",
        },
    )
    adapter = BinanceMarginAdapter(
        "key",
        "secret",
        base,
        "BTCUSDT",
        audit_db_url=audit,
        side_effect_type="MARGIN_BUY",
    )

    ack = adapter.place_order(
        "buy",
        0.5,
        price=10000.0,
        type_="Limit",
        clOrdID="H1crossmargin-260601000001",
    )

    assert ack.order_id == "9"
    assert ack.client_order_id == "H1crossmargin-260601000001"
    post_qs = _body_query(responses.calls[1])
    assert post_qs["symbol"] == ["BTCUSDT"]
    assert post_qs["type"] == ["LIMIT"]
    assert post_qs["sideEffectType"] == ["MARGIN_BUY"]
    assert "isIsolated" not in post_qs
    assert "reduceOnly" not in post_qs
    assert "workingType" not in post_qs


@responses.activate
def test_isolated_margin_balance_uses_symbols_parameter(postgres_url_factory) -> None:
    base = "https://test-margin"
    audit = postgres_url_factory("audit")
    responses.add(
        responses.GET,
        f"{base}/sapi/v1/margin/isolated/account",
        json={
            "assets": [
                {
                    "baseAsset": {"asset": "BTC", "free": "0", "netAsset": "0"},
                    "quoteAsset": {
                        "asset": "USDT",
                        "free": "12.5",
                        "netAsset": "12.5",
                    },
                }
            ]
        },
    )
    adapter = BinanceMarginAdapter(
        "key",
        "secret",
        base,
        "BTCUSDT",
        audit_db_url=audit,
        is_isolated=True,
    )

    balance = adapter.get_balance()

    assert balance == 12.5
    balance_qs = _url_query(responses.calls[0])
    assert balance_qs["symbols"] == ["BTCUSDT"]
    assert "symbol" not in balance_qs


@responses.activate
def test_spot_position_reads_base_asset_inventory(postgres_url_factory) -> None:
    base = "https://test-spot"
    audit = postgres_url_factory("audit")
    responses.add(
        responses.GET,
        f"{base}/api/v3/account",
        json={
            "balances": [
                {"asset": "BTC", "free": "0.25", "locked": "0.05"},
                {"asset": "USDT", "free": "1000", "locked": "0"},
            ]
        },
    )
    adapter = BinanceSpotAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)

    position = adapter.get_position()

    assert position.symbol == "BTCUSDT"
    assert position.qty == 0.30
    assert position.entry_price is None


@responses.activate
def test_cross_margin_position_reads_signed_base_net_asset(
    postgres_url_factory,
) -> None:
    base = "https://test-margin"
    audit = postgres_url_factory("audit")
    responses.add(
        responses.GET,
        f"{base}/sapi/v1/margin/account",
        json={
            "userAssets": [
                {"asset": "BTC", "free": "0", "netAsset": "-0.125"},
                {"asset": "USDT", "free": "500", "netAsset": "500"},
            ]
        },
    )
    adapter = BinanceMarginAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)

    position = adapter.get_position()

    assert position.symbol == "BTCUSDT"
    assert position.qty == -0.125
    assert position.entry_price is None


@responses.activate
def test_isolated_margin_position_reads_base_net_asset_with_symbols_param(
    postgres_url_factory,
) -> None:
    base = "https://test-margin"
    audit = postgres_url_factory("audit")
    responses.add(
        responses.GET,
        f"{base}/sapi/v1/margin/isolated/account",
        json={
            "assets": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": {"asset": "BTC", "free": "0.40", "netAsset": "0.35"},
                    "quoteAsset": {"asset": "USDT", "free": "10", "netAsset": "10"},
                }
            ]
        },
    )
    adapter = BinanceMarginAdapter(
        "key",
        "secret",
        base,
        "BTCUSDT",
        audit_db_url=audit,
        is_isolated=True,
    )

    position = adapter.get_position()

    assert position.qty == 0.35
    position_qs = _url_query(responses.calls[0])
    assert position_qs["symbols"] == ["BTCUSDT"]
    assert "symbol" not in position_qs


@responses.activate
def test_isolated_margin_amend_cancel_replace_preserves_route_flags(
    postgres_url_factory,
) -> None:
    base = "https://test-margin"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/api/v3/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.POST,
        f"{base}/sapi/v1/margin/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 7,
            "clientOrderId": "H1margin-260601000002",
            "status": "NEW",
            "price": "10000.00",
            "origQty": "0.500",
            "executedQty": "0",
            "side": "BUY",
        },
    )
    responses.add(
        responses.DELETE,
        f"{base}/sapi/v1/margin/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 7,
            "clientOrderId": "H1margin-260601000002",
            "status": "CANCELED",
        },
    )
    responses.add(
        responses.POST,
        f"{base}/sapi/v1/margin/order",
        json={
            "symbol": "BTCUSDT",
            "orderId": 8,
            "clientOrderId": "replacement-client",
            "status": "NEW",
            "price": "10010.00",
            "origQty": "0.500",
            "executedQty": "0",
            "side": "BUY",
        },
    )
    adapter = BinanceMarginAdapter(
        "key",
        "secret",
        base,
        "BTCUSDT",
        audit_db_url=audit,
        is_isolated=True,
        auto_repay_at_cancel=True,
    )
    adapter.place_order(
        "buy",
        0.5,
        price=10000.0,
        type_="Limit",
        clOrdID="H1margin-260601000002",
    )

    ack = adapter.amend_order("7", price=10010.0)

    assert ack.order_id == "8"
    cancel_qs = _url_query(responses.calls[2])
    assert cancel_qs["symbol"] == ["BTCUSDT"]
    assert cancel_qs["isIsolated"] == ["TRUE"]
    assert cancel_qs["autoRepayAtCancel"] == ["TRUE"]
    replace_qs = _body_query(responses.calls[3])
    assert replace_qs["symbol"] == ["BTCUSDT"]
    assert replace_qs["isIsolated"] == ["TRUE"]
    assert replace_qs["sideEffectType"] == ["NO_SIDE_EFFECT"]
    assert replace_qs["newClientOrderId"][0] != "H1margin-260601000002"
    assert "reduceOnly" not in replace_qs


@responses.activate
def test_binance_futures_permission_status_uses_test_order_without_live_order(
    postgres_url_factory,
) -> None:
    base = "https://test-fapi"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/fapi/v1/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.GET,
        f"{base}/fapi/v1/ticker/bookTicker",
        json={"symbol": "BTCUSDT", "bidPrice": "10000.00", "askPrice": "10001.00"},
    )
    responses.add(responses.POST, f"{base}/fapi/v1/order/test", json={})
    adapter = BinanceAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)

    payload = adapter.permission_status()

    assert payload == {
        "exchange": "binance",
        "market_type": "futures",
        "symbol": "BTCUSDT",
        "permission_probe": "test_order",
        "test_order_path": "/fapi/v1/order/test",
        "can_place_orders": True,
        "reason": "ok",
    }
    assert len(responses.calls) == 3
    post_qs = _body_query(responses.calls[2])
    assert post_qs["symbol"] == ["BTCUSDT"]
    assert post_qs["side"] == ["BUY"]
    assert post_qs["type"] == ["LIMIT"]
    assert post_qs["timeInForce"] == ["GTC"]
    assert post_qs["quantity"] == ["0.001"]
    assert post_qs["price"] == ["10000"]
    assert "signature" in post_qs


@responses.activate
def test_binance_spot_permission_status_reports_test_order_failure(
    postgres_url_factory,
) -> None:
    base = "https://test-spot"
    audit = postgres_url_factory("audit")
    responses.add(responses.GET, f"{base}/api/v3/exchangeInfo", json=EXCHANGE_INFO)
    responses.add(
        responses.GET,
        f"{base}/api/v3/ticker/bookTicker",
        json={"symbol": "BTCUSDT", "bidPrice": "10000.00", "askPrice": "10001.00"},
    )
    responses.add(
        responses.POST,
        f"{base}/api/v3/order/test",
        json={"code": -2015, "msg": "Invalid API-key"},
        status=401,
    )
    adapter = BinanceSpotAdapter("key", "secret", base, "BTCUSDT", audit_db_url=audit)

    payload = adapter.permission_status()

    assert payload["exchange"] == "binance"
    assert payload["market_type"] == "spot"
    assert payload["permission_probe"] == "test_order"
    assert payload["test_order_path"] == "/api/v3/order/test"
    assert payload["can_place_orders"] is False
    assert payload["reason"] == "test_order_failed"
    assert "Invalid API-key" in str(payload["error"])


def test_binance_margin_permission_status_reports_unsupported_probe(
    postgres_url_factory,
) -> None:
    adapter = BinanceMarginAdapter(
        "key",
        "secret",
        "https://test-margin",
        "BTCUSDT",
        audit_db_url=postgres_url_factory("audit"),
    )

    payload = adapter.permission_status()

    assert payload == {
        "exchange": "binance",
        "market_type": "margin",
        "symbol": "BTCUSDT",
        "permission_probe": "not_supported",
        "can_place_orders": None,
        "reason": "adapter does not expose a no-order permission probe",
    }
