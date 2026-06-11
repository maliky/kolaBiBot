from __future__ import annotations

from typing import Any, Dict, List

import pytest
from kolabi.shared.exchanges import get_adapter
from kolabi.shared.exchanges.bitmex_api.custom_api import BitMEX
from kolabi.shared.exchanges.bitmex_adapter import BitmexAdapter
from kolabi.shared.persistence import ExchangeRestCall
from sqlalchemy import select
from sqlalchemy.orm import Session


class _FakeBitmexClient:
    last_instance: "_FakeBitmexClient | None" = None

    def __init__(self, **kwargs: Any) -> None:
        _FakeBitmexClient.last_instance = self
        self.kwargs = kwargs

    def place(self, orderQty: float, side: str | None = None, **opts: Any) -> Dict[str, Any]:
        self.last_place = {"orderQty": orderQty, "side": side, **opts}
        return {
            "orderID": "abc",
            "ordStatus": "New",
            "price": opts.get("price"),
            "clOrdID": opts.get("clOrdID"),
            "orderQty": orderQty,
            "cumQty": 0,
            "side": side,
        }

    def amend(self, order: Dict[str, Any], **params: Any) -> List[Dict[str, Any]]:
        self.last_amend = {"order": order, **params}
        return [
            {
                "orderID": order["orderID"],
                "ordStatus": "Replaced",
                "price": params.get("price"),
                "orderQty": params.get("orderQty"),
            }
        ]

    def cancel(self, order_id: object) -> List[Dict[str, Any]]:
        self.last_cancel = order_id
        if isinstance(order_id, dict):
            client_ids = order_id.get("clIDList") or []
            client_id = client_ids[0] if client_ids else ""
            return [{"clOrdID": client_id}]
        return [{"orderID": order_id}]

    def position(self, symbol: str) -> Dict[str, Any]:
        self.last_position_symbol = symbol
        return {"symbol": symbol, "currentQty": 5, "avgEntryPrice": 10000}

    def margin(self, currency: str = "XBt") -> Dict[str, Any] | List[Dict[str, Any]]:
        self.last_margin_currency = currency
        if currency == "all":
            return [
                {
                    "currency": "XBt",
                    "walletBalance": 0.25,
                    "availableMargin": 0.20,
                },
                {
                    "currency": "USDt",
                    "walletBalance": 1000.0,
                    "availableMargin": 800.0,
                },
            ]
        return {"availableMargin": 12345}

    def instrument(self, symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "state": "Open",
            "tickSize": 0.5,
            "lotSize": 1,
            "bidPrice": 100.0,
            "askPrice": 101.0,
            "lastPrice": 100.5,
            "markPrice": 100.25,
            "multiplier": 1,
        }

    def instruments(self) -> List[Dict[str, Any]]:
        return [
            {
                "symbol": "XBTUSD",
                "state": "Open",
                "tickSize": 0.5,
                "lotSize": 1,
                "multiplier": 1,
            }
        ]

    def open_orders(self) -> List[Dict[str, Any]]:
        return [
            {
                "orderID": "OID-L",
                "clOrdID": "H1bitmex-260610000000",
                "symbol": "XBTUSD",
                "side": "Buy",
                "ordType": "Limit",
                "orderQty": 2,
                "cumQty": 0,
                "price": 100,
                "ordStatus": "New",
            },
            {
                "orderID": "OID-S",
                "clOrdID": "T1bitmex-260610000000",
                "symbol": "XBTUSD",
                "side": "Sell",
                "ordType": "Stop",
                "orderQty": 2,
                "cumQty": 0,
                "stopPx": 90,
                "ordStatus": "New",
            },
        ]


