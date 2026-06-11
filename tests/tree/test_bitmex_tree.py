import json
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from kolabi.shared.persistence import MarketLevel, MarketSnapshot
from kolabi.tree.bitmex import (
    BitmexConfig,
    BitmexTree,
    apply_bitmex_book_message,
    book_payload_from_state,
    normalise_public_message,
    parse_bitmex_time,
    public_stream_url,
    ticker_prices_from_message,
)


def test_public_stream_url_uses_bitmex_realtime_subscriptions() -> None:
    config = BitmexConfig(
        pair="XBTUSD",
        ws_url="wss://example/realtime",
        instrument_refresh_on_start=False,
    )

    assert public_stream_url(config) == (
        "wss://example/realtime?subscribe="
        "orderBookL2_25:XBTUSD,instrument:XBTUSD,trade:XBTUSD"
    )


def test_orderbook_table_partial_update_and_delete_build_shared_payload() -> None:
    partial = normalise_public_message(
        {
            "table": "orderBookL2_25",
            "action": "partial",
            "data": [
                {
                    "symbol": "XBTUSD",
                    "id": 1,
                    "side": "Buy",
                    "size": 200,
                    "price": 9999.5,
                    "timestamp": "2026-06-10T12:00:00.000Z",
                },
                {
                    "symbol": "XBTUSD",
                    "id": 2,
                    "side": "Sell",
                    "size": 100,
                    "price": 10000.0,
                    "timestamp": "2026-06-10T12:00:00.000Z",
                },
            ],
        },
        symbol="XBTUSD",
    )
    state, changed = apply_bitmex_book_message(None, partial, symbol="XBTUSD")

    assert changed is True
    assert state is not None
    payload = book_payload_from_state(state)
    assert payload is not None
    assert payload.bids == ((9999.5, 200.0),)
    assert payload.asks == ((10000.0, 100.0),)

    update = normalise_public_message(
        {"table": "orderBookL2_25", "action": "update", "data": [{"id": 1, "size": 250}]},
        symbol="XBTUSD",
    )
    state, changed = apply_bitmex_book_message(state, update, symbol="XBTUSD")

    assert changed is True
    assert state is not None
    payload = book_payload_from_state(state)
    assert payload is not None
    assert payload.bids == ((9999.5, 250.0),)

    delete = normalise_public_message(
        {"table": "orderBookL2_25", "action": "delete", "data": [{"id": 1}]},
        symbol="XBTUSD",
    )
    state, changed = apply_bitmex_book_message(state, delete, symbol="XBTUSD")

    assert changed is True
    assert state is not None
    assert book_payload_from_state(state) is None


def test_ticker_prices_from_instrument_and_trade_events() -> None:
    instrument = ticker_prices_from_message(
        {
            "table": "instrument",
            "action": "update",
            "data": [
                {
                    "symbol": "XBTUSD",
                    "lastPrice": 100.5,
                    "markPrice": 100.4,
                    "indicativeSettlePrice": 100.3,
                }
            ],
        }
    )
    trade = ticker_prices_from_message(
        {
            "table": "trade",
            "action": "insert",
            "data": [{"symbol": "XBTUSD", "price": 101.5}],
        }
    )

    assert instrument is not None
    assert instrument.last_price == 100.5
    assert instrument.mark_price == 100.4
    assert instrument.index_price == 100.3
    assert trade is not None
    assert trade.last_price == 101.5


def test_parse_bitmex_time_is_utc() -> None:
    parsed = parse_bitmex_time("2026-06-10T12:00:00.000Z")

    assert parsed is not None
    assert parsed.tzinfo == timezone.utc


def test_bitmex_tree_persists_normalised_market_snapshot(postgres_url_factory) -> None:
    db_url = postgres_url_factory("bitmex-public")
    tree = BitmexTree(
        BitmexConfig(
            db_url=db_url,
            pair="XBTUSD",
            depth=2,
            instrument_refresh_on_start=False,
        )
    )
    pending = tree.handle_message(
        json.dumps(
            {
                "table": "orderBookL2_25",
                "action": "partial",
                "data": [
                    {"symbol": "XBTUSD", "id": 1, "side": "Buy", "size": 200, "price": 9999.5},
                    {"symbol": "XBTUSD", "id": 2, "side": "Sell", "size": 100, "price": 10000.0},
                    {"symbol": "XBTUSD", "id": 3, "side": "Sell", "size": 50, "price": 10000.5},
                ],
            }
        )
    )

    assert pending is not None
    snapshot = tree.persist_market_snapshot(pending, pending.received_at)

    with Session(tree.engine) as session:
        snap = session.get(MarketSnapshot, snapshot.id)
        levels = session.execute(select(MarketLevel)).scalars().all()
        assert snap is not None
        assert snap.exchange == "bitmex"
        assert snap.symbol == "XBTUSD"
        assert snap.best_bid == 9999.5
        assert snap.best_ask == 10000.0
        assert len(levels) == 3
