from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from kolabi.shared.exchanges import get_adapter
from kolabi.shared.exchanges.kraken_adapter import (
    KrakenFuturesAdapter,
    _extract_available_margin,
    build_exec_orders,
)
from kolabi.shared.persistence import (
    Base,
    ExchangeFill,
    ExchangeInstrument,
    ExchangeOrder,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


class DummyResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class DummySession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return DummyResponse(self.responses.pop(0))


def test_get_adapter_loads_kraken():
    adapter_cls = get_adapter("kraken")
    assert adapter_cls.__name__ == "KrakenFuturesAdapter"


def test_place_maps_limit_order_to_sendorder(tmp_path):
    session = DummySession(
        [
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-1",
                    "cli_ord_id": "CID-1",
                    "limit_price": 80000.0,
                    "qty": 2,
                    "filled": 0,
                    "direction": 0,
                    "reason": "new_placed_order_by_user",
                    "last_update_time": 1778371200000,
                },
            },
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-1",
                    "cli_ord_id": "CID-1",
                    "limit_price": 80000.0,
                    "qty": 2,
                    "filled": 0,
                    "direction": 0,
                    "reason": "new_placed_order_by_user",
                    "last_update_time": 1778371200000,
                },
            },
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=f"sqlite:///{tmp_path / 'prv.sqlite'}",
        session=cast(Any, session),
    )

    reply = adapter.place(
        orderQty=2,
        side="buy",
        ordType="Limit",
        price=80000,
        clOrdID="CID-1",
        execInst="ParticipateDoNotInitiate",
    )

    assert reply["orderID"] == "OID-1"
    assert reply["clOrdID"] == "CID-1"
    assert session.calls[0]["url"].endswith("/sendorder")
    sent_payload = dict(session.calls[0]["data"])
    assert sent_payload["orderType"] == "lmt"
    assert sent_payload["postOnly"] is True


def test_place_fills_ack_defaults_when_sendstatus_is_sparse(tmp_path):
    session = DummySession(
        [
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-2",
                    "status": "placed",
                    "orderEvents": [
                        {
                            "type": "EXECUTION",
                            "price": 79006.0,
                            "amount": 1,
                        }
                    ],
                },
            }
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=f"sqlite:///{tmp_path / 'prv.sqlite'}",
        session=cast(Any, session),
    )

    ack = adapter.place_order(
        side="sell",
        orderQty=1,
        type_="MARKET",
        reduceOnly=True,
    )

    assert ack.order_id == "OID-2"
    assert ack.side == "Sell"
    assert ack.orig_qty == 1.0
    assert ack.executed_qty == 1.0
    assert ack.price == 79006.0
    assert ack.status == "Filled"
    sent_payload = dict(session.calls[0]["data"])
    assert sent_payload["orderType"] == "mkt"
    assert "limitPrice" not in sent_payload


def test_place_order_merges_execinst_and_reduceonly_without_duplicate_kwarg(tmp_path):
    session = DummySession(
        [
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-3",
                    "cli_ord_id": "CID-3",
                    "limit_price": 79000.0,
                    "qty": 1,
                    "filled": 0,
                    "direction": 0,
                    "reason": "new_placed_order_by_user",
                    "last_update_time": 1778371200000,
                },
            }
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=f"sqlite:///{tmp_path / 'prv.sqlite'}",
        session=cast(Any, session),
    )

    ack = adapter.place_order(
        side="buy",
        orderQty=1,
        price=79000.0,
        type_="LIMIT",
        execInst="ParticipateDoNotInitiate",
        reduceOnly=True,
        clOrdID="CID-3",
    )

    assert ack.order_id == "OID-3"
    sent_payload = dict(session.calls[0]["data"])
    assert sent_payload["postOnly"] is True
    assert sent_payload["reduceOnly"] is True


def test_build_exec_orders_maps_rows_and_fills():
    order = ExchangeOrder(
        id=1,
        local_uuid="u1",
        exchange="kraken",
        environment="demo",
        market_type="futures",
        account_scope="default",
        symbol="PI_XBTUSD",
        exchange_order_id="OID-1",
        client_order_id="CID-1",
        side="buy",
        order_type="limit",
        status="filled",
        price=80000,
        quantity=2,
        filled_quantity=2,
        source_timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
        local_timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )
    fill = ExchangeFill(
        id=1,
        local_uuid="f1",
        order_id=1,
        exchange="kraken",
        exchange_fill_id="FID-1",
        price=80001,
        quantity=2,
        source_timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
        local_timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )

    rows = build_exec_orders([order], [fill])

    assert any(row["execType"] == "Trade" for row in rows)
    assert any(row["ordStatus"] == "Filled" for row in rows)