class _FakeBitmexRestClient(_FakeBitmexClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.curl_calls: list[dict[str, Any]] = []
        self.api_key_payload = kwargs.get(
            "api_key_payload",
            [
                {
                    "id": kwargs.get("apiKey", "k"),
                    "permissions": ["order"],
                    "enabled": True,
                }
            ],
        )

    def _curl_bitmex(
        self,
        *,
        path: str,
        postdict: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        verb: str,
        **kwargs: Any,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        call = {
            "path": path,
            "verb": verb,
            **kwargs,
        }
        if postdict is not None:
            call["postdict"] = dict(postdict)
        if query is not None:
            call["query"] = dict(query)
        self.curl_calls.append(call)
        if path == "apiKey":
            return self.api_key_payload
        if path == "user/margin":
            return [
                {"currency": "XBt", "walletBalance": 0.125},
                {"currency": "USDt", "walletBalance": 500.0, "availableMargin": 450.0},
            ]
        payload = dict(postdict or {})
        payload.setdefault("orderID", "OID-DIRECT")
        payload.setdefault("ordStatus", "Canceled" if verb == "DELETE" else "New")
        payload.setdefault("cumQty", 0)
        return payload


def test_bitmex_adapter_wraps_client() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexClient,
        orderIDPrefix="abc",
    )

    ack = adapter.place_order("buy", 1, price=100, type_="limit", clOrdID="CID-1")
    assert ack.order_id == "abc"
    assert ack.client_order_id == "CID-1"
    assert _FakeBitmexClient.last_instance is not None
    assert _FakeBitmexClient.last_instance.kwargs["useWebsocket"] is False
    assert _FakeBitmexClient.last_instance.last_place["ordType"] == "Limit"

    ack = adapter.amend_order("abc", price=101)
    assert ack.status == "Replaced"

    ack = adapter.cancel_order("abc")
    assert ack.status.lower() == "canceled"

    pos = adapter.get_position()
    assert pos.qty == 5
    assert adapter.get_balance() == 12345
    assert adapter.instrument_rules("XBTUSD")["tickSize"] == 0.5
    assert adapter.instrument_rules("XBTUSD")["bidPrice"] == 100.0
    assert adapter.instrument_rules("XBTUSD")["askPrice"] == 101.0
    assert adapter.instrument_rules("XBTUSD")["lastPrice"] == 100.5
    assert adapter.instrument_rules("XBTUSD")["markPrice"] == 100.25
    assert adapter.instrument("XBTUSD")["symbol"] == "XBTUSD"
    assert adapter.validate_symbol("XBTUSD")["tradeable"] is True
    assert adapter.list_instruments()[0]["symbol"] == "XBTUSD"
    assert adapter.live_open_orders()[0]["order_id"] == "OID-L"
    assert adapter.live_trigger_orders()[0]["order_id"] == "OID-S"
    assert len(adapter.open_orders()) == 2


def test_bitmex_rest_only_client_accepts_unknown_symbol_precision() -> None:
    client = BitMEX(
        base_url="https://example/api/v1/",
        symbol="XBT_USDT",
        apiKey="k",
        apiSecret="s",
        useWebsocket=False,
    )

    assert client.prec == 1e-8
    assert client.ws is None


def test_bitmex_futures_place_order_writes_rest_audit(postgres_url_factory) -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexClient,
        environment="demo",
        market_type="futures",
        audit_db_url=postgres_url_factory("audit"),
        account_scope="advers",
    )

    adapter.place_order("buy", 1, price=100, type_="limit", clOrdID="CID-1")

    assert adapter._audit_engine is not None
    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    assert row.exchange == "bitmex"
    assert row.environment == "demo"
    assert row.market_type == "futures"
    assert row.account_scope == "advers"
    assert row.symbol == "XBTUSD"
    assert row.method == "POST"
    assert row.path == "/order"
    assert row.result_kind == "ok"
    assert row.client_order_id == "CID-1"
    assert row.exchange_order_id == "abc"
    assert row.endpoint_order_id == "abc"
    assert row.correlation_id == "CID-1"
    assert row.ack_status == "New"
    assert row.request_params["ordType"] == "Limit"
    assert row.request_params["orderQty"] == 1.0
    assert row.response_payload["orderID"] == "abc"


def test_bitmex_futures_reduce_only_maps_to_exec_inst(
    postgres_url_factory,
) -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexClient,
        environment="demo",
        market_type="futures",
        audit_db_url=postgres_url_factory("audit"),
    )

    adapter.place_order(
        "sell",
        1,
        type_="Market",
        reduceOnly=True,
    )

    client = _FakeBitmexClient.last_instance
    assert client is not None
    assert client.last_place["execInst"] == "ReduceOnly"
    assert "reduceOnly" not in client.last_place
    assert adapter._audit_engine is not None
    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    assert row.request_params["execInst"] == "ReduceOnly"
    assert "reduceOnly" not in row.request_params


def test_bitmex_futures_reduce_only_preserves_existing_exec_inst() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexClient,
        market_type="futures",
    )

    adapter.place_order(
        "buy",
        1,
        price=100,
        type_="Limit",
        execInst="ParticipateDoNotInitiate",
        reduceOnly=True,
    )

    client = _FakeBitmexClient.last_instance
    assert client is not None
    assert client.last_place["execInst"] == "ParticipateDoNotInitiate,ReduceOnly"


