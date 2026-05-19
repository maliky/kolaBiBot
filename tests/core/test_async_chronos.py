from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd

from kolabi.runtime.legacy.kola.async_chronos import AsyncChronos
from kolabi.shared.core.runtime_types import RuntimeEventKind


class _FakeCryptoApi:
    dummy = False
    dummyID = ""

    def __init__(self, exec_orders: list[dict[str, object]] | None = None) -> None:
        self._exec_orders = exec_orders or []

    def exec_orders(self) -> list[dict[str, object]]:
        return list(self._exec_orders)


class _FakeBargain:
    symbol = "PI_XBTUSD"

    def __init__(self, exec_orders: list[dict[str, object]] | None = None) -> None:
        self.crypto_api = _FakeCryptoApi(exec_orders)

    def prices(
        self,
        typeprice: str | None = None,
        side: str = "buy",
        symbol_: str | None = None,
        force_live: bool = False,
    ) -> float:
        del typeprice, side, symbol_, force_live
        return 100.0


def sample_load() -> dict[str, object]:
    return {
        "sender": object(),
        "timeOut": pd.Timedelta(1, unit="m"),
        "symbol": "PI_XBTUSD",
        "order": {
            "clOrdID": "mlk_CID-1",
            "ordType": "Limit",
            "side": "buy",
            "orderQty": 2,
            "price": 100.0,
            "execInst": "",
        },
    }


def test_async_chronos_handle_load_emits_request_and_ack(monkeypatch) -> None:
    async def scenario() -> None:
        recpt_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        valid_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        event_queue: asyncio.Queue[object] = asyncio.Queue()
        chronos = AsyncChronos(
            _FakeBargain(),
            recpt_queue,
            valid_queue,
            event_queue=event_queue,
        )

        async def fake_wait_for_change(**kwargs: object) -> None:
            del kwargs

        def fake_execute(
            brg: object,
            command: object,
            *,
            amend_absdelta: float,
        ) -> dict[str, object]:
            del brg, command, amend_absdelta
            return {"orderID": "OID-1", "clOrdID": "mlk_CID-1"}

        async def immediate_to_thread(func: object, *args: object, **kwargs: object) -> object:
            return func(*args, **kwargs)  # type: ignore[misc]

        monkeypatch.setattr(chronos, "wait_for_change", fake_wait_for_change)
        monkeypatch.setattr(
            "kolabi.runtime.legacy.kola.async_chronos.execute_runtime_command",
            fake_execute,
        )
        monkeypatch.setattr(
            "kolabi.runtime.legacy.kola.async_chronos.asyncio.to_thread",
            immediate_to_thread,
        )

        await chronos.handle_load(sample_load())  # type: ignore[arg-type]
        await asyncio.sleep(0)

        request_event = await event_queue.get()
        ack_event = await event_queue.get()
        reply = await chronos.reply_queue.get()

        assert request_event.kind == RuntimeEventKind.ORDER_REQUESTED
        assert request_event.order["ordType"] == "Limit"
        assert ack_event.kind == RuntimeEventKind.ORDER_ACK
        assert reply["orderID"] == "OID-1"

    asyncio.run(scenario())


def test_async_chronos_wait_for_change_emits_validation_event(monkeypatch) -> None:
    async def scenario() -> None:
        recpt_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        valid_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        event_queue: asyncio.Queue[object] = asyncio.Queue()
        chronos = AsyncChronos(
            _FakeBargain(),
            recpt_queue,
            valid_queue,
            event_queue=event_queue,
        )

        await chronos.reply_queue.put(
            {
                "orderID": "OID-1",
                "clOrdID": "mlk_CID-1",
                "ordStatus": "Filled",
                "execType": "Trade",
                "transactTime": "2026-05-19T00:00:00+00:00",
            }
        )

        def _sync_changed(*args: object, **kwargs: object) -> bool:
            del args, kwargs
            return True

        chronos.is_changed_ = _sync_changed  # type: ignore[method-assign]
        async def immediate_to_thread(func: object, *args: object, **kwargs: object) -> object:
            return func(*args, **kwargs)  # type: ignore[misc]

        monkeypatch.setattr(
            "kolabi.runtime.legacy.kola.async_chronos.asyncio.to_thread",
            immediate_to_thread,
        )
        await chronos.wait_for_change(
            valconditions=({"exectype": "Trade", "orderstatus": "Filled"},),
            rcvload=sample_load(),  # type: ignore[arg-type]
            timeout=pd.Timedelta(1, unit="m"),
            waitstep=0.01,
        )

        payload = await valid_queue.get()
        event = await event_queue.get()

        assert payload["execValidation"]["orderID"] == "OID-1"
        assert event.kind == RuntimeEventKind.ORDER_VALIDATED
        assert event.reply["ordStatus"] == "Filled"

    asyncio.run(scenario())
