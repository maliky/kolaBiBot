from __future__ import annotations

from datetime import datetime, timezone

from kolabi.bot.persistence import PersistenceConfig, TailTelemetryRecorder
from kolabi.bot.telemetry import TailTelemetryRow
from kolabi.shared.persistence import TailTelemetry
from kolabi.shared.pruning import TimeCountPruning
from sqlalchemy import select


def test_tail_telemetry_recorder_prunes_history_by_limit(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'telemetry.sqlite'}"
    recorder = TailTelemetryRecorder(
        PersistenceConfig(
            db_url=db_url,
            tail_telemetry_pruning=TimeCountPruning(
                retention_minutes=0,
                retention_limit=1,
            ),
        )
    )
    with recorder._sessionmaker() as session:
        session.add(
            TailTelemetry(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                strategy_id="old",
                pair_name="pair-a",
                symbol="PI_XBTUSD",
                head_state="closed",
                tail_state="living",
                tail_mode="flying",
                reference_price=1.0,
                stop_price=0.5,
                initial_distance=0.5,
                current_distance=0.5,
                recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.commit()

    recorder.record_rows(
        (
            TailTelemetryRow(
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                strategy_id="new",
                pair_name="pair-a",
                symbol="PI_XBTUSD",
                head_state="closed",
                tail_state="living",
                tail_mode="flying",
                reference_price=2.0,
                stop_price=1.5,
                initial_distance=0.5,
                current_distance=0.5,
                last_tail_update_at=None,
                recorded_at=datetime.now(timezone.utc),
            ),
        )
    )

    with recorder._sessionmaker() as session:
        rows = session.execute(select(TailTelemetry)).scalars().all()
    assert [row.strategy_id for row in rows] == ["new"]
