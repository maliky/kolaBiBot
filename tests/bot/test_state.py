from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kolabi.shared.persistence import (
    AccountPosition,
    Base,
    ExchangeConnection,
    ExchangeFill,
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


def test_runtime_state_filters_same_symbol_by_exchange(postgres_url_factory) -> None:
    market_db = postgres_url_factory("multi-exchange-pub")
    account_db = postgres_url_factory("multi-exchange-prv")
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(market_engine) as session:
        session.add_all(
            [
                MarketSnapshot(
                    local_uuid="snap-kraken",
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    symbol="BTCUSD",
                    best_bid=100.0,
                    best_ask=101.0,
                    avg_bid=100.0,
                    avg_ask=101.0,
                    mid_price=100.5,
                    spread=1.0,
                    imbalance=0.5,
                    source_timestamp=now - timedelta(seconds=1),
                    local_timestamp=now - timedelta(seconds=1),
                ),
                MarketSnapshot(
                    local_uuid="snap-binance",
                    exchange="binance",
                    environment="demo",
                    market_type="futures",
                    symbol="BTCUSD",
                    best_bid=200.0,
                    best_ask=202.0,
                    avg_bid=200.0,
                    avg_ask=202.0,
                    mid_price=201.0,
                    spread=2.0,
                    imbalance=0.6,
                    source_timestamp=now - timedelta(seconds=1),
                    local_timestamp=now - timedelta(seconds=1),
                ),
            ]
        )
        session.commit()
    with Session(account_engine) as session:
        session.add_all(
            [
                ExchangeOrder(
                    local_uuid="order-kraken",
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="BTCUSD",
                    exchange_order_id="OID-K",
                    client_order_id="CID-K",
                    side="buy",
                    order_type="limit",
                    status="open",
                    price=100.5,
                    quantity=1.0,
                    filled_quantity=0.0,
                    local_timestamp=now - timedelta(seconds=1),
                ),
                ExchangeOrder(
                    local_uuid="order-binance",
                    exchange="binance",
                    environment="demo",
                    market_type="futures",
                    account_scope="default",
                    symbol="BTCUSD",
                    exchange_order_id="OID-B",
                    client_order_id="CID-B",
                    side="buy",
                    order_type="limit",
                    status="open",
                    price=201.0,
                    quantity=1.0,
                    filled_quantity=0.0,
                    local_timestamp=now - timedelta(seconds=1),
                ),
            ]
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        symbol="BTCUSD",
    )

    kraken_market = client.fetch_market_state(symbol="BTCUSD", exchange="kraken")
    binance_market = client.fetch_market_state(symbol="BTCUSD", exchange="binance")
    binance_orders = client.fetch_latest_private_orders(
        symbol="BTCUSD",
        exchange="binance",
        open_only=True,
    )

    assert kraken_market.mid_price == 100.5
    assert binance_market.mid_price == 201.0
    assert [(record.exchange, record.exchange_order_id) for record in binance_orders] == [
        ("binance", "OID-B")
    ]


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


def test_runtime_state_private_reads_are_account_scope_isolated(
    postgres_url_factory,
) -> None:
    market_db = postgres_url_factory("scope-pub")
    account_db = postgres_url_factory("scope-prv")
    critical_db = postgres_url_factory("scope-critical")
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
                local_uuid="snap-scope",
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
        default_order = ExchangeOrder(
            local_uuid="order-scope-default",
            exchange="kraken",
            environment="demo",
            market_type="futures",
            account_scope="default",
            symbol="PI_XBTUSD",
            exchange_order_id="OID-DEFAULT",
            client_order_id="CID-DEFAULT",
            side="buy",
            order_type="limit",
            status="partial_fill",
            price=100.0,
            quantity=4.0,
            filled_quantity=1.0,
            reduce_only=False,
            raw_payload={"price": 100.0},
            source_timestamp=now - timedelta(seconds=3),
            local_timestamp=now - timedelta(seconds=3),
        )
        advers_order = ExchangeOrder(
            local_uuid="order-scope-advers",
            exchange="kraken",
            environment="demo",
            market_type="futures",
            account_scope="advers",
            symbol="PI_XBTUSD",
            exchange_order_id="OID-ADVERS",
            client_order_id="CID-ADVERS",
            side="buy",
            order_type="limit",
            status="open",
            price=101.0,
            quantity=5.0,
            filled_quantity=0.0,
            reduce_only=False,
            raw_payload={"price": 101.0},
            source_timestamp=now - timedelta(seconds=2),
            local_timestamp=now - timedelta(seconds=2),
        )
        session.add_all(
            [
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="private_ws",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=1),
                    updated_at=now - timedelta(seconds=1),
                ),
                default_order,
                advers_order,
            ]
        )
        session.flush()
        session.add_all(
            [
                ExchangeFill(
                    local_uuid="fill-scope-default",
                    order_id=default_order.id,
                    exchange="kraken",
                    exchange_fill_id="FID-DEFAULT",
                    price=100.0,
                    quantity=1.0,
                    raw_payload={"price": 100.0},
                    source_timestamp=now - timedelta(seconds=3),
                    local_timestamp=now - timedelta(seconds=3),
                ),
                ExchangeFill(
                    local_uuid="fill-scope-advers",
                    order_id=advers_order.id,
                    exchange="kraken",
                    exchange_fill_id="FID-ADVERS",
                    price=101.0,
                    quantity=1.0,
                    raw_payload={"price": 101.0},
                    source_timestamp=now - timedelta(seconds=2),
                    local_timestamp=now - timedelta(seconds=2),
                ),
            ]
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        critical_account_db_url=critical_db,
        symbol="PI_XBTUSD",
        account_scope="default",
    )

    state = client.fetch_runtime_state()
    since = client.fetch_private_orders_since(
        after_local_timestamp=now - timedelta(minutes=1)
    )
    fills_since = client.fetch_private_fills_since(
        after_local_timestamp=now - timedelta(minutes=1)
    )
    identity_orders = client.fetch_private_orders_for_identities(
        client_order_ids=("CID-DEFAULT", "CID-ADVERS"),
        exchange_order_ids=("OID-DEFAULT", "OID-ADVERS"),
    )
    identity_fills = client.fetch_private_fills_for_identities(
        client_order_ids=("CID-DEFAULT", "CID-ADVERS"),
        exchange_order_ids=("OID-DEFAULT", "OID-ADVERS"),
    )

    assert state.ready is True
    assert state.open_order_count == 1
    assert state.fill_count == 1
    assert [record.client_order_id for record in since] == ["CID-DEFAULT"]
    assert [record.exchange_order_id for record in fills_since] == ["OID-DEFAULT"]
    assert [record.client_order_id for record in identity_orders] == ["CID-DEFAULT"]
    assert [record.exchange_order_id for record in identity_fills] == ["OID-DEFAULT"]


def test_runtime_state_private_health_is_account_scope_isolated(
    postgres_url_factory,
) -> None:
    market_db = postgres_url_factory("health-scope-pub")
    account_db = postgres_url_factory("health-scope-account")
    critical_db = postgres_url_factory("health-scope-critical")
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
                local_uuid="snap-health-scope",
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
                account_scope="advers",
                stream_kind="private_ws",
                status="healthy",
                last_heartbeat_at=now - timedelta(seconds=1),
                updated_at=now - timedelta(seconds=1),
            )
        )
        session.commit()

    default_client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        critical_account_db_url=critical_db,
        symbol="PI_XBTUSD",
        account_scope="default",
    )
    advers_client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        critical_account_db_url=critical_db,
        symbol="PI_XBTUSD",
        account_scope="advers",
    )

    default_state = default_client.fetch_runtime_state()
    advers_state = advers_client.fetch_runtime_state()

    assert default_state.ready is False
    assert default_state.private_ws.status == "missing"
    assert default_state.private_ws.reason == "missing private_ws state"
    assert advers_state.ready is True
    assert advers_state.private_ws.status == "healthy"


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
