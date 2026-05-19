from __future__ import annotations

import asyncio
import pickle
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from kolabi.runtime.legacy.kola.chronos import Chronos
from kolabi.runtime.legacy.kola.orders.trailstop import TrailStop
from kolabi.runtime.legacy.kola.utils.constantes import PRICE_PRECISION
from kolabi.runtime.legacy.kola.utils.datefunc import now, setdef_timedelta
from kolabi.runtime.legacy.kola.utils.general import opt_pop_if_in_
from kolabi.runtime.legacy.kola.utils.logfunc import get_logger
from kolabi.runtime.legacy.kola.utils.orderfunc import get_order_from, normalize_order_dict
from kolabi.shared.core.runtime_commands import (
    execute_runtime_command,
    runtime_command_from_order,
    timeout_override_minutes_for,
    validation_conditions_for,
)
from kolabi.shared.core.runtime_types import (
    BrokerReply,
    OrderLoad,
    RuntimeCommand,
    RuntimeEvent,
    RuntimeEventKind,
    Symbol,
    ValidationCondition,
    ValidationLoad,
)


class AsyncChronos:
    """Async interpreter over the typed runtime command/event surface.

    It accepts the same legacy order payload syntax as Chronos:
    ``ordType``, ``orderQty``, ``clOrdID``, ``stopPx`` and friends.
    Blocking broker interactions stay in the legacy layer and are executed via
    ``asyncio.to_thread`` so the queue protocol remains stable.
    """

    def __init__(
        self,
        brg: object,
        recpt_queue: asyncio.Queue[OrderLoad | None],
        valid_queue: asyncio.Queue[ValidationLoad] | None = None,
        *,
        event_queue: asyncio.Queue[RuntimeEvent] | None = None,
        logger: Any = None,
        nameT: str = "achrsT",
    ) -> None:
        self.brg = brg
        self.recpt_queue = recpt_queue
        self.valid_queue = valid_queue if valid_queue is not None else asyncio.Queue()
        self.event_queue = event_queue if event_queue is not None else asyncio.Queue()
        self.reply_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.name = nameT
        self._stop_requested = False
        self._validation_tasks: set[asyncio.Task[None]] = set()
        self.logger = get_logger(logger, name=__name__, sLL="INFO")

        self.logger.info(f"Fini init {self}")

    def __repr__(self) -> str:
        queues = {
            "reception": self.recpt_queue,
            "validation": self.valid_queue,
            "reply": self.reply_queue,
            "events": self.event_queue,
        }
        return f"AsyncChronos interpreter, using queues {queues}"

    async def request_stop(self) -> None:
        self._stop_requested = True
        await self.recpt_queue.put(None)

    async def run(self) -> None:
        self.logger.info("AsyncChronos started...")
        while not self._stop_requested:
            self.logger.info("AsyncChronos en ecoute...")
            rcvLoad = await self.recpt_queue.get()
            if rcvLoad is None:
                break
            rcvOrder: dict[str, Any] = {}
            ordType = ""
            try:
                rcvOrder = normalize_order_dict(
                    pickle.loads(pickle.dumps(rcvLoad["order"]))
                )
                ordType = str(rcvOrder.get("ordType", ""))
                await self.handle_load(rcvLoad)
            except Exception as exc:
                await self.emit_event(
                    RuntimeEventKind.ERROR,
                    symbol=rcvLoad["symbol"],
                    order=rcvOrder or rcvLoad["order"],
                    note=str(exc),
                )
                self.logger.exception(f"Unknown exception. RcvOrder={rcvOrder}")
                if ordType.startswith("amend"):
                    await self.valid_queue.put(
                        {
                            "brokerReply": False,
                            "exgLoad": rcvLoad,
                            "execValidation": False,
                        }
                    )
                else:
                    raise

    async def handle_load(self, rcvLoad: OrderLoad) -> None:
        sender = rcvLoad["sender"]
        timeout = rcvLoad["timeOut"]
        symbol = rcvLoad["symbol"]

        rcvOrder = normalize_order_dict(
            pickle.loads(pickle.dumps(rcvLoad["order"]))
        )
        ordType = rcvOrder.get("ordType")
        assert ordType, f"Should have an ordType in rcvOrder but {rcvOrder}"

        command = self.build_runtime_command(rcvOrder, symbol)
        await self.emit_event(
            RuntimeEventKind.ORDER_REQUESTED,
            symbol=symbol,
            order=command.order,
            note=command.reason,
        )

        minutes_override = timeout_override_minutes_for(command)
        if minutes_override is not None:
            timeout = pd.Timedelta(minutes_override, unit="m")

        reply = await asyncio.to_thread(
            execute_runtime_command,
            self.brg,
            command,
            amend_absdelta=PRICE_PRECISION.get(symbol, 1),
        )
        await self.reply_queue.put(reply)
        await self.emit_event(
            RuntimeEventKind.ORDER_ACK,
            symbol=symbol,
            order=command.order,
            reply=reply if isinstance(reply, dict) else None,
            note=str(command.kind.value),
        )

        valconditions = validation_conditions_for(
            command,
            trailstop_sender=isinstance(sender, TrailStop),
        )
        task = asyncio.create_task(
            self.wait_for_change(
                valconditions=valconditions,
                rcvload=rcvLoad,
                timeout=timeout,
                waitstep=0.1,
            )
        )
        self._validation_tasks.add(task)
        task.add_done_callback(self._validation_tasks.discard)

    async def wait_for_reply(
        self,
        *,
        block: bool = True,
        timeout: float | None = None,
    ) -> Any:
        try:
            if not block:
                return self.reply_queue.get_nowait()
            if timeout is None:
                return await self.reply_queue.get()
            if timeout <= 0:
                return None
            return await asyncio.wait_for(self.reply_queue.get(), timeout)
        except asyncio.QueueEmpty:
            return None
        except asyncio.TimeoutError:
            self.logger.error("reply timeout=%s block=%s", timeout, block)
            return None

    async def wait_for_change(
        self,
        *,
        valconditions: tuple[ValidationCondition, ...] | None = None,
        rcvload: OrderLoad,
        timeout: pd.Timedelta | None = None,
        waitstep: float = 1.0,
    ) -> None:
        self.logger.debug("Async validation task started.")
        timeOut = setdef_timedelta(timeout, default=pd.Timedelta(60, unit="m"))
        clOrdID = self.get_ID_from(rcvload, "clOrdID")
        startTime = now()

        def update_timeleft(
            timeout_: pd.Timedelta = timeOut,
            starttime: pd.Timestamp = startTime,
        ) -> float:
            remaining = (timeout_ + starttime - now()).total_seconds()
            return remaining if remaining > 0 else 0.0

        timeLeft = update_timeleft()
        while timeLeft > 0:
            changed = await asyncio.to_thread(
                self.is_changed_,
                clOrdID,
                valconditions,
                False,
            )
            if changed:
                break
            await asyncio.sleep(waitstep)
            timeLeft = update_timeleft()

        reply = await self.wait_for_reply(timeout=timeLeft)
        if rcvload["order"]["ordType"].lower() == "cancel":
            replyID = rcvload["order"]["clOrdID"]
        elif reply is None:
            replyID = rcvload["order"]["clOrdID"]
        else:
            replyID = self.get_ID_from(reply, "clOrdID")

        seenReplyIDs: dict[Any, int] = {}
        while replyID != clOrdID and timeLeft > 0:
            if replyID is not None:
                seenReplyIDs[replyID] = seenReplyIDs.get(replyID, 0) + 1
            if reply:
                await self.reply_queue.put(reply)
            reply = await self.wait_for_reply(timeout=timeLeft)
            replyID = clOrdID if reply is None else self.get_ID_from(reply, "clOrdID")
            timeLeft = update_timeleft()

        if timeLeft and reply is not None and not reply.get("error", False):
            validation = reply
        else:
            validation = False

        payload: ValidationLoad = {
            "brokerReply": reply,
            "exgLoad": rcvload,
            "execValidation": validation,
        }
        await self.valid_queue.put(payload)
        await self.emit_event(
            RuntimeEventKind.ORDER_VALIDATED,
            symbol=rcvload["symbol"],
            order=rcvload["order"],
            reply=reply if isinstance(reply, dict) else None,
            note="validated" if validation else "not_validated",
        )

    async def emit_event(
        self,
        kind: RuntimeEventKind,
        *,
        symbol: str,
        order: dict[str, Any] | None = None,
        reply: BrokerReply | None = None,
        note: str | None = None,
    ) -> None:
        await self.event_queue.put(
            RuntimeEvent(
                kind=kind,
                at=datetime.now(timezone.utc),
                symbol=Symbol(symbol),
                order=order,
                reply=reply,
                note=note,
            )
        )

    def build_runtime_command(
        self,
        rcvOrder: dict[str, Any],
        symbol: str,
    ) -> RuntimeCommand:
        prepared = dict(rcvOrder)
        ordType = str(prepared["ordType"])

        if ordType == "cancel" or ordType.startswith("amend"):
            return runtime_command_from_order(symbol=symbol, order=prepared)

        side = str(prepared["side"])
        execInst = str(prepared.get("execInst", ""))
        price = self.pop_price_from_(prepared, side, execInst, symbol)
        stopPx = self.pop_stopPx_from_(prepared, price, side, ordType, symbol)

        if ordType == "Market":
            prepared["execInst"] = opt_pop_if_in_(
                "price", str(prepared.get("execInst", ""))
            )
        elif ordType == "Limit":
            prepared["execInst"] = opt_pop_if_in_(
                "price", str(prepared.get("execInst", ""))
            )
            prepared["price"] = price
        elif ordType in {"Stop", "MarketIfTouched"}:
            prepared["stopPx"] = stopPx
        elif ordType in {"StopLimit", "LimitIfTouched"}:
            prepared["price"] = price
            prepared["stopPx"] = stopPx

        return runtime_command_from_order(symbol=symbol, order=prepared)

    def get_ID_from(self, rcvOrder: object, idType: str = "clOrdID") -> Any:
        assert rcvOrder is not None, f"rcvOrder={rcvOrder} should not be None."
        return get_order_from(rcvOrder).get(idType)

    def is_changed_(
        self,
        ID: object,
        valconditions: tuple[ValidationCondition, ...] | None = None,
        validateCancel: bool = True,
    ) -> bool:
        statusType: dict[str, object] = {}
        conditions = valconditions or ({"exectype": "New", "orderstatus": "New"},)
        for dic in conditions:
            exectype = dic["exectype"]
            orderstatus = dic["orderstatus"]
            statusType[f"is_{exectype}-{orderstatus}"] = self.ID_type_status_exec(
                ID,
                exectype,
                orderstatus,
            )
        if validateCancel:
            statusType["is_canceled"] = self.ID_type_status_exec(
                ID,
                "Canceled",
                "Canceled",
            )
        return any(statusType.values())

    def ID_type_status_exec(
        self,
        ID: object,
        exectype: str = "New",
        orderstatus: str = "New",
    ) -> dict[str, Any]:
        execOrders = [
            order
            for order in self.brg.crypto_api.exec_orders()
            if order["execType"] == exectype
        ]
        ordWstatus = [
            order
            for order in execOrders
            if order["ordStatus"] == orderstatus and order["ordStatus"] != "PartiallyFilled"
        ]
        dummy = getattr(self.brg.crypto_api, "dummy", False)
        if dummy:
            ID = self.brg.crypto_api.dummyID
        oidWstatus = [order for order in ordWstatus if ID in [order["orderID"], order["clOrdID"]]]
        if len(oidWstatus) == 0:
            return {}
        if len(oidWstatus) == 1:
            return oidWstatus[0]
        return self._latest(oidWstatus)

    @staticmethod
    def _latest(ordList: list[dict[str, Any]]) -> dict[str, Any]:
        return sorted(ordList, key=lambda order: order["transactTime"])[-1]

    def pop_price_from_(
        self,
        rcvOrder: dict[str, Any],
        side: str,
        execInst: str,
        symbol: str | None = None,
    ) -> Any:
        if "price" in rcvOrder:
            return rcvOrder.pop("price")
        return Chronos.pop_price_from_(self, rcvOrder, side, execInst, symbol)

    def pop_stopPx_from_(
        self,
        rcvOrder: dict[str, Any],
        price: Any,
        side: str,
        ordtype: str,
        symbol: str | None = None,
    ) -> Any:
        return Chronos.pop_stopPx_from_(self, rcvOrder, price, side, ordtype, symbol)


__all__ = ["AsyncChronos"]
