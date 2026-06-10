from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

import pytest
from kolabi.shared.exchanges import get_adapter
from kolabi.shared.exchanges.kraken_adapter import (
    KrakenFuturesAdapter,
    _extract_available_margin,
    _map_order_status_from_payload,
    build_exec_orders,
)
from kolabi.shared.persistence import (
    Base,
    ExchangeFill,
    ExchangeInstrument,
    ExchangeOrder,
    ExchangeRestCall,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


class DummyResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

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
        current = self.responses.pop(0)
        if isinstance(current, tuple) and len(current) == 2:
            status_code, payload = current
            return DummyResponse(payload, status_code=int(status_code))
        return DummyResponse(current)


def test_get_adapter_loads_kraken():
    adapter_cls = get_adapter("kraken")
    assert adapter_cls.__name__ == "KrakenFuturesAdapter"


def test_default_audit_lane_is_postgres_environment_scoped(monkeypatch) -> None:
    monkeypatch.setattr(Base.metadata, "create_all", lambda *args, **kwargs: None)
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url="postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/account",
        session=cast(Any, DummySession([])),
    )

    assert adapter.audit_db_url == "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_audit"


def test_place_maps_limit_order_to_sendorder(postgres_url_factory):
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
        account_db_url=postgres_url_factory("prv"),
        audit_db_url=postgres_url_factory("audit"),
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
    with Session(adapter._audit_engine) as db_session:
        rows = db_session.execute(select(ExchangeRestCall)).scalars().all()
    assert len(rows) == 1
    assert rows[0].path == "/sendorder"
    assert rows[0].result_kind == "ok"
    assert rows[0].client_order_id == "CID-1"
    assert rows[0].endpoint_order_id == "OID-1"


def test_duplicate_client_id_status_maps_to_new() -> None:
    status = _map_order_status_from_payload({"status": "clientOrderIdAlreadyExist"})

    assert status == "New"


def test_sendorder_http_error_is_persisted_for_forensics(postgres_url_factory):
    session = DummySession(
        [
            (503, {"raw_text": "Service Unavailable"}),
            (503, {"raw_text": "Service Unavailable"}),
            (503, {"raw_text": "Service Unavailable"}),
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        audit_db_url=postgres_url_factory("audit"),
        session=cast(Any, session),
    )

    with pytest.raises(RuntimeError):
        adapter.place(
            orderQty=2,
            side="buy",
            ordType="Limit",
            price=80000,
            clOrdID="CID-ERR",
        )

    with Session(adapter._audit_engine) as db_session:
        rows = db_session.execute(select(ExchangeRestCall)).scalars().all()
    assert len(rows) == 1
    assert rows[0].path == "/sendorder"
    assert rows[0].result_kind == "http_error"
    assert rows[0].http_status == 503
    assert rows[0].client_order_id == "CID-ERR"


def test_openorders_get_is_not_persisted_in_rest_call_audit(postgres_url_factory):
    session = DummySession(
        [
            {"result": "success", "openOrders": []},
        ]
    )
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        audit_db_url=postgres_url_factory("audit"),
        session=cast(Any, session),
    )

    assert adapter.live_open_orders() == []
    with Session(adapter._audit_engine) as db_session:
        rows = db_session.execute(select(ExchangeRestCall)).scalars().all()
    assert rows == []


def test_record_rest_call_serializes_decimal_request_params(postgres_url_factory) -> None:
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        audit_db_url=postgres_url_factory("audit"),
        session=cast(Any, DummySession([])),
    )

    adapter._record_rest_call(
        method="POST",
        path="/editorder",
        request_params=[("order_id", "OID-1"), ("stopPx", Decimal("77245.0"))],
        attempt_count=1,
        http_status=200,
        response_payload={"result": "success"},
        result_kind="ok",
        error_text=None,
    )

    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    assert row.request_params["stopPx"] == "77245.0"


def test_record_rest_call_serializes_nested_decimal_payload(postgres_url_factory) -> None:
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        audit_db_url=postgres_url_factory("audit"),
        session=cast(Any, DummySession([])),
    )

    adapter._record_rest_call(
        method="POST",
        path="/sendorder",
        request_params=[("cliOrdId", "t-fox-260526010101")],
        attempt_count=1,
        http_status=200,
        response_payload={
            "sendStatus": {
                "order_id": "OID-X",
                "nested": {"stopPx": Decimal("77166.5")},
                "history": [Decimal("1.2"), "ok"],
            }
        },
        result_kind="ok",
        error_text=None,
    )

    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    nested = row.response_payload["sendStatus"]["nested"]
    history = row.response_payload["sendStatus"]["history"]
    assert nested["stopPx"] == "77166.5"
    assert history[0] == "1.2"


