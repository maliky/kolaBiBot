from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship

Base = declarative_base()


class OrderRun(Base):
    __tablename__ = "order_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    exchange: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    strategy: Mapped[dict] = mapped_column(JSON, default=dict)

    events: Mapped[list["OrderEvent"]] = relationship(
        "OrderEvent", back_populates="run", cascade="all, delete-orphan"
    )


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("order_runs.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    run: Mapped["OrderRun"] = relationship("OrderRun", back_populates="events")


class ExchangeConnection(Base):
    """Etat normalise d'un flux public, prive, ou REST."""

    __tablename__ = "exchange_connections"
    __table_args__ = (
        Index(
            "ix_exchange_connections_identity",
            "exchange",
            "environment",
            "market_type",
            "stream_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    stream_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class MarketSnapshot(Base):
    """Snapshot public compact commun aux plateformes."""

    __tablename__ = "market_snapshots"
    __table_args__ = (
        Index(
            "ix_market_snapshots_lookup",
            "exchange",
            "market_type",
            "symbol",
            "local_timestamp",
        ),
        Index("ix_market_snapshots_uuid", "local_uuid", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    best_bid: Mapped[float] = mapped_column(Float, nullable=False)
    best_ask: Mapped[float] = mapped_column(Float, nullable=False)
    avg_bid: Mapped[float] = mapped_column(Float, nullable=False)
    avg_ask: Mapped[float] = mapped_column(Float, nullable=False)
    mid_price: Mapped[float] = mapped_column(Float, nullable=False)
    spread: Mapped[float] = mapped_column(Float, nullable=False)
    imbalance: Mapped[float] = mapped_column(Float, nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    levels: Mapped[list["MarketLevel"]] = relationship(
        "MarketLevel",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )
    indicators: Mapped[list["MarketIndicator"]] = relationship(
        "MarketIndicator",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class MarketLevel(Base):
    """Niveau L2 sparse lie a un snapshot normalise."""

    __tablename__ = "market_levels"
    __table_args__ = (
        Index("ix_market_levels_snapshot_side", "snapshot_id", "side", "level_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(String(3), nullable=False)
    level_index: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)

    snapshot: Mapped["MarketSnapshot"] = relationship(
        "MarketSnapshot",
        back_populates="levels",
    )


class MarketIndicator(Base):
    """Indicateur public compact consomme par les strategies."""

    __tablename__ = "market_indicators"
    __table_args__ = (
        Index(
            "ix_market_indicators_lookup",
            "exchange",
            "market_type",
            "symbol",
            "indicator_name",
            "computed_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="SET NULL")
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    indicator_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    source_age_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    snapshot: Mapped["MarketSnapshot | None"] = relationship(
        "MarketSnapshot",
        back_populates="indicators",
    )


class ExchangeInstrument(Base):
    """Cached exchange instrument metadata for fast local consultation."""

    __tablename__ = "exchange_instruments"
    __table_args__ = (
        Index(
            "ix_exchange_instruments_identity",
            "exchange",
            "environment",
            "market_type",
            "symbol",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_type: Mapped[str | None] = mapped_column(String(64))
    tradeable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tick_size: Mapped[float | None] = mapped_column(Float)
    contract_size: Mapped[float | None] = mapped_column(Float)
    min_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ExchangeOrder(Base):
    """Ordre prive normalise provenant du websocket ou REST."""

    __tablename__ = "exchange_orders"
    __table_args__ = (
        Index("ix_exchange_orders_local_uuid", "local_uuid", unique=True),
        Index("ix_exchange_orders_exchange_id", "exchange", "exchange_order_id"),
        Index("ix_exchange_orders_client_id", "exchange", "client_order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128))
    client_order_id: Mapped[str | None] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    filled_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    fills: Mapped[list["ExchangeFill"]] = relationship(
        "ExchangeFill",
        back_populates="order",
        cascade="all, delete-orphan",
    )


class ExchangeFill(Base):
    """Execution privee normalisee."""

    __tablename__ = "exchange_fills"
    __table_args__ = (
        Index("ix_exchange_fills_local_uuid", "local_uuid", unique=True),
        Index("ix_exchange_fills_exchange_id", "exchange", "exchange_fill_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_orders.id", ondelete="CASCADE"), nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange_fill_id: Mapped[str | None] = mapped_column(String(128))
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float | None] = mapped_column(Float)
    fee_currency: Mapped[str | None] = mapped_column(String(16))
    liquidity_role: Mapped[str | None] = mapped_column(String(16))
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    order: Mapped["ExchangeOrder"] = relationship(
        "ExchangeOrder",
        back_populates="fills",
    )


class AccountBalance(Base):
    """Solde prive normalise."""

    __tablename__ = "account_balances"
    __table_args__ = (
        Index("ix_account_balances_lookup", "exchange", "account_scope", "asset"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    available: Mapped[float] = mapped_column(Float, nullable=False)
    locked: Mapped[float] = mapped_column(Float, nullable=False)
    total: Mapped[float] = mapped_column(Float, nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AccountPosition(Base):
    """Position privee normalisee, spot ou futures."""

    __tablename__ = "account_positions"
    __table_args__ = (
        Index(
            "ix_account_positions_lookup",
            "exchange",
            "market_type",
            "account_scope",
            "symbol",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Float)
    leverage: Mapped[float | None] = mapped_column(Float)
    liquidation_price: Mapped[float | None] = mapped_column(Float)
    available_margin: Mapped[float | None] = mapped_column(Float)
    maintenance_margin: Mapped[float | None] = mapped_column(Float)
    maintenance_margin_buffer: Mapped[float | None] = mapped_column(Float)
    funding_rate: Mapped[float | None] = mapped_column(Float)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class RawExchangeEvent(Base):
    """Payload brut optionnel, desactive par defaut cote services."""

    __tablename__ = "raw_exchange_events"
    __table_args__ = (
        Index(
            "ix_raw_exchange_events_retention", "exchange", "stream_kind", "created_at"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    stream_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
