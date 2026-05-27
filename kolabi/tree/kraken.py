from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import uuid4

import requests
import websockets
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
    delete,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, sessionmaker

from kolabi.shared.kraken_futures import kraken_futures_environment
from kolabi.shared.logging import setup_logging
from kolabi.shared.persistence import (
    Base,
    MarketIndicator,
    MarketLevel,
    MarketSnapshot,
    RawExchangeEvent,
)

BookLevelT = tuple[float, float]
BookSignatureT = tuple[tuple[BookLevelT, ...], tuple[BookLevelT, ...]]
RawLevelT = object


@dataclass(frozen=True)
class KrakenConfig:
    """Configuration runtime du service public KrakenTree Futures."""

    pair: str = "PI_XBTUSD"
    depth: int = 25
    ws_url: str = "wss://demo-futures.kraken.com/ws/v1"
    db_url: str = "sqlite:///pub-futures-demo.sqlite"
    private_db_url: str = "sqlite:///prv-futures-demo.sqlite"
    rest_url: str = "https://demo-futures.kraken.com/derivatives/api/v3"
    exchange: str = "kraken"
    environment: str = "demo"
    market_type: str = "futures"
    log_level: str = "INFO"
    snapshot_interval_seconds: float = 1.0
    indicator_interval_seconds: float = 6.0
    log_interval_seconds: float = 6.0
    retention_minutes: int = 30
    reconnect_seconds: int = 5
    raw_retention_minutes: int = 1440
    raw_retention_limit: int = 100000
    trace_ws: bool = False
    trace_ws_format: str = "compact"
    trace_ws_max_lines: int = 0
    ticker_interval_seconds: float = 2.0


@dataclass(frozen=True)
class BookMetrics:
    """Indicateurs compacts calcules depuis les niveaux L2 retenus."""

    avg_ask: float
    avg_bid: float
    best_ask: float
    best_bid: float
    spread: float
    mid_price: float
    imbalance: float


@dataclass(frozen=True)
class PendingBook:
    """Dernier carnet recu en memoire avant flush planifie vers la DB."""

    asks: list[BookLevelT]
    bids: list[BookLevelT]
    metrics: BookMetrics
    received_at: datetime
    source_timestamp: datetime | None
    sequence: int | None
    signature: BookSignatureT


@dataclass(frozen=True)
class FlushResult:
    """Resultat d'un tick de persistence planifie."""

    snapshot: MarketSnapshot | None
    indicators_written: int


@dataclass(frozen=True)
class TickerPrices:
    last_price: float | None
    mark_price: float | None
    index_price: float | None


@dataclass(frozen=True)
class BookPayload:
    """Snapshot ou delta du book Kraken Futures."""

    message_type: str
    symbol: str
    asks: tuple[BookLevelT, ...]
    bids: tuple[BookLevelT, ...]
    source_timestamp: datetime | None
    sequence: int | None
    side: str | None = None
    price: float | None = None
    quantity: float | None = None


@dataclass(frozen=True)
class OrderBookState:
    """Etat local du carnet pour un produit Futures."""

    symbol: str
    asks: tuple[BookLevelT, ...]
    bids: tuple[BookLevelT, ...]
    sequence: int | None
    source_timestamp: datetime | None


