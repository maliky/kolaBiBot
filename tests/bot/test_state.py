from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kolabi.shared.persistence import (
    AccountPosition,
    Base,
    ExchangeConnection,
    MarketIndicator,
    MarketSnapshot,
)
from kolabi.shared.runtime_state import KrakenRuntimeStateClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def test_runtime_state_reports_ready_when_public_and_private_are_fresh(tmp_path) -> None:
    market_db = f"sqlite:///{tmp_path / 'pub.sqlite'}"
    account_db = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(market_engine) as session:
        session.add(
            MarketSnapshot(
                local_uuid="snap-1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                best_bid=100.0,
                best_ask=101.0,
                avg_bid=99.5,
                avg_ask=101.5,
                mid_price=100.5,
                spread=1.0,
                imbalance=0.55,
                source_timestamp=now - timedelta(seconds=1),
                local_timestamp=now - timedelta(seconds=1),
            )
        )
        session.add(
            MarketIndicator(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                indicator_name="mark_price",
                value=100.7,
                source_age_seconds=1.0,
                computed_at=now - timedelta(seconds=1),
            )
        )
        session.commit()
    with Session(account_engine) as session:
        session.add_all(
            [
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="private_ws",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=2),
                    updated_at=now - timedelta(seconds=2),
                ),
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="rest_reconciler",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=5),
                    updated_at=now - timedelta(seconds=5),
                ),
                AccountPosition(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="PI_XBTUSD",
                    side="long",
                    size=2.0,
                    entry_price=100.0,
                    local_timestamp=now - timedelta(seconds=2),
                ),
            ]
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        symbol="PI_XBTUSD",
    )

    state = client.fetch_runtime_state()

    assert state.ready is True
    assert state.public.mid_price == 100.5
    assert state.public.indicators["mark_price"] == 100.7
    assert state.position_size == 2.0
    assert state.reasons == ()


def test_runtime_state_flags_stale_public_market(tmp_path) -> None:
    market_db = f"sqlite:///{tmp_path / 'pub.sqlite'}"
    account_db = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(market_engine) as session:
        session.add(
            MarketSnapshot(
                local_uuid="snap-1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                best_bid=100.0,
                best_ask=101.0,
                avg_bid=99.5,
                avg_ask=101.5,
                mid_price=100.5,
                spread=1.0,
                imbalance=0.55,
                source_timestamp=now - timedelta(seconds=120),
                local_timestamp=now - timedelta(seconds=120),
            )
        )
        session.commit()
    with Session(account_engine) as session:
        session.add_all(
            [
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="private_ws",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=2),
                    updated_at=now - timedelta(seconds=2),
                ),
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="rest_reconciler",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=5),
                    updated_at=now - timedelta(seconds=5),
                ),
            ]
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        symbol="PI_XBTUSD",
        max_public_age_seconds=10.0,
    )

    state = client.fetch_runtime_state()

    assert state.ready is False
    assert "public market data is stale" in state.reasons


def test_runtime_state_accepts_recent_indicator_write_when_book_is_unchanged(tmp_path) -> None:
    market_db = f"sqlite:///{tmp_path / 'pub.sqlite'}"
    account_db = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(market_engine) as session:
        session.add(
            MarketSnapshot(
                local_uuid="snap-1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                best_bid=100.0,
                best_ask=101.0,
                avg_bid=99.5,
                avg_ask=101.5,
                mid_price=100.5,
                spread=1.0,
                imbalance=0.55,
                source_timestamp=now - timedelta(seconds=120),
                local_timestamp=now - timedelta(seconds=120),
            )
        )
        session.add(
            MarketIndicator(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                indicator_name="source_age_seconds",
                value=0.0,
                source_age_seconds=0.0,
                computed_at=now - timedelta(seconds=2),
            )
        )
        session.commit()
    with Session(account_engine) as session:
        session.add_all(
            [
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="private_ws",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=2),
                    updated_at=now - timedelta(seconds=2),
                ),
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="rest_reconciler",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=5),
                    updated_at=now - timedelta(seconds=5),
                ),
            ]
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        symbol="PI_XBTUSD",
        max_public_age_seconds=10.0,
    )

    state = client.fetch_runtime_state()

    assert state.ready is True
    assert state.public.age_seconds is not None
    assert state.public.age_seconds <= 10.0


def test_runtime_state_ignores_stale_rest_reconcile_error(tmp_path) -> None:
    market_db = f"sqlite:///{tmp_path / 'pub.sqlite'}"
    account_db = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(market_engine) as session:
        session.add(
            MarketSnapshot(
                local_uuid="snap-1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                best_bid=100.0,
                best_ask=101.0,
                avg_bid=99.5,
                avg_ask=101.5,
                mid_price=100.5,
                spread=1.0,
                imbalance=0.55,
                source_timestamp=now - timedelta(seconds=1),
                local_timestamp=now - timedelta(seconds=1),
            )
        )
        session.commit()
    with Session(account_engine) as session:
        session.add_all(
            [
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="private_ws",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=2),
                    updated_at=now - timedelta(seconds=2),
                ),
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="rest_reconciler",
                    status="error",
                    last_heartbeat_at=now - timedelta(hours=12),
                    updated_at=now - timedelta(hours=12),
                    last_error="nonceBelowThreshold",
                ),
            ]
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        symbol="PI_XBTUSD",
        max_reconcile_age_seconds=300.0,
    )

    state = client.fetch_runtime_state()

    assert state.private_ws.ready is True
    assert state.rest_reconciler.ready is True
    assert state.ready is True
