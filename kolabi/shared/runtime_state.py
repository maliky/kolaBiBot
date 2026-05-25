"""Runtime DB state reader for strategy readiness and market/account snapshots.

Purpose: provide typed public/private state snapshots used by bot runtime
preflight and pair-cycle execution.
Inputs: SQLite URLs, exchange/environment/symbol filters.
Outputs: `PublicMarketState`, `PrivateFeedState`, `StrategyRuntimeState`.
Side effects: database reads and polling sleeps in wait loops.
Important types: typed DB records (`PublicBookRecord`, `PrivateOrderRecord`,
`PrivatePositionRecord`) and state dataclasses.
Role: boundary adapter.
Transitional: yes, typed row adapters are incremental over existing ORM models.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import sleep
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kolabi.shared.core.runtime_types import (
    PrivateFillRecord,
    PrivateOrderRecord,
    PrivatePositionRecord,
    PublicBookRecord,
    PublicIndicatorRecord,
)
from kolabi.shared.persistence import AccountPosition, ExchangeFill, ExchangeOrder
from kolabi.tree.account import AccountStreamConfig, latest_connection
from kolabi.tree.kraken import latest_indicator_values, latest_snapshot


@dataclass(frozen=True)
class PublicMarketState:
    """Latest public market view for one symbol."""

    symbol: str
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    spread: float | None
    imbalance: float | None
    avg_bid: float | None
    avg_ask: float | None
    recorded_at: str | None
    source_timestamp: str | None
    age_seconds: float | None
    source_age_seconds: float | None
    indicators: dict[str, float]
    ready: bool
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping."""
        return asdict(self)


@dataclass(frozen=True)
class PrivateFeedState:
    """Freshness summary for one private runtime feed."""

    stream_kind: str
    status: str
    updated_at: str | None
    last_heartbeat_at: str | None
    age_seconds: float | None
    ready: bool
    last_error: str | None
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping."""
        return asdict(self)


@dataclass(frozen=True)
class StrategyRuntimeState:
    """Combined public/private readiness view for one strategy symbol."""

    symbol: str
    public: PublicMarketState
    private_ws: PrivateFeedState
    rest_reconciler: PrivateFeedState
    open_order_count: int
    fill_count: int
    position_size: float | None
    position_entry_price: float | None
    ready: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping."""
        payload = asdict(self)
        payload["public"] = self.public.as_dict()
        payload["private_ws"] = self.private_ws.as_dict()
        payload["rest_reconciler"] = self.rest_reconciler.as_dict()
        return payload


