from __future__ import annotations

from datetime import datetime, timezone

from kolabi.shared.persistence import (
    AccountBalance,
    AccountPosition,
    ExchangeFill,
    ExchangeOrder,
)
from kolabi.tree.account import (
    AccountStateStore,
    AccountStreamConfig,
    FillWrite,
    OrderWrite,
    map_balances,
    map_fill_event,
    map_order,
    map_positions,
    map_rest_balances,
    sign_challenge,
    sign_rest_auth,
    subscribe_messages,
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
