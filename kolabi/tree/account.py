from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence, Iterable, cast
from urllib.parse import urlencode
from uuid import uuid4

import requests
import websockets
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from kolabi.shared.kraken_futures import kraken_futures_environment
from kolabi.shared.logging import setup_logging
from kolabi.shared.persistence import (
    AccountBalance,
    AccountPosition,
    Base,
    ExchangeConnection,
    ExchangeFill,
    PrivateIngestAudit,
    ExchangeOrder,
    RawExchangeEvent,
)
from kolabi.tree.kraken import build_engine

JsonMapT = Mapping[str, Any]
JsonDictT = dict[str, Any]


@dataclass(frozen=True)
class AccountStreamConfig:
    """Configuration de la memoire privee ordre/compte."""

    db_url: str = "sqlite:///prv-futures-demo.sqlite"
    exchange: str = "kraken"
    environment: str = "demo"
    market_type: str = "futures"
    account_scope: str = "default"
    ws_url: str = "wss://demo-futures.kraken.com/ws/v1"
    rest_url: str = "https://demo-futures.kraken.com/derivatives/api/v3"
    api_key_env: str = "KRAKEN_FUTURE_DEMO_API_KEY"
    api_secret_env: str = "KRAKEN_FUTURE_DEMO_API_SECRET"
    feeds: tuple[str, ...] = (
        "open_orders",
        "fills",
        "balances",
        "open_positions",
        "account_log",
        "notifications_auth",
    )
    critical_feeds: tuple[str, ...] = ("open_orders", "fills")
    account_feeds: tuple[str, ...] = (
        "balances",
        "open_positions",
        "account_log",
        "notifications_auth",
    )
    ingest_profile: str = "split"
    reconnect_seconds: int = 5
    ping_seconds: int = 50
    heartbeat_log_seconds: int = 60
    maintenance_seconds: int = 60
    account_batch_max_rows: int = 64
    account_batch_max_wait_seconds: float = 0.35
    queue_maxsize: int = 5000
    raw_retention_minutes: int = 1440
    raw_retention_limit: int = 100000
    log_level: str = "INFO"


@dataclass(frozen=True)
class KrakenFuturesCredentials:
    """API key/secret lus depuis l'environnement, jamais depuis le code."""

    api_key: str
    api_secret: str


@dataclass(frozen=True)
class OrderWrite:
    """Evenement ordre normalise, pret a etre persiste."""

    symbol: str
    side: str
    order_type: str
    status: str
    quantity: float
    exchange_order_id: str | None = None
    client_order_id: str | None = None
    price: float | None = None
    filled_quantity: float = 0.0
    reduce_only: bool = False
    raw_payload: dict[str, Any] | None = None
    source_timestamp: datetime | None = None


@dataclass(frozen=True)
class FillWrite:
    """Execution normalisee liee a un ordre local."""

    order_id: int
    price: float
    quantity: float
    exchange_fill_id: str | None = None
    fee: float | None = None
    fee_currency: str | None = None
    liquidity_role: str | None = None
    raw_payload: dict[str, Any] | None = None
    source_timestamp: datetime | None = None


@dataclass(frozen=True)
class BalanceWrite:
    """Solde normalise d'un actif."""

    asset: str
    available: float
    locked: float
    total: float
    raw_payload: dict[str, Any] | None = None
    source_timestamp: datetime | None = None


@dataclass(frozen=True)
class PositionWrite:
    """Position normalisee, futures ou spot."""

    symbol: str
    side: str
    size: float
    entry_price: float | None = None
    leverage: float | None = None
    liquidation_price: float | None = None
    available_margin: float | None = None
    maintenance_margin: float | None = None
    maintenance_margin_buffer: float | None = None
    funding_rate: float | None = None
    raw_payload: dict[str, Any] | None = None
    source_timestamp: datetime | None = None


@dataclass(frozen=True)
class FillEvent:
    """Execution recue d'un flux avant resolution de l'ordre local."""

    exchange_order_id: str | None
    client_order_id: str | None
    symbol: str
    side: str
    order_type: str
    price: float
    quantity: float
    exchange_fill_id: str | None = None
    fee: float | None = None
    fee_currency: str | None = None
    liquidity_role: str | None = None
    raw_payload: dict[str, Any] | None = None
    source_timestamp: datetime | None = None


@dataclass(frozen=True)
class PrivateStreamProfile:
    """Defines one private websocket ingestion lane."""

    name: str
    stream_kind: str
    feeds: tuple[str, ...]
    is_critical: bool
    health_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngestMessage:
    """Queue payload carrying a decoded private websocket message."""

    payload: JsonDictT
    received_at: datetime


def upgrade_private_schema(engine: Any) -> None:
    """Apply additive SQLite schema upgrades for existing private DBs."""
    if getattr(engine.dialect, "name", "") != "sqlite":
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "exchange_orders" in tables:
            ensure_columns(connection, "exchange_orders", {"raw_payload": "JSON"})
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_exchange_orders_symbol_cursor "
                    "ON exchange_orders "
                    "(exchange, environment, market_type, symbol, local_timestamp, id)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_exchange_orders_env_client "
                    "ON exchange_orders "
                    "(exchange, environment, market_type, client_order_id)"
                )
            )
        if "exchange_fills" in tables:
            ensure_columns(connection, "exchange_fills", {"raw_payload": "JSON"})
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_exchange_fills_cursor "
                    "ON exchange_fills (exchange, local_timestamp, id, order_id)"
                )
            )
        if "account_balances" in tables:
            ensure_columns(connection, "account_balances", {"raw_payload": "JSON"})
        if "account_positions" in tables:
            ensure_columns(connection, "account_positions", {"raw_payload": "JSON"})
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