def test_bitmex_spot_cancel_order_writes_spot_rest_audit(postgres_url_factory) -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBT_USDT",
        client_factory=_FakeBitmexClient,
        environment="demo",
        market_type="spot",
        audit_db_url=postgres_url_factory("audit"),
        account_scope="advers",
    )

    adapter.cancel_order("OID-SPOT")

    assert adapter._audit_engine is not None
    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    assert row.exchange == "bitmex"
    assert row.market_type == "spot"
    assert row.symbol == "XBT_USDT"
    assert row.method == "DELETE"
    assert row.path == "/order"
    assert row.result_kind == "ok"
    assert row.client_order_id is None
    assert row.exchange_order_id == "OID-SPOT"
    assert row.endpoint_order_id == "OID-SPOT"
    assert row.correlation_id == "OID-SPOT"
    assert row.ack_status == "Canceled"


def test_bitmex_cancel_client_id_uses_direct_clordid(postgres_url_factory) -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexRestClient,
        environment="demo",
        market_type="futures",
        audit_db_url=postgres_url_factory("audit"),
    )

    ack = adapter.cancel_order("H1bitmex-260610000000")

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert client.curl_calls == [
        {
            "path": "order",
            "postdict": {"clOrdID": "H1bitmex-260610000000"},
            "verb": "DELETE",
        }
    ]
    assert ack.status == "Canceled"
    assert ack.client_order_id == "H1bitmex-260610000000"
    assert adapter._audit_engine is not None
    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    assert row.request_params == {"clOrdID": "H1bitmex-260610000000"}
    assert row.client_order_id == "H1bitmex-260610000000"
    assert row.exchange_order_id == "OID-DIRECT"
    assert row.endpoint_order_id == "OID-DIRECT"
    assert row.correlation_id == "H1bitmex-260610000000"
    assert row.ack_status == "Canceled"


def test_bitmex_cancel_exchange_id_uses_direct_orderid() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexRestClient,
        market_type="futures",
    )

    ack = adapter.cancel_order("OID-1")

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert client.curl_calls == [
        {
            "path": "order",
            "postdict": {"orderID": "OID-1"},
            "verb": "DELETE",
        }
    ]
    assert ack.order_id == "OID-1"
    assert ack.client_order_id is None


def test_bitmex_place_order_rejects_cancel_only_key_before_order_post() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexRestClient,
        market_type="futures",
        api_key_payload=[
            {"id": "k", "permissions": ["orderCancel"], "enabled": True}
        ],
    )

    with pytest.raises(RuntimeError, match="cannot place orders"):
        adapter.place_order("buy", 1, price=100, type_="limit", clOrdID="CID-1")

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert client.curl_calls == [{"path": "apiKey", "verb": "GET"}]


def test_bitmex_permission_status_reports_cancel_only_key_without_order_post() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexRestClient,
        market_type="futures",
        api_key_payload=[
            {"id": "k", "permissions": ["orderCancel"], "enabled": True}
        ],
    )

    payload = adapter.permission_status()

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert client.curl_calls == [{"path": "apiKey", "verb": "GET"}]
    assert payload == {
        "exchange": "bitmex",
        "market_type": "futures",
        "symbol": "XBTUSD",
        "permission_probe": "apiKey",
        "can_place_orders": False,
        "api_key_enabled": True,
        "permissions": ["orderCancel"],
        "reason": "missing_order_write_permission",
    }


def test_bitmex_permission_status_reports_order_write_key() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexRestClient,
        market_type="futures",
        api_key_payload=[{"id": "k", "permissions": ["order"], "enabled": True}],
    )

    payload = adapter.permission_status()

    assert payload["can_place_orders"] is True
    assert payload["api_key_enabled"] is True
    assert payload["permissions"] == ["order"]
    assert payload["reason"] == "ok"


def test_bitmex_place_order_allows_order_write_key() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBT_USDT",
        client_factory=_FakeBitmexRestClient,
        market_type="spot",
        api_key_payload=[{"id": "k", "permissions": ["order"], "enabled": True}],
    )

    ack = adapter.place_order("buy", 0.125, price=100.5, type_="Limit")

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert [call["path"] for call in client.curl_calls] == ["apiKey", "order"]
    assert ack.status == "New"


def test_bitmex_cancel_client_id_uses_legacy_clidlist_without_direct_rest() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexClient,
        market_type="futures",
    )

    ack = adapter.cancel_order("T2bitmex-260610000001")

    client = _FakeBitmexClient.last_instance
    assert client is not None
    assert client.last_cancel == {"clIDList": ["T2bitmex-260610000001"]}
    assert ack.client_order_id == "T2bitmex-260610000001"
    assert ack.status == "Canceled"


