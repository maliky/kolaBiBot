from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from kolabi.shared.persistence import (
    AccountBalance,
    AccountPosition,
    ExchangeFill,
    ExchangeOrder,
    PrivateIngestAudit,
    RawExchangeEvent,
)
from kolabi.tree import account as account_module
from kolabi.tree.account import (
    AccountStateStore,
    AccountStreamConfig,
    BalanceWrite,
    FillWrite,
    IngestMessage,
    KrakenFuturesCredentials,
    KrakenFuturesPrivateStream,
    KrakenFuturesRestReconciler,
    OrderWrite,
    PrivateIngestMirror,
    PrivateStreamProfile,
    critical_mirror_config,
    critical_private_config,
    map_balances,
    map_fill_event,
    map_order,
    map_positions,
    map_rest_balances,
    prune_raw_events,
    sign_challenge,
    sign_rest_auth,
    stream_kind_uses_critical_db,
    subscribe_messages,
    upgrade_private_schema,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


def _result_items(result: dict[str, object], key: str) -> list[Any]:
    return cast(list[Any], result[key])


def test_account_state_status_is_empty_before_events(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))

    status = store.latest_status("private_ws")

    assert status["status"] == "empty"
    assert status["stream_kind"] == "private_ws"


def test_record_connection_status_updates_existing_row(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    first_time = datetime(2026, 5, 6, 1, 0, tzinfo=timezone.utc)
    second_time = datetime(2026, 5, 6, 1, 1, tzinfo=timezone.utc)

    first = store.record_connection_status("private_ws", "connecting", first_time)
    second = store.record_connection_status("private_ws", "healthy", second_time)

    assert first.id == second.id
    assert second.status == "healthy"
    assert second.last_heartbeat_at == second_time.replace(tzinfo=None)


def test_reconnecting_status_does_not_refresh_connection_heartbeat(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    healthy_time = datetime(2026, 5, 6, 1, 0, tzinfo=timezone.utc)
    reconnect_time = datetime(2026, 5, 6, 1, 1, tzinfo=timezone.utc)

    healthy = store.record_connection_status("private_ws", "healthy", healthy_time)
    reconnecting = store.record_connection_status(
        "private_ws",
        "reconnecting",
        reconnect_time,
        last_error="server rejected WebSocket connection: HTTP 503",
    )

    assert healthy.id == reconnecting.id
    assert reconnecting.status == "reconnecting"
    assert reconnecting.updated_at == reconnect_time.replace(tzinfo=None)
    assert reconnecting.last_heartbeat_at == healthy_time.replace(tzinfo=None)
    assert reconnecting.last_error == "server rejected WebSocket connection: HTTP 503"


def test_critical_liveness_refresh_updates_private_ws_alias(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    config = AccountStreamConfig(db_url=db_url, health_write_seconds=1.0)
    store = AccountStateStore(config)
    profile = PrivateStreamProfile(
        name="critical",
        stream_kind="private_ws_critical",
        feeds=config.critical_feeds,
        is_critical=True,
        health_aliases=("private_ws",),
    )
    stream = KrakenFuturesPrivateStream(
        config,
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
        profile=profile,
    )
    baseline = datetime(2026, 5, 6, 1, 0, tzinfo=timezone.utc)
    store.record_connection_status("private_ws_critical", "healthy", baseline)
    store.record_connection_status("private_ws", "healthy", baseline)

    stream._liveness_enabled = True
    stream._last_health_write_monotonic = time.monotonic() - 5.0
    stream._record_local_liveness_due(time.monotonic())

    baseline_naive = baseline.replace(tzinfo=None)
    status_alias = store.latest_status("private_ws")
    status_critical = store.latest_status("private_ws_critical")
    assert datetime.fromisoformat(str(status_alias["updated_at"])) > baseline_naive
    assert datetime.fromisoformat(str(status_critical["updated_at"])) > baseline_naive


def test_account_liveness_refresh_does_not_touch_private_ws_alias(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    config = AccountStreamConfig(db_url=db_url, health_write_seconds=1.0)
    store = AccountStateStore(config)
    profile = PrivateStreamProfile(
        name="account",
        stream_kind="private_ws_account",
        feeds=config.account_feeds,
        is_critical=False,
    )
    stream = KrakenFuturesPrivateStream(
        config,
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
        profile=profile,
    )
    baseline = datetime(2026, 5, 6, 1, 0, tzinfo=timezone.utc)
    store.record_connection_status("private_ws", "healthy", baseline)
    store.record_connection_status("private_ws_account", "healthy", baseline)

    stream._liveness_enabled = True
    stream._last_health_write_monotonic = time.monotonic() - 5.0
    stream._record_local_liveness_due(time.monotonic())

    baseline_naive = baseline.replace(tzinfo=None)
    status_alias = store.latest_status("private_ws")
    status_account = store.latest_status("private_ws_account")
    assert datetime.fromisoformat(str(status_alias["updated_at"])) == baseline_naive
    assert datetime.fromisoformat(str(status_account["updated_at"])) > baseline_naive


def test_private_ws_status_uses_critical_db_selector() -> None:
    assert stream_kind_uses_critical_db("private_ws") is True
    assert stream_kind_uses_critical_db("private_ws_critical") is True
    assert stream_kind_uses_critical_db("private_ws_account") is False
    assert stream_kind_uses_critical_db("rest_reconciler") is False


def test_critical_stream_writes_critical_db_and_mirrors_account_db(tmp_path):
    account_db = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    critical_db = f"sqlite:///{tmp_path / 'critical.sqlite'}"
    config = AccountStreamConfig(db_url=account_db, critical_db_url=critical_db)
    account_store = AccountStateStore(config)
    critical_config = critical_private_config(config)
    critical_store = AccountStateStore(critical_config)
    profile = PrivateStreamProfile(
        name="critical",
        stream_kind="private_ws_critical",
        feeds=config.critical_feeds,
        is_critical=True,
        health_aliases=("private_ws",),
    )
    stream = KrakenFuturesPrivateStream(
        critical_config,
        critical_store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
        profile=profile,
        mirror_store=account_store,
    )
    message = {
        "feed": "open_orders",
        "order": {
            "instrument": "PI_XBTUSD",
            "direction": 1,
            "type": "stop",
            "qty": 4,
            "filled": 0,
            "stop_price": 73361.5,
            "order_id": "OID-CRIT",
            "cli_ord_id": "CID-CRIT",
            "status": "open",
            "reduce_only": True,
        },
    }

    stream.handle_message(message)
    try:
        with Session(critical_store.engine) as session:
            critical_order = (
                session.execute(
                    select(ExchangeOrder).where(
                        ExchangeOrder.client_order_id == "CID-CRIT"
                    )
                )
                .scalars()
                .one()
            )
            assert critical_order.exchange_order_id == "OID-CRIT"

        mirrored_order = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and mirrored_order is None:
            with Session(account_store.engine) as session:
                mirrored_order = (
                    session.execute(
                        select(ExchangeOrder).where(
                            ExchangeOrder.client_order_id == "CID-CRIT"
                        )
                    )
                    .scalars()
                    .first()
                )
            if mirrored_order is None:
                time.sleep(0.02)
        assert mirrored_order is not None
        assert mirrored_order.exchange_order_id == "OID-CRIT"
    finally:
        stream.stop()


def test_record_order_and_fill(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))

    order = store.record_order(
        OrderWrite(
            symbol="PF_XBTUSD",
            side="buy",
            order_type="limit",
            status="open",
            quantity=1.0,
            exchange_order_id="OID-1",
            client_order_id="CID-1",
            price=80000.0,
            reduce_only=False,
        )
    )
    fill = store.record_fill(
        FillWrite(
            order_id=order.id,
            exchange_fill_id="FID-1",
            price=80001.0,
            quantity=0.25,
            fee=0.01,
            fee_currency="USD",
            liquidity_role="maker",
        )
    )

    with Session(store.engine) as session:
        orders = session.execute(select(ExchangeOrder)).scalars().all()
        fills = session.execute(select(ExchangeFill)).scalars().all()
        assert len(orders) == 1
        assert len(fills) == 1
        assert fills[0].order_id == order.id
        assert fill.exchange_fill_id == "FID-1"


def test_sign_challenge_matches_kraken_documented_example():
    challenge = "c100b894-1729-464d-ace1-52dbce11db42"
    api_secret = (
        "7zxMEF5p/Z8l2p2U7Ghv6x14Af+Fx+92tPgUdVQ748FOIrEoT9bgT+"
        "bTRfXc5pz8na+hL/QdrCVG7bh9KpT0eMTm"
    )

    signed = sign_challenge(challenge, api_secret)

    assert (
        signed
        == "4JEpF3ix66GA2B+ooK128Ift4XQVtc137N9yeg4Kqsn9PI0Kpzbysl9M1IeCEdjg0zl00wkVqcsnG4bmnlMb3A=="
    )


def test_sign_rest_auth_uses_v3_path_without_derivatives_prefix():
    api_secret = (
        "7zxMEF5p/Z8l2p2U7Ghv6x14Af+Fx+92tPgUdVQ748FOIrEoT9bgT+"
        "bTRfXc5pz8na+hL/QdrCVG7bh9KpT0eMTm"
    )
    signed = sign_rest_auth(
        post_data="greeting=hello%20world",
        nonce="1415957147987",
        endpoint_path="/api/v3/orderbook",
        api_secret=api_secret,
    )
    signed_with_wrong_prefix = sign_rest_auth(
        post_data="greeting=hello%20world",
        nonce="1415957147987",
        endpoint_path="/derivatives/api/v3/orderbook",
        api_secret=api_secret,
    )

    assert isinstance(signed, str)
    assert signed
    assert signed != signed_with_wrong_prefix


def test_subscribe_messages_include_signed_challenge_without_secret():
    messages = subscribe_messages(
        feeds=("open_orders", "fills"),
        api_key="public-key",
        challenge="challenge",
        signed_challenge="signed",
    )

    assert messages == [
        {
            "event": "subscribe",
            "feed": "open_orders",
            "api_key": "public-key",
            "original_challenge": "challenge",
            "signed_challenge": "signed",
        },
        {
            "event": "subscribe",
            "feed": "fills",
            "api_key": "public-key",
            "original_challenge": "challenge",
            "signed_challenge": "signed",
        },
    ]


def test_map_open_order_delta_to_normalized_order():
    order = map_order(
        {
            "instrument": "PF_XBTUSD",
            "direction": 0,
            "type": "limit",
            "qty": 2,
            "filled": 0.5,
            "limit_price": 80000,
            "order_id": "OID-1",
            "cli_ord_id": "CID-1",
            "reduce_only": True,
            "reason": "partial_fill",
            "last_update_time": 1778025600000,
        }
    )

    assert order.symbol == "PF_XBTUSD"
    assert order.side == "buy"
    assert order.status == "partial_fill"
    assert order.filled_quantity == 0.5
    assert order.reduce_only is True
    assert order.source_timestamp == datetime(2026, 5, 6, tzinfo=timezone.utc)


def test_map_fill_event_and_record_with_placeholder_order(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    event = map_fill_event(
        {
            "instrument": "PF_XBTUSD",
            "direction": 1,
            "order_id": "OID-2",
            "fill_id": "FID-2",
            "price": "80100",
            "qty": "0.1",
            "fee": "0.02",
            "fee_currency": "USD",
            "liquidity": "taker",
        }
    )

    fill = store.record_fill_event(event)

    with Session(store.engine) as session:
        orders = session.execute(select(ExchangeOrder)).scalars().all()
        fills = session.execute(select(ExchangeFill)).scalars().all()
        assert len(orders) == 1
        assert len(fills) == 1
        assert fill.order_id == orders[0].id
        assert orders[0].exchange_order_id == "OID-2"


def test_fill_event_persists_client_order_id_from_cli_ord_id(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    event = map_fill_event(
        {
            "instrument": "PI_XBTUSD",
            "direction": 0,
            "order_id": "OID-H",
            "cli_ord_id": "kolabi-M-head-1",
            "fill_id": "FID-H",
            "price": "76637",
            "qty": "3",
        }
    )

    store.record_fill_event(event)

    with Session(store.engine) as session:
        order = session.execute(select(ExchangeOrder)).scalars().one()
        assert order.exchange_order_id == "OID-H"
        assert order.client_order_id == "kolabi-M-head-1"


def test_kraken_fill_payload_maps_side_type_and_raw_payload(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    payload = {
        "buy": True,
        "fee_currency": "BTC",
        "fee_paid": 6.55e-09,
        "fill_id": "FID-3",
        "fill_type": "taker",
        "instrument": "PI_XBTUSD",
        "order_id": "OID-3",
        "order_type": "market",
        "price": 76448.0,
        "qty": 1.0,
        "time": 1779577521578,
    }

    event = map_fill_event(payload)
    fill = store.record_fill_event(event)

    assert event.side == "buy"
    assert event.order_type == "market"
    assert event.fee == 6.55e-09
    assert event.liquidity_role == "taker"
    with Session(store.engine) as session:
        order = session.execute(select(ExchangeOrder)).scalars().one()
        stored_fill = session.execute(select(ExchangeFill)).scalars().one()
        assert order.exchange_order_id == "OID-3"
        assert order.side == "buy"
        assert order.order_type == "market"
        assert order.raw_payload["fill_id"] == "FID-3"
        assert stored_fill.id == fill.id
        assert stored_fill.raw_payload["order_id"] == "OID-3"


def test_duplicate_fill_id_is_idempotent(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    event = map_fill_event(
        {
            "buy": False,
            "fill_id": "FID-4",
            "instrument": "PI_XBTUSD",
            "order_id": "OID-4",
            "order_type": "market",
            "price": 76447.0,
            "qty": 1.0,
        }
    )

    first = store.record_fill_event(event)
    second = store.record_fill_event(event)

    assert second.id == first.id
    with Session(store.engine) as session:
        assert len(session.execute(select(ExchangeFill)).scalars().all()) == 1


def test_handle_message_persists_raw_event_and_fill_snapshot(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "fills_snapshot",
        "fills": [
            {
                "buy": True,
                "fill_id": "FID-5",
                "instrument": "PI_XBTUSD",
                "order_id": "OID-5",
                "order_type": "market",
                "price": 76448.0,
                "qty": 1.0,
                "seq": 33,
                "time": 1779577521578,
            }
        ],
    }

    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    stream.handle_message(message)

    with Session(store.engine) as session:
        raw = session.execute(select(RawExchangeEvent)).scalars().one()
        order = session.execute(select(ExchangeOrder)).scalars().one()
        fill = session.execute(select(ExchangeFill)).scalars().one()
        assert raw.event_type == "fills_snapshot"
        assert raw.symbol == "PI_XBTUSD"
        assert raw.correlation_id == "OID-5"
        assert order.exchange_order_id == "OID-5"
        assert fill.exchange_fill_id == "FID-5"


def test_handle_message_logs_order_delta_event(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "open_orders",
        "order": {
            "instrument": "PI_XBTUSD",
            "direction": 0,
            "type": "limit",
            "qty": 10,
            "filled": 0,
            "limit_price": 1000,
            "order_id": "OID-L1",
            "cli_ord_id": "CID-L1",
            "status": "new",
            "stop_price": 950.0,
            "reduce_only": True,
            "reason": "new_placed_order_by_user",
        },
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "order_event feed=open_orders" in caplog.text
    assert "order_id=OID-L1" in caplog.text
    assert "status=new" in caplog.text
    assert "stop_price=950.0" in caplog.text
    assert "reduce_only=True" in caplog.text
    assert "reason=new_placed_order_by_user" in caplog.text


def test_handle_message_logs_cancel_status_on_order_event(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "open_orders",
        "order": {
            "instrument": "PI_XBTUSD",
            "direction": 0,
            "type": "limit",
            "qty": 10,
            "filled": 0,
            "limit_price": 1000,
            "order_id": "OID-C1",
            "cli_ord_id": "CID-C1",
            "is_cancel": True,
            "reason": "requested_by_user",
        },
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "order_event feed=open_orders" in caplog.text
    assert "order_id=OID-C1" in caplog.text
    assert "status=canceled" in caplog.text


def test_handle_message_logs_cancel_status_for_root_order_delta(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "open_orders",
        "order_id": "OID-C2",
        "is_cancel": True,
        "reason": "cancelled_by_user",
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "order_event feed=open_orders" in caplog.text
    assert "order_id=OID-C2" in caplog.text
    assert "status=canceled" in caplog.text
    assert "reason=cancelled_by_user" in caplog.text


def test_handle_message_enriches_root_cancel_with_existing_order_state(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    stream.handle_message(
        {
            "feed": "open_orders",
            "order": {
                "instrument": "PI_XBTUSD",
                "direction": 0,
                "type": "limit",
                "qty": 10,
                "filled": 0,
                "limit_price": 1000,
                "order_id": "OID-C3",
                "cli_ord_id": "CID-C3",
                "status": "new",
            },
        }
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(
            {
                "feed": "open_orders",
                "order_id": "OID-C3",
                "is_cancel": True,
                "reason": "cancelled_by_user",
            }
        )
    assert "order_event feed=open_orders" in caplog.text
    assert "order_id=OID-C3" in caplog.text
    assert "symbol=PI_XBTUSD" in caplog.text
    assert "type=limit" in caplog.text
    assert "qty=10.00000000" in caplog.text
    assert "status=canceled" in caplog.text


def test_handle_message_logs_fill_delta_event(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "fills",
        "fill": {
            "buy": True,
            "fill_id": "FID-L1",
            "instrument": "PI_XBTUSD",
            "order_id": "OID-L1",
            "order_type": "market",
            "price": 76448.0,
            "qty": 1.0,
            "fill_type": "taker",
            "fee_paid": 6.55e-09,
            "fee_currency": "BTC",
        },
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "fill_event feed=fills" in caplog.text
    assert "order_id=OID-L1" in caplog.text
    assert "fill_id=FID-L1" in caplog.text
    assert "type=market" in caplog.text
    assert "fee=6.55e-09" in caplog.text
    assert "fee_ccy=BTC" in caplog.text


def test_handle_message_logs_snapshot_summary_only(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "open_orders_snapshot",
        "orders": [
            {
                "instrument": "PI_XBTUSD",
                "direction": 0,
                "type": "limit",
                "qty": 10,
                "filled": 0,
                "limit_price": 1000,
                "order_id": "OID-S1",
            },
            {
                "instrument": "PI_XBTUSD",
                "direction": 1,
                "type": "limit",
                "qty": 5,
                "filled": 0,
                "limit_price": 1100,
                "order_id": "OID-S2",
            },
        ],
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "private_snapshot feed=open_orders_snapshot rows=2" in caplog.text
    assert "order_event feed=open_orders_snapshot" not in caplog.text


def test_open_orders_snapshot_tombstones_missing_open_order(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    config = AccountStreamConfig(db_url=db_url, snapshot_tombstone_grace_seconds=0.0)
    store = AccountStateStore(config)
    stream = KrakenFuturesPrivateStream(
        config,
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    stream.handle_message(
        {
            "feed": "open_orders",
            "order": {
                "instrument": "PI_XBTUSD",
                "direction": 1,
                "type": "stop",
                "qty": 6,
                "filled": 0,
                "stop_price": 73448.0,
                "order_id": "OID-T",
                "cli_ord_id": "CID-T",
                "status": "open",
            },
        }
    )

    with caplog.at_level(logging.INFO):
        stream.handle_message({"feed": "open_orders_snapshot", "orders": []})

    with Session(store.engine) as session:
        row = session.execute(
            select(ExchangeOrder).where(ExchangeOrder.exchange_order_id == "OID-T")
        ).scalar_one()

    assert row.status == "canceled"
    assert row.raw_payload["reason"] == "absent_from_open_orders_snapshot"
    assert row.raw_payload["previous_status"] == "open"
    assert "private_snapshot feed=open_orders_snapshot rows=0 tombstoned=1" in caplog.text


def test_open_orders_snapshot_tombstones_missing_untouched_order(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    config = AccountStreamConfig(db_url=db_url, snapshot_tombstone_grace_seconds=0.0)
    store = AccountStateStore(config)
    store.record_order(
        OrderWrite(
            symbol="PI_XBTUSD",
            side="buy",
            order_type="stop",
            status="untouched",
            quantity=6.0,
            exchange_order_id="OID-UNTOUCHED",
            client_order_id="H1stale-260609220000",
            price=73448.0,
        )
    )
    stream = KrakenFuturesPrivateStream(
        config,
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )

    with caplog.at_level(logging.INFO):
        stream.handle_message({"feed": "open_orders_snapshot", "orders": []})

    with Session(store.engine) as session:
        row = session.execute(
            select(ExchangeOrder).where(
                ExchangeOrder.exchange_order_id == "OID-UNTOUCHED"
            )
        ).scalar_one()

    assert row.status == "canceled"
    assert row.raw_payload["reason"] == "absent_from_open_orders_snapshot"
    assert row.raw_payload["previous_status"] == "untouched"
    assert "private_snapshot feed=open_orders_snapshot rows=0 tombstoned=1" in caplog.text


def test_open_orders_snapshot_does_not_tombstone_fresh_missing_order(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    config = AccountStreamConfig(db_url=db_url)
    store = AccountStateStore(config)
    stream = KrakenFuturesPrivateStream(
        config,
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    stream.handle_message(
        {
            "feed": "open_orders",
            "order": {
                "instrument": "PI_XBTUSD",
                "direction": 1,
                "type": "stop",
                "qty": 6,
                "filled": 0,
                "stop_price": 73448.0,
                "order_id": "OID-FRESH",
                "cli_ord_id": "H1fresh-260609220000",
                "status": "open",
            },
        }
    )

    stream.handle_message({"feed": "open_orders_snapshot", "orders": []})

    with Session(store.engine) as session:
        row = session.execute(
            select(ExchangeOrder).where(ExchangeOrder.exchange_order_id == "OID-FRESH")
        ).scalar_one()

    assert row.status == "open"


def test_rest_reconcile_tombstones_missing_open_order_in_critical_db(tmp_path):
    account_db = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    critical_db = f"sqlite:///{tmp_path / 'critical.sqlite'}"
    config = AccountStreamConfig(
        db_url=account_db,
        critical_db_url=critical_db,
        snapshot_tombstone_grace_seconds=0.0,
    )
    account_store = AccountStateStore(config)
    critical_store = AccountStateStore(critical_private_config(config))
    open_order = OrderWrite(
        symbol="PI_XBTUSD",
        side="buy",
        order_type="stop",
        status="open",
        quantity=6.0,
        exchange_order_id="OID-REST",
        client_order_id="CID-REST",
        price=73448.0,
    )
    account_store.record_order_snapshot([open_order])
    critical_store.record_order_snapshot([open_order])

    class _Reconciler(KrakenFuturesRestReconciler):
        def get_json(self, endpoint_path, params=None):
            del params
            if endpoint_path == "/openorders":
                return {"openOrders": []}
            if endpoint_path == "/openpositions":
                return {"openPositions": []}
            if endpoint_path == "/accounts":
                return {"accounts": {}}
            raise AssertionError(endpoint_path)

    reconciler = _Reconciler(
        config,
        account_store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
        critical_store=critical_store,
    )

    stats = reconciler.reconcile_once()

    assert stats["orders"] == 0
    for store in (account_store, critical_store):
        with Session(store.engine) as session:
            row = session.execute(
                select(ExchangeOrder).where(
                    ExchangeOrder.exchange_order_id == "OID-REST"
                )
            ).scalar_one()
        assert row.status == "canceled"
        assert row.raw_payload["reason"] == "absent_from_open_orders_snapshot"


def test_rest_reconcile_summary_logs_success_at_most_every_five_minutes(caplog):
    logger = logging.getLogger("kola")
    state = account_module._RestReconcileLogState(
        environment="live",
        interval_seconds=10.0,
    )
    stats = {"orders": 3, "positions": 1, "balances": 10}

    with caplog.at_level(logging.INFO, logger="kola"):
        state.log_start(logger)
        state.log_success(logger, stats, now=0.0)
        state.log_success(logger, stats, now=299.0)
        state.log_success(logger, stats, now=300.0)

    messages = [record.getMessage() for record in caplog.records]
    rows = [message for message in messages if message.startswith("kraken_reconcile\tlive")]
    assert rows == [
        "kraken_reconcile\tlive\t3\t1\t10\t10.0s\t1\t0\t-",
        "kraken_reconcile\tlive\t3\t1\t10\t10.0s\t3\t0\t-",
    ]
    assert messages.count(account_module.format_rest_reconcile_header()) == 1


def test_rest_reconcile_failure_logs_immediately_and_counts_next_summary(caplog):
    logger = logging.getLogger("kola")
    state = account_module._RestReconcileLogState(
        environment="live",
        interval_seconds=10.0,
    )

    with caplog.at_level(logging.INFO, logger="kola"):
        state.log_start(logger)
        state.log_failure(logger, "timeout")
        state.log_success(logger, {"orders": 2, "positions": 1, "balances": 10}, now=0.0)

    messages = [record.getMessage() for record in caplog.records]
    assert "kraken_reconcile_failed\tlive\t10.0s\ttimeout" in messages
    assert "kraken_reconcile\tlive\t2\t1\t10\t10.0s\t1\t1\ttimeout" in messages


def test_critical_mirror_config_uses_short_sqlite_timeout() -> None:
    config = AccountStreamConfig(
        db_url="sqlite:///main.sqlite",
        critical_db_url="sqlite:///critical.sqlite",
        sqlite_busy_timeout_seconds=30.0,
        critical_mirror_busy_timeout_seconds=0.5,
    )

    mirror = critical_mirror_config(config)

    assert mirror.db_url == config.db_url
    assert mirror.sqlite_busy_timeout_seconds == 0.5


def test_reconciler_status_write_failure_is_fail_open(caplog) -> None:
    class _Store:
        def record_connection_status(self, *args, **kwargs) -> None:
            raise RuntimeError("status lane locked")

    class _Reconciler:
        store = _Store()

    with caplog.at_level(logging.WARNING):
        account_module._record_reconciler_error_status(
            _Reconciler(),
            RuntimeError("snapshot lane locked"),
            logging.getLogger("kola"),
        )

    assert "rest_reconcile status write skipped" in caplog.text
    assert "snapshot lane locked" in caplog.text


def test_private_ingest_mirror_skips_sqlite_lock(caplog) -> None:
    class _LockedStore:
        def ingest_message(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("sqlite3.OperationalError: database is locked")

    logger = logging.getLogger("test-private-mirror")
    mirror = PrivateIngestMirror(
        cast(AccountStateStore, _LockedStore()),
        logger,
        stream_kind="private_ws_critical",
        is_critical=True,
    )
    mirror._queue.put_nowait(
        IngestMessage(
            payload={"feed": "open_orders"},
            received_at=datetime.now(timezone.utc),
        )
    )
    mirror._queue.put_nowait(None)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        mirror._run()

    assert "critical mirror skipped locked message" in caplog.text


def test_handle_message_logs_balance_delta_event(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "balances",
        "holding": {"USD": 100.0},
        "timestamp": 1779577521578,
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "balance_event feed=balances asset=USD" in caplog.text


def test_handle_message_suppresses_unchanged_balance_logs(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "balances",
        "holding": {"USD": 100.0},
        "timestamp": 1779577521578,
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
        stream.handle_message(message)
    assert caplog.text.count("balance_event feed=balances asset=USD") == 1


def test_handle_message_suppresses_balance_log_if_same_as_db(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    config = AccountStreamConfig(db_url=db_url)
    store = AccountStateStore(config)
    store.record_balance(
        BalanceWrite(
            asset="USD",
            available=100.0,
            locked=0.0,
            total=100.0,
        )
    )
    message = {
        "feed": "balances",
        "holding": {"USD": 100.0},
        "timestamp": 1779577521578,
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        config,
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "balance_event feed=balances asset=USD" not in caplog.text


def test_handle_message_suppresses_null_balance_logs(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "balances",
        "holding": {"USD": None},
        "timestamp": 1779577521578,
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "balance_event feed=balances asset=USD" not in caplog.text


def test_handle_message_logs_position_delta_event(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "open_positions",
        "positions": [
            {
                "instrument": "PI_XBTUSD",
                "side": "long",
                "size": 1.0,
                "entry_price": 76000.0,
                "liquidation_price": 70000.0,
                "leverage": 3.0,
            }
        ],
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "position_event feed=open_positions symbol=PI_XBTUSD" in caplog.text
    assert "side=long" in caplog.text


def test_handle_message_suppresses_unchanged_position_event(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "open_positions",
        "positions": [
            {
                "instrument": "PI_XBTUSD",
                "side": "long",
                "size": 1.0,
                "entry_price": 76000.0,
                "liquidation_price": 70000.0,
                "leverage": 3.0,
            }
        ],
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
        stream.handle_message(message)
    assert caplog.text.count("position_event feed=open_positions symbol=PI_XBTUSD") == 1


def test_empty_open_positions_snapshot_records_flat_position(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    open_message = {
        "feed": "open_positions",
        "positions": [
            {
                "instrument": "PI_XBTUSD",
                "side": "short",
                "size": -11.0,
                "entry_price": 73356.0,
            }
        ],
    }
    flat_snapshot = {
        "feed": "open_positions_snapshot",
        "positions": [],
        "timestamp": 1778025600000,
    }

    with caplog.at_level(logging.INFO):
        stream.handle_message(open_message)
        stream.handle_message(flat_snapshot)

    with Session(store.engine) as session:
        latest = (
            session.execute(
                select(AccountPosition)
                .where(AccountPosition.symbol == "PI_XBTUSD")
                .order_by(AccountPosition.local_timestamp.desc(), AccountPosition.id.desc())
            )
            .scalars()
            .first()
        )

    assert latest is not None
    assert latest.size == 0.0
    assert latest.side == "short"
    assert "position_event feed=open_positions_snapshot symbol=PI_XBTUSD" in caplog.text
    assert "size=0.00000000" in caplog.text


def test_handle_message_logs_private_notice_delta_event(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "notifications_auth",
        "event": "stop_triggered",
        "order_id": "OID-N1",
        "message": "stop triggered",
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "private_notice feed=notifications_auth" in caplog.text
    assert "order_id=OID-N1" in caplog.text


def test_handle_message_suppresses_empty_unknown_private_notice_info(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "notifications_auth",
        "event": "notifications_auth",
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "private_notice feed=notifications_auth" not in caplog.text
    assert "private_notice_ignored feed=notifications_auth" not in caplog.text


def test_handle_message_logs_unknown_private_notice_with_order_id(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {
        "feed": "notifications_auth",
        "event": "unknown",
        "order_id": "OID-N2",
    }
    from kolabi.tree.account import KrakenFuturesCredentials, KrakenFuturesPrivateStream

    stream = KrakenFuturesPrivateStream(
        AccountStreamConfig(db_url=db_url),
        store,
        KrakenFuturesCredentials(api_key="key", api_secret="secret"),
    )
    with caplog.at_level(logging.INFO):
        stream.handle_message(message)
    assert "private_notice feed=notifications_auth" in caplog.text
    assert "order_id=OID-N2" in caplog.text


def test_map_balances_handles_flex_futures_currencies_shape():
    rows = map_balances(
        {
            "feed": "balances",
            "timestamp": 1778025600000,
            "flex_futures": {
                "currencies": {
                    "USD": {"available_balance": 20548.5, "balance_value": 20558.5},
                    "BTC": {"available_balance": "0.01", "balance_value": "0.02"},
                }
            },
        }
    )
    by_asset = {row.asset: row for row in rows}
    assert by_asset["USD"].available == 20548.5
    assert by_asset["USD"].total == 20558.5
    assert by_asset["USD"].locked == 10.0
    assert by_asset["BTC"].available == 0.01
    assert by_asset["BTC"].total == 0.02


def test_raw_event_retention_limit_keeps_latest_events(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            raw_retention_minutes=0,
            raw_retention_limit=2,
        )
    )

    for seq in range(3):
        store.record_raw_event({"feed": "fills", "seq": seq, "fills": []})

    with Session(store.engine) as session:
        rows = (
            session.execute(
                select(RawExchangeEvent).order_by(
                    RawExchangeEvent.received_at.asc(),
                    RawExchangeEvent.id.asc(),
                )
            )
            .scalars()
            .all()
        )
        assert [row.exchange_sequence for row in rows] == ["1", "2"]


def test_raw_event_time_retention_deletes_old_events(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            raw_retention_minutes=0,
            raw_retention_limit=0,
        )
    )
    old = store.record_raw_event({"feed": "fills", "seq": "old", "fills": []})
    new = store.record_raw_event({"feed": "fills", "seq": "new", "fills": []})

    now = datetime.now(timezone.utc)
    with Session(store.engine) as session:
        old_row = session.get(RawExchangeEvent, old.id)
        new_row = session.get(RawExchangeEvent, new.id)
        assert old_row is not None
        assert new_row is not None
        old_row.received_at = now - timedelta(minutes=10)
        new_row.received_at = now
        prune_raw_events(
            session,
            config=store.config,
            retention_minutes=5,
            retention_limit=0,
            now=now,
        )
        session.commit()
        rows = session.execute(select(RawExchangeEvent)).scalars().all()
        assert [row.exchange_sequence for row in rows] == ["new"]


def test_private_storage_maintenance_prunes_duplicate_state_and_audits(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            state_retention_minutes=60,
            state_retention_limit=10,
            ingest_audit_retention_minutes=0,
            ingest_audit_retention_limit=2,
            balance_write_min_interval_seconds=300,
            position_write_min_interval_seconds=60,
        )
    )
    now = datetime.now(timezone.utc)
    with Session(store.engine) as session:
        session.add_all(
            [
                AccountBalance(
                    exchange="kraken",
                    environment="demo",
                    account_scope="default",
                    asset="USD",
                    available=10.0,
                    locked=0.0,
                    total=10.0,
                    raw_payload={},
                    local_timestamp=now - timedelta(seconds=10),
                ),
                AccountBalance(
                    exchange="kraken",
                    environment="demo",
                    account_scope="default",
                    asset="USD",
                    available=10.0,
                    locked=0.0,
                    total=10.0,
                    raw_payload={},
                    local_timestamp=now - timedelta(seconds=20),
                ),
                AccountBalance(
                    exchange="kraken",
                    environment="demo",
                    account_scope="default",
                    asset="USD",
                    available=10.0,
                    locked=0.0,
                    total=10.0,
                    raw_payload={},
                    local_timestamp=now - timedelta(seconds=400),
                ),
                AccountBalance(
                    exchange="kraken",
                    environment="demo",
                    account_scope="default",
                    asset="USD",
                    available=10.0,
                    locked=0.0,
                    total=10.0,
                    raw_payload={},
                    local_timestamp=now - timedelta(days=2),
                ),
                AccountPosition(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="PI_XBTUSD",
                    side="long",
                    size=1.0,
                    raw_payload={},
                    local_timestamp=now - timedelta(seconds=10),
                ),
                AccountPosition(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="PI_XBTUSD",
                    side="long",
                    size=1.0,
                    raw_payload={},
                    local_timestamp=now - timedelta(seconds=20),
                ),
                AccountPosition(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="PI_XBTUSD",
                    side="long",
                    size=1.0,
                    raw_payload={},
                    local_timestamp=now - timedelta(seconds=90),
                ),
                AccountPosition(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="PI_XBTUSD",
                    side="long",
                    size=1.0,
                    raw_payload={},
                    local_timestamp=now - timedelta(days=2),
                ),
                *[
                    PrivateIngestAudit(
                        exchange="kraken",
                        environment="demo",
                        market_type="futures",
                        account_scope="default",
                        stream_kind="private_ws_account",
                        feed="balances",
                        is_critical=False,
                        event_type="balances",
                        received_at=now - timedelta(seconds=index),
                        raw_committed_at=now - timedelta(seconds=index),
                        normalized_committed_at=now - timedelta(seconds=index),
                        row_count=1,
                    )
                    for index in range(3)
                ],
            ]
        )
        session.commit()

    store.prune_private_storage_now(stream_kind="private_ws_account")

    with Session(store.engine) as session:
        balances = (
            session.execute(
                select(AccountBalance).order_by(AccountBalance.local_timestamp.desc())
            )
            .scalars()
            .all()
        )
        positions = (
            session.execute(
                select(AccountPosition).order_by(AccountPosition.local_timestamp.desc())
            )
            .scalars()
            .all()
        )
        audits = (
            session.execute(
                select(PrivateIngestAudit).order_by(PrivateIngestAudit.received_at.desc())
            )
            .scalars()
            .all()
        )

    assert len(balances) == 2
    assert [row.available for row in balances] == [10.0, 10.0]
    assert len(positions) == 2
    assert [row.size for row in positions] == [1.0, 1.0]
    assert len(audits) == 2


def test_critical_private_storage_shields_raw_and_ingest_audits(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'critical.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            raw_retention_minutes=0,
            raw_retention_limit=1,
            ingest_audit_retention_minutes=0,
            ingest_audit_retention_limit=1,
        )
    )
    received_at = datetime(2026, 5, 31, 10, 0, tzinfo=timezone.utc)
    for seq in range(3):
        store.ingest_message(
            {"feed": "open_orders", "seq": str(seq), "orders": []},
            stream_kind="private_ws_critical",
            is_critical=True,
            received_at=received_at + timedelta(seconds=seq),
        )

    with caplog.at_level(logging.INFO):
        store.prune_private_storage_now(stream_kind="private_ws_critical")

    with Session(store.engine) as session:
        raw_rows = (
            session.execute(
                select(RawExchangeEvent).order_by(RawExchangeEvent.received_at.asc())
            )
            .scalars()
            .all()
        )
        audits = (
            session.execute(
                select(PrivateIngestAudit).order_by(PrivateIngestAudit.received_at.asc())
            )
            .scalars()
            .all()
        )

    assert [row.exchange_sequence for row in raw_rows] == ["0", "1", "2"]
    assert [row.row_count for row in audits] == [0, 0, 0]
    assert "FORENSIC_SHIELD stream=private_ws_critical" in caplog.text


def test_critical_private_storage_prunes_when_forensic_prune_is_allowed(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'critical.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            raw_retention_minutes=0,
            raw_retention_limit=1,
            ingest_audit_retention_minutes=0,
            ingest_audit_retention_limit=1,
            forensic_shield_critical=False,
        )
    )
    received_at = datetime(2026, 5, 31, 10, 0, tzinfo=timezone.utc)
    for seq in range(3):
        store.ingest_message(
            {"feed": "open_orders", "seq": str(seq), "orders": []},
            stream_kind="private_ws_critical",
            is_critical=True,
            received_at=received_at + timedelta(seconds=seq),
        )

    store.prune_private_storage_now(stream_kind="private_ws_critical")

    with Session(store.engine) as session:
        raw_rows = (
            session.execute(
                select(RawExchangeEvent).order_by(RawExchangeEvent.received_at.asc())
            )
            .scalars()
            .all()
        )
        audits = (
            session.execute(
                select(PrivateIngestAudit).order_by(PrivateIngestAudit.received_at.asc())
            )
            .scalars()
            .all()
        )

    assert [row.exchange_sequence for row in raw_rows] == ["2"]
    assert len(audits) == 1


def test_raw_event_consecutive_duplicate_is_collapsed_with_counter(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    message = {"feed": "notifications_auth", "notifications": []}

    first = store.record_raw_event(message)
    second = store.record_raw_event(message)

    assert first.id == second.id
    with Session(store.engine) as session:
        rows = (
            session.execute(
                select(RawExchangeEvent).order_by(RawExchangeEvent.id.asc())
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].duplicate_count == 1
        assert rows[0].last_seen_at is not None
        assert rows[0].last_seen_at >= rows[0].received_at


def test_raw_event_dedup_is_scoped_by_event_and_stream(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    payload: dict[str, list[object]] = {"notifications": []}

    store.record_raw_event({"feed": "notifications_auth", **payload}, stream_kind="private_ws")
    store.record_raw_event({"feed": "account_log", **payload}, stream_kind="private_ws")
    store.record_raw_event({"feed": "notifications_auth", **payload}, stream_kind="rest_reconciler")

    with Session(store.engine) as session:
        rows = (
            session.execute(
                select(RawExchangeEvent).order_by(RawExchangeEvent.id.asc())
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3
        assert [row.event_type for row in rows] == [
            "notifications_auth",
            "account_log",
            "notifications_auth",
        ]
        assert [row.stream_kind for row in rows] == [
            "private_ws",
            "private_ws",
            "rest_reconciler",
        ]


def test_schema_upgrade_skips_non_sqlite_engines(monkeypatch):
    class Dialect:
        name = "postgresql"

    class Engine:
        dialect = Dialect()

    def fail_inspect(_engine):
        raise AssertionError("inspect should not run for non-sqlite engines")

    monkeypatch.setattr(account_module, "inspect", fail_inspect)

    upgrade_private_schema(Engine())


def test_private_schema_adds_raw_event_latest_lookup_index(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))

    with store.engine.connect() as connection:
        indexes = {
            str(row[1])
            for row in connection.exec_driver_sql(
                "PRAGMA index_list(raw_exchange_events)"
            )
        }

    assert "ix_raw_exchange_events_event_latest" in indexes


def test_main_handles_ctrl_c_as_clean_stop(tmp_path, monkeypatch, capsys):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    critical_db_url = f"sqlite:///{tmp_path / 'critical.sqlite'}"
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_KEY", "key")
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_SECRET", "secret")

    class InterruptingStream:
        def __init__(self, *args, **kwargs):
            self.stopped = False

        async def run(self):
            raise KeyboardInterrupt

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(
        account_module,
        "KrakenFuturesPrivateStream",
        InterruptingStream,
    )

    result = account_module.main(
        [
            "run",
            "--account-db-url",
            db_url,
            "--critical-db-url",
            critical_db_url,
        ]
    )

    assert result == 0
    assert "stopped by operator" in capsys.readouterr().out
    store = AccountStateStore(
        AccountStreamConfig(db_url=critical_db_url, critical_db_url=critical_db_url)
    )
    status = store.latest_status("private_ws")
    assert status["status"] == "stopped"


def test_account_parser_uses_critical_db_url_only() -> None:
    parser = account_module.build_parser()

    args = parser.parse_args(
        [
            "run",
            "--account-db-url",
            "sqlite:///account.sqlite",
            "--critical-db-url",
            "sqlite:///critical.sqlite",
        ]
    )

    assert args.db_url == "sqlite:///account.sqlite"
    assert args.critical_db_url == "sqlite:///critical.sqlite"
    config = account_module.config_from_args(args)
    assert config.forensic_shield_critical is True
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run",
                "--critical-account-db-url",
                "sqlite:///critical.sqlite",
            ]
        )


def test_account_parser_can_explicitly_allow_critical_forensic_prune() -> None:
    parser = account_module.build_parser()

    args = parser.parse_args(
        [
            "run",
            "--allow-critical-forensic-prune",
        ]
    )

    config = account_module.config_from_args(args)
    assert config.forensic_shield_critical is False


def test_map_balances_and_positions_are_persisted(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    balances = map_balances(
        {
            "feed": "balances",
            "timestamp": 1778025600000,
            "holding": {"USD": 100.0, "BTC": "0.5"},
        }
    )
    positions = map_positions(
        {
            "feed": "open_positions",
            "positions": [
                {
                    "instrument": "PF_XBTUSD",
                    "balance": "2",
                    "entry_price": "80000",
                    "leverage": "3",
                    "liquidation_price": "70000",
                    "funding_rate": "0.0001",
                }
            ],
        }
    )

    for balance in balances:
        store.record_balance(balance)
    for position in positions:
        store.record_position(position)

    with Session(store.engine) as session:
        balance_rows = session.execute(select(AccountBalance)).scalars().all()
        position_rows = session.execute(select(AccountPosition)).scalars().all()
        assert len(balance_rows) == 2
        assert len(position_rows) == 1
        assert position_rows[0].symbol == "PF_XBTUSD"
        assert position_rows[0].funding_rate == 0.0001


def test_unchanged_balance_snapshots_are_throttled(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            balance_write_min_interval_seconds=30.0,
        )
    )
    first_at = datetime(2026, 5, 30, 1, 0, tzinfo=timezone.utc)
    message = {
        "feed": "balances",
        "holding": {"USD": 100.0},
        "timestamp": 1778025600000,
    }

    first = store.ingest_message(
        message,
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at,
    )
    second = store.ingest_message(
        message,
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at + timedelta(seconds=10),
    )
    third = store.ingest_message(
        message,
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at + timedelta(seconds=31),
    )

    with Session(store.engine) as session:
        rows = session.execute(select(AccountBalance)).scalars().all()
    assert len(rows) == 2
    assert len(_result_items(first, "balances")) == 1
    assert len(_result_items(second, "balances")) == 0
    assert len(_result_items(third, "balances")) == 1


def test_changed_balance_snapshot_bypasses_throttle(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            balance_write_min_interval_seconds=30.0,
        )
    )
    first_at = datetime(2026, 5, 30, 1, 0, tzinfo=timezone.utc)

    store.ingest_message(
        {"feed": "balances", "holding": {"USD": 100.0}},
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at,
    )
    changed = store.ingest_message(
        {"feed": "balances", "holding": {"USD": 101.0}},
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at + timedelta(seconds=1),
    )

    with Session(store.engine) as session:
        rows = session.execute(select(AccountBalance)).scalars().all()
    assert len(rows) == 2
    assert len(_result_items(changed, "balances")) == 1


def test_default_balance_snapshot_throttle_is_longer_than_positions() -> None:
    assert AccountStreamConfig.balance_write_min_interval_seconds == 300.0
    assert AccountStreamConfig.position_write_min_interval_seconds == 60.0


def test_balance_assets_are_normalized_and_payloads_are_compact(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    received_at = datetime(2026, 5, 30, 1, 0, tzinfo=timezone.utc)

    result = store.ingest_message(
        {
            "feed": "balances",
            "account": "A1",
            "holding": {"xbt": 0.25},
            "flex_futures": {
                "currencies": {
                    "usd": {
                        "available_balance": 5000.0,
                        "balance_value": 5001.0,
                        "large_unused_field": "x" * 1000,
                    }
                }
            },
        },
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=received_at,
    )

    with Session(store.engine) as session:
        rows = (
            session.execute(select(AccountBalance).order_by(AccountBalance.asset.asc()))
            .scalars()
            .all()
        )
    assert {row.asset for row in rows} == {"USD", "XBT"}
    assert {balance.asset for balance in _result_items(result, "balances")} == {"USD", "XBT"}
    for row in rows:
        assert row.raw_payload["asset"] == row.asset
        assert "holding" not in row.raw_payload
        assert "flex_futures" not in row.raw_payload


def test_zero_balance_noise_is_skipped_until_nonzero_transition(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    first_at = datetime(2026, 5, 30, 1, 0, tzinfo=timezone.utc)

    first = store.ingest_message(
        {"feed": "balances", "holding": {"USD": 0.0}},
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at,
    )
    second = store.ingest_message(
        {"feed": "balances", "holding": {"USD": 10.0}},
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at + timedelta(seconds=1),
    )
    third = store.ingest_message(
        {"feed": "balances", "holding": {"USD": 0.0}},
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at + timedelta(seconds=2),
    )

    with Session(store.engine) as session:
        rows = (
            session.execute(select(AccountBalance).order_by(AccountBalance.id.asc()))
            .scalars()
            .all()
        )
    assert len(_result_items(first, "balances")) == 0
    assert len(_result_items(second, "balances")) == 1
    assert len(_result_items(third, "balances")) == 1
    assert [row.total for row in rows] == [10.0, 0.0]


def test_unchanged_position_snapshots_are_throttled(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            position_write_min_interval_seconds=30.0,
        )
    )
    first_at = datetime(2026, 5, 30, 1, 0, tzinfo=timezone.utc)
    message = {
        "feed": "open_positions",
        "positions": [
            {
                "instrument": "PF_XBTUSD",
                "side": "long",
                "balance": "2",
                "entry_price": "80000",
            }
        ],
    }

    first = store.ingest_message(
        message,
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at,
    )
    second = store.ingest_message(
        message,
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at + timedelta(seconds=10),
    )
    third = store.ingest_message(
        message,
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at + timedelta(seconds=31),
    )

    with Session(store.engine) as session:
        rows = session.execute(select(AccountPosition)).scalars().all()
    assert len(rows) == 2
    assert len(_result_items(first, "positions")) == 1
    assert len(_result_items(second, "positions")) == 0
    assert len(_result_items(third, "positions")) == 1


def test_changed_position_snapshot_bypasses_throttle(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
    store = AccountStateStore(
        AccountStreamConfig(
            db_url=db_url,
            position_write_min_interval_seconds=30.0,
        )
    )
    first_at = datetime(2026, 5, 30, 1, 0, tzinfo=timezone.utc)

    store.ingest_message(
        {
            "feed": "open_positions",
            "positions": [{"instrument": "PF_XBTUSD", "side": "long", "balance": "2"}],
        },
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at,
    )
    changed = store.ingest_message(
        {
            "feed": "open_positions",
            "positions": [{"instrument": "PF_XBTUSD", "side": "long", "balance": "3"}],
        },
        stream_kind="private_ws_account",
        is_critical=False,
        received_at=first_at + timedelta(seconds=1),
    )

    with Session(store.engine) as session:
        rows = session.execute(select(AccountPosition)).scalars().all()
    assert len(rows) == 2
    assert len(_result_items(changed, "positions")) == 1


def test_map_rest_balances_handles_nested_accounts_payload():
    rows = map_rest_balances(
        {
            "accounts": {
                "flex": {
                    "name": "flex",
                    "unit": "USD",
                    "holding": {"USD": 125.5, "BTC": "0.02"},
                    "auxiliary": {"availableFunds": 120.0, "pv": 126.0},
                }
            }
        }
    )

    by_asset = {row.asset: row for row in rows}

    assert by_asset["USD"].total in {125.5, 126.0}
    assert by_asset["USD"].available in {125.5, 120.0}
    assert by_asset["BTC"].total == 0.02