class KrakenRuntimeStateClient:
    """Read strategy-facing public and private state from local SQLite stores."""

    def __init__(
        self,
        *,
        market_db_url: str,
        account_db_url: str,
        symbol: str,
        exchange: str = "kraken",
        environment: str = "demo",
        market_type: str = "futures",
        account_scope: str = "default",
        max_public_age_seconds: float = 15.0,
        max_private_age_seconds: float = 30.0,
        max_reconcile_age_seconds: float = 300.0,
    ) -> None:
        self.market_db_url = market_db_url
        self.account_db_url = account_db_url
        self.symbol = symbol
        self.exchange = exchange
        self.environment = environment
        self.market_type = market_type
        self.account_scope = account_scope
        self.max_public_age_seconds = max_public_age_seconds
        self.max_private_age_seconds = max_private_age_seconds
        self.max_reconcile_age_seconds = max_reconcile_age_seconds
        self._market_sessionmaker = sessionmaker(
            bind=create_engine(self.market_db_url),
            expire_on_commit=False,
            class_=Session,
        )
        self._account_sessionmaker = sessionmaker(
            bind=create_engine(self.account_db_url),
            expire_on_commit=False,
            class_=Session,
        )

    def fetch_market_state(self, symbol: str | None = None) -> PublicMarketState:
        """Load the latest public book snapshot and compact indicators."""
        target_symbol = symbol or self.symbol
        current_time = datetime.now(timezone.utc)
        with self._market_sessionmaker() as session:
            snapshot = latest_snapshot(
                session,
                target_symbol,
                self.exchange,
                self.environment,
                self.market_type,
            )
            if snapshot is None:
                return PublicMarketState(
                    symbol=target_symbol,
                    best_bid=None,
                    best_ask=None,
                    mid_price=None,
                    spread=None,
                    imbalance=None,
                    avg_bid=None,
                    avg_ask=None,
                    recorded_at=None,
                    source_timestamp=None,
                    age_seconds=None,
                    source_age_seconds=None,
                    indicators={},
                    ready=False,
                    reason="missing public market snapshot",
                )
            indicators = latest_indicator_values(
                session,
                target_symbol,
                self.exchange,
                self.environment,
                self.market_type,
            )
            public_book = _public_book_record_from_snapshot(snapshot, target_symbol)
            public_indicators = _public_indicator_records(indicators, target_symbol)
            freshest_local_time = _latest_public_timestamp(
                snapshot.local_timestamp,
                indicators,
            )
            age_seconds = _age_seconds(freshest_local_time, current_time)
            source_age_seconds = _age_seconds(snapshot.source_timestamp, current_time)
            ready = age_seconds is not None and age_seconds <= self.max_public_age_seconds
            reason = None if ready else "public market data is stale"
            return PublicMarketState(
                symbol=target_symbol,
                best_bid=public_book.best_bid,
                best_ask=public_book.best_ask,
                mid_price=public_book.mid_price,
                spread=public_book.spread,
                imbalance=public_book.imbalance,
                avg_bid=public_book.avg_bid,
                avg_ask=public_book.avg_ask,
                recorded_at=public_book.recorded_at,
                source_timestamp=public_book.source_timestamp,
                age_seconds=age_seconds,
                source_age_seconds=source_age_seconds,
                indicators={record.name: record.value for record in public_indicators},
                ready=ready,
                reason=reason,
            )

    def fetch_runtime_state(self, symbol: str | None = None) -> StrategyRuntimeState:
        """Load the combined runtime state used by the Kraken TSV route."""
        target_symbol = symbol or self.symbol
        public = self.fetch_market_state(target_symbol)
        with self._account_sessionmaker() as session:
            private_ws = self._private_feed_state(
                session,
                "private_ws",
                self.max_private_age_seconds,
            )
            reconciler = self._private_feed_state(
                session,
                "rest_reconciler",
                self.max_reconcile_age_seconds,
            )
            open_order_count = self._count_open_orders(session, target_symbol)
            fill_count = self._count_fills(session, target_symbol)
            position = self._latest_position(session, target_symbol)
        reasons = tuple(
            reason
            for reason in (
                public.reason,
                private_ws.reason,
                reconciler.reason,
            )
            if reason
        )
        return StrategyRuntimeState(
            symbol=target_symbol,
            public=public,
            private_ws=private_ws,
            rest_reconciler=reconciler,
            open_order_count=open_order_count,
            fill_count=fill_count,
            position_size=None if position is None else position.size,
            position_entry_price=None if position is None else position.entry_price,
            ready=public.ready and private_ws.ready and reconciler.ready,
            reasons=reasons,
        )

    def fetch_private_orders_since(
        self,
        *,
        after_local_timestamp: datetime | None = None,
        after_local_id: int | None = None,
        symbol: str | None = None,
        limit: int = 200,
    ) -> tuple[PrivateOrderRecord, ...]:
        """Return private order rows strictly newer than the supplied cursor."""
        target_symbol = symbol or self.symbol
        with self._account_sessionmaker() as session:
            statement = (
                select(ExchangeOrder)
                .where(
                    ExchangeOrder.exchange == self.exchange,
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == self.market_type,
                    ExchangeOrder.symbol == target_symbol,
                )
                .order_by(ExchangeOrder.local_timestamp.asc(), ExchangeOrder.id.asc())
                .limit(limit)
            )
            rows = session.execute(statement).scalars().all()
        records: list[PrivateOrderRecord] = []
        for row in rows:
            if after_local_timestamp is not None:
                row_timestamp = _normalise_cursor_timestamp(
                    row.local_timestamp,
                    after_local_timestamp,
                )
                cursor_timestamp = _normalise_cursor_timestamp(
                    after_local_timestamp,
                    row.local_timestamp,
                )
                if row_timestamp < cursor_timestamp:
                    continue
                if (
                    row_timestamp == cursor_timestamp
                    and after_local_id is not None
                    and row.id <= after_local_id
                ):
                    continue
            records.append(_private_order_record(row))
        return tuple(records)

    def wait_until_ready(
        self,
        *,
        timeout_seconds: float,
        poll_seconds: float = 1.0,
    ) -> StrategyRuntimeState:
        """Block until public and private runtime state are fresh enough."""
        deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
        last_state = self.fetch_runtime_state()
        while datetime.now(timezone.utc).timestamp() < deadline:
            last_state = self.fetch_runtime_state()
            if last_state.ready:
                return last_state
            sleep(poll_seconds)
        return last_state

    def _private_feed_state(
        self,
        session: Session,
        stream_kind: str,
        max_age_seconds: float,
    ) -> PrivateFeedState:
        """Read one private feed status with a strict freshness gate."""
        config = AccountStreamConfig(
            db_url=self.account_db_url,
            exchange=self.exchange,
            environment=self.environment,
            market_type=self.market_type,
            account_scope=self.account_scope,
        )
        connection = latest_connection(session, config, stream_kind)
        if connection is None:
            if stream_kind == "rest_reconciler":
                return PrivateFeedState(
                    stream_kind=stream_kind,
                    status="missing",
                    updated_at=None,
                    last_heartbeat_at=None,
                    age_seconds=None,
                    ready=True,
                    last_error=None,
                    reason=None,
                )
            return PrivateFeedState(
                stream_kind=stream_kind,
                status="missing",
                updated_at=None,
                last_heartbeat_at=None,
                age_seconds=None,
                ready=False,
                last_error=None,
                reason=f"missing {stream_kind} state",
            )
        current_time = datetime.now(timezone.utc)
        reference_time = connection.last_heartbeat_at or connection.updated_at
        age_seconds = _age_seconds(reference_time, current_time)
        if stream_kind == "rest_reconciler":
            recent_error = connection.status.lower() == "error" and (
                age_seconds is None or age_seconds <= max_age_seconds
            )
            return PrivateFeedState(
                stream_kind=stream_kind,
                status=connection.status,
                updated_at=connection.updated_at.isoformat(),
                last_heartbeat_at=connection.last_heartbeat_at.isoformat()
                if connection.last_heartbeat_at
                else None,
                age_seconds=age_seconds,
                ready=not recent_error,
                last_error=connection.last_error,
                reason="recent rest_reconciler error" if recent_error else None,
            )
        is_healthy = connection.status.lower() == "healthy"
        is_fresh = age_seconds is not None and age_seconds <= max_age_seconds
        ready = is_healthy and is_fresh
        reason = None
        if not is_healthy:
            reason = f"{stream_kind} status is {connection.status}"
        elif not is_fresh:
            reason = f"{stream_kind} state is stale"
        return PrivateFeedState(
            stream_kind=stream_kind,
            status=connection.status,
            updated_at=connection.updated_at.isoformat(),
            last_heartbeat_at=connection.last_heartbeat_at.isoformat()
            if connection.last_heartbeat_at
            else None,
            age_seconds=age_seconds,
            ready=ready,
            last_error=connection.last_error,
            reason=reason,
        )

    def _count_open_orders(self, session: Session, symbol: str) -> int:
        """Count strategy-relevant open orders for one symbol."""
        rows = (
            session.execute(
                select(ExchangeOrder).where(
                    ExchangeOrder.exchange == self.exchange,
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == self.market_type,
                    ExchangeOrder.symbol == symbol,
                )
            )
            .scalars()
            .all()
        )
        open_statuses = {"new", "open", "partiallyfilled", "partialfill"}
        order_records = [_private_order_record(row) for row in rows]
        return sum(
            1
            for row in order_records
            if row.status.replace(" ", "").replace("_", "").lower() in open_statuses
        )

    def _count_fills(self, session: Session, symbol: str) -> int:
        """Count fills for one symbol."""
        rows = (
            session.execute(
                select(ExchangeFill)
                .join(ExchangeOrder)
                .where(
                    ExchangeOrder.exchange == self.exchange,
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == self.market_type,
                    ExchangeOrder.symbol == symbol,
                )
            )
            .scalars()
            .all()
        )
        fill_records = [_private_fill_record(symbol) for _ in rows]
        return len(fill_records)

    def _latest_position(self, session: Session, symbol: str) -> PrivatePositionRecord | None:
        """Read the latest position row for one symbol."""
        stmt = (
            select(AccountPosition)
            .where(
                AccountPosition.exchange == self.exchange,
                AccountPosition.environment == self.environment,
                AccountPosition.market_type == self.market_type,
                AccountPosition.account_scope == self.account_scope,
                AccountPosition.symbol == symbol,
            )
            .order_by(AccountPosition.local_timestamp.desc(), AccountPosition.id.desc())
        )
        row = session.execute(stmt).scalars().first()
        return None if row is None else _private_position_record(row)


