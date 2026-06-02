from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TimeCountPruning:
    """Central retention knobs for append-only persistence lanes."""

    retention_minutes: int
    retention_limit: int
    maintenance_seconds: float = 60.0


@dataclass(frozen=True)
class StatePruning:
    """Retention knobs for sampled private account state history."""

    retention_minutes: int
    retention_limit: int


@dataclass(frozen=True)
class PruningConfig:
    """One place for operational persistence retention defaults."""

    raw_events: TimeCountPruning = field(
        default_factory=lambda: TimeCountPruning(
            retention_minutes=1440,
            retention_limit=100000,
        )
    )
    account_state: StatePruning = field(
        default_factory=lambda: StatePruning(
            retention_minutes=1440,
            retention_limit=2000,
        )
    )
    private_ingest_audit: TimeCountPruning = field(
        default_factory=lambda: TimeCountPruning(
            retention_minutes=1440,
            retention_limit=100000,
        )
    )
    rest_audit: TimeCountPruning = field(
        default_factory=lambda: TimeCountPruning(
            retention_minutes=10080,
            retention_limit=50000,
        )
    )
    tail_telemetry: TimeCountPruning = field(
        default_factory=lambda: TimeCountPruning(
            retention_minutes=1440,
            retention_limit=20000,
        )
    )


DEFAULT_PRUNING = PruningConfig()


__all__ = [
    "DEFAULT_PRUNING",
    "PruningConfig",
    "StatePruning",
    "TimeCountPruning",
]
