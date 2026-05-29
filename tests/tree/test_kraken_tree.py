from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from kolabi.bot.indicators import KrakenDbIndicatorClient
from kolabi.shared.persistence import (
    ExchangeInstrument,
    MarketIndicator,
    MarketLevel,
    MarketSnapshot,
    RawExchangeEvent,
)
from kolabi.tree.kraken import (
    KrakenConfig,
    KrakenTree,
    apply_book_payload,
    calculate_metrics,
    extract_book_payload,
    normal_mgf,
    parse_levels,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


def fixed_time(offset_seconds: int = 0) -> datetime:
    return datetime(2026, 5, 10, 0, 0, offset_seconds, tzinfo=timezone.utc)


def test_process_book_persists_normalized_snapshot_levels_and_indicators(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(KrakenConfig(db_url=db_url, pair="PI_XBTUSD", depth=2))
    asks = [{"price": 100, "qty": 1}, {"price": 101, "qty": 3}]
    bids = [{"price": 99, "qty": 2}, {"price": 98, "qty": 2}]

    snapshot = tree.process_book(asks, bids)

    with Session(tree.engine) as session:
        snap = session.get(MarketSnapshot, snapshot.id)
        levels = session.execute(select(MarketLevel)).scalars().all()
        indicators = session.execute(select(MarketIndicator)).scalars().all()
        assert snap is not None
        assert snap.environment == "demo"
        assert snap.market_type == "futures"
        assert snap.avg_ask == 100.75
        assert snap.avg_bid == 98.5
        assert len(levels) == 4
        assert len(indicators) == 6


def test_extract_book_payload_reads_futures_snapshot():
    payload = {
        "feed": "book_snapshot",
        "product_id": "PI_XBTUSD",
        "timestamp": 1778025600000,
        "seq": 7,
        "asks": [{"price": 101.0, "qty": 1.5}],
        "bids": [{"price": 99.0, "qty": 2.0}],
    }

    parsed = extract_book_payload(payload)

    assert parsed is not None
    assert parsed.symbol == "PI_XBTUSD"
    assert parsed.sequence == 7
    assert parsed.asks == ((101.0, 1.5),)
    assert parsed.bids == ((99.0, 2.0),)


def test_apply_book_payload_accepts_one_sided_delta():
    snapshot = extract_book_payload(
        {
            "feed": "book_snapshot",
            "product_id": "PI_XBTUSD",
            "timestamp": 1778025600000,
            "seq": 10,
            "asks": [{"price": 101.0, "qty": 1.0}],
            "bids": [{"price": 99.0, "qty": 1.0}],
        }
    )
    update = extract_book_payload(
        {
            "feed": "book",
            "product_id": "PI_XBTUSD",
            "timestamp": 1778025601000,
            "seq": 11,
            "side": "buy",
            "price": 99.5,
            "qty": 1.25,
        }
    )
    assert snapshot is not None
    assert update is not None

    state = apply_book_payload(None, snapshot, depth=3)
    state = apply_book_payload(state, update, depth=3)

    assert state.bids[0] == (99.5, 1.25)
    assert state.asks[0] == (101.0, 1.0)


def test_apply_book_payload_removes_zero_qty_level():
    snapshot = extract_book_payload(
        {
            "feed": "book_snapshot",
            "product_id": "PI_XBTUSD",
            "timestamp": 1778025600000,
            "seq": 10,
            "asks": [{"price": 101.0, "qty": 1.0}, {"price": 102.0, "qty": 1.0}],
            "bids": [{"price": 99.0, "qty": 1.0}, {"price": 98.0, "qty": 1.0}],
        }
    )
    update = extract_book_payload(
        {
            "feed": "book",
            "product_id": "PI_XBTUSD",
            "timestamp": 1778025601000,
            "seq": 11,
            "side": "sell",
            "price": 101.0,
            "qty": 0.0,
        }
    )
    assert snapshot is not None
    assert update is not None

    state = apply_book_payload(None, snapshot, depth=3)
    state = apply_book_payload(state, update, depth=3)

    assert state.asks == ((102.0, 1.0),)


def test_scheduler_leaves_gap_when_book_does_not_change(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(
        KrakenConfig(db_url=db_url, pair="PI_XBTUSD", snapshot_interval_seconds=1)
    )
    tree.ingest_book([{"price": 101, "qty": 1}], [{"price": 99, "qty": 1}], fixed_time(0))

    first = tree.flush_due(fixed_time(0))
    second = tree.flush_due(fixed_time(2))

    with Session(tree.engine) as session:
        snapshots = session.execute(select(MarketSnapshot)).scalars().all()
        assert first.snapshot is not None
        assert second.snapshot is None
        assert len(snapshots) == 1


def test_status_log_suppresses_identical_lines(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(
        KrakenConfig(db_url=db_url, pair="PI_XBTUSD", log_interval_seconds=0)
    )
    tree.ingest_book(
        [{"price": 101, "qty": 1}],
        [{"price": 99, "qty": 1}],
        fixed_time(0),
    )

    with caplog.at_level("INFO"):
        tree.log_due(fixed_time(0), snapshot=None)
        tree.log_due(fixed_time(1), snapshot=None)

    assert caplog.text.count(
        "kraken_tree\tenv\tcount\tpair\tpersisted\traw\tbook\tbest_bid\tbest_ask\tspread\tmid\timbalance"
    ) == 1
    assert caplog.text.count("kraken_tree\tdemo\t0\tPI_XBTUSD\tFalse") == 1


def test_status_log_prints_header_every_fifty_rows(tmp_path, caplog):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(
        KrakenConfig(db_url=db_url, pair="PI_XBTUSD", log_interval_seconds=0)
    )
    tree.ingest_book(
        [{"price": 101, "qty": 1}],
        [{"price": 99, "qty": 1}],
        fixed_time(0),
    )

    with caplog.at_level("INFO"):
        for offset in range(51):
            tree._stored_count = offset + 1
            tree.log_due(fixed_time(offset), snapshot=None)

    header = (
        "kraken_tree\tenv\tcount\tpair\tpersisted\traw\tbook\tbest_bid\tbest_ask\tspread\tmid\timbalance"
    )
    assert caplog.text.count(header) == 2


def test_indicator_client_filters_pair_and_environment(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    btc_tree = KrakenTree(KrakenConfig(db_url=db_url, pair="PI_XBTUSD"))
    eth_tree = KrakenTree(KrakenConfig(db_url=db_url, pair="PI_ETHUSD"))
    btc_tree.process_book([{"price": 101, "qty": 1}], [{"price": 99, "qty": 1}])
    eth_tree.process_book([{"price": 51, "qty": 1}], [{"price": 49, "qty": 1}])

    client = KrakenDbIndicatorClient(db_url=db_url, environment="demo", market_type="futures")
    snapshot = client.fetch_snapshot("PI_XBTUSD")

    assert snapshot["symbol"] == "PI_XBTUSD"
    assert snapshot["environment"] == "demo"
    assert snapshot["market_type"] == "futures"
    assert snapshot["indicators"]["mid_price"] == 100.0


def test_handle_message_ignores_non_book_events(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(KrakenConfig(db_url=db_url))

    result = tree.handle_message(json.dumps({"event": "heartbeat"}))

    assert result is None
    assert tree.latest_status()["status"] == "empty"


def test_extract_book_payload_ignores_book_subscribe_ack():
    parsed = extract_book_payload(
        {
            "event": "subscribed",
            "feed": "book",
            "product_ids": ["PI_XBTUSD"],
        }
    )

    assert parsed is None


def test_parse_levels_supports_futures_dict_rows():
    levels = parse_levels(
        [{"price": 100, "qty": 1}, {"price": 101, "qty": 0}, {"price": 102, "qty": 2}],
        depth=3,
    )

    assert levels == [(100.0, 1.0), (102.0, 2.0)]


def test_calculate_metrics_uses_top_book_prices_for_spread():
    metrics = calculate_metrics(
        asks=[(100.0, 1.0), (101.0, 3.0)],
        bids=[(99.0, 2.0), (98.0, 2.0)],
    )

    assert metrics.avg_ask == 100.75
    assert metrics.avg_bid == 98.5
    assert metrics.spread == 1
    assert metrics.mid_price == 99.5


def test_normal_mgf_uses_normal_distribution_formula():
    assert normal_mgf(0.1, mean=2.0, variance=3.0) > 1


def test_normal_mgf_clips_large_exponent_instead_of_raising():
    value = normal_mgf(1.0, mean=10_000.0, variance=1_000.0)

    assert math.isfinite(value)
    assert value > 0


def test_retention_prunes_old_normalized_rows(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(KrakenConfig(db_url=db_url, retention_minutes=1))
    tree.ingest_book([{"price": 101, "qty": 1}], [{"price": 99, "qty": 1}], fixed_time(0))
    tree.flush_due(fixed_time(0))
    tree.ingest_book([{"price": 102, "qty": 1}], [{"price": 100, "qty": 1}], fixed_time(2))

    tree.flush_due(fixed_time(2) + timedelta(minutes=2))

    with Session(tree.engine) as session:
        snapshots = session.execute(select(MarketSnapshot)).scalars().all()
        levels = session.execute(select(MarketLevel)).scalars().all()
        assert len(snapshots) == 1
        assert len(levels) == 2


def test_sqlite_reader_can_query_while_writer_session_is_open(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(KrakenConfig(db_url=db_url, pair="PI_XBTUSD"))

    tree.process_book([{"price": 101, "qty": 1}], [{"price": 99, "qty": 1}])
    with Session(tree.engine) as writer_session:
        writer_session.add(
            MarketSnapshot(
                local_uuid="pending",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                best_bid=100,
                best_ask=101,
                avg_bid=100,
                avg_ask=101,
                spread=1,
                mid_price=100.5,
                imbalance=0,
                local_timestamp=fixed_time(1),
            )
        )
        writer_session.flush()
        with Session(tree.engine) as reader_session:
            results = reader_session.execute(select(MarketSnapshot)).scalars().all()
            assert len(results) == 1
            assert results[0].environment == "demo"


def test_sequence_regression_delta_is_ignored():
    snapshot = extract_book_payload(
        {
            "feed": "book_snapshot",
            "product_id": "PI_XBTUSD",
            "timestamp": 1778025600000,
            "seq": 10,
            "asks": [{"price": 101.0, "qty": 1.0}],
            "bids": [{"price": 99.0, "qty": 1.0}],
        }
    )
    stale = extract_book_payload(
        {
            "feed": "book",
            "product_id": "PI_XBTUSD",
            "timestamp": 1778025601000,
            "seq": 9,
            "side": "buy",
            "price": 99.5,
            "qty": 1.25,
        }
    )
    assert snapshot is not None
    assert stale is not None

    state = apply_book_payload(None, snapshot, depth=3)
    next_state = apply_book_payload(state, stale, depth=3)

    assert next_state == state


def test_snapshot_status_reports_environment(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(KrakenConfig(db_url=db_url, pair="PI_XBTUSD"))
    tree.process_book([{"price": 101, "qty": 1}], [{"price": 99, "qty": 1}])

    status = tree.latest_status("PI_XBTUSD")

    assert status["environment"] == "demo"
    assert status["market_type"] == "futures"


def test_snapshot_status_rounds_prices_to_cached_tick_size(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(KrakenConfig(db_url=db_url, pair="PI_XBTUSD"))
    tree.process_book([{"price": 101.24, "qty": 1}], [{"price": 99.26, "qty": 1}])

    with Session(tree.engine) as session:
        session.add(
            ExchangeInstrument(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                tick_size=0.5,
                min_quantity=1.0,
                tradeable=True,
                raw_payload={},
            )
        )
        session.commit()

    status = tree.latest_status("PI_XBTUSD")

    assert status["tick_size"] == 0.5
    assert status["best_ask"] == 101.0
    assert status["best_bid"] == 99.5
    assert status["mid_price"] == 100.5


def test_snapshot_status_keeps_raw_precision_when_tick_size_missing(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(KrakenConfig(db_url=db_url, pair="PI_XBTUSD"))
    tree.process_book([{"price": 101.24, "qty": 1}], [{"price": 99.26, "qty": 1}])

    status = tree.latest_status("PI_XBTUSD")

    assert status["tick_size"] is None
    assert status["best_ask"] == 101.24
    assert status["best_bid"] == 99.26


def test_delta_before_snapshot_raises():
    delta = extract_book_payload(
        {
            "feed": "book",
            "product_id": "PI_XBTUSD",
            "timestamp": 1778025601000,
            "seq": 11,
            "side": "buy",
            "price": 99.5,
            "qty": 1.25,
        }
    )
    assert delta is not None

    with pytest.raises(ValueError):
        apply_book_payload(None, delta, depth=3)


def test_public_raw_event_consecutive_duplicate_is_collapsed(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pub-futures-demo.sqlite'}"
    tree = KrakenTree(KrakenConfig(db_url=db_url, pair="PI_XBTUSD"))
    message = {
        "feed": "notifications_auth",
        "notifications": [],
    }

    tree.handle_message(json.dumps(message))
    tree.handle_message(json.dumps(message))

    with Session(tree.engine) as session:
        rows = (
            session.execute(
                select(RawExchangeEvent).order_by(RawExchangeEvent.id.asc())
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].event_type == "notifications_auth"
        assert rows[0].duplicate_count == 1