def _age_seconds(value: datetime | None, current_time: datetime) -> float | None:
    """Return the age in seconds for an optional UTC timestamp."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0.0, (current_time - value).total_seconds())


def _latest_public_timestamp(
    snapshot_time: datetime | None,
    indicators: dict[str, Any],
) -> datetime | None:
    """Return the freshest local timestamp exposed by the public market service."""
    latest_time = snapshot_time
    for indicator in indicators.values():
        computed_at = getattr(indicator, "computed_at", None)
        if computed_at is None:
            continue
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=timezone.utc)
        if latest_time is None:
            latest_time = computed_at
            continue
        snapshot_tz = (
            latest_time
            if latest_time.tzinfo
            else latest_time.replace(tzinfo=timezone.utc)
        )
        if computed_at > snapshot_tz:
            latest_time = computed_at
    return latest_time


def _public_book_record_from_snapshot(snapshot: Any, symbol: str) -> PublicBookRecord:
    return PublicBookRecord(
        symbol=symbol,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        mid_price=snapshot.mid_price,
        spread=snapshot.spread,
        imbalance=snapshot.imbalance,
        avg_bid=snapshot.avg_bid,
        avg_ask=snapshot.avg_ask,
        recorded_at=snapshot.local_timestamp.isoformat(),
        source_timestamp=snapshot.source_timestamp.isoformat()
        if snapshot.source_timestamp
        else None,
    )


def _public_indicator_records(
    indicators: dict[str, Any],
    symbol: str,
) -> tuple[PublicIndicatorRecord, ...]:
    records: list[PublicIndicatorRecord] = []
    for name, indicator in indicators.items():
        computed_at = getattr(indicator, "computed_at", None)
        records.append(
            PublicIndicatorRecord(
                symbol=symbol,
                name=name,
                value=float(indicator.value),
                recorded_at=computed_at.isoformat() if computed_at is not None else None,
            )
        )
    return tuple(records)


def _normalise_cursor_timestamp(value: datetime, peer: datetime) -> datetime:
    """Compare SQLite naive timestamps with aware runtime cursors safely."""
    if value.tzinfo is None or peer.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _private_order_record(row: ExchangeOrder) -> PrivateOrderRecord:
    reason, is_cancel = _order_reason_flags_from_payload(row.raw_payload)
    return PrivateOrderRecord(
        symbol=row.symbol,
        status=row.status,
        exchange_order_id=row.exchange_order_id,
        client_order_id=row.client_order_id,
        reason=reason,
        is_cancel=is_cancel,
        side=row.side,
        order_type=row.order_type,
        price=row.price,
        quantity=row.quantity,
        filled_quantity=row.filled_quantity,
        source_timestamp=(
            row.source_timestamp.isoformat() if row.source_timestamp is not None else None
        ),
        local_timestamp=row.local_timestamp.isoformat(),
        local_id=row.id,
    )


def _order_reason_flags_from_payload(
    payload: object,
) -> tuple[str | None, bool | None]:
    if not isinstance(payload, dict):
        return None, None
    reason_obj = payload.get("reason")
    reason = str(reason_obj) if isinstance(reason_obj, str) and reason_obj else None
    is_cancel_obj = payload.get("is_cancel")
    is_cancel = is_cancel_obj if isinstance(is_cancel_obj, bool) else None
    return reason, is_cancel


def _private_fill_record(symbol: str) -> PrivateFillRecord:
    return PrivateFillRecord(symbol=symbol)


def _private_position_record(row: AccountPosition) -> PrivatePositionRecord:
    return PrivatePositionRecord(
        symbol=row.symbol,
        size=row.size,
        entry_price=row.entry_price,
    )