def test_open_orders_reads_private_db(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=db_url,
        session=cast(Any, DummySession([])),
    )
    Base.metadata.create_all(adapter._engine)
    with Session(adapter._engine) as session:
        session.add(
            ExchangeOrder(
                local_uuid="u1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                symbol="PI_XBTUSD",
                exchange_order_id="OID-1",
                client_order_id="CID-1",
                side="buy",
                order_type="limit",
                status="open",
                price=80000,
                quantity=2,
                filled_quantity=0,
            )
        )
        session.commit()

    rows = adapter.open_orders()

    assert len(rows) == 1
    assert rows[0]["orderID"] == "OID-1"


def test_extract_available_margin_reads_auxiliary_available_funds():
    payload = {
        "accounts": {
            "flex": {
                "name": "flex",
                "auxiliary": {"availableFunds": 123.45},
            }
        }
    }

    available = _extract_available_margin(payload)

    assert available == 123.45


def test_validate_symbol_suggests_pi_for_pf_prefix(tmp_path):
    session = DummySession(
        [
            {
                "result": "success",
                "instruments": [
                    {"symbol": "PI_ADAUSD", "tradeable": True},
                    {"symbol": "PI_XBTUSD", "tradeable": True},
                ],
            }
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PF_ADAUSD",
        environment="demo",
        account_db_url=f"sqlite:///{tmp_path / 'prv.sqlite'}",
        session=cast(Any, session),
    )

    try:
        adapter.validate_symbol("PF_ADAUSD")
    except ValueError as exc:
        assert "PI_ADAUSD" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid PF_ symbol")


def test_amend_maps_to_editorder(tmp_path):
    session = DummySession(
        [
            {
                "result": "success",
                "editStatus": {
                    "order_id": "OID-1",
                    "cli_ord_id": "CID-1",
                    "limit_price": 80100.0,
                    "qty": 3,
                    "filled": 0,
                    "direction": 0,
                    "reason": "edited_by_user",
                    "last_update_time": 1778371201000,
                },
            }
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=f"sqlite:///{tmp_path / 'prv.sqlite'}",
        session=cast(Any, session),
    )

    ack = adapter.amend_order("OID-1", price=80100, orderQty=3)

    assert ack.order_id == "OID-1"
    assert session.calls[0]["url"].endswith("/editorder")
    sent_payload = dict(session.calls[0]["data"])
    assert sent_payload["order_id"] == "OID-1"
    assert sent_payload["limitPrice"] == 80100
    assert sent_payload["size"] == 3


def test_cancel_sparse_response_maps_to_canceled_status(tmp_path):
    session = DummySession([{"result": "success", "cancelStatus": {"order_id": "OID-1"}}])
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=f"sqlite:///{tmp_path / 'prv.sqlite'}",
        session=cast(Any, session),
    )

    ack = adapter.cancel_order("OID-1")

    assert ack.order_id == "OID-1"
    assert ack.status == "Canceled"


def test_live_order_normalization_reads_camel_case_price_and_quantity(tmp_path):
    session = DummySession(
        [
            {
                "result": "success",
                "openOrders": [
                    {
                        "orderId": "OID-1",
                        "symbol": "PI_XBTUSD",
                        "side": "buy",
                        "orderType": "lmt",
                        "unfilledSize": 2,
                        "limitPrice": 70000.0,
                        "filledSize": 0,
                    }
                ],
            }
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=f"sqlite:///{tmp_path / 'prv.sqlite'}",
        session=cast(Any, session),
    )

    rows = adapter.live_open_orders()

    assert rows == [
        {
            "order_id": "OID-1",
            "client_order_id": "",
            "symbol": "PI_XBTUSD",
            "side": "buy",
            "order_type": "lmt",
            "qty": 2.0,
            "filled": 0.0,
            "price": 70000.0,
            "stop_price": None,
            "status": "New",
        }
    ]


def test_validate_symbol_syncs_instrument_rules_to_public_db(tmp_path):
    session = DummySession(
        [
            {
                "result": "success",
                "instruments": [
                    {
                        "symbol": "PI_XBTUSD",
                        "type": "futures_inverse",
                        "tradeable": True,
                        "tickSize": 0.5,
                        "contractSize": 1,
                    }
                ],
            }
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=f"sqlite:///{tmp_path / 'prv.sqlite'}",
        public_db_url=f"sqlite:///{tmp_path / 'pub.sqlite'}",
        session=cast(Any, session),
    )

    adapter.validate_symbol("PI_XBTUSD")

    with Session(adapter._public_engine) as db:
        row = db.execute(select(ExchangeInstrument)).scalars().one()
        assert row.symbol == "PI_XBTUSD"
        assert row.tick_size == 0.5
        assert row.min_quantity == 1.0