class AccountStateStore:
    """Writer DB pour les evenements prives normalises.

    Cette classe est le bord persistence. Le websocket prive et le reconcileur
    REST l'utilisent tous les deux pour produire le meme schema normalise.
    """

    def __init__(self, config: AccountStreamConfig) -> None:
        self.config = config
        self.engine = build_engine(config.db_url)
        upgrade_private_schema(self.engine)
        Base.metadata.create_all(self.engine)
        self.sessionmaker = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=Session,
        )

    def ingest_message(
        self,
        message: JsonMapT,
        *,
        stream_kind: str,
        is_critical: bool,
        received_at: datetime | None = None,
        prune_raw: bool = False,
    ) -> dict[str, object]:
        """Persist one private message with a single short transaction."""
        payload = dict(message)
        feed = str(payload.get("feed", ""))
        now = received_at or datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            raw_row = self._record_raw_event_in_session(
                session,
                payload,
                stream_kind=stream_kind,
                received_at=now,
                prune_raw=prune_raw,
            )
            row_count = 0
            order_rows: list[ExchangeOrder] = []
            fill_rows: list[tuple[FillEvent, ExchangeFill]] = []
            balance_rows: list[BalanceWrite] = []
            position_rows: list[PositionWrite] = []
            if feed.startswith("open_orders"):
                for order_payload in iter_order_payloads(payload):
                    mapped = map_order(order_payload)
                    row = self._ensure_order_in_session(
                        session,
                        mapped,
                        now=now,
                    )
                    order_rows.append(row)
                    row_count += 1
            elif feed.startswith("fills"):
                for fill_payload in iter_fill_payloads(payload):
                    mapped_fill = map_fill_event(fill_payload)
                    fill_row = self._record_fill_event_in_session(
                        session,
                        mapped_fill,
                        now=now,
                    )
                    fill_rows.append((mapped_fill, fill_row))
                    row_count += 1
            elif feed.startswith("balances"):
                balances = map_balances(payload)
                for balance in balances:
                    self._record_balance_in_session(session, balance, now=now)
                balance_rows = balances
                row_count += len(balances)
            elif feed.startswith("open_positions"):
                positions = map_positions(payload)
                for position in positions:
                    self._record_position_in_session(session, position, now=now)
                position_rows = positions
                row_count += len(positions)
            committed_at = datetime.now(timezone.utc)
            self._record_ingest_audit_in_session(
                session,
                message=payload,
                feed=feed or "unknown",
                stream_kind=stream_kind,
                is_critical=is_critical,
                source_timestamp=raw_row.source_timestamp,
                received_at=now,
                raw_committed_at=committed_at,
                normalized_committed_at=committed_at,
                row_count=row_count,
                error_text=None,
            )
            session.commit()
        return {
            "feed": feed,
            "raw_event": raw_row,
            "orders": tuple(order_rows),
            "fills": tuple(fill_rows),
            "balances": tuple(balance_rows),
            "positions": tuple(position_rows),
            "row_count": row_count,
        }

    def ingest_message_batch(
        self,
        messages: Iterable[IngestMessage],
        *,
        stream_kind: str,
        is_critical: bool,
        prune_raw: bool = False,
    ) -> tuple[dict[str, object], ...]:
        """Persist many messages in one transaction (used for non-critical feeds)."""
        results: list[dict[str, object]] = []
        with self.sessionmaker() as session:
            for queued in messages:
                payload = dict(queued.payload)
                feed = str(payload.get("feed", ""))
                raw_row = self._record_raw_event_in_session(
                    session,
                    payload,
                    stream_kind=stream_kind,
                    received_at=queued.received_at,
                    prune_raw=False,
                )
                row_count = 0
                order_rows: list[ExchangeOrder] = []
                fill_rows: list[tuple[FillEvent, ExchangeFill]] = []
                balance_rows: list[BalanceWrite] = []
                position_rows: list[PositionWrite] = []
                if feed.startswith("open_orders"):
                    for order_payload in iter_order_payloads(payload):
                        mapped = map_order(order_payload)
                        order_rows.append(
                            self._ensure_order_in_session(
                                session,
                                mapped,
                                now=queued.received_at,
                            )
                        )
                        row_count += 1
                elif feed.startswith("fills"):
                    for fill_payload in iter_fill_payloads(payload):
                        mapped_fill = map_fill_event(fill_payload)
                        fill_rows.append(
                            (
                                mapped_fill,
                                self._record_fill_event_in_session(
                                    session,
                                    mapped_fill,
                                    now=queued.received_at,
                                ),
                            )
                        )
                        row_count += 1
                elif feed.startswith("balances"):
                    balances = map_balances(payload)
                    for balance in balances:
                        self._record_balance_in_session(
                            session,
                            balance,
                            now=queued.received_at,
                        )
                    balance_rows = balances
                    row_count += len(balances)
                elif feed.startswith("open_positions"):
                    positions = map_positions(payload)
                    for position in positions:
                        self._record_position_in_session(
                            session,
                            position,
                            now=queued.received_at,
                        )
                    position_rows = positions
                    row_count += len(positions)
                committed_at = datetime.now(timezone.utc)
                self._record_ingest_audit_in_session(
                    session,
                    message=payload,
                    feed=feed or "unknown",
                    stream_kind=stream_kind,
                    is_critical=is_critical,
                    source_timestamp=raw_row.source_timestamp,
                    received_at=queued.received_at,
                    raw_committed_at=committed_at,
                    normalized_committed_at=committed_at,
                    row_count=row_count,
                    error_text=None,
                )
                results.append(
                    {
                        "feed": feed,
                        "raw_event": raw_row,
                        "orders": tuple(order_rows),
                        "fills": tuple(fill_rows),
                        "balances": tuple(balance_rows),
                        "positions": tuple(position_rows),
                        "row_count": row_count,
                    }
                )
            if prune_raw:
                prune_raw_events(
                    session,
                    config=self.config,
                    retention_minutes=self.config.raw_retention_minutes,
                    retention_limit=self.config.raw_retention_limit,
                    now=datetime.now(timezone.utc),
                    stream_kind=stream_kind,
                )
            session.commit()
        return tuple(results)

    def prune_raw_events_now(self, *, stream_kind: str) -> None:
        """Apply raw retention outside the critical ingest path."""
        now = datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            prune_raw_events(
                session,
                config=self.config,
                retention_minutes=self.config.raw_retention_minutes,
                retention_limit=self.config.raw_retention_limit,
                now=now,
                stream_kind=stream_kind,
            )
            session.commit()

    def _find_order_by_client_id_in_session(
        self,
        session: Session,
        client_order_id: str | None,
    ) -> ExchangeOrder | None:
        if not client_order_id:
            return None
        stmt = (
            select(ExchangeOrder)
            .where(
                ExchangeOrder.exchange == self.config.exchange,
                ExchangeOrder.environment == self.config.environment,
                ExchangeOrder.market_type == self.config.market_type,
                ExchangeOrder.client_order_id == client_order_id,
            )
            .order_by(ExchangeOrder.local_timestamp.desc(), ExchangeOrder.id.desc())
        )
        return session.execute(stmt).scalars().first()

    def _ensure_order_in_session(
        self,
        session: Session,
        order: OrderWrite,
        *,
        now: datetime,
    ) -> ExchangeOrder:
        row = find_order(session, self.config, order.exchange_order_id)
        if row is None and order.client_order_id is not None:
            row = self._find_order_by_client_id_in_session(session, order.client_order_id)
        if row is None:
            row = ExchangeOrder(
                local_uuid=str(uuid4()),
                exchange=self.config.exchange,
                environment=self.config.environment,
                market_type=self.config.market_type,
                account_scope=self.config.account_scope,
                symbol=order.symbol,
                exchange_order_id=order.exchange_order_id,
                client_order_id=order.client_order_id,
                side=order.side,
                order_type=order.order_type,
                status=order.status,
                price=order.price,
                quantity=order.quantity,
                filled_quantity=order.filled_quantity,
                reduce_only=order.reduce_only,
                raw_payload=order.raw_payload or {},
                source_timestamp=order.source_timestamp,
                local_timestamp=now,
            )
            session.add(row)
            return row
        row.status = _prefer_known(order.status, row.status)
        row.side = _prefer_known(order.side, row.side)
        row.order_type = _prefer_known(order.order_type, row.order_type)
        row.symbol = _prefer_known(order.symbol, row.symbol)
        row.client_order_id = order.client_order_id or row.client_order_id
        row.exchange_order_id = order.exchange_order_id or row.exchange_order_id
        row.filled_quantity = max(row.filled_quantity, order.filled_quantity)
        row.quantity = max(row.quantity, order.quantity)
        row.price = order.price if order.price is not None else row.price
        row.source_timestamp = order.source_timestamp or row.source_timestamp
        row.raw_payload = _merge_raw_payload(row.raw_payload, order.raw_payload)
        row.local_timestamp = now
        return row

    def _record_fill_in_session(
        self,
        session: Session,
        fill: FillWrite,
        *,
        now: datetime,
    ) -> ExchangeFill:
        existing = find_fill(session, self.config, fill.exchange_fill_id)
        if existing is not None:
            return existing
        row = ExchangeFill(
            local_uuid=str(uuid4()),
            order_id=fill.order_id,
            exchange=self.config.exchange,
            exchange_fill_id=fill.exchange_fill_id,
            price=fill.price,
            quantity=fill.quantity,
            fee=fill.fee,
            fee_currency=fill.fee_currency,
            liquidity_role=fill.liquidity_role,
            raw_payload=fill.raw_payload or {},
            source_timestamp=fill.source_timestamp,
            local_timestamp=now,
        )
        session.add(row)
        return row

    def _record_fill_event_in_session(
        self,
        session: Session,
        event: FillEvent,
        *,
        now: datetime,
    ) -> ExchangeFill:
        order = self._ensure_order_in_session(
            session,
            OrderWrite(
                symbol=event.symbol,
                side=event.side,
                order_type=event.order_type,
                status="filled",
                quantity=event.quantity,
                exchange_order_id=event.exchange_order_id,
                client_order_id=event.client_order_id,
                filled_quantity=event.quantity,
                raw_payload=event.raw_payload,
                source_timestamp=event.source_timestamp,
            ),
            now=now,
        )
        session.flush()
        return self._record_fill_in_session(
            session,
            FillWrite(
                order_id=order.id,
                exchange_fill_id=event.exchange_fill_id,
                price=event.price,
                quantity=event.quantity,
                fee=event.fee,
                fee_currency=event.fee_currency,
                liquidity_role=event.liquidity_role,
                raw_payload=event.raw_payload,
                source_timestamp=event.source_timestamp,
            ),
            now=now,
        )

    def _record_balance_in_session(
        self,
        session: Session,
        balance: BalanceWrite,
        *,
        now: datetime,
    ) -> AccountBalance:
        row = AccountBalance(
            exchange=self.config.exchange,
            environment=self.config.environment,
            account_scope=self.config.account_scope,
            asset=balance.asset,
            available=balance.available,
            locked=balance.locked,
            total=balance.total,
            raw_payload=balance.raw_payload or {},
            source_timestamp=balance.source_timestamp,
            local_timestamp=now,
        )
        session.add(row)
        return row

    def _record_position_in_session(
        self,
        session: Session,
        position: PositionWrite,
        *,
        now: datetime,
    ) -> AccountPosition:
        row = AccountPosition(
            exchange=self.config.exchange,
            environment=self.config.environment,
            market_type=self.config.market_type,
            account_scope=self.config.account_scope,
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            leverage=position.leverage,
            liquidation_price=position.liquidation_price,
            available_margin=position.available_margin,
            maintenance_margin=position.maintenance_margin,
            maintenance_margin_buffer=position.maintenance_margin_buffer,
            funding_rate=position.funding_rate,
            raw_payload=position.raw_payload or {},
            source_timestamp=position.source_timestamp,
            local_timestamp=now,
        )
        session.add(row)
        return row

    def _record_raw_event_in_session(
        self,
        session: Session,
        message: JsonMapT,
        *,
        stream_kind: str,
        received_at: datetime,
        prune_raw: bool,
    ) -> RawExchangeEvent:
        event_type = str(message.get("feed") or message.get("event") or "unknown")
        payload = dict(message)
        source_timestamp = raw_event_source_timestamp(payload)
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
            previous.last_seen_at = received_at
            if prune_raw:
                prune_raw_events(
                    session,
                    config=self.config,
                    retention_minutes=self.config.raw_retention_minutes,
                    retention_limit=self.config.raw_retention_limit,
                    now=received_at,
                    stream_kind=stream_kind,
                )
            return previous
        row = RawExchangeEvent(
            exchange=self.config.exchange,
            environment=self.config.environment,
            market_type=self.config.market_type,
            account_scope=self.config.account_scope,
            symbol=raw_event_symbol(payload),
            stream_kind=stream_kind,
            event_type=event_type,
            correlation_id=raw_event_correlation_id(payload),
            exchange_sequence=optional_str(payload.get("seq")),
            payload=payload,
            source_timestamp=source_timestamp,
            duplicate_count=0,
            last_seen_at=received_at,
            received_at=received_at,
            created_at=received_at,
        )
        session.add(row)
        if prune_raw:
            prune_raw_events(
                session,
                config=self.config,
                retention_minutes=self.config.raw_retention_minutes,
                retention_limit=self.config.raw_retention_limit,
                now=received_at,
                stream_kind=stream_kind,
            )
        return row

    def _record_ingest_audit_in_session(
        self,
        session: Session,
        *,
        message: JsonMapT,
        feed: str,
        stream_kind: str,
        is_critical: bool,
        source_timestamp: datetime | None,
        received_at: datetime,
        raw_committed_at: datetime,
        normalized_committed_at: datetime,
        row_count: int,
        error_text: str | None,
    ) -> None:
        session.add(
            PrivateIngestAudit(
                exchange=self.config.exchange,
                environment=self.config.environment,
                market_type=self.config.market_type,
                account_scope=self.config.account_scope,
                stream_kind=stream_kind,
                feed=feed,
                is_critical=is_critical,
                symbol=raw_event_symbol(message),
                event_type=str(message.get("event") or message.get("feed") or "unknown"),
                correlation_id=raw_event_correlation_id(message),
                client_order_id=optional_str(
                    first_present(message, "cli_ord_id", "cliOrdId")
                ),
                exchange_order_id=optional_str(
                    first_present(message, "order_id", "orderId")
                ),
                source_timestamp=source_timestamp,
                received_at=received_at,
                raw_committed_at=raw_committed_at,
                normalized_committed_at=normalized_committed_at,
                row_count=row_count,
                error_text=error_text,
            )
        )

    def record_connection_status(
        self,
        stream_kind: str,
        status: str,
        now: datetime | None = None,
        last_error: str | None = None,
    ) -> ExchangeConnection:
        """Cree ou met a jour le statut d'un flux prive/public."""
        current_time = now or datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            connection = latest_connection(session, self.config, stream_kind)
            if connection is None:
                connection = ExchangeConnection(
                    exchange=self.config.exchange,
                    environment=self.config.environment,
                    market_type=self.config.market_type,
                    stream_kind=stream_kind,
                    status=status,
                    last_heartbeat_at=current_time,
                    last_error=last_error,
                    updated_at=current_time,
                )
                session.add(connection)
            else:
                connection.status = status
                connection.last_heartbeat_at = current_time
                connection.last_error = last_error
                connection.updated_at = current_time
            session.commit()
            session.refresh(connection)
            return connection

    def record_order(self, order: OrderWrite) -> ExchangeOrder:
        """Persiste une ligne d'ordre normalise."""
        with self.sessionmaker() as session:
            row = ExchangeOrder(
                local_uuid=str(uuid4()),
                exchange=self.config.exchange,
                environment=self.config.environment,
                market_type=self.config.market_type,
                account_scope=self.config.account_scope,
                symbol=order.symbol,
                exchange_order_id=order.exchange_order_id,
                client_order_id=order.client_order_id,
                side=order.side,
                order_type=order.order_type,
                status=order.status,
                price=order.price,
                quantity=order.quantity,
                filled_quantity=order.filled_quantity,
                reduce_only=order.reduce_only,
                raw_payload=order.raw_payload or {},
                source_timestamp=order.source_timestamp,
                local_timestamp=datetime.now(timezone.utc),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def ensure_order(self, order: OrderWrite) -> ExchangeOrder:
        """Retourne l'ordre existant ou cree une ligne placeholder."""
        now = datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            row = self._ensure_order_in_session(session, order, now=now)
            session.commit()
            return row

    def record_fill(self, fill: FillWrite) -> ExchangeFill:
        """Persiste une execution normalisee."""
        now = datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            row = self._record_fill_in_session(session, fill, now=now)
            session.commit()
            return row

    def record_fill_event(self, event: FillEvent) -> ExchangeFill:
        """Persiste un fill en creant l'ordre local si necessaire."""
        now = datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            row = self._record_fill_event_in_session(session, event, now=now)
            session.commit()
            return row

    def record_balance(self, balance: BalanceWrite) -> AccountBalance:
        """Persiste un solde normalise."""
        now = datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            row = self._record_balance_in_session(session, balance, now=now)
            session.commit()
            return row

    def latest_balance_by_asset(self) -> dict[str, tuple[float, float, float]]:
        """Retourne le dernier solde connu par actif depuis la DB."""
        with self.sessionmaker() as session:
            rows = session.execute(
                select(AccountBalance)
                .where(
                    AccountBalance.exchange == self.config.exchange,
                    AccountBalance.environment == self.config.environment,
                    AccountBalance.account_scope == self.config.account_scope,
                )
                .order_by(AccountBalance.id.desc())
            ).scalars()
            latest: dict[str, tuple[float, float, float]] = {}
            for row in rows:
                if row.asset in latest:
                    continue
                latest[row.asset] = (row.available, row.locked, row.total)
            return latest

    def record_position(self, position: PositionWrite) -> AccountPosition:
        """Persiste une position normalisee."""
        now = datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            row = self._record_position_in_session(session, position, now=now)
            session.commit()
            return row

    def record_raw_event(
        self,
        message: JsonMapT,
        stream_kind: str = "private_ws",
        *,
        received_at: datetime | None = None,
        prune_raw: bool = True,
    ) -> RawExchangeEvent:
        """Persist the exchange-native event before any normalized mapping."""
        now = received_at or datetime.now(timezone.utc)
        with self.sessionmaker() as session:
            row = self._record_raw_event_in_session(
                session,
                message,
                stream_kind=stream_kind,
                received_at=now,
                prune_raw=prune_raw,
            )
            session.commit()
            return row

    def latest_status(self, stream_kind: str = "private_ws") -> dict[str, object]:
        """Retourne le dernier statut de connexion privee et les compteurs."""
        with self.sessionmaker() as session:
            connection = latest_connection(session, self.config, stream_kind)
            status: dict[str, object] = (
                {
                    "exchange": self.config.exchange,
                    "market_type": self.config.market_type,
                    "status": "empty",
                    "stream_kind": stream_kind,
                }
                if connection is None
                else connection_to_status(connection)
            )
            status.update(count_private_rows(session))
            return status


class KrakenFuturesPrivateStream:
    """Client websocket prive Kraken Futures.

    Il s'abonne aux feeds prives, mappe les messages vers les dataclasses
    normalisees, puis laisse AccountStateStore gerer la persistence.
    """

    def __init__(
        self,
        config: AccountStreamConfig,
        store: AccountStateStore,
        credentials: KrakenFuturesCredentials,
        profile: PrivateStreamProfile | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.credentials = credentials
        self.logger = setup_logging(config.log_level)
        self.profile = profile or PrivateStreamProfile(
            name="default",
            stream_kind="private_ws",
            feeds=config.feeds,
            is_critical=True,
            health_aliases=(),
        )
        self._running = True
        self._maintenance_task: asyncio.Task[None] | None = None
        self._ingest_task: asyncio.Task[None] | None = None
        self._last_balances: dict[str, tuple[float, float, float]] = (
            self.store.latest_balance_by_asset()
        )
        self._last_positions: dict[
            tuple[str, str],
            tuple[float, float | None, float | None, float | None],
        ] = {}

    async def run(self) -> None:
        """Tourne en continu avec reconnexion conservative."""
        self.logger.info(
            "kraken_account starting env=%s db=%s ws=%s rest=%s profile=%s feeds=%s",
            self.config.environment,
            self.config.db_url,
            self.config.ws_url,
            self.config.rest_url,
            self.profile.name,
            ",".join(self.profile.feeds),
        )
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_shutdown_error(exc):
                    self._running = False
                    self._set_status("stopped", last_error=str(exc))
                    self.logger.info(
                        "kraken_account stopping private stream during shutdown: %s",
                        exc,
                    )
                    break
                self._set_status("reconnecting", last_error=str(exc))
                self.logger.warning(
                    "kraken_account reconnecting in %ss after error: %s profile=%s",
                    self.config.reconnect_seconds,
                    exc,
                    self.profile.name,
                )
                await asyncio.sleep(self.config.reconnect_seconds)

    @staticmethod
    def _is_shutdown_error(exc: Exception) -> bool:
        """Detecte les erreurs de shutdown qui ne doivent pas boucler en reconnect."""
        message = str(exc).lower()
        return (
            "cannot schedule new futures after shutdown" in message
            or "can't register atexit after shutdown" in message
            or "cannot register atexit after shutdown" in message
            or "interpreter shutdown" in message
        )

    async def run_once(self) -> None:
        """Ouvre une session privee, challenge, subscribe, puis consomme."""
        async with websockets.connect(self.config.ws_url) as ws:
            self._set_status("connecting")
            challenge = await request_challenge(ws, self.credentials.api_key)
            signed = sign_challenge(challenge, self.credentials.api_secret)
            for message in subscribe_messages(
                feeds=self.profile.feeds,
                api_key=self.credentials.api_key,
                challenge=challenge,
                signed_challenge=signed,
            ):
                await ws.send(json.dumps(message))
            self._set_status("subscribed")
            self.logger.info(
                "kraken_account subscribed env=%s feeds=%s ws=%s profile=%s",
                self.config.environment,
                ",".join(self.profile.feeds),
                self.config.ws_url,
                self.profile.name,
            )
            queue: asyncio.Queue[IngestMessage] = asyncio.Queue(
                maxsize=max(self.config.queue_maxsize, 1)
            )
            self._ingest_task = asyncio.create_task(self._drain_ingest_queue(queue))
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())
            last_ping = time.monotonic()
            last_heartbeat_log = time.monotonic()
            try:
                while self._running:
                    if time.monotonic() - last_ping >= self.config.ping_seconds:
                        await ws.ping()
                        last_ping = time.monotonic()
                    if (
                        time.monotonic() - last_heartbeat_log
                        >= self.config.heartbeat_log_seconds
                    ):
                        self.logger.info(
                            "kraken_account heartbeat env=%s db=%s stream=%s",
                            self.config.environment,
                            self.config.db_url,
                            self.profile.stream_kind,
                        )
                        last_heartbeat_log = time.monotonic()
                    try:
                        raw_message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    payload = json.loads(raw_message)
                    event = payload.get("event")
                    if event == "heartbeat":
                        self._set_status("healthy")
                        continue
                    await queue.put(
                        IngestMessage(
                            payload=payload,
                            received_at=datetime.now(timezone.utc),
                        )
                    )
            finally:
                await self._cancel_worker_tasks()

    def stop(self) -> None:
        """Demande l'arret apres le message courant."""
        self._running = False

    def handle_message(self, message: JsonMapT) -> None:
        """Compatibility hook used by tests and single-message tooling."""
        payload = dict(message)
        if payload.get("event") == "heartbeat":
            self._set_status("healthy")
            return
        self._process_message(
            IngestMessage(
                payload=payload,
                received_at=datetime.now(timezone.utc),
            )
        )

    async def _cancel_worker_tasks(self) -> None:
        tasks = [task for task in (self._ingest_task, self._maintenance_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ingest_task = None
        self._maintenance_task = None

    async def _maintenance_loop(self) -> None:
        interval = max(1, self.config.maintenance_seconds)
        while self._running:
            await asyncio.sleep(interval)
            self.store.prune_raw_events_now(stream_kind=self.profile.stream_kind)

    async def _drain_ingest_queue(self, queue: asyncio.Queue[IngestMessage]) -> None:
        if self.profile.is_critical:
            await self._drain_critical_queue(queue)
            return
        await self._drain_account_queue(queue)

    async def _drain_critical_queue(self, queue: asyncio.Queue[IngestMessage]) -> None:
        while self._running:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            self._process_message(item)

    async def _drain_account_queue(self, queue: asyncio.Queue[IngestMessage]) -> None:
        pending: deque[IngestMessage] = deque()
        batch_wait = max(0.05, self.config.account_batch_max_wait_seconds)
        batch_limit = max(1, self.config.account_batch_max_rows)
        while self._running:
            timeout = batch_wait if pending else 1.0
            timed_out = False
            try:
                pending.append(await asyncio.wait_for(queue.get(), timeout=timeout))
            except TimeoutError:
                timed_out = True
            if not pending:
                continue
            if not timed_out and len(pending) < batch_limit:
                continue
            batch = tuple(pending)
            pending.clear()
            for result in self.store.ingest_message_batch(
                batch,
                stream_kind=self.profile.stream_kind,
                is_critical=False,
                prune_raw=False,
            ):
                self._log_ingest_result(result)
            self._set_status("healthy")

    def _process_message(self, item: IngestMessage) -> None:
        payload = item.payload
        event = payload.get("event")
        feed = str(payload.get("feed", ""))
        if event == "subscribed":
            self.store.record_raw_event(
                payload,
                stream_kind=self.profile.stream_kind,
                received_at=item.received_at,
                prune_raw=False,
            )
            self._set_status("healthy")
            return
        if event == "error":
            self.store.record_raw_event(
                payload,
                stream_kind=self.profile.stream_kind,
                received_at=item.received_at,
                prune_raw=False,
            )
            self._set_status("error", last_error=str(payload.get("message", "error")))
            return
        try:
            result = self.store.ingest_message(
                payload,
                stream_kind=self.profile.stream_kind,
                is_critical=self.profile.is_critical,
                received_at=item.received_at,
                prune_raw=False,
            )
            self._log_ingest_result(result)
        except Exception as exc:
            self._set_status("error", last_error=f"{feed}: {exc}")
            raise
        self._set_status("healthy")

    def _set_status(
        self,
        status: str,
        *,
        last_error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        targets = (self.profile.stream_kind,) + self.profile.health_aliases
        for stream_kind in targets:
            self.store.record_connection_status(
                stream_kind,
                status,
                now=now,
                last_error=last_error,
            )

    def _log_ingest_result(self, result: Mapping[str, object]) -> None:
        feed = str(result.get("feed", ""))
        if feed.startswith("open_orders"):
            orders = cast(tuple[ExchangeOrder, ...], result.get("orders", ()))
            if feed.endswith("snapshot"):
                self.logger.info(
                    "kraken_account private_snapshot feed=%s rows=%d",
                    feed,
                    len(orders),
                )
            for persisted in orders:
                if feed.endswith("snapshot"):
                    continue
                stop_price = _stop_price_from_payload(persisted.raw_payload)
                self.logger.info(
                    "kraken_account order_event feed=%s symbol=%s order_id=%s client_id=%s side=%s type=%s status=%s qty=%.8f filled=%.8f price=%s stop_price=%s reduce_only=%s reason=%s",
                    feed,
                    persisted.symbol,
                    persisted.exchange_order_id or "-",
                    persisted.client_order_id or "-",
                    persisted.side,
                    persisted.order_type,
                    persisted.status,
                    persisted.quantity,
                    persisted.filled_quantity,
                    persisted.price,
                    stop_price,
                    persisted.reduce_only,
                    optional_str(_reason_from_raw_payload(persisted.raw_payload)) or "-",
                )
            return
        if feed.startswith("fills"):
            fills = cast(tuple[tuple[FillEvent, ExchangeFill], ...], result.get("fills", ()))
            if feed.endswith("snapshot"):
                self.logger.info(
                    "kraken_account private_snapshot feed=%s rows=%d",
                    feed,
                    len(fills),
                )
            for mapped_fill, _fill_row in fills:
                if feed.endswith("snapshot"):
                    continue
                self.logger.info(
                    "kraken_account fill_event feed=%s symbol=%s order_id=%s fill_id=%s side=%s type=%s qty=%.8f price=%.8f liquidity=%s fee=%s fee_ccy=%s",
                    feed,
                    mapped_fill.symbol,
                    mapped_fill.exchange_order_id or "-",
                    mapped_fill.exchange_fill_id or "-",
                    mapped_fill.side,
                    mapped_fill.order_type,
                    mapped_fill.quantity,
                    mapped_fill.price,
                    mapped_fill.liquidity_role or "-",
                    mapped_fill.fee,
                    mapped_fill.fee_currency or "-",
                )
            return
        if feed.startswith("balances"):
            balances = cast(tuple[BalanceWrite, ...], result.get("balances", ()))
            if feed.endswith("snapshot"):
                self.logger.info(
                    "kraken_account private_snapshot feed=%s rows=%d",
                    feed,
                    len(balances),
                )
            for balance in balances:
                if feed.endswith("snapshot"):
                    continue
                if _is_null_balance(balance):
                    continue
                if not self._balance_changed(balance):
                    continue
                self.logger.info(
                    "kraken_account balance_event feed=%s asset=%s available=%.8f locked=%.8f total=%.8f",
                    feed,
                    balance.asset,
                    balance.available,
                    balance.locked,
                    balance.total,
                )
            return
        if feed.startswith("open_positions"):
            positions = cast(tuple[PositionWrite, ...], result.get("positions", ()))
            if feed.endswith("snapshot"):
                self.logger.info(
                    "kraken_account private_snapshot feed=%s rows=%d",
                    feed,
                    len(positions),
                )
            for position in positions:
                if feed.endswith("snapshot"):
                    continue
                if not self._position_changed(position):
                    continue
                self.logger.info(
                    "kraken_account position_event feed=%s symbol=%s side=%s size=%.8f entry_price=%s liquidation_price=%s leverage=%s",
                    feed,
                    position.symbol,
                    position.side,
                    position.size,
                    position.entry_price,
                    position.liquidation_price,
                    position.leverage,
                )
            return
        if feed.startswith(("account_log", "notifications_auth")):
            message = result.get("raw_event")
            payload = message.payload if isinstance(message, RawExchangeEvent) else {}
            kind = str(payload.get("type") or payload.get("event") or "unknown")
            order_id = optional_str(first_present(payload, "order_id", "orderId")) or "-"
            notice = optional_str(first_present(payload, "message", "reason")) or "-"
            if _is_actionable_private_notice(kind, order_id, notice):
                self.logger.info(
                    "kraken_account private_notice feed=%s kind=%s order_id=%s message=%s",
                    feed,
                    kind,
                    order_id,
                    notice,
                )
            else:
                self.logger.debug(
                    "kraken_account private_notice_ignored feed=%s kind=%s order_id=%s message=%s",
                    feed,
                    kind,
                    order_id,
                    notice,
                )

    def _balance_changed(self, balance: BalanceWrite) -> bool:
        current = (balance.available, balance.locked, balance.total)
        previous = self._last_balances.get(balance.asset)
        self._last_balances[balance.asset] = current
        return previous != current

    def _position_changed(self, position: PositionWrite) -> bool:
        key = (position.symbol, position.side)
        current = (
            position.size,
            position.entry_price,
            position.liquidation_price,
            position.leverage,
        )
        previous = self._last_positions.get(key)
        self._last_positions[key] = current
        return previous != current


def stream_profiles_from_config(config: AccountStreamConfig) -> tuple[PrivateStreamProfile, ...]:
    """Resolve run profiles for private websocket ingestion."""
    profile = config.ingest_profile.strip().lower()
    if profile == "all":
        return (
            PrivateStreamProfile(
                name="all",
                stream_kind="private_ws",
                feeds=config.feeds,
                is_critical=True,
            ),
        )
    if profile == "critical":
        return (
            PrivateStreamProfile(
                name="critical",
                stream_kind="private_ws",
                feeds=config.critical_feeds,
                is_critical=True,
            ),
        )
    if profile == "account":
        return (
            PrivateStreamProfile(
                name="account",
                stream_kind="private_ws_account",
                feeds=config.account_feeds,
                is_critical=False,
            ),
        )
    return (
        PrivateStreamProfile(
            name="critical",
            stream_kind="private_ws_critical",
            feeds=config.critical_feeds,
            is_critical=True,
            health_aliases=("private_ws",),
        ),
        PrivateStreamProfile(
            name="account",
            stream_kind="private_ws_account",
            feeds=config.account_feeds,
            is_critical=False,
        ),
    )


async def run_private_stream_group(streams: tuple[KrakenFuturesPrivateStream, ...]) -> None:
    tasks = [asyncio.create_task(stream.run()) for stream in streams]
    try:
        await asyncio.gather(*tasks)
    finally:
        for stream in streams:
            stream.stop()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class KrakenFuturesRestReconciler:
    """REST reconcileur explicite et rate-limit aware par construction."""

    def __init__(
        self,
        config: AccountStreamConfig,
        store: AccountStateStore,
        credentials: KrakenFuturesCredentials,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.credentials = credentials
        self.session = session or requests.Session()

    def reconcile_once(self) -> dict[str, int]:
        """Execute un reconcile REST ponctuel, jamais en boucle serree."""
        stats = {"orders": 0, "positions": 0, "balances": 0}
        for order in self.get_json("/openorders").get("openOrders", []):
            self.store.ensure_order(map_order(order))
            stats["orders"] += 1
        for position in extract_list(self.get_json("/openpositions"), "openPositions"):
            self.store.record_position(map_position(position))
            stats["positions"] += 1
        accounts = self.get_json("/accounts")
        for balance in map_rest_balances(accounts):
            self.store.record_balance(balance)
            stats["balances"] += 1
        self.store.record_connection_status("rest_reconciler", "healthy")
        return stats

    def get_json(
        self, endpoint_path: str, params: Mapping[str, Any] | None = None
    ) -> JsonDictT:
        """GET REST authentifie selon la signature Futures v3."""
        nonce = str(int(time.time() * 1000))
        post_data = urlencode(params or {})
        authent = sign_rest_auth(
            post_data=post_data,
            nonce=nonce,
            # Le chemin signe pour Authent est /api/v3/... meme si l'URL
            # complete appelee est /derivatives/api/v3/...
            endpoint_path=f"/api/v3{endpoint_path}",
            api_secret=self.credentials.api_secret,
        )
        url = f"{self.config.rest_url}{endpoint_path}"
        response = self.session.get(
            url,
            params=params,
            headers={
                "APIKey": self.credentials.api_key,
                "Authent": authent,
                "Nonce": nonce,
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected REST payload: {type(payload)!r}")
        if payload.get("result") == "error":
            raise RuntimeError(payload)
        return payload


def credentials_from_env(config: AccountStreamConfig) -> KrakenFuturesCredentials:
    """Lit les credentials futures sans jamais les logger."""
    api_key = os.environ.get(config.api_key_env)
    api_secret = os.environ.get(config.api_secret_env)
    if not api_key or not api_secret:
        raise RuntimeError(f"missing {config.api_key_env} or {config.api_secret_env}")
    return KrakenFuturesCredentials(api_key=api_key, api_secret=api_secret)


def sign_challenge(challenge: str, api_secret: str) -> str:
    """Signe le challenge websocket Futures officiel Kraken."""
    challenge_hash = hashlib.sha256(challenge.encode("utf-8")).digest()
    secret = base64.b64decode(api_secret)
    digest = hmac.new(secret, challenge_hash, hashlib.sha512).digest()
    return base64.b64encode(digest).decode("ascii")


def sign_rest_auth(
    post_data: str,
    nonce: str,
    endpoint_path: str,
    api_secret: str,
) -> str:
    """Signe une requete REST Futures v3 selon la documentation Kraken."""
    encoded = f"{post_data}{nonce}{endpoint_path}".encode("utf-8")
    message_hash = hashlib.sha256(encoded).digest()
    secret = base64.b64decode(api_secret)
    digest = hmac.new(secret, message_hash, hashlib.sha512).digest()
    return base64.b64encode(digest).decode("ascii")


async def request_challenge(ws: Any, api_key: str) -> str:
    """Demande le challenge d'authentification au websocket Futures."""
    await ws.send(json.dumps({"event": "challenge", "api_key": api_key}))
    while True:
        response = json.loads(await ws.recv())
        event = response.get("event")
        if event == "challenge" and "message" in response:
            return str(response["message"])
        if event == "error":
            raise RuntimeError(f"challenge error: {response}")


def subscribe_messages(
    feeds: Sequence[str],
    api_key: str,
    challenge: str,
    signed_challenge: str,
) -> list[dict[str, object]]:
    """Construit les messages subscribe prives Kraken Futures."""
    return [
        {
            "event": "subscribe",
            "feed": feed,
            "api_key": api_key,
            "original_challenge": challenge,
            "signed_challenge": signed_challenge,
        }
        for feed in feeds
    ]


def iter_order_payloads(message: JsonMapT) -> list[JsonMapT]:
    """Retourne les payloads ordre depuis snapshot ou delta."""
    if isinstance(message.get("orders"), list):
        return [item for item in message["orders"] if isinstance(item, Mapping)]
    order = message.get("order")
    if isinstance(order, Mapping):
        return [order]
    # Some Kraken cancel deltas arrive as top-level order_id/is_cancel payloads.
    if message.get("order_id") is not None or message.get("orderId") is not None:
        return [message]
    return []


def iter_fill_payloads(message: JsonMapT) -> list[JsonMapT]:
    """Retourne les payloads fill depuis snapshot ou delta."""
    for key in ("fills", "fill"):
        value = message.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            return [value]
    return []


def map_order(payload: JsonMapT) -> OrderWrite:
    """Mappe un ordre Kraken Futures vers OrderWrite."""
    quantity = first_float(payload, "qty", "quantity", "size", "unfilledSize") or 0.0
    filled = first_float(payload, "filled", "filled_quantity", "filledSize") or 0.0
    return OrderWrite(
        symbol=str(payload.get("instrument") or payload.get("symbol") or "unknown"),
        side=map_side(first_present(payload, "direction", "side", "buy")),
        order_type=str(
            payload.get("type")
            or payload.get("orderType")
            or payload.get("order_type")
            or payload.get("taker_order_type")
            or "unknown"
        ),
        status=map_order_status(payload),
        quantity=quantity,
        exchange_order_id=optional_str(payload.get("order_id") or payload.get("orderId")),
        client_order_id=optional_str(
            payload.get("cli_ord_id") or payload.get("cliOrdId")
        ),
        price=first_float(payload, "limit_price", "limitPrice", "price", "stop_price"),
        filled_quantity=filled,
        reduce_only=bool(
            payload.get("reduce_only") or payload.get("reduceOnly") or False
        ),
        raw_payload=dict(payload),
        source_timestamp=parse_kraken_time(
            payload.get("last_update_time")
            or payload.get("lastUpdateTime")
            or payload.get("time")
        ),
    )


def map_fill_event(payload: JsonMapT) -> FillEvent:
    """Mappe un fill Kraken Futures vers FillEvent."""
    return FillEvent(
        exchange_order_id=optional_str(payload.get("order_id") or payload.get("orderId")),
        client_order_id=optional_str(payload.get("cli_ord_id") or payload.get("cliOrdId")),
        symbol=str(payload.get("instrument") or payload.get("symbol") or "unknown"),
        side=map_side(first_present(payload, "direction", "side", "buy")),
        order_type=str(
            payload.get("type")
            or payload.get("orderType")
            or payload.get("order_type")
            or payload.get("taker_order_type")
            or "unknown"
        ),
        price=as_float(payload.get("price")),
        quantity=as_float(payload.get("qty") or payload.get("quantity")),
        exchange_fill_id=optional_str(payload.get("fill_id") or payload.get("fillId")),
        fee=first_float(payload, "fee", "fee_paid"),
        fee_currency=optional_str(
            payload.get("fee_currency") or payload.get("feeCurrency")
        ),
        liquidity_role=optional_str(
            payload.get("liquidity")
            or payload.get("liquidity_role")
            or payload.get("fill_type")
        ),
        raw_payload=dict(payload),
        source_timestamp=parse_kraken_time(
            payload.get("time") or payload.get("timestamp")
        ),
    )


def map_balances(message: JsonMapT) -> list[BalanceWrite]:
    """Mappe le feed balances vers des soldes normalises."""
    source_time = parse_kraken_time(message.get("timestamp"))
    rows: list[BalanceWrite] = []
    for container_key in ("holding", "balances", "cash"):
        container = message.get(container_key)
        if isinstance(container, Mapping):
            for asset, value in container.items():
                container_total = as_float(value)
                rows.append(
                    BalanceWrite(
                        asset=str(asset),
                        available=container_total,
                        locked=0.0,
                        total=container_total,
                        raw_payload=dict(message),
                        source_timestamp=source_time,
                    )
                )
    flex_futures = message.get("flex_futures")
    if isinstance(flex_futures, Mapping):
        currencies = flex_futures.get("currencies")
        if isinstance(currencies, Mapping):
            for asset, value in currencies.items():
                if not isinstance(value, Mapping):
                    continue
                available = first_float(value, "available_balance", "availableBalance")
                if available is None:
                    available = first_float(value, "balance_value", "balanceValue")
                total = first_float(value, "balance_value", "balanceValue")
                if total is None:
                    total = available or 0.0
                locked = max(total - (available or 0.0), 0.0)
                rows.append(
                    BalanceWrite(
                        asset=str(asset),
                        available=available or 0.0,
                        locked=locked,
                        total=total,
                        raw_payload=dict(message),
                        source_timestamp=source_time,
                    )
                )
    return rows


def map_rest_balances(payload: JsonMapT) -> list[BalanceWrite]:
    """Mappe les reponses REST accounts de facon defensive."""
    rows: list[BalanceWrite] = []
    accounts = payload.get("accounts")
    iterable: list[tuple[object, object]]
    if isinstance(accounts, Mapping):
        iterable = list(accounts.items())
    elif isinstance(accounts, list):
        iterable = [
            (item.get("currency", "unknown"), item)
            for item in accounts
            if isinstance(item, Mapping)
        ]
    else:
        iterable = []
    for asset, value in iterable:
        if isinstance(value, Mapping):
            rows.extend(map_rest_balance_entry(asset, value))
            continue
        scalar_total = as_float(value)
        rows.append(
            BalanceWrite(
                asset=str(asset),
                available=scalar_total,
                locked=0.0,
                total=scalar_total,
                raw_payload=dict(payload),
            )
        )
    return rows


def map_rest_balance_entry(asset_key: object, payload: JsonMapT) -> list[BalanceWrite]:
    """Mappe une entree REST /accounts, y compris les structures imbriquees.

    Kraken renvoie des comptes nommes dont certaines valeurs utiles vivent dans
    `holding`, d'autres dans la racine, et d'autres encore sous `auxiliary`.
    On extrait les soldes par devise de facon defensive sans supposer une seule
    forme de payload.
    """
    rows: list[BalanceWrite] = []
    holding = payload.get("holding")
    source_time = parse_kraken_time(first_present(payload, "timestamp", "time"))
    if isinstance(holding, Mapping):
        for asset, value in holding.items():
            holding_total = as_float(value)
            rows.append(
                BalanceWrite(
                    asset=str(asset),
                    available=holding_total,
                    locked=0.0,
                    total=holding_total,
                    raw_payload=dict(payload),
                    source_timestamp=source_time,
                )
            )

    auxiliary = payload.get("auxiliary")
    auxiliary_payload = auxiliary if isinstance(auxiliary, Mapping) else {}
    unit = optional_str(first_present(payload, "unit", "currency"))
    settlement_asset = unit or str(asset_key)
    total = first_float(
        payload,
        "balance",
        "total",
        "portfolio_value",
        "portfolioValue",
        "balanceValue",
    )
    if total is None:
        total = first_float(
            auxiliary_payload,
            "balance",
            "total",
            "portfolio_value",
            "portfolioValue",
            "balanceValue",
            "pv",
        )
    available = first_float(payload, "available", "available_funds", "availableFunds")
    if available is None:
        available = first_float(
            auxiliary_payload,
            "available",
            "available_funds",
            "availableFunds",
        )
    if total is not None or available is not None:
        safe_total = total if total is not None else available or 0.0
        safe_available = available if available is not None else safe_total
        rows.append(
            BalanceWrite(
                asset=settlement_asset,
                available=safe_available,
                locked=max(safe_total - safe_available, 0.0),
                total=safe_total,
                raw_payload=dict(payload),
                source_timestamp=source_time,
            )
        )
    return rows


def map_positions(message: JsonMapT) -> list[PositionWrite]:
    """Mappe un message open_positions vers positions normalisees."""
    positions = extract_list(message, "positions", "openPositions")
    if not positions and any(key in message for key in ("instrument", "symbol")):
        positions = [message]
    return [map_position(position) for position in positions]


def map_position(payload: JsonMapT) -> PositionWrite:
    """Mappe une position Kraken Futures defensive."""
    size = first_float(payload, "balance", "size", "qty", "quantity") or 0.0
    side = str(payload.get("side") or ("long" if size >= 0 else "short"))
    return PositionWrite(
        symbol=str(payload.get("instrument") or payload.get("symbol") or "unknown"),
        side=side,
        size=size,
        entry_price=first_float(payload, "entry_price", "entryPrice", "price"),
        leverage=first_float(payload, "leverage"),
        liquidation_price=first_float(payload, "liquidation_price", "liquidationPrice"),
        available_margin=first_float(payload, "available_margin", "availableMargin"),
        maintenance_margin=first_float(
            payload, "maintenance_margin", "maintenanceMargin"
        ),
        maintenance_margin_buffer=first_float(
            payload, "maintenance_margin_buffer", "maintenanceMarginBuffer"
        ),
        funding_rate=first_float(payload, "funding_rate", "fundingRate"),
        raw_payload=dict(payload),
        source_timestamp=parse_kraken_time(
            payload.get("time") or payload.get("timestamp") or payload.get("fill_time")
        ),
    )


def latest_connection(
    session: Session,
    config: AccountStreamConfig,
    stream_kind: str,
) -> ExchangeConnection | None:
    """Lit le dernier statut de connexion pour une identite de flux."""
    stmt = (
        select(ExchangeConnection)
        .where(
            ExchangeConnection.exchange == config.exchange,
            ExchangeConnection.environment == config.environment,
            ExchangeConnection.market_type == config.market_type,
            ExchangeConnection.stream_kind == stream_kind,
        )
        .order_by(ExchangeConnection.updated_at.desc(), ExchangeConnection.id.desc())
    )
    return session.execute(stmt).scalars().first()


def find_order(
    session: Session,
    config: AccountStreamConfig,
    exchange_order_id: str | None,
) -> ExchangeOrder | None:
    """Cherche un ordre par id exchange."""
    if not exchange_order_id:
        return None
    stmt = (
        select(ExchangeOrder)
        .where(
            ExchangeOrder.exchange == config.exchange,
            ExchangeOrder.exchange_order_id == exchange_order_id,
        )
        .order_by(ExchangeOrder.local_timestamp.desc(), ExchangeOrder.id.desc())
    )
    return session.execute(stmt).scalars().first()


def find_fill(
    session: Session,
    config: AccountStreamConfig,
    exchange_fill_id: str | None,
) -> ExchangeFill | None:
    """Cherche une execution par id exchange pour rendre le flux idempotent."""
    if not exchange_fill_id:
        return None
    stmt = (
        select(ExchangeFill)
        .where(
            ExchangeFill.exchange == config.exchange,
            ExchangeFill.exchange_fill_id == exchange_fill_id,
        )
        .order_by(ExchangeFill.local_timestamp.desc(), ExchangeFill.id.desc())
    )
    return session.execute(stmt).scalars().first()


def prune_raw_events(
    session: Session,
    *,
    config: AccountStreamConfig,
    retention_minutes: int,
    retention_limit: int,
    now: datetime,
    stream_kind: str = "private_ws",
) -> None:
    """Apply bounded raw private-event retention for one stream identity."""
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


def count_private_rows(session: Session) -> dict[str, object]:
    """Compte les tables privees principales pour la CLI."""
    return {
        "balance_count": int(
            session.execute(select(func.count()).select_from(AccountBalance)).scalar_one()
        ),
        "fill_count": int(
            session.execute(select(func.count()).select_from(ExchangeFill)).scalar_one()
        ),
        "order_count": int(
            session.execute(select(func.count()).select_from(ExchangeOrder)).scalar_one()
        ),
        "position_count": int(
            session.execute(
                select(func.count()).select_from(AccountPosition)
            ).scalar_one()
        ),
        "raw_event_count": int(
            session.execute(
                select(func.count()).select_from(RawExchangeEvent)
            ).scalar_one()
        ),
        "ingest_audit_count": int(
            session.execute(
                select(func.count()).select_from(PrivateIngestAudit)
            ).scalar_one()
        ),
        "latest_raw_event_at": latest_iso(session, RawExchangeEvent.received_at),
        "latest_order_at": latest_iso(session, ExchangeOrder.local_timestamp),
        "latest_fill_at": latest_iso(session, ExchangeFill.local_timestamp),
        "latest_position_at": latest_iso(session, AccountPosition.local_timestamp),
        "latest_ingest_audit_at": latest_iso(session, PrivateIngestAudit.received_at),
    }


def latest_iso(session: Session, column: Any) -> str | None:
    value = session.execute(select(func.max(column))).scalar_one()
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def connection_to_status(connection: ExchangeConnection) -> dict[str, object]:
    """Formate un statut de connexion pour la CLI."""
    return {
        "exchange": connection.exchange,
        "environment": connection.environment,
        "last_error": connection.last_error,
        "last_heartbeat_at": (
            connection.last_heartbeat_at.isoformat()
            if connection.last_heartbeat_at
            else None
        ),
        "market_type": connection.market_type,
        "status": connection.status,
        "stream_kind": connection.stream_kind,
        "updated_at": connection.updated_at.isoformat(),
    }


def extract_list(payload: JsonMapT, *keys: str) -> list[JsonMapT]:
    """Extrait une liste de mappings depuis plusieurs noms possibles."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def parse_kraken_time(value: object) -> datetime | None:
    """Convertit millisecondes epoch ou ISO en datetime UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
    if isinstance(value, str):
        if value.isdigit():
            return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _is_null_balance(balance: BalanceWrite) -> bool:
    """Treat fully zero balances as null-noise for log emission."""
    return balance.available == 0.0 and balance.locked == 0.0 and balance.total == 0.0


def _is_actionable_private_notice(kind: str, order_id: str, message: str) -> bool:
    """Return True when a private notice carries operator-actionable payload."""
    normalized_kind = kind.strip().lower()
    normalized_order_id = order_id.strip()
    normalized_message = message.strip()
    if normalized_kind not in {"", "unknown", "notifications_auth", "subscribed"}:
        return True
    if normalized_order_id not in {"", "-"}:
        return True
    if normalized_message not in {"", "-"}:
        return True
    return False


def map_side(value: object) -> str:
    """Mappe direction Kraken 0/1 ou texte vers buy/sell."""
    if isinstance(value, bool):
        return "buy" if value else "sell"
    if value in (0, "0", "buy", "BUY"):
        return "buy"
    if value in (1, "1", "sell", "SELL"):
        return "sell"
    return str(value or "unknown").lower()


def map_order_status(payload: JsonMapT) -> str:
    """Mappe le couple is_cancel/reason vers un statut compact."""
    if bool(payload.get("is_cancel")):
        reason = str(payload.get("reason") or "")
        if "full_fill" in reason:
            return "filled"
        if "reject" in reason or "not_enough" in reason or "would_" in reason:
            return "rejected"
        return "canceled"
    reason = str(payload.get("reason") or "")
    if "partial_fill" in reason:
        return "partial_fill"
    return str(payload.get("status") or "open")


def first_float(payload: JsonMapT, *keys: str) -> float | None:
    """Retourne le premier champ convertible en float."""
    for key in keys:
        if key in payload and payload[key] is not None:
            if isinstance(payload[key], Mapping):
                continue
            return as_float(payload[key])
    return None


def first_present(payload: JsonMapT, *keys: str) -> object:
    """Retourne le premier champ present meme si sa valeur vaut 0."""
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def as_float(value: object) -> float:
    """Convertit defensivement en float."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"cannot convert {type(value)!r} to float")


def optional_str(value: object) -> str | None:
    """Convertit une valeur optionnelle en str."""
    if value is None or value == "":
        return None
    return str(value)


def _prefer_known(new_value: str, old_value: str) -> str:
    """Prefer a concrete normalized value over placeholders."""
    if new_value and new_value != "unknown":
        return new_value
    return old_value


def _merge_raw_payload(
    old_payload: dict[str, Any] | None,
    new_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if new_payload:
        return new_payload
    return old_payload or {}


def raw_event_symbol(payload: JsonMapT) -> str | None:
    direct = optional_str(payload.get("instrument") or payload.get("symbol"))
    if direct:
        return direct
    for key in ("orders", "fills", "positions"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    nested = optional_str(item.get("instrument") or item.get("symbol"))
                    if nested:
                        return nested
    return None


def raw_event_source_timestamp(payload: JsonMapT) -> datetime | None:
    """Best-effort exchange timestamp extraction for raw private events."""
    feed = str(payload.get("feed") or "")
    if feed.startswith("fills"):
        fills = iter_fill_payloads(payload)
        if fills:
            return parse_kraken_time(
                first_present(fills[0], "time", "timestamp", "fill_time")
            )
    if feed.startswith("open_orders"):
        orders = iter_order_payloads(payload)
        if orders:
            return parse_kraken_time(
                first_present(
                    orders[0],
                    "last_update_time",
                    "lastUpdateTime",
                    "time",
                    "timestamp",
                )
            )
    if feed.startswith("open_positions"):
        positions = extract_list(payload, "positions", "openPositions")
        if positions:
            return parse_kraken_time(
                first_present(positions[0], "time", "timestamp", "fill_time")
            )
    return parse_kraken_time(
        first_present(payload, "timestamp", "time", "last_update_time")
    )


def raw_event_correlation_id(payload: JsonMapT) -> str | None:
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
    for key in ("orders", "fills"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    nested = raw_event_correlation_id(item)
                    if nested:
                        return nested
    return optional_str(payload.get("seq"))


def _reason_from_raw_payload(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason
    return None


def _stop_price_from_payload(payload: object) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in ("stop_price", "stopPrice", "stopPx", "triggerPrice"):
        value = payload.get(key)
        if isinstance(value, (int, float, str)):
            try:
                return float(value)
            except ValueError:
                continue
    trigger = payload.get("orderTrigger")
    if isinstance(trigger, dict):
        return _stop_price_from_payload(trigger)
    return None


def build_parser() -> argparse.ArgumentParser:
    """Construit la CLI de memoire privee."""
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.tree.account",
        description="Private account/order state service CLI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="<command>",
    )
    command_help = {
        "status": "Show private DB counts and stream health.",
        "run": "Run private websocket listener and persist account/order events.",
        "reconcile": "Fetch one private REST snapshot and persist normalized rows.",
    }
    for command in ("status", "run", "reconcile"):
        cmd = subparsers.add_parser(
            command,
            help=command_help[command],
            description=command_help[command],
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        cmd.add_argument("--db-url", help="Private SQLite database URL.")
        cmd.add_argument(
            "--exchange",
            default=AccountStreamConfig.exchange,
            help="Exchange label stored with rows.",
        )
        cmd.add_argument(
            "--environment",
            choices=("demo", "live"),
            default=AccountStreamConfig.environment,
            help="Endpoint family.",
        )
        cmd.add_argument(
            "--market-type",
            default=AccountStreamConfig.market_type,
            help="Market type label stored with rows.",
        )
        cmd.add_argument(
            "--account-scope",
            default=AccountStreamConfig.account_scope,
            help="Logical account scope label.",
        )
        cmd.add_argument("--ws-url", help="Override private websocket URL.")
        cmd.add_argument("--rest-url", help="Override private REST base URL.")
        cmd.add_argument("--api-key-env", help="Environment variable name for API key.")
        cmd.add_argument(
            "--api-secret-env", help="Environment variable name for API secret."
        )
        cmd.add_argument(
            "--stream-kind", default="private_ws", help="Stream kind for status queries."
        )
        cmd.add_argument(
            "--ingest-profile",
            choices=("split", "all", "critical", "account"),
            default=AccountStreamConfig.ingest_profile,
            help="Run-mode feed profile for private websocket ingestion.",
        )
        cmd.add_argument(
            "--log-level",
            default=AccountStreamConfig.log_level,
            help="Logging verbosity.",
        )
        cmd.add_argument(
            "--raw-retention-minutes",
            type=int,
            default=AccountStreamConfig.raw_retention_minutes,
            help="Raw private-event retention window in minutes; 0 disables time cleanup.",
        )
        cmd.add_argument(
            "--raw-retention-limit",
            type=int,
            default=AccountStreamConfig.raw_retention_limit,
            help="Maximum raw private events kept per stream identity; 0 disables count cleanup.",
        )
    return parser


def config_from_args(args: argparse.Namespace) -> AccountStreamConfig:
    """Convertit les arguments CLI en configuration."""
    env_cfg = kraken_futures_environment(args.environment)
    return AccountStreamConfig(
        db_url=args.db_url or env_cfg.private_db_url,
        exchange=args.exchange,
        environment=args.environment,
        market_type=args.market_type,
        account_scope=args.account_scope,
        ws_url=args.ws_url or env_cfg.private_ws_url,
        rest_url=args.rest_url or env_cfg.rest_url,
        api_key_env=args.api_key_env or env_cfg.api_key_env,
        api_secret_env=args.api_secret_env or env_cfg.api_secret_env,
        ingest_profile=args.ingest_profile,
        raw_retention_minutes=args.raw_retention_minutes,
        raw_retention_limit=args.raw_retention_limit,
        log_level=args.log_level,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Point d'entree CLI pour inspecter ou lancer la memoire privee."""
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args)
    store = AccountStateStore(config)
    if args.command == "status":
        print(json.dumps(store.latest_status(args.stream_kind), sort_keys=True))
        return 0
    credentials = credentials_from_env(config)
    if args.command == "reconcile":
        stats = KrakenFuturesRestReconciler(config, store, credentials).reconcile_once()
        print(json.dumps(stats, sort_keys=True))
        return 0
    profiles = stream_profiles_from_config(config)
    streams = tuple(
        KrakenFuturesPrivateStream(config, store, credentials, profile=profile)
        for profile in profiles
    )
    try:
        asyncio.run(run_private_stream_group(streams))
    except KeyboardInterrupt:
        for stream in streams:
            stream.stop()
        for profile in profiles:
            for stream_kind in (profile.stream_kind,) + profile.health_aliases:
                store.record_connection_status(
                    stream_kind,
                    "stopped",
                    last_error="stopped by operator",
                )
        print("private account stream stopped by operator")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
