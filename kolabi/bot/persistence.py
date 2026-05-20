from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from sqlalchemy.orm import Session

from kolabi.bot.tsv import OrderSpec
from kolabi.shared.persistence import OrderEvent, OrderRun, get_sessionmaker


@dataclass
class PersistenceConfig:
    db_url: str = "sqlite:///kolabi_bot.db"


class OrderRecorder:
    def __init__(self, config: PersistenceConfig) -> None:
        self.config = config
        self._sessionmaker = get_sessionmaker(config.db_url)

    def start_run(self, spec: OrderSpec, indicators: Dict[str, Any]) -> OrderRun:
        session: Session = self._sessionmaker()
        run = OrderRun(
            name=spec.name,
            exchange="",
            symbol="",
            strategy={
                "tps_run": (spec.window.start_minutes, spec.window.end_minutes),
                "prix": spec.head.price_interval,
                "otype": spec.head.order_type,
                "atype": spec.amount_type,
                "indicators": indicators,
            },
            status="submitted",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        session.close()
        return run

    def record_event(self, run_id: int, event_type: str, status: str, payload: dict) -> None:
        session: Session = self._sessionmaker()
        event = OrderEvent(
            run_id=run_id,
            event_type=event_type,
            status=status,
            payload=payload,
        )
        session.add(event)
        session.commit()
        session.close()
