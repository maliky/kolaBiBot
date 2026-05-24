from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from kolabi.tree import account as account_module
from kolabi.shared.persistence import (
    AccountBalance,
    AccountPosition,
    ExchangeFill,
    ExchangeOrder,
    RawExchangeEvent,
)
from kolabi.tree.account import (
    AccountStateStore,
    AccountStreamConfig,
    BalanceWrite,
    FillWrite,
    OrderWrite,
    map_balances,
    map_fill_event,
    map_order,
    map_positions,
    map_rest_balances,
    prune_raw_events,
    sign_challenge,
    sign_rest_auth,
    subscribe_messages,
    upgrade_private_schema,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


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


def test_schema_upgrade_skips_non_sqlite_engines(monkeypatch):
    class Dialect:
        name = "postgresql"

    class Engine:
        dialect = Dialect()

    def fail_inspect(_engine):
        raise AssertionError("inspect should not run for non-sqlite engines")

    monkeypatch.setattr(account_module, "inspect", fail_inspect)

    upgrade_private_schema(Engine())


def test_main_handles_ctrl_c_as_clean_stop(tmp_path, monkeypatch, capsys):
    db_url = f"sqlite:///{tmp_path / 'prv-market.sqlite'}"
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

    result = account_module.main(["run", "--db-url", db_url])

    assert result == 0
    assert "stopped by operator" in capsys.readouterr().out
    store = AccountStateStore(AccountStreamConfig(db_url=db_url))
    status = store.latest_status("private_ws")
    assert status["status"] == "stopped"


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
