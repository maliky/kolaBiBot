from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from kolabi.shared.persistence.models import (
    AccountBalance,
    AccountPosition,
    ExchangeRestCall,
    PrivateIngestAudit,
    TailTelemetry,
)

T = TypeVar("T")


@dataclass(frozen=True)
class PruneResult:
    """Small accounting object returned by maintenance pruning."""

    deleted_rows: int = 0

    def __add__(self, other: "PruneResult") -> "PruneResult":
        return PruneResult(deleted_rows=self.deleted_rows + other.deleted_rows)


def prune_time_count(
    session: Session,
    *,
    model: type[Any],
    time_column: Any,
    filters: Sequence[Any],
    retention_minutes: int,
    retention_limit: int,
    now: datetime,
) -> PruneResult:
    """Prune an append-only table by time and then by newest-row count."""

    deleted = 0
    if retention_minutes > 0:
        cutoff = now - timedelta(minutes=retention_minutes)
        result = cast(CursorResult[Any], session.execute(
            delete(model)
            .where(*filters, time_column < cutoff)
            .execution_options(synchronize_session=False)
        ))
        deleted += int(result.rowcount or 0)
    if retention_limit > 0:
        keep_ids = (
            select(model.id)
            .where(*filters)
            .order_by(time_column.desc(), model.id.desc())
            .limit(retention_limit)
        )
        result = cast(CursorResult[Any], session.execute(
            delete(model)
            .where(*filters, model.id.notin_(keep_ids))
            .execution_options(synchronize_session=False)
        ))
        deleted += int(result.rowcount or 0)
    return PruneResult(deleted_rows=deleted)


def prune_account_balances(
    session: Session,
    *,
    exchange: str,
    environment: str,
    account_scope: str,
    retention_minutes: int,
    retention_limit: int,
    sample_interval_seconds: float,
    now: datetime,
) -> PruneResult:
    rows = (
        session.execute(
            select(AccountBalance)
            .where(
                AccountBalance.exchange == exchange,
                AccountBalance.environment == environment,
                AccountBalance.account_scope == account_scope,
            )
            .order_by(
                AccountBalance.asset.asc(),
                AccountBalance.local_timestamp.desc(),
                AccountBalance.id.desc(),
            )
        )
        .scalars()
        .all()
    )
    return _prune_sampled_state(
        session,
        model=AccountBalance,
        rows=rows,
        identity=lambda row: (row.asset,),
        state=lambda row: (row.available, row.locked, row.total),
        timestamp=lambda row: row.local_timestamp,
        retention_minutes=retention_minutes,
        retention_limit=retention_limit,
        sample_interval_seconds=sample_interval_seconds,
        now=now,
    )


def prune_account_positions(
    session: Session,
    *,
    exchange: str,
    environment: str,
    market_type: str,
    account_scope: str,
    retention_minutes: int,
    retention_limit: int,
    sample_interval_seconds: float,
    now: datetime,
) -> PruneResult:
    rows = (
        session.execute(
            select(AccountPosition)
            .where(
                AccountPosition.exchange == exchange,
                AccountPosition.environment == environment,
                AccountPosition.market_type == market_type,
                AccountPosition.account_scope == account_scope,
            )
            .order_by(
                AccountPosition.symbol.asc(),
                AccountPosition.side.asc(),
                AccountPosition.local_timestamp.desc(),
                AccountPosition.id.desc(),
            )
        )
        .scalars()
        .all()
    )
    return _prune_sampled_state(
        session,
        model=AccountPosition,
        rows=rows,
        identity=lambda row: (row.symbol, row.side),
        state=lambda row: (
            row.size,
            row.entry_price,
            row.leverage,
            row.liquidation_price,
            row.available_margin,
            row.maintenance_margin,
            row.maintenance_margin_buffer,
            row.funding_rate,
        ),
        timestamp=lambda row: row.local_timestamp,
        retention_minutes=retention_minutes,
        retention_limit=retention_limit,
        sample_interval_seconds=sample_interval_seconds,
        now=now,
    )