def test_bitmex_cancel_configured_prefix_uses_client_id() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexRestClient,
        market_type="futures",
        orderIDPrefix="mlk_",
    )

    adapter.cancel_order("mlk_operator_1")

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert client.curl_calls == [
        {
            "path": "order",
            "postdict": {"clOrdID": "mlk_operator_1"},
            "verb": "DELETE",
        }
    ]


def test_bitmex_spot_place_uses_direct_rest_without_quantity_rounding(
    postgres_url_factory,
) -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBT_USDT",
        client_factory=_FakeBitmexRestClient,
        environment="demo",
        market_type="spot",
        audit_db_url=postgres_url_factory("audit"),
    )

    ack = adapter.place_order(
        "buy",
        0.125,
        price=100.5,
        type_="Limit",
        clOrdID="H1bitmexspot-260610000000",
    )

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert not hasattr(client, "last_place")
    assert client.curl_calls == [
        {
            "path": "apiKey",
            "verb": "GET",
        },
        {
            "path": "order",
            "postdict": {
                "symbol": "XBT_USDT",
                "side": "Buy",
                "orderQty": 0.125,
                "ordType": "Limit",
                "price": 100.5,
                "clOrdID": "H1bitmexspot-260610000000",
            },
            "verb": "POST",
        }
    ]
    assert ack.order_id == "OID-DIRECT"
    assert ack.client_order_id == "H1bitmexspot-260610000000"
    assert ack.orig_qty == 0.125
    assert adapter._audit_engine is not None
    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    assert row.request_params["orderQty"] == 0.125
    assert row.request_params["side"] == "Buy"


def test_bitmex_amend_uses_direct_payload_when_available() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBTUSD",
        client_factory=_FakeBitmexRestClient,
        market_type="futures",
    )

    ack = adapter.amend_order("OID-1", price=101.5, orderQty=0.25)

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert client.curl_calls == [
        {
            "path": "order",
            "postdict": {
                "orderID": "OID-1",
                "price": 101.5,
                "orderQty": 0.25,
            },
            "verb": "PUT",
        }
    ]
    assert ack.order_id == "OID-1"
    assert ack.price == 101.5
    assert ack.orig_qty == 0.25


def test_get_adapter_loads_bitmex_market_types() -> None:
    assert get_adapter("bitmex", "futures") is BitmexAdapter
    assert get_adapter("bitmex", "spot") is BitmexAdapter


def test_bitmex_spot_rejects_derivatives_only_exec_flags() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBT_USDT",
        client_factory=_FakeBitmexClient,
        market_type="spot",
    )

    with pytest.raises(ValueError, match="spot orders do not support"):
        adapter.place_order(
            "sell",
            1,
            price=90,
            type_="Limit",
            execInst="ReduceOnly,IndexPrice",
        )


def test_bitmex_spot_rejects_reduce_only_param() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBT_USDT",
        client_factory=_FakeBitmexClient,
        market_type="spot",
    )

    with pytest.raises(ValueError, match="spot orders do not support"):
        adapter.place_order(
            "sell",
            1,
            price=90,
            type_="Limit",
            reduceOnly=True,
        )


def test_bitmex_spot_rejects_trigger_order_types_without_exec_flags() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBT_USDT",
        client_factory=_FakeBitmexClient,
        market_type="spot",
    )

    with pytest.raises(ValueError, match="only support Limit and Market"):
        adapter.place_order("sell", 1, stopPx=90, type_="Stop")


def test_bitmex_spot_position_reads_margin_balance_without_futures_position_call() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBT_USDT",
        client_factory=_FakeBitmexClient,
        market_type="spot",
    )
    client = _FakeBitmexClient.last_instance
    assert client is not None

    position = adapter.get_position()
    balance = adapter.get_balance()

    assert position.qty == 0.25
    assert position.entry_price is None
    assert balance == 800.0
    assert client.last_margin_currency == "all"
    assert not hasattr(client, "last_position_symbol")


def test_bitmex_spot_position_uses_direct_margin_all_when_available() -> None:
    adapter = BitmexAdapter(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="XBT_USDT",
        client_factory=_FakeBitmexRestClient,
        market_type="spot",
    )

    position = adapter.get_position()
    balance = adapter.get_balance()

    client = _FakeBitmexRestClient.last_instance
    assert isinstance(client, _FakeBitmexRestClient)
    assert position.qty == 0.125
    assert balance == 450.0
    assert client.curl_calls == [
        {
            "path": "user/margin",
            "verb": "GET",
            "query": {"currency": "all"},
        },
        {
            "path": "user/margin",
            "verb": "GET",
            "query": {"currency": "all"},
        },
    ]