class KrakenTree:
    """Lecteur async Kraken Futures qui ecrit une memoire normalisee."""

    def __init__(self, config: KrakenConfig) -> None:
        self.config = config
        self.logger = setup_logging(config.log_level)
        self.engine = build_engine(config.db_url)
        Base.metadata.create_all(self.engine)
        upgrade_public_schema(self.engine)
        self.sessionmaker = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=Session,
        )
        self._running = True
        self._latest_book: PendingBook | None = None
        self._book_state: OrderBookState | None = None
        self._last_snapshot_signature: BookSignatureT | None = None
        self._last_snapshot_flush_at: datetime | None = None
        self._last_indicator_flush_at: datetime | None = None
        self._last_log_at: datetime | None = None
        self._latest_snapshot_id: int | None = None
        self._stored_count = 0
        self._raw_message_count = 0
        self._book_message_count = 0
        self._trace_count = 0
        self._last_trace_cap_logged = False
        self._last_status_log_line: str | None = None
        self._stop_event = asyncio.Event()
        self._rest_session = requests.Session()
        self._last_ticker_fetch_at: datetime | None = None
        self._latest_ticker_prices: TickerPrices | None = None

    async def run(self) -> None:
        """Tourne en continu et relance la session websocket apres erreur."""
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "kraken_tree reconnecting in %ss after error: %s",
                    self.config.reconnect_seconds,
                    exc,
                )
                await self._wait_or_stop(float(self.config.reconnect_seconds))

    async def run_once(self) -> None:
        """Ouvre une session websocket et laisse le scheduler cadencer la DB."""
        async with websockets.connect(self.config.ws_url, ping_interval=20) as ws:
            await ws.send(json.dumps(subscription_message(self.config)))
            self.logger.info(
                "kraken_tree subscribed pair=%s depth=%s env=%s db=%s ws=%s",
                self.config.pair,
                self.config.depth,
                self.config.environment,
                self.config.db_url,
                self.config.ws_url,
            )
            while self._running:
                try:
                    raw_message = await asyncio.wait_for(ws.recv(), timeout=0.25)
                    self.handle_message(raw_message)
                except TimeoutError:
                    pass
                self.flush_due(datetime.now(timezone.utc))

    def stop(self) -> None:
        """Demande l'arret apres la reception websocket courante."""
        self._running = False
        self._stop_event.set()

    async def _wait_or_stop(self, seconds: float) -> None:
        """Wait for delay unless stop is requested earlier."""
        if seconds <= 0:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)

    def handle_message(self, raw_message: str | bytes) -> PendingBook | None:
        """Parse un message Kraken Futures et met le dernier carnet en memoire."""
        payload = (
            raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message
        )
        message = json.loads(payload)
        self._raw_message_count += 1
        self.record_raw_event(message, stream_kind="public_ws")
        self.trace_message(message, payload)
        parsed = extract_book_payload(message)
        if parsed is None:
            return None
        self._book_message_count += 1
        return self.ingest_payload(parsed, datetime.now(timezone.utc))

    def record_raw_event(
        self,
        message: dict[str, object],
        stream_kind: str = "public_ws",
    ) -> RawExchangeEvent:
        """Persiste le payload brut public avec collapse des doublons consecutifs."""
        event_type = str(message.get("feed") or message.get("event") or "unknown")
        payload = dict(message)
        source_timestamp = parse_kraken_time(
            first_present(payload, "timestamp", "time", "last_update_time")
        )
        now = datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            previous = (
                session.execute(
                    select(RawExchangeEvent)
                    .where(
                        RawExchangeEvent.exchange == self.config.exchange,
                        RawExchangeEvent.environment == self.config.environment,
                        RawExchangeEvent.stream_kind == stream_kind,
                        RawExchangeEvent.event_type == event_type,
                    )
                    .order_by(RawExchangeEvent.received_at.desc(), RawExchangeEvent.id.desc())
                )
                .scalars()
                .first()
            )
            if previous is not None and previous.payload == payload:
                previous.duplicate_count = int(previous.duplicate_count or 0) + 1
                previous.last_seen_at = now
                prune_raw_events(
                    session,
                    config=self.config,
                    retention_minutes=self.config.raw_retention_minutes,
                    retention_limit=self.config.raw_retention_limit,
                    now=now,
                    stream_kind=stream_kind,
                )
                session.commit()
                session.refresh(previous)
                return previous

            row = RawExchangeEvent(
                exchange=self.config.exchange,
                environment=self.config.environment,
                market_type=self.config.market_type,
                account_scope="public",
                symbol=raw_event_symbol(payload),
                stream_kind=stream_kind,
                event_type=event_type,
                correlation_id=raw_event_correlation_id(payload),
                exchange_sequence=optional_str(payload.get("seq")),
                payload=payload,
                source_timestamp=source_timestamp,
                duplicate_count=0,
                last_seen_at=now,
                received_at=now,
                created_at=now,
            )
            session.add(row)
            prune_raw_events(
                session,
                config=self.config,
                retention_minutes=self.config.raw_retention_minutes,
                retention_limit=self.config.raw_retention_limit,
                now=now,
                stream_kind=stream_kind,
            )
            session.commit()
            session.refresh(row)
            return row

    def ingest_payload(self, payload: BookPayload, received_at: datetime) -> PendingBook:
        """Applique un snapshot ou un delta puis derive les indicateurs."""
        self._book_state = apply_book_payload(self._book_state, payload, self.config.depth)
        state = self._book_state
        assert state is not None
        metrics = calculate_metrics(state.asks, state.bids)
        pending = PendingBook(
            asks=list(state.asks),
            bids=list(state.bids),
            metrics=metrics,
            received_at=received_at,
            source_timestamp=state.source_timestamp,
            sequence=state.sequence,
            signature=book_signature(state.asks, state.bids),
        )
        self._latest_book = pending
        return pending

    def ingest_book(
        self,
        asks: Sequence[RawLevelT],
        bids: Sequence[RawLevelT],
        received_at: datetime,
    ) -> PendingBook:
        """Compatibilite tests/scripts: injecte directement un carnet complet."""
        payload = BookPayload(
            message_type="snapshot",
            symbol=self.config.pair,
            asks=tuple(parse_levels(asks, self.config.depth)),
            bids=tuple(parse_levels(bids, self.config.depth)),
            source_timestamp=received_at,
            sequence=1,
        )
        return self.ingest_payload(payload, received_at)

    def flush_due(self, now: datetime) -> FlushResult:
        """Ecrit snapshots/indicateurs dont la cadence est arrivee."""
        if self._latest_book is None:
            return FlushResult(snapshot=None, indicators_written=0)

        snapshot: MarketSnapshot | None = None
        if is_due(
            self._last_snapshot_flush_at, now, self.config.snapshot_interval_seconds
        ):
            if self._latest_book.signature != self._last_snapshot_signature:
                snapshot = self.persist_market_snapshot(self._latest_book, now)
                self._latest_snapshot_id = snapshot.id
                self._last_snapshot_signature = self._latest_book.signature
                self._stored_count += 1
            self._last_snapshot_flush_at = now

        indicators_written = 0
        if is_due(
            self._last_indicator_flush_at,
            now,
            self.config.indicator_interval_seconds,
        ):
            indicators_written = self.persist_indicators(self._latest_book, now)
            self._last_indicator_flush_at = now

        self.log_due(now, snapshot)
        return FlushResult(snapshot=snapshot, indicators_written=indicators_written)

    def process_book(
        self,
        asks: Sequence[RawLevelT],
        bids: Sequence[RawLevelT],
    ) -> MarketSnapshot:
        """Compatibilite tests/scripts: ingere puis force un snapshot."""
        now = datetime.now(timezone.utc)
        pending = self.ingest_book(asks, bids, now)
        snapshot = self.persist_market_snapshot(pending, now)
        self._latest_snapshot_id = snapshot.id
        self._last_snapshot_signature = pending.signature
        self._last_snapshot_flush_at = now
        self.persist_indicators(pending, now)
        return snapshot

    def persist_market_snapshot(
        self,
        pending: PendingBook,
        now: datetime,
    ) -> MarketSnapshot:
        """Persiste un snapshot public normalise et ses niveaux sparse."""
        with self.sessionmaker() as session:
            snapshot = MarketSnapshot(
                local_uuid=str(uuid4()),
                exchange=self.config.exchange,
                environment=self.config.environment,
                market_type=self.config.market_type,
                symbol=self.config.pair,
                best_bid=pending.metrics.best_bid,
                best_ask=pending.metrics.best_ask,
                avg_bid=pending.metrics.avg_bid,
                avg_ask=pending.metrics.avg_ask,
                mid_price=pending.metrics.mid_price,
                spread=pending.metrics.spread,
                imbalance=pending.metrics.imbalance,
                source_timestamp=pending.source_timestamp or pending.received_at,
                local_timestamp=now,
            )
            session.add(snapshot)
            session.flush()
            session.add_all(
                build_market_level_rows(snapshot.id, "ask", pending.asks)
                + build_market_level_rows(snapshot.id, "bid", pending.bids)
            )
            prune_old_market_data(session, self.config.retention_minutes, now)
            session.commit()
            session.refresh(snapshot)
            return snapshot

    def persist_indicators(self, pending: PendingBook, now: datetime) -> int:
        """Persiste les indicateurs compacts normalises."""
        source_anchor = pending.source_timestamp or pending.received_at
        source_age = max((now - source_anchor).total_seconds(), 0.0)
        ticker_prices = self._ticker_prices_due(now)
        indicators = build_indicator_rows(
            config=self.config,
            pending=pending,
            snapshot_id=self._latest_snapshot_id,
            source_age_seconds=source_age,
            computed_at=now,
            ticker_prices=ticker_prices,
        )
        with self.sessionmaker() as session:
            session.add_all(indicators)
            prune_old_indicators(session, self.config.retention_minutes, now)
            session.commit()
        return len(indicators)

    def _ticker_prices_due(self, now: datetime) -> TickerPrices | None:
        if self._latest_ticker_prices is not None and not is_due(
            self._last_ticker_fetch_at, now, self.config.ticker_interval_seconds
        ):
            return self._latest_ticker_prices
        try:
            ticker = self._fetch_ticker_prices()
        except Exception as exc:
            self.logger.debug("kraken_tree ticker fetch failed: %s", exc)
            return self._latest_ticker_prices
        self._latest_ticker_prices = ticker
        self._last_ticker_fetch_at = now
        return ticker

    def _fetch_ticker_prices(self) -> TickerPrices:
        response = self._rest_session.get(
            f"{self.config.rest_url.rstrip('/')}/tickers/{self.config.pair}",
            timeout=5.0,
        )
        response.raise_for_status()
        payload = response.json()
        ticker = (
            payload.get("ticker")
            if isinstance(payload, dict)
            else None
        )
        ticker_map = ticker if isinstance(ticker, dict) else (
            ticker[0] if isinstance(ticker, list) and ticker and isinstance(ticker[0], dict) else payload
        )
        if not isinstance(ticker_map, dict):
            ticker_map = {}
        return TickerPrices(
            last_price=optional_float(
                first_present(ticker_map, "last", "lastPrice")
            ),
            mark_price=optional_float(
                first_present(ticker_map, "markPrice", "mark_price")
            ),
            index_price=optional_float(
                first_present(ticker_map, "indexPrice", "index_price", "indicativeSettlePrice")
            ),
        )

    def log_due(self, now: datetime, snapshot: MarketSnapshot | None) -> None:
        """Imprime un statut compact lisible dans screen ou un log."""
        if not is_due(self._last_log_at, now, self.config.log_interval_seconds):
            return
        self._last_log_at = now
        if self._latest_book is None:
            return
        status_line = format_market_status(
            config=self.config,
            metrics=self._latest_book.metrics,
            stored_count=self._stored_count,
            persisted=snapshot is not None,
            raw_message_count=self._raw_message_count,
            book_message_count=self._book_message_count,
        )
        if status_line == self._last_status_log_line:
            return
        self._last_status_log_line = status_line
        self.logger.info(status_line)

    def trace_message(self, message: object, raw_payload: str) -> None:
        """Ecrit les messages websocket bruts si le mode trace est actif."""
        if not self.config.trace_ws:
            return
        if self.config.trace_ws_max_lines > 0 and self._trace_count >= self.config.trace_ws_max_lines:
            if not self._last_trace_cap_logged:
                self.logger.info(
                    "kraken_ws_trace cap reached lines=%s",
                    self.config.trace_ws_max_lines,
                )
                self._last_trace_cap_logged = True
            return
        self._trace_count += 1
        self.logger.info(
            "kraken_ws_raw %s",
            format_trace_message(message, raw_payload, self.config.trace_ws_format),
        )

    def latest_status(self, pair: str | None = None) -> dict[str, object]:
        """Retourne l'etat compact le plus recent pour un produit."""
        target_pair = pair or self.config.pair
        with self.sessionmaker() as session:
            snapshot = latest_snapshot(
                session,
                target_pair,
                self.config.exchange,
                self.config.environment,
                self.config.market_type,
            )
            snapshot_count = count_snapshots(
                session,
                target_pair,
                self.config.exchange,
                self.config.environment,
                self.config.market_type,
            )
            level_count = count_levels(
                session,
                target_pair,
                self.config.exchange,
                self.config.environment,
                self.config.market_type,
            )
            indicator_count = count_indicators(
                session,
                target_pair,
                self.config.exchange,
                self.config.environment,
                self.config.market_type,
            )
            if snapshot is None:
                return {
                    "environment": self.config.environment,
                    "exchange": self.config.exchange,
                    "indicator_count": indicator_count,
                    "level_count": level_count,
                    "market_type": self.config.market_type,
                    "pair": target_pair,
                    "snapshot_count": snapshot_count,
                    "status": "empty",
                }
            return snapshot_to_status(
                snapshot=snapshot,
                snapshot_count=snapshot_count,
                level_count=level_count,
                indicator_count=indicator_count,
            )