def test_record_rest_call_recovers_order_id_from_request_for_edit_failures(postgres_url_factory) -> None:
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        audit_db_url=postgres_url_factory("audit"),
        session=cast(Any, DummySession([])),
    )

    adapter._record_rest_call(
        method="POST",
        path="/editorder",
        request_params=[("order_id", "OID-EDIT-1"), ("stopPrice", 77166.5)],
        attempt_count=1,
        http_status=503,
        response_payload={"raw_text": "Service Unavailable"},
        result_kind="http_error",
        error_text="503",
    )

    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    assert row.endpoint_order_id == "OID-EDIT-1"
    assert row.exchange_order_id == "OID-EDIT-1"
    assert row.correlation_id == "OID-EDIT-1"


def test_record_rest_call_recovers_order_id_from_request_for_cancel_failures(postgres_url_factory) -> None:
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        audit_db_url=postgres_url_factory("audit"),
        session=cast(Any, DummySession([])),
    )

    adapter._record_rest_call(
        method="POST",
        path="/cancelorder",
        request_params=[("order_id", "OID-CANCEL-1")],
        attempt_count=1,
        http_status=503,
        response_payload={"raw_text": "Service Unavailable"},
        result_kind="http_error",
        error_text="503",
    )

    with Session(adapter._audit_engine) as db_session:
        row = db_session.execute(select(ExchangeRestCall)).scalars().one()
    assert row.endpoint_order_id == "OID-CANCEL-1"
    assert row.exchange_order_id == "OID-CANCEL-1"
    assert row.correlation_id == "OID-CANCEL-1"


def test_record_rest_call_persistence_failure_is_fail_open(postgres_url_factory, caplog) -> None:
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, DummySession([])),
    )

    class _FailSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def add(self, _obj: object) -> None:
            return

        def commit(self) -> None:
            raise RuntimeError("forced commit failure")

        def rollback(self) -> None:
            return

    def _fail_sessionmaker():
        return _FailSession()

    adapter._audit_sessionmaker = _fail_sessionmaker  # type: ignore[assignment]

    with caplog.at_level("WARNING"):
        adapter._record_rest_call(
            method="POST",
            path="/editorder",
            request_params=[("cliOrdId", "t-fail-260526010102")],
            attempt_count=1,
            http_status=200,
            response_payload={"result": "success"},
            result_kind="ok",
            error_text=None,
        )
    assert "rest call audit persistence failed" in caplog.text
    assert adapter.rest_audit_errors
    assert "forced commit failure" in adapter.rest_audit_errors[0]


