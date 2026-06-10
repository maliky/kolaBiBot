from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kolabi.shared.persistence import (
    AccountPosition,
    Base,
    ExchangeConnection,
    ExchangeOrder,
    MarketIndicator,
    MarketSnapshot,
)
from kolabi.shared.runtime_state import KrakenRuntimeStateClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def test_runtime_state_reports_ready_when_public_and_private_are_fresh(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
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


def test_runtime_state_reports_missing_public_schema_as_not_ready(postgres_url_factory) -> None:
    market_db = postgres_url_factory("empty-pub")
    account_db = postgres_url_factory("prv")
    account_engine = create_engine(account_db)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(account_engine) as session:
        session.add(
            ExchangeConnection(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                stream_kind="private_ws",
                status="healthy",
                last_heartbeat_at=now - timedelta(seconds=2),
                updated_at=now - timedelta(seconds=2),
            )
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        symbol="PI_XBTUSD",
    )

    state = client.fetch_runtime_state()

    assert state.ready is False
    assert state.public.ready is False
    assert state.public.reason == "public market DB schema missing"
    assert "public market DB schema missing" in state.reasons


def test_runtime_state_reports_missing_critical_private_schema_as_not_ready(
    postgres_url_factory,
) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
    critical_db = postgres_url_factory("empty-critical")
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

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        critical_account_db_url=critical_db,
        symbol="PI_XBTUSD",
    )

    state = client.fetch_runtime_state()

    assert state.ready is False
    assert state.private_ws.status == "missing_schema"
    assert state.private_ws.reason == "private_ws DB schema missing"
    assert state.open_order_count == 0
    assert state.fill_count == 0


def test_runtime_state_tolerates_missing_broad_account_schema(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("empty-account")
    critical_db = postgres_url_factory("critical")
    market_engine = create_engine(market_db)
    critical_engine = create_engine(critical_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(critical_engine)
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
    with Session(critical_engine) as session:
        session.add(
            ExchangeConnection(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                stream_kind="private_ws",
                status="healthy",
                last_heartbeat_at=now - timedelta(seconds=2),
                updated_at=now - timedelta(seconds=2),
            )
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        critical_account_db_url=critical_db,
        symbol="PI_XBTUSD",
    )

    state = client.fetch_runtime_state()

    assert state.ready is True
    assert state.private_ws.ready is True
    assert state.rest_reconciler.status == "missing_schema"
    assert state.position_size is None
    assert state.position_entry_price is None


def test_runtime_state_flags_stale_public_market(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
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


def test_runtime_state_accepts_recent_indicator_write_when_book_is_unchanged(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
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


def test_runtime_state_ignores_stale_rest_reconcile_error(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
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


def test_runtime_state_falls_back_to_private_ws_critical_when_alias_missing(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
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
                    stream_kind="private_ws_critical",
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
    )
    state = client.fetch_runtime_state()

    assert state.private_ws.ready is True
    assert state.ready is True


def test_runtime_state_reads_order_lifecycle_from_critical_db(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
    critical_db = postgres_url_factory("critical")
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    critical_engine = create_engine(critical_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    Base.metadata.create_all(critical_engine)
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
        session.add(
            AccountPosition(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                symbol="PI_XBTUSD",
                side="long",
                size=4.0,
                entry_price=100.0,
                local_timestamp=now - timedelta(seconds=2),
            )
        )
        session.commit()
    with Session(critical_engine) as session:
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
                ExchangeOrder(
                    local_uuid="order-critical",
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
                    price=99.0,
                    quantity=4.0,
                    filled_quantity=0.0,
                    reduce_only=True,
                    raw_payload={},
                    source_timestamp=now,
                    local_timestamp=now,
                ),
            ]
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        critical_account_db_url=critical_db,
        symbol="PI_XBTUSD",
    )
    state = client.fetch_runtime_state()
    records = client.fetch_private_orders_for_identities(
        client_order_ids=("CID-T",),
        exchange_order_ids=("OID-T",),
    )

    assert state.ready is True
    assert state.open_order_count == 1
    assert state.position_size == 4.0
    assert len(records) == 1
    assert records[0].client_order_id == "CID-T"


def test_runtime_state_open_order_count_uses_latest_identity_row(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
    critical_db = postgres_url_factory("critical")
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    critical_engine = create_engine(critical_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    Base.metadata.create_all(critical_engine)
    now = datetime.now(timezone.utc)
    with Session(market_engine) as session:
        session.add(
            MarketSnapshot(
                local_uuid="snap-latest-count",
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
    with Session(critical_engine) as session:
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
                ExchangeOrder(
                    local_uuid="order-latest-count-open",
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="PI_XBTUSD",
                    exchange_order_id="OID-LATEST",
                    client_order_id="CID-LATEST",
                    side="sell",
                    order_type="stop",
                    status="open",
                    price=99.0,
                    quantity=4.0,
                    filled_quantity=0.0,
                    reduce_only=True,
                    raw_payload={},
                    source_timestamp=now - timedelta(seconds=20),
                    local_timestamp=now - timedelta(seconds=20),
                ),
                ExchangeOrder(
                    local_uuid="order-latest-count-canceled",
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="PI_XBTUSD",
                    exchange_order_id="OID-LATEST",
                    client_order_id="CID-LATEST",
                    side="sell",
                    order_type="stop",
                    status="canceled",
                    price=99.0,
                    quantity=4.0,
                    filled_quantity=0.0,
                    reduce_only=True,
                    raw_payload={},
                    source_timestamp=now,
                    local_timestamp=now,
                ),
            ]
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        critical_account_db_url=critical_db,
        symbol="PI_XBTUSD",
    )

    state = client.fetch_runtime_state()

    assert state.ready is True
    assert state.open_order_count == 0


def test_private_order_records_use_raw_execution_price_when_column_is_empty(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(account_engine) as session:
        session.add(
            ExchangeOrder(
                local_uuid="order-1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                symbol="PI_XBTUSD",
                exchange_order_id="OID-H",
                client_order_id="CID-H",
                side="buy",
                order_type="market",
                status="filled",
                price=None,
                quantity=6.0,
                filled_quantity=6.0,
                reduce_only=False,
                raw_payload={"price": 73585.5},
                source_timestamp=now,
                local_timestamp=now,
            )
        )
        session.commit()
    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        symbol="PI_XBTUSD",
    )

    records = client.fetch_private_orders_for_identities(
        client_order_ids=("CID-H",),
        exchange_order_ids=("OID-H",),
    )

    assert len(records) == 1
    assert records[0].price == 73585.5


def test_runtime_state_does_not_accept_fresh_account_stream_when_private_ws_stale(
    postgres_url_factory,
) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
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
                    last_heartbeat_at=now - timedelta(minutes=10),
                    updated_at=now - timedelta(minutes=10),
                ),
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="private_ws_account",
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
        max_private_age_seconds=30.0,
    )
    state = client.fetch_runtime_state()

    assert state.private_ws.ready is False
    assert "private_ws state is stale" in state.reasons
    assert state.ready is False
