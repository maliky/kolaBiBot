from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from typing import Protocol

from kolabi.bot.domain import HeadSpec, OrderIdentity, OrderPairSpec, PairCycleState, Side, StrategyState, TailSpec, TimeWindow
from kolabi.bot.strategy_runtime import KrakenPrivateOrderPollingSource
from kolabi.shared.core.runtime_types import PrivateOrderRecord
from kolabi.shared.persistence import Base, ExchangeOrder
from kolabi.shared.runtime_state import KrakenRuntimeStateClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def sample_pair(name: str) -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
        window=TimeWindow(start_minutes=0.0, end_minutes=60.0),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=Side.BUY, order_type="Limit"),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=1,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL, order_type="Stop", delta=0.5),
        tail_price_spec=99.0,
        tail_price_spec_type="tA",
        amount_type="qApD",
    )


def percent_tail_pair(name: str) -> OrderPairSpec:
    pair = sample_pair(name)
    return OrderPairSpec(
        name=pair.name,
        window=pair.window,
        try_num=pair.try_num,
        dr_pause=pair.dr_pause,
        timeout=pair.timeout,
        head=pair.head,
        head_price=pair.head_price,
        head_price_type="p%",
        head_quantity=3,
        head_quantity_type=pair.head_quantity_type,
        tail=pair.tail,
        tail_price_spec=1.5,
        tail_price_spec_type="t%",
        amount_type="qAt%p%",
    )


def test_private_order_poller_emits_head_confirmation_from_db(tmp_path) -> None:
    market_db = f"sqlite:///{tmp_path / 'pub.sqlite'}"
    account_db = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(account_engine) as session:
        session.add(
            ExchangeOrder(
                local_uuid="ord-1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                symbol="PI_XBTUSD",
                exchange_order_id="OID-H",
                client_order_id="CID-H",
                side="buy",
                order_type="limit",
                status="partially_filled",
                price=100.0,
                quantity=1.0,
                filled_quantity=0.5,
                reduce_only=False,
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
    source = KrakenPrivateOrderPollingSource(client, poll_seconds=0.0)
    emitted = []

    class _RuntimeLike(Protocol):
        strategy: object
        symbol: str
        running: bool
        state: StrategyState

        @property
        def all_pairs_terminal(self) -> bool: ...

        async def enqueue(self, event: object) -> None: ...

        def record_targets_head(self, record: object) -> bool: ...

    class _Runtime:
        def __init__(self) -> None:
            self.strategy = object()
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
                        head_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="head",
                            client_order_id="CID-H",
                            exchange_order_id="OID-H",
                        ),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(event)
            self.running = False

        def record_targets_head(self, record) -> bool:
            return True

        def head_pair_state_for_record(self, record) -> PairCycleState | None:
            return self.state.pairs["pair-a"]

    runtime: _RuntimeLike = _Runtime()
    asyncio.run(source.pump(runtime))

    assert len(emitted) == 1
    assert emitted[0].is_private is True
    assert emitted[0].kind.value == "played_not_canceled"


def test_private_order_poller_injects_side_aware_reference_price() -> None:
    now = datetime.now(timezone.utc)
    record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="partially_filled",
        exchange_order_id="OID-H",
        client_order_id="CID-H",
        quantity=2.0,
        filled_quantity=1.0,
        local_id=1,
        local_timestamp=now.isoformat(),
    )

    class _Market:
        best_bid = 99.0
        best_ask = 101.0
        mid_price = 100.0
        recorded_at = now.isoformat()

    class _Client:
        def __init__(self) -> None:
            self.sent = False

        def fetch_private_orders_since(self, **_kwargs):
            if self.sent:
                return ()
            self.sent = True
            return (record,)

        def fetch_market_state(self, symbol: str | None = None):
            assert symbol == "PI_XBTUSD"
            return _Market()

    class _Runtime:
        def __init__(self) -> None:
            pair = sample_pair("pair-a")
            pair = OrderPairSpec(
                name=pair.name,
                window=pair.window,
                try_num=pair.try_num,
                dr_pause=pair.dr_pause,
                timeout=pair.timeout,
                head=HeadSpec(side=Side.SELL, order_type="Market"),
                head_price=pair.head_price,
                head_price_type=pair.head_price_type,
                head_quantity=pair.head_quantity,
                head_quantity_type=pair.head_quantity_type,
                tail=pair.tail,
                tail_price_spec=0.5,
                tail_price_spec_type="t%",
                amount_type="qAt%p%",
            )
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=pair,
                        head_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="head",
                            client_order_id="CID-H",
                            exchange_order_id="OID-H",
                        ),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(event)
            self.running = False

        def record_targets_head(self, record) -> bool:
            return self.head_pair_state_for_record(record) is not None

        def head_pair_state_for_record(self, record) -> PairCycleState | None:
            return self.state.pairs["pair-a"]

    emitted = []
    source = KrakenPrivateOrderPollingSource(_Client(), poll_seconds=0.0)

    asyncio.run(source.pump(_Runtime()))

    assert emitted[0].reply["reference_price"] == 101.0