class OrderBookSnapshot(Base):
    """Ancienne table KrakenTree gardee seulement pour compatibilite."""

    __tablename__ = "kraken_orderbook"
    __table_args__ = (
        Index("ix_kraken_orderbook_pair_recorded_at", "pair", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair: Mapped[str] = mapped_column(String(32), nullable=False)
    avg_ask: Mapped[float] = mapped_column(Float, nullable=False)
    avg_bid: Mapped[float] = mapped_column(Float, nullable=False)
    spread: Mapped[float] = mapped_column(Float, nullable=False)
    mid_price: Mapped[float] = mapped_column(Float, nullable=False)
    imbalance: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    levels: Mapped[list["OrderBookLevel"]] = relationship(
        "OrderBookLevel",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class OrderBookLevel(Base):
    """Ancienne table de niveaux KrakenTree gardee pour compatibilite."""

    __tablename__ = "kraken_orderbook_level"
    __table_args__ = (
        Index("ix_kraken_orderbook_level_snapshot_side", "snapshot_id", "side"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("kraken_orderbook.id", ondelete="CASCADE"),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(String(3), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    level_index: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[OrderBookSnapshot] = relationship(
        "OrderBookSnapshot",
        back_populates="levels",
    )


def build_engine(db_url: str) -> Engine:
    """Cree l'engine DB du service avec reglages SQLite locaux."""
    connect_args = {"timeout": 30} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, future=True, connect_args=connect_args)
    if db_url.startswith("sqlite"):
        configure_sqlite_engine(engine)
    return engine


def configure_sqlite_engine(engine: Engine) -> None:
    """Installe WAL et les pragmas de concurrence sur chaque connexion."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
        cursor = cast_dbapi_connection(dbapi_connection).cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def cast_dbapi_connection(connection: object) -> Any:
    """Expose la connexion DBAPI avec les methodes attendues par SQLite."""
    return connection


def upgrade_public_schema(engine: Engine) -> None:
    """Apply additive SQLite schema upgrades for existing public DBs."""
    if getattr(engine.dialect, "name", "") != "sqlite":
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "raw_exchange_events" in tables:
            ensure_columns(
                connection,
                "raw_exchange_events",
                {
                    "environment": "VARCHAR(32)",
                    "market_type": "VARCHAR(32)",
                    "account_scope": "VARCHAR(64)",
                    "symbol": "VARCHAR(64)",
                    "exchange_sequence": "VARCHAR(128)",
                    "source_timestamp": "DATETIME",
                    "duplicate_count": "INTEGER",
                    "last_seen_at": "DATETIME",
                    "received_at": "DATETIME",
                },
            )
            connection.execute(
                text(
                    "UPDATE raw_exchange_events "
                    "SET duplicate_count = COALESCE(duplicate_count, 0) "
                    "WHERE duplicate_count IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE raw_exchange_events "
                    "SET last_seen_at = COALESCE(last_seen_at, received_at) "
                    "WHERE last_seen_at IS NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_raw_exchange_events_identity "
                    "ON raw_exchange_events "
                    "(exchange, environment, stream_kind, event_type, correlation_id)"
                )
            )


def ensure_columns(connection: Any, table_name: str, columns: dict[str, str]) -> None:
    existing = {
        str(row[1])
        for row in connection.execute(text(f"PRAGMA table_info({table_name})"))
    }
    for column_name, column_type in columns.items():
        if column_name not in existing:
            connection.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            )


def subscription_message(config: KrakenConfig) -> dict[str, object]:
    """Construit le message d'abonnement Kraken Futures book."""
    return {
        "event": "subscribe",
        "feed": "book",
        "product_ids": [config.pair],
    }


def extract_book_payload(message: object) -> BookPayload | None:
    """Extrait snapshot ou delta du feed Futures `book`."""
    if not isinstance(message, dict):
        return None
    # Les accusés de reception `event=subscribed` reutilisent parfois `feed=book`.
    # Il faut donc filtrer d'abord les evenements d'administration pour ne pas
    # les confondre avec un vrai delta.
    if message.get("event") is not None:
        return None
    feed = message.get("feed")
    if feed == "book_snapshot":
        asks = tuple(parse_levels(message.get("asks", []), depth=10_000))
        bids = tuple(parse_levels(message.get("bids", []), depth=10_000))
        return BookPayload(
            message_type="snapshot",
            symbol=str(message.get("product_id") or ""),
            asks=asks,
            bids=bids,
            source_timestamp=parse_kraken_time(message.get("timestamp")),
            sequence=parse_optional_int(message.get("seq")),
        )
    if feed == "book":
        if message.get("product_id") in (None, "") or message.get("side") in (None, ""):
            return None
        side = str(message.get("side") or "")
        return BookPayload(
            message_type="update",
            symbol=str(message.get("product_id") or ""),
            asks=(),
            bids=(),
            source_timestamp=parse_kraken_time(message.get("timestamp")),
            sequence=parse_optional_int(message.get("seq")),
            side=side,
            price=optional_float(message.get("price")),
            quantity=optional_float(message.get("qty")),
        )
    return None


def extract_book_update(
    message: object,
) -> tuple[Sequence[RawLevelT], Sequence[RawLevelT]] | None:
    """Compatibilite tests/scripts: extrait asks/bids d'un snapshot."""
    payload = extract_book_payload(message)
    if payload is None:
        return None
    if payload.message_type == "snapshot":
        return payload.asks, payload.bids
    if payload.side == "buy":
        return (), ((payload.price or 0.0), (payload.quantity or 0.0))
    return ((payload.price or 0.0), (payload.quantity or 0.0)), ()


def apply_book_payload(
    state: OrderBookState | None,
    payload: BookPayload,
    depth: int,
) -> OrderBookState:
    """Applique snapshot/update Futures au carnet local."""
    if payload.message_type == "snapshot":
        asks = truncate_book_side(payload.asks, depth, reverse=False)
        bids = truncate_book_side(payload.bids, depth, reverse=True)
        return OrderBookState(
            symbol=payload.symbol,
            asks=asks,
            bids=bids,
            sequence=payload.sequence,
            source_timestamp=payload.source_timestamp,
        )
    if state is None:
        raise ValueError("kraken_tree received delta before snapshot")
    if payload.sequence is not None and state.sequence is not None:
        if payload.sequence <= state.sequence:
            return state
    asks = apply_side_update(state.asks, payload, depth, side_name="sell")
    bids = apply_side_update(state.bids, payload, depth, side_name="buy")
    if not asks or not bids:
        raise ValueError("kraken_tree local book lost one side")
    return OrderBookState(
        symbol=payload.symbol,
        asks=asks,
        bids=bids,
        sequence=payload.sequence or state.sequence,
        source_timestamp=payload.source_timestamp or state.source_timestamp,
    )


def apply_side_update(
    current: Sequence[BookLevelT],
    payload: BookPayload,
    depth: int,
    side_name: str,
) -> tuple[BookLevelT, ...]:
    """Applique un delta a un seul cote du carnet."""
    levels = {price: quantity for price, quantity in current}
    if payload.side == side_name and payload.price is not None and payload.quantity is not None:
        if payload.quantity <= 0:
            levels.pop(payload.price, None)
        else:
            levels[payload.price] = payload.quantity
    reverse = side_name == "buy"
    return truncate_book_side(tuple(levels.items()), depth, reverse=reverse)


def truncate_book_side(
    levels: Sequence[BookLevelT],
    depth: int,
    reverse: bool,
) -> tuple[BookLevelT, ...]:
    """Trie un cote du carnet et tronque a la profondeur retenue."""
    ordered = sorted(
        [(float(price), float(quantity)) for price, quantity in levels if quantity > 0],
        key=lambda item: item[0],
        reverse=reverse,
    )
    return tuple(ordered[:depth])


def parse_levels(levels: Sequence[RawLevelT], depth: int) -> list[BookLevelT]:
    """Convertit des niveaux bruts Futures en couples prix-volume."""
    parsed: list[BookLevelT] = []
    for level in levels[:depth]:
        if isinstance(level, dict):
            price = optional_float(level.get("price"))
            quantity = optional_float(level.get("qty"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = optional_float(level[0])
            quantity = optional_float(level[1])
        else:
            continue
        if price is None or quantity is None or quantity <= 0:
            continue
        parsed.append((price, quantity))
    return parsed


def optional_float(value: object) -> float | None:
    """Convertit defensivement une valeur optionnelle en float."""
    if value in (None, ""):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    return float(value)


def parse_optional_int(value: object) -> int | None:
    """Parse un entier optionnel depuis le payload websocket."""
    if value in (None, ""):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    return int(value)


def parse_kraken_time(value: object) -> datetime | None:
    """Parse le temps Kraken Futures en datetime timezone-aware."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def first_present(payload: dict[str, object], *keys: str) -> object:
    """Retourne le premier champ present meme si sa valeur vaut 0."""
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def optional_str(value: object) -> str | None:
    """Convertit une valeur optionnelle en str."""
    if value is None or value == "":
        return None
    return str(value)


def raw_event_symbol(payload: dict[str, object]) -> str | None:
    direct = optional_str(payload.get("product_id") or payload.get("symbol"))
    if direct:
        return direct
    for key in ("asks", "bids", "orders", "fills"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    nested = optional_str(item.get("product_id") or item.get("symbol"))
                    if nested:
                        return nested
    return None


def raw_event_correlation_id(payload: dict[str, object]) -> str | None:
    direct = optional_str(
        payload.get("order_id")
        or payload.get("orderId")
        or payload.get("fill_id")
        or payload.get("fillId")
        or payload.get("cli_ord_id")
        or payload.get("cliOrdId")
    )
    if direct:
        return direct
    return optional_str(payload.get("seq"))


def calculate_metrics(
    asks: Sequence[BookLevelT], bids: Sequence[BookLevelT]
) -> BookMetrics:
    """Calcule les indicateurs compacts depuis les niveaux conserves."""
    if not asks or not bids:
        raise ValueError("asks and bids must contain at least one level")
    best_ask = min(price for price, _volume in asks)
    best_bid = max(price for price, _volume in bids)
    ask_volume = sum(volume for _price, volume in asks)
    bid_volume = sum(volume for _price, volume in bids)
    total_volume = max(ask_volume + bid_volume, 1e-8)
    return BookMetrics(
        avg_ask=weighted_average(asks),
        avg_bid=weighted_average(bids),
        best_ask=best_ask,
        best_bid=best_bid,
        spread=best_ask - best_bid,
        mid_price=(best_ask + best_bid) / 2,
        imbalance=(bid_volume - ask_volume) / total_volume,
    )


def weighted_average(levels: Sequence[BookLevelT]) -> float:
    """Retourne le prix moyen pondere par le volume."""
    volume = sum(level_volume for _price, level_volume in levels)
    return sum(price * level_volume for price, level_volume in levels) / max(volume, 1e-8)


def book_signature(
    asks: Sequence[BookLevelT],
    bids: Sequence[BookLevelT],
) -> BookSignatureT:
    """Construit une signature stable pour detecter les carnets inchanges."""
    return (tuple(asks), tuple(bids))


def is_due(last_at: datetime | None, now: datetime, interval_seconds: float) -> bool:
    """Indique si un traitement periodique doit s'executer."""
    if interval_seconds <= 0:
        return True
    if last_at is None:
        return True
    return (now - last_at).total_seconds() >= interval_seconds


def build_market_level_rows(
    snapshot_id: int,
    side: str,
    levels: Sequence[BookLevelT],
) -> list[MarketLevel]:
    """Construit les lignes normalisees de niveaux L2."""
    return [
        MarketLevel(
            snapshot_id=snapshot_id,
            side=side,
            price=price,
            volume=volume,
            level_index=index,
        )
        for index, (price, volume) in enumerate(levels)
    ]


def build_indicator_rows(
    config: KrakenConfig,
    pending: PendingBook,
    snapshot_id: int | None,
    source_age_seconds: float,
    computed_at: datetime,
    ticker_prices: TickerPrices | None = None,
) -> list[MarketIndicator]:
    """Construit les indicateurs compactes, incluant une MGF simple."""
    metrics = pending.metrics
    variance = max((metrics.spread / 2) ** 2, 0.0)
    values = {
        "avg_ask": metrics.avg_ask,
        "avg_bid": metrics.avg_bid,
        "spread": metrics.spread,
        "mid_price": metrics.mid_price,
        "imbalance": metrics.imbalance,
        "price_mgf_t0_0001": normal_mgf(0.0001, metrics.mid_price, variance),
    }
    if ticker_prices is not None:
        if ticker_prices.last_price is not None:
            values["last_price"] = ticker_prices.last_price
        if ticker_prices.mark_price is not None:
            values["mark_price"] = ticker_prices.mark_price
        if ticker_prices.index_price is not None:
            values["index_price"] = ticker_prices.index_price
    return [
        MarketIndicator(
            snapshot_id=snapshot_id,
            exchange=config.exchange,
            environment=config.environment,
            market_type=config.market_type,
            symbol=config.pair,
            indicator_name=name,
            value=value,
            source_age_seconds=source_age_seconds,
            computed_at=computed_at,
        )
        for name, value in values.items()
    ]


def normal_mgf(moment_t: float, mean: float, variance: float) -> float:
    """Moment generating function d'une loi normale: exp(mu*t + var*t^2/2)."""
    exponent = (mean * moment_t) + (variance * moment_t * moment_t / 2)
    # Cet indicateur est diagnostique; il ne doit jamais faire tomber le flux public.
    if exponent >= 700:
        return math.exp(700)
    if exponent <= -745:
        return 0.0
    return math.exp(exponent)


def prune_old_market_data(
    session: Session,
    retention_minutes: int,
    now: datetime,
) -> None:
    """Supprime les snapshots/niveaux publics hors retention."""
    if retention_minutes <= 0:
        return
    cutoff = now - timedelta(minutes=retention_minutes)
    old_ids = select(MarketSnapshot.id).where(MarketSnapshot.local_timestamp < cutoff)
    session.execute(delete(MarketLevel).where(MarketLevel.snapshot_id.in_(old_ids)))
    session.execute(
        delete(MarketIndicator).where(MarketIndicator.snapshot_id.in_(old_ids))
    )
    session.execute(delete(MarketSnapshot).where(MarketSnapshot.local_timestamp < cutoff))


def prune_raw_events(
    session: Session,
    *,
    config: KrakenConfig,
    retention_minutes: int,
    retention_limit: int,
    now: datetime,
    stream_kind: str = "public_ws",
) -> None:
    """Apply bounded raw public-event retention for one stream identity."""
    base_filters = (
        RawExchangeEvent.exchange == config.exchange,
        RawExchangeEvent.environment == config.environment,
        RawExchangeEvent.stream_kind == stream_kind,
    )
    if retention_minutes > 0:
        cutoff = now - timedelta(minutes=retention_minutes)
        session.execute(
            delete(RawExchangeEvent)
            .where(
                *base_filters,
                RawExchangeEvent.received_at < cutoff,
            )
            .execution_options(synchronize_session=False)
        )
    if retention_limit > 0:
        keep_ids = (
            select(RawExchangeEvent.id)
            .where(*base_filters)
            .order_by(RawExchangeEvent.received_at.desc(), RawExchangeEvent.id.desc())
            .limit(retention_limit)
        )
        session.execute(
            delete(RawExchangeEvent)
            .where(
                *base_filters,
                RawExchangeEvent.id.notin_(keep_ids),
            )
            .execution_options(synchronize_session=False)
        )


def prune_old_indicators(
    session: Session,
    retention_minutes: int,
    now: datetime,
) -> None:
    """Supprime les indicateurs hors retention."""
    if retention_minutes <= 0:
        return
    cutoff = now - timedelta(minutes=retention_minutes)
    session.execute(delete(MarketIndicator).where(MarketIndicator.computed_at < cutoff))


def latest_snapshot(
    session: Session,
    pair: str,
    exchange: str = "kraken",
    environment: str | None = None,
    market_type: str | None = None,
) -> MarketSnapshot | None:
    """Lit le snapshot normalise le plus recent pour un produit."""
    stmt = select(MarketSnapshot).where(
        MarketSnapshot.exchange == exchange,
        MarketSnapshot.symbol == pair,
    )
    if environment is not None:
        stmt = stmt.where(MarketSnapshot.environment == environment)
    if market_type is not None:
        stmt = stmt.where(MarketSnapshot.market_type == market_type)
    stmt = stmt.order_by(MarketSnapshot.local_timestamp.desc(), MarketSnapshot.id.desc())
    return session.execute(stmt).scalars().first()


def count_snapshots(
    session: Session,
    pair: str,
    exchange: str,
    environment: str | None = None,
    market_type: str | None = None,
) -> int:
    """Compte les snapshots normalises pour un produit."""
    stmt = select(func.count()).select_from(MarketSnapshot).where(
        MarketSnapshot.exchange == exchange,
        MarketSnapshot.symbol == pair,
    )
    if environment is not None:
        stmt = stmt.where(MarketSnapshot.environment == environment)
    if market_type is not None:
        stmt = stmt.where(MarketSnapshot.market_type == market_type)
    return int(session.execute(stmt).scalar_one())


def count_levels(
    session: Session,
    pair: str,
    exchange: str,
    environment: str | None = None,
    market_type: str | None = None,
) -> int:
    """Compte les niveaux normalises pour un produit."""
    stmt = (
        select(func.count())
        .select_from(MarketLevel)
        .join(MarketSnapshot)
        .where(MarketSnapshot.exchange == exchange, MarketSnapshot.symbol == pair)
    )
    if environment is not None:
        stmt = stmt.where(MarketSnapshot.environment == environment)
    if market_type is not None:
        stmt = stmt.where(MarketSnapshot.market_type == market_type)
    return int(session.execute(stmt).scalar_one())


def count_indicators(
    session: Session,
    pair: str,
    exchange: str,
    environment: str | None = None,
    market_type: str | None = None,
) -> int:
    """Compte les indicateurs normalises pour un produit."""
    stmt = (
        select(func.count())
        .select_from(MarketIndicator)
        .where(MarketIndicator.exchange == exchange, MarketIndicator.symbol == pair)
    )
    if environment is not None:
        stmt = stmt.where(MarketIndicator.environment == environment)
    if market_type is not None:
        stmt = stmt.where(MarketIndicator.market_type == market_type)
    return int(session.execute(stmt).scalar_one())


def latest_indicator_values(
    session: Session,
    pair: str,
    exchange: str = "kraken",
    environment: str | None = None,
    market_type: str | None = None,
) -> dict[str, MarketIndicator]:
    """Retourne le dernier indicateur par nom pour un produit."""
    stmt = (
        select(MarketIndicator)
        .where(MarketIndicator.exchange == exchange, MarketIndicator.symbol == pair)
        .order_by(MarketIndicator.computed_at.desc(), MarketIndicator.id.desc())
    )
    if environment is not None:
        stmt = stmt.where(MarketIndicator.environment == environment)
    if market_type is not None:
        stmt = stmt.where(MarketIndicator.market_type == market_type)
    latest: dict[str, MarketIndicator] = {}
    for indicator in session.execute(stmt).scalars():
        latest.setdefault(indicator.indicator_name, indicator)
    return latest


def snapshot_to_status(
    snapshot: MarketSnapshot,
    snapshot_count: int,
    level_count: int,
    indicator_count: int,
) -> dict[str, object]:
    """Formate un snapshot normalise pour la CLI et les lecteurs JSON."""
    return {
        "avg_ask": snapshot.avg_ask,
        "avg_bid": snapshot.avg_bid,
        "best_ask": snapshot.best_ask,
        "best_bid": snapshot.best_bid,
        "environment": snapshot.environment,
        "exchange": snapshot.exchange,
        "imbalance": snapshot.imbalance,
        "indicator_count": indicator_count,
        "level_count": level_count,
        "market_type": snapshot.market_type,
        "mid_price": snapshot.mid_price,
        "pair": snapshot.symbol,
        "recorded_at": snapshot.local_timestamp.isoformat(),
        "snapshot_count": snapshot_count,
        "source_timestamp": snapshot.source_timestamp.isoformat()
        if snapshot.source_timestamp
        else None,
        "spread": snapshot.spread,
        "status": "ok",
    }


def format_market_status(
    config: KrakenConfig,
    metrics: BookMetrics,
    stored_count: int,
    persisted: bool,
    raw_message_count: int,
    book_message_count: int,
) -> str:
    """Retourne une ligne courte lisible dans screen ou un log."""
    return (
        f"kraken_tree env={config.environment} count={stored_count} pair={config.pair} "
        f"persisted={persisted} raw={raw_message_count} book={book_message_count} "
        f"best_bid={metrics.best_bid:.2f} best_ask={metrics.best_ask:.2f} "
        f"spread={metrics.spread:.2f} mid={metrics.mid_price:.2f} "
        f"imbalance={metrics.imbalance:.4f}"
    )


def format_trace_message(message: object, raw_payload: str, trace_format: str) -> str:
    """Produit une trace lisible d'un message websocket."""
    if trace_format == "json":
        return raw_payload
    if isinstance(message, dict):
        feed = message.get("feed")
        if feed in {"book_snapshot", "book"}:
            return (
                f"feed={feed} product_id={message.get('product_id')} "
                f"seq={message.get('seq')} side={message.get('side')} "
                f"price={message.get('price')} qty={message.get('qty')}"
            )
        if message.get("event") == "subscribed":
            return f"subscribed feed={message.get('feed')} products={message.get('product_ids')}"
    compact = " ".join(raw_payload.split())
    return compact[:300]


def build_parser() -> argparse.ArgumentParser:
    """Construit le parser CLI des commandes `run`, `probe` et `status`."""
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.tree.kraken",
        description="Public market-data service CLI for Kraken Futures.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="<command>",
    )
    command_help = {
        "run": "Run public websocket listener and persist market snapshots.",
        "probe": "Run listener for a bounded duration and print status.",
        "status": "Show latest public DB status for a symbol.",
    }
    for command in ("run", "probe", "status"):
        command_parser = subparsers.add_parser(
            command,
            help=command_help[command],
            description=command_help[command],
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        command_parser.add_argument("--pair", default=KrakenConfig.pair, help="Futures product id.")
        command_parser.add_argument("--depth", type=int, default=KrakenConfig.depth, help="Orderbook depth to keep.")
        command_parser.add_argument("--environment", choices=("demo", "live"), default=KrakenConfig.environment, help="Endpoint family.")
        command_parser.add_argument("--ws-url", help="Override public websocket URL.")
        command_parser.add_argument("--rest-url", help="Override public REST URL for ticker polling.")
        command_parser.add_argument("--db-url", help="Override public SQLite DB URL.")
        command_parser.add_argument("--private-db-url", help="Override private SQLite DB URL used for correlation.")
        command_parser.add_argument("--exchange", default=KrakenConfig.exchange, help="Exchange label stored with rows.")
        command_parser.add_argument("--market-type", default=KrakenConfig.market_type, help="Market type label stored with rows.")
        command_parser.add_argument("--log-level", default=KrakenConfig.log_level, help="Logging verbosity.")
        command_parser.add_argument(
            "--snapshot-interval-seconds",
            type=float,
            default=KrakenConfig.snapshot_interval_seconds,
            help="Snapshot write cadence.",
        )
        command_parser.add_argument(
            "--indicator-interval-seconds",
            type=float,
            default=KrakenConfig.indicator_interval_seconds,
            help="Indicator update cadence.",
        )
        command_parser.add_argument(
            "--ticker-interval-seconds",
            type=float,
            default=KrakenConfig.ticker_interval_seconds,
            help="Ticker REST polling cadence used for last/mark/index prices.",
        )
        command_parser.add_argument(
            "--log-interval-seconds",
            type=float,
            default=KrakenConfig.log_interval_seconds,
            help="Heartbeat log cadence.",
        )
        command_parser.add_argument(
            "--retention-minutes",
            type=int,
            default=KrakenConfig.retention_minutes,
            help="Retention window for service-managed cleanup.",
        )
        command_parser.add_argument(
            "--reconnect-seconds",
            type=int,
            default=KrakenConfig.reconnect_seconds,
            help="Reconnect delay after websocket failure.",
        )
        command_parser.add_argument("--trace-ws", action="store_true", help="Print websocket payload traces.")
        command_parser.add_argument(
            "--trace-ws-format",
            choices=("compact", "json"),
            default=KrakenConfig.trace_ws_format,
            help="Trace output format.",
        )
        command_parser.add_argument(
            "--trace-ws-max-lines",
            type=int,
            default=KrakenConfig.trace_ws_max_lines,
            help="Maximum number of websocket trace lines to emit.",
        )
    probe_parser = subparsers.choices["probe"]
    probe_parser.add_argument("--seconds", type=float, default=10.0, help="Probe duration before auto-stop.")
    return parser


def config_from_args(args: argparse.Namespace) -> KrakenConfig:
    """Convertit les arguments CLI en configuration immuable."""
    env_cfg = kraken_futures_environment(args.environment)
    return KrakenConfig(
        pair=args.pair,
        depth=args.depth,
        ws_url=args.ws_url or env_cfg.public_ws_url,
        rest_url=args.rest_url or env_cfg.rest_url,
        db_url=args.db_url or env_cfg.public_db_url,
        private_db_url=args.private_db_url or env_cfg.private_db_url,
        exchange=args.exchange,
        environment=args.environment,
        market_type=args.market_type,
        log_level=args.log_level,
        snapshot_interval_seconds=args.snapshot_interval_seconds,
        indicator_interval_seconds=args.indicator_interval_seconds,
        ticker_interval_seconds=args.ticker_interval_seconds,
        log_interval_seconds=args.log_interval_seconds,
        retention_minutes=args.retention_minutes,
        reconnect_seconds=args.reconnect_seconds,
        trace_ws=args.trace_ws,
        trace_ws_format=args.trace_ws_format,
        trace_ws_max_lines=args.trace_ws_max_lines,
    )


def print_status(tree: KrakenTree, pair: str) -> None:
    """Imprime le dernier statut DB en JSON pour scripts shell."""
    print(json.dumps(tree.latest_status(pair), sort_keys=True))


async def run_service(tree: KrakenTree, stop_after_seconds: float | None = None) -> None:
    """Lance le service avec gestion simple de SIGINT/SIGTERM."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def _request_stop() -> None:
        tree.stop()
        if task is not None:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    stopper: asyncio.Task[None] | None = None
    if stop_after_seconds is not None:
        stopper = asyncio.create_task(stop_tree_after(tree, stop_after_seconds))
    try:
        await tree.run()
    except asyncio.CancelledError:
        if tree._running:
            raise
    finally:
        if stopper is not None:
            stopper.cancel()
            with suppress(asyncio.CancelledError):
                await stopper


async def stop_tree_after(tree: KrakenTree, delay_seconds: float) -> None:
    """Arrete le service apres un delai borne pour un probe ou un smoke test."""
    await asyncio.sleep(max(delay_seconds, 0.0))
    tree.stop()


def main(argv: Sequence[str] | None = None) -> int:
    """Point d'entree CLI de KrakenTree."""
    parser = build_parser()
    args = parser.parse_args(argv)
    tree = KrakenTree(config_from_args(args))
    if args.command == "status":
        print_status(tree, args.pair)
        return 0
    if args.command == "probe":
        try:
            asyncio.run(run_service(tree, stop_after_seconds=args.seconds))
        except KeyboardInterrupt:
            tree.stop()
        print_status(tree, args.pair)
        return 0
    try:
        asyncio.run(run_service(tree))
    except KeyboardInterrupt:
        tree.stop()
        print("public market stream stopped by operator")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
