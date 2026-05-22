from __future__ import annotations

from dataclasses import dataclass

import asyncio
import pytest

from kolabi.runtime.kola.ogun_executor import OgunExecutor, RetryPolicy
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    ExchangePort,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
)


@dataclass
class _Call:
    name: str
    pair_name: str


class _FakePort(ExchangePort):
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[_Call] = []
        self.fail_first = fail_first
        self._count = 0

    async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
        self._count += 1
        if self.fail_first and self._count == 1:
            raise RuntimeError("boom")
        self.calls.append(_Call("place_head", command.pair_name))
        return OrderAck(order_id="1", status="New")

    async def place_tail(self, command: PlaceTailCommand) -> OrderAck:
        self.calls.append(_Call("place_tail", command.pair_name))
        return OrderAck(order_id="2", status="New")

    async def amend_head(self, command: AmendHeadCommand) -> OrderAck:
        self.calls.append(_Call("amend_head", command.pair_name))
        return OrderAck(order_id="3", status="Replaced")

    async def amend_tail(self, command: AmendTailCommand) -> OrderAck:
        self.calls.append(_Call("amend_tail", command.pair_name))
        return OrderAck(order_id="4", status="Replaced")

    async def cancel(self, command: CancelCommand) -> OrderAck:
        self.calls.append(_Call("cancel", command.pair_name))
        return OrderAck(order_id="5", status="Canceled")


def _place_head() -> PlaceHeadCommand:
    return PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=1,
            price=100.0,
        ),
    )


def _place_tail() -> PlaceTailCommand:
    return PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderQty=1,
            stopPx=90.0,
        ),
    )


def _amend_head() -> AmendHeadCommand:
    return AmendHeadCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=AmendOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderID="OID-H",
            newPrice=101.0,
        ),
    )


def _amend_tail() -> AmendTailCommand:
    return AmendTailCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=AmendOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderID="OID-T",
            newPrice=89.0,
        ),
    )


def _cancel() -> CancelCommand:
    return CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=CancelOrderCommandRequest(pair_name="pair-a", clOrdID="CID-1"),
    )


def test_dispatch_place_head() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    ack = asyncio.run(executor.execute(_place_head()))
    assert ack.status == "New"
    assert port.calls[0].name == "place_head"


def test_dispatch_place_tail() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    asyncio.run(executor.execute(_place_tail()))
    assert port.calls[0].name == "place_tail"


def test_dispatch_amend_head() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    asyncio.run(executor.execute(_amend_head()))
    assert port.calls[0].name == "amend_head"


def test_dispatch_amend_tail() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    asyncio.run(executor.execute(_amend_tail()))
    assert port.calls[0].name == "amend_tail"


def test_dispatch_cancel() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    asyncio.run(executor.execute(_cancel()))
    assert port.calls[0].name == "cancel"


def test_retries_then_succeeds() -> None:
    port = _FakePort(fail_first=True)
    executor = OgunExecutor(port, retry_policy=RetryPolicy(attempts=2, base_delay_seconds=0.0))
    ack = asyncio.run(executor.execute(_place_head()))
    assert ack.status == "New"
    assert [call.name for call in port.calls] == ["place_head"]


def test_exhausted_retries_raises() -> None:
    class _AlwaysFailPort(_FakePort):
        async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
            raise RuntimeError("nope")

    executor = OgunExecutor(_AlwaysFailPort(), retry_policy=RetryPolicy(attempts=2, base_delay_seconds=0.0))
    with pytest.raises(RuntimeError, match="nope"):
        asyncio.run(executor.execute(_place_head()))