def test_record_rest_call_prunes_audit_history_by_limit(postgres_url_factory) -> None:
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        audit_db_url=postgres_url_factory("audit"),
        rest_audit_retention_minutes=0,
        rest_audit_retention_limit=1,
        session=cast(Any, DummySession([])),
    )
    with Session(adapter._audit_engine) as db_session:
        db_session.add(
            ExchangeRestCall(
                local_uuid="old",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                symbol="PI_XBTUSD",
                method="POST",
                path="/sendorder",
                request_params={},
                attempt_count=1,
                result_kind="ok",
                response_payload={},
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        db_session.commit()

    adapter._record_rest_call(
        method="POST",
        path="/sendorder",
        request_params=[("cliOrdId", "CID-NEW")],
        attempt_count=1,
        http_status=200,
        response_payload={"sendStatus": {"order_id": "OID-NEW", "cli_ord_id": "CID-NEW"}},
        result_kind="ok",
        error_text=None,
    )

    with Session(adapter._audit_engine) as db_session:
        rows = db_session.execute(select(ExchangeRestCall)).scalars().all()
    assert [row.client_order_id for row in rows] == ["CID-NEW"]


def test_place_generates_client_order_id_when_missing(postgres_url_factory):
    session = DummySession(
        [
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-GEN-1",
                    "status": "placed",
                    "qty": 1,
                    "filled": 0,
                    "direction": 0,
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
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    reply = adapter.place(orderQty=1, side="buy", ordType="Limit", price=80000)

    payload = dict(session.calls[0]["data"])
    assert payload["cliOrdId"].startswith("k-")
    assert len(payload["cliOrdId"]) == 34
    assert reply["clOrdID"] == payload["cliOrdId"]


def test_sendorder_without_client_order_id_is_not_retried_on_503(postgres_url_factory):
    session = DummySession(
        [
            (503, {"raw_text": "Service Unavailable"}),
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-LATE",
                    "status": "placed",
                    "qty": 1,
                    "filled": 0,
                    "direction": 0,
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
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    try:
        adapter._request(
            "POST",
            "/sendorder",
            params=[("orderType", "mkt"), ("symbol", "PI_XBTUSD"), ("side", "buy"), ("size", 1)],
            auth=True,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected single-attempt sendorder failure without cliOrdId")

    assert len(session.calls) == 1


def test_sendorder_with_client_order_id_retries_on_503(postgres_url_factory):
    session = DummySession(
        [
            (503, {"raw_text": "Service Unavailable"}),
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-OK",
                    "cli_ord_id": "CID-RETRY",
                    "status": "placed",
                    "qty": 1,
                    "filled": 0,
                    "direction": 0,
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
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    payload = adapter._request(
        "POST",
        "/sendorder",
        params=[
            ("orderType", "mkt"),
            ("symbol", "PI_XBTUSD"),
            ("side", "buy"),
            ("size", 1),
            ("cliOrdId", "CID-RETRY"),
        ],
        auth=True,
    )

    assert payload["result"] == "success"
    assert len(session.calls) == 2


def test_place_fills_ack_defaults_when_sendstatus_is_sparse(postgres_url_factory):
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
        account_db_url=postgres_url_factory("prv"),
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


def test_place_order_merges_execinst_and_reduceonly_without_duplicate_kwarg(postgres_url_factory):
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
        account_db_url=postgres_url_factory("prv"),
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


def test_live_trigger_orders_normalises_nested_order_trigger_payload(postgres_url_factory):
    session = DummySession(
        [
            {
                "result": "success",
                "openOrders": [
                    {
                        "order_id": "OID-T",
                        "cli_ord_id": "CID-T",
                        "symbol": "PI_XBTUSD",
                        "direction": 1,
                        "qty": "1",
                        "reduceOnly": True,
                        "status": "untouched",
                        "orderTrigger": {
                            "type": "stp",
                            "stopPrice": "75454.94",
                            "triggerSignal": "last",
                        },
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
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    orders = adapter.live_trigger_orders()

    assert orders == [
        {
            "order_id": "OID-T",
            "client_order_id": "CID-T",
            "symbol": "PI_XBTUSD",
            "side": "sell",
            "order_type": "stp",
            "qty": 1.0,
            "filled": None,
            "price": None,
            "stop_price": 75454.94,
            "trigger_signal": "last",
            "reduce_only": True,
            "status": "New",
        }
    ]


def test_place_order_rounds_stop_price_to_cached_tick_size(postgres_url_factory):
    public_db_url = postgres_url_factory("pub")
    session = DummySession(
        [
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-T",
                    "cli_ord_id": "CID-T",
                    "status": "placed",
                    "qty": 1,
                    "filled": 0,
                    "direction": 1,
                    "stop_price": 75436.5,
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
        account_db_url=postgres_url_factory("prv"),
        public_db_url=public_db_url,
        session=cast(Any, session),
    )
    with Session(adapter._public_engine) as db_session:
        db_session.add(
            ExchangeInstrument(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                tick_size=0.5,
            )
        )
        db_session.commit()

    adapter.place_order(
        side="sell",
        orderQty=1,
        stopPx=75436.7175,
        type_="S",
        execInst="ReduceOnly,LastPrice",
        clOrdID="CID-T",
    )

    sent_payload = dict(session.calls[0]["data"])
    assert sent_payload["stopPrice"] == 75436.5


@pytest.mark.parametrize(
    ("exec_inst", "expected_signal"),
    (
        ("ReduceOnly,LastPrice", "last"),
        ("ReduceOnly,MarkPrice", "mark"),
        ("ReduceOnly,IndexPrice", "index"),
    ),
)
def test_place_order_maps_execinst_trigger_signal(exec_inst, expected_signal, postgres_url_factory):
    session = DummySession(
        [
            {
                "result": "success",
                "sendStatus": {
                    "order_id": "OID-T",
                    "cli_ord_id": "CID-T",
                    "status": "placed",
                    "qty": 1,
                    "filled": 0,
                    "direction": 1,
                    "stop_price": 75436.5,
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
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    adapter.place_order(
        side="sell",
        orderQty=1,
        stopPx=75436.5,
        type_="S",
        execInst=exec_inst,
        clOrdID="CID-T",
    )

    sent_payload = dict(session.calls[0]["data"])
    assert sent_payload["triggerSignal"] == expected_signal


def test_authenticated_requests_retry_with_increasing_nonce(postgres_url_factory, monkeypatch):
    session = DummySession(
        [
            {
                "result": "error",
                "error": "nonceBelowThreshold: TOO_SMALL",
            },
            {
                "result": "success",
                "openOrders": [],
            },
        ]
    )
    monkeypatch.setattr("kolabi.shared.exchanges.kraken_adapter.time.time", lambda: 1.0)
    monkeypatch.setattr("kolabi.shared.exchanges.kraken_adapter.time.sleep", lambda _seconds: None)
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    assert adapter.live_trigger_orders() == []
    first_nonce = int(session.calls[0]["headers"]["Nonce"])
    second_nonce = int(session.calls[1]["headers"]["Nonce"])
    assert second_nonce == first_nonce + 1


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


def test_open_orders_reads_private_db(postgres_url_factory):
    db_url = postgres_url_factory("prv")
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


def test_live_trigger_orders_db_reads_open_stop_rows(postgres_url_factory):
    db_url = postgres_url_factory("prv")
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
                local_uuid="u-stop",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                symbol="PI_XBTUSD",
                exchange_order_id="OID-T",
                client_order_id="CID-T",
                side="sell",
                order_type="stop",
                status="open",
                price=77000.5,
                quantity=2,
                filled_quantity=0,
                reduce_only=True,
            )
        )
        session.commit()

    orders = adapter.live_trigger_orders_db()

    assert len(orders) == 1
    assert orders[0]["order_id"] == "OID-T"
    assert orders[0]["client_order_id"] == "CID-T"
    assert orders[0]["stop_price"] == 77000.5
    assert orders[0]["reduce_only"] is True


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


def test_validate_symbol_suggests_pi_for_pf_prefix(postgres_url_factory):
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
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    try:
        adapter.validate_symbol("PF_ADAUSD")
    except ValueError as exc:
        assert "PI_ADAUSD" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid PF_ symbol")


def test_amend_maps_to_editorder(postgres_url_factory):
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
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    ack = adapter.amend_order("OID-1", price=80100, orderQty=3)

    assert ack.order_id == "OID-1"
    assert session.calls[0]["url"].endswith("/editorder")
    sent_payload = dict(session.calls[0]["data"])
    assert sent_payload["order_id"] == "OID-1"
    assert sent_payload["limitPrice"] == 80100
    assert sent_payload["size"] == 3


def test_amend_rounds_tail_stop_to_contract_tick(postgres_url_factory):
    public_db_url = postgres_url_factory("pub")
    session = DummySession(
        [
            {
                "result": "success",
                "editStatus": {
                    "order_id": "OID-1",
                    "cli_ord_id": "CID-1",
                    "qty": 3,
                    "filled": 0,
                    "direction": 1,
                    "status": "edited",
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
        account_db_url=postgres_url_factory("prv"),
        public_db_url=public_db_url,
        session=cast(Any, session),
    )
    with Session(adapter._public_engine) as db_session:
        db_session.add(
            ExchangeInstrument(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                tick_size=0.5,
            )
        )
        db_session.commit()

    ack = adapter.amend_order("OID-1", stopPx=74757.668749999999, orderQty=3)

    sent_payload = dict(session.calls[0]["data"])
    assert sent_payload["stopPrice"] == 74757.5
    assert ack.status == "New"
    assert ack.price == 74757.5


def test_amend_invalid_price_maps_to_rejected_ack(postgres_url_factory):
    session = DummySession(
        [
            {
                "result": "success",
                "editStatus": {
                    "order_id": "OID-1",
                    "status": "invalidPrice",
                    "reason": "INVALID_PRICE",
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
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    ack = adapter.amend_order("OID-1", stopPx=74757.668749999999, orderQty=3)

    assert ack.status == "Rejected"


def test_cancel_sparse_response_maps_to_canceled_status(postgres_url_factory):
    session = DummySession([{"result": "success", "cancelStatus": {"order_id": "OID-1"}}])
    adapter = KrakenFuturesAdapter(
        api_key="k",
        api_secret="c2VjcmV0",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        environment="demo",
        account_db_url=postgres_url_factory("prv"),
        session=cast(Any, session),
    )

    ack = adapter.cancel_order("OID-1")

    assert ack.order_id == "OID-1"
    assert ack.status == "Canceled"


def test_live_order_normalization_reads_camel_case_price_and_quantity(postgres_url_factory):
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
        account_db_url=postgres_url_factory("prv"),
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
            "trigger_signal": "",
            "reduce_only": False,
            "status": "New",
        }
    ]


def test_validate_symbol_syncs_instrument_rules_to_public_db(postgres_url_factory):
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
        account_db_url=postgres_url_factory("prv"),
        public_db_url=postgres_url_factory("pub"),
        session=cast(Any, session),
    )

    adapter.validate_symbol("PI_XBTUSD")

    with Session(adapter._public_engine) as db:
        row = db.execute(select(ExchangeInstrument)).scalars().one()
        assert row.symbol == "PI_XBTUSD"
        assert row.tick_size == 0.5
        assert row.min_quantity == 1.0