def prune_private_ingest_audits(
    session: Session,
    *,
    exchange: str,
    environment: str,
    market_type: str,
    account_scope: str,
    retention_minutes: int,
    retention_limit: int,
    now: datetime,
) -> PruneResult:
    return prune_time_count(
        session,
        model=PrivateIngestAudit,
        time_column=PrivateIngestAudit.received_at,
        filters=(
            PrivateIngestAudit.exchange == exchange,
            PrivateIngestAudit.environment == environment,
            PrivateIngestAudit.market_type == market_type,
            PrivateIngestAudit.account_scope == account_scope,
        ),
        retention_minutes=retention_minutes,
        retention_limit=retention_limit,
        now=now,
    )


def prune_exchange_rest_calls(
    session: Session,
    *,
    exchange: str,
    environment: str,
    market_type: str,
    account_scope: str,
    retention_minutes: int,
    retention_limit: int,
    now: datetime,
) -> PruneResult:
    return prune_time_count(
        session,
        model=ExchangeRestCall,
        time_column=ExchangeRestCall.created_at,
        filters=(
            ExchangeRestCall.exchange == exchange,
            ExchangeRestCall.environment == environment,
            ExchangeRestCall.market_type == market_type,
            ExchangeRestCall.account_scope == account_scope,
        ),
        retention_minutes=retention_minutes,
        retention_limit=retention_limit,
        now=now,
    )


def prune_tail_telemetry(
    session: Session,
    *,
    exchange: str | None = None,
    environment: str | None = None,
    market_type: str | None = None,
    account_scope: str | None = None,
    retention_minutes: int,
    retention_limit: int,
    now: datetime,
) -> PruneResult:
    filters: list[Any] = []
    if exchange is not None:
        filters.append(TailTelemetry.exchange == exchange)
    if environment is not None:
        filters.append(TailTelemetry.environment == environment)
    if market_type is not None:
        filters.append(TailTelemetry.market_type == market_type)
    if account_scope is not None:
        filters.append(TailTelemetry.account_scope == account_scope)
    return prune_time_count(
        session,
        model=TailTelemetry,
        time_column=TailTelemetry.recorded_at,
        filters=tuple(filters),
        retention_minutes=retention_minutes,
        retention_limit=retention_limit,
        now=now,
    )


def _prune_sampled_state(
    session: Session,
    *,
    model: type[Any],
    rows: Sequence[T],
    identity: Callable[[T], tuple[Any, ...]],
    state: Callable[[T], tuple[Any, ...]],
    timestamp: Callable[[T], datetime | None],
    retention_minutes: int,
    retention_limit: int,
    sample_interval_seconds: float,
    now: datetime,
) -> PruneResult:
    cutoff = now - timedelta(minutes=retention_minutes) if retention_minutes > 0 else None
    keep_ids: set[int] = set()
    delete_ids: list[int] = []
    groups: dict[tuple[Any, ...], _StateGroup] = {}
    min_gap = max(0.0, sample_interval_seconds)
    for row in rows:
        row_id = int(getattr(row, "id"))
        key = identity(row)
        row_state = state(row)
        row_time = _as_utc(timestamp(row) or now)
        group = groups.setdefault(key, _StateGroup())
        keep = False
        if group.kept_count == 0:
            keep = True
        elif cutoff is not None and row_time < cutoff:
            keep = False
        elif retention_limit > 0 and group.kept_count >= retention_limit:
            keep = False
        elif group.last_state != row_state:
            keep = True
        elif group.last_time is None:
            keep = True
        else:
            keep = (group.last_time - row_time).total_seconds() >= min_gap
        if keep:
            keep_ids.add(row_id)
            group.kept_count += 1
            group.last_state = row_state
            group.last_time = row_time
        else:
            delete_ids.append(row_id)
    if not delete_ids:
        return PruneResult()
    deleted = 0
    for start in range(0, len(delete_ids), 1000):
        chunk = delete_ids[start : start + 1000]
        result = cast(CursorResult[Any], session.execute(
            delete(model)
            .where(model.id.in_(chunk), model.id.notin_(keep_ids))
            .execution_options(synchronize_session=False)
        ))
        deleted += int(result.rowcount or 0)
    return PruneResult(deleted_rows=deleted)


@dataclass
class _StateGroup:
    kept_count: int = 0
    last_state: tuple[Any, ...] | None = None
    last_time: datetime | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "PruneResult",
    "prune_account_balances",
    "prune_account_positions",
    "prune_exchange_rest_calls",
    "prune_private_ingest_audits",
    "prune_tail_telemetry",
    "prune_time_count",
]
