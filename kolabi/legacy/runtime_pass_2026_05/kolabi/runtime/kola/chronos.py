# -*- coding: utf-8 -*-
# mypy: ignore-errors
"""Legacy Chronos order interpreter shell.

Purpose: consume order loads from queues, submit/amend/cancel through exchange
helpers, and confirm execution transitions.
Inputs: `OrderLoad` messages, broker runtime state, validation conditions.
Outputs: broker replies and validation payloads pushed to queues.
Side effects: thread lifecycle, blocking queue IO, exchange calls, logging.
Important types: `OrderLoad`, `RuntimeCommand`, `ValidationCondition`,
`BrokerReply`.
Role: interpreter shell.
Transitional: yes, public method names are migrated but legacy class shape
remains for compatibility.
"""
import pickle
import threading
from queue import Empty, Queue
from time import sleep
from typing import Any, Dict, Optional

# from kolabi.runtime.kola.orders import orders
import pandas as pd

import kolabi.runtime.kola.utils.exceptions as ke
from kolabi.runtime.kola.orders.orders import (
    get_execPrice,
)
from kolabi.runtime.kola.orders.trailstop import TrailStop
from kolabi.runtime.kola.utils.constantes import PRICE_PRECISION
from kolabi.runtime.kola.utils.datefunc import now, setdef_timedelta
from kolabi.runtime.kola.utils.general import (
    contains,
    opt_pop_if_in_,
    sort_dic_list,
    trim_dic,
)
from kolabi.runtime.kola.utils.logfunc import get_logger
from kolabi.runtime.kola.utils.orderfunc import (
    get_order_from,
    normalize_order_dict,
)
from kolabi.runtime.kola.utils.pricefunc import setdef_stopPrice
from kolabi.runtime.kola.ogun_executor import execute_runtime_command
from kolabi.shared.core.runtime_commands import (
    runtime_command_from_order,
    timeout_override_minutes_for,
    validation_conditions_for,
)
from kolabi.shared.core.runtime_types import (
    BargainLike,
    BrokerReply,
    OrderDict,
    OrderLoad,
    RuntimeCommand,
    ValidationCondition,
)


class Chronos(threading.Thread):
    # Cet objet s'assure que les orders reçus sont bien exécutés.
    # et il sert d'interface à plusieur thread vers la même connexion

    def __init__(
        self,
        brg: BargainLike,
        recpt_queue: Queue,
        confirmation_queue: Optional[Queue] = None,
        valid_queue: Optional[Queue] = None,
        logger=None,
        nameT: str = "chrsT",
    ) -> None:
        """Un thread qui tourne jsuqu'à ce que stop soit vrai.
        utilise brg pour passer les orders reçu dans la queue.
        vérifie la queue chaque freq secondes"""
        threading.Thread.__init__(self, name=nameT)
        self.brg: BargainLike = brg
        self.recpt_queue = recpt_queue
        self.confirmation_queue = confirmation_queue if confirmation_queue is not None else valid_queue
        self.submission_ack_queue: Queue = Queue()
        self.stop = False
        self.logger = get_logger(logger, name=__name__, sLL="INFO")

        self.logger.info(f"Fini init {self}")

    @property
    def valid_queue(self) -> Optional[Queue]:
        return self.confirmation_queue

    @property
    def reply_queue(self) -> Queue:
        return self.submission_ack_queue

    def __repr__(self) -> str:
        queues = {
            "reception": self.recpt_queue,
            "validation": self.confirmation_queue,
            "reply": self.submission_ack_queue,
        }
        rep = f"Chronos thread, using queues {queues}"
        return rep

    def run(self) -> None:
        """Tourne jusqu'à ce que stop soit mis en faute."""
        self.logger.info("Chronos started...")

        while not self.stop:
            self.logger.info("Chronos en écoute...")

            # on bloque le thread
            # attend ordre et oid associés qui arrive dans cette queue
            rcvLoad: OrderLoad = self.recpt_queue.get(block=True)
            self.logger.debug(f"Chronos received load: {rcvLoad}")
            rcvOrder = {}
            ordType = ""
            try:
                rcvOrder = normalize_order_dict(
                    pickle.loads(pickle.dumps(rcvLoad["order"]))
                )
                ordType = str(rcvOrder.get("ordType", ""))
                self.handle_load(rcvLoad)

            except KeyError as k:
                self.logger.exception(f"rcvOrder={rcvOrder} et rcvLoad={rcvLoad}")
                raise k

            except (ke.InvalidOrdStatus, ke.InvalidOrderID) as e:
                if ordType.startswith("amend"):
                    self.logger.error("Amending failed.  No validation!")
                    assert self.confirmation_queue is not None, "confirmation_queue doit etre definie"
                    self.confirmation_queue.put(
                        {
                            "brokerReply": False,
                            "exgLoad": rcvLoad,
                            "execValidation": False,
                        }
                    )
                else:
                    raise (e)

            except ke.InvalidOrderQty:
                self.logger.error("Canceling order and closing the essai.")
                assert self.confirmation_queue is not None, "confirmation_queue doit etre definie"
                self.confirmation_queue.put(
                    {"brokerReply": False, "exgLoad": rcvLoad, "execValidation": False}
                )

            except ke.InsufficientBalance:
                self.logger.error("Insufficient Balance, Closing the essai.")
                # we do so because to keep consistency with attached stop tail
                self.logger.warning(f"Replacing 80% of the rcvLoad {rcvLoad}")
                # attention chronos pourrait traiter un autre ordre
                # que celui générant l'erreur, non ?
                sender = rcvLoad["sender"]
                overQty = sender.order.get("orderQty", sender.order.get("quantity", 0))
                reducedQty = round(overQty * 0.8)
                if reducedQty < 31:
                    self.logger.exception("Canceling order.  Closing the essai?")
                    assert self.confirmation_queue is not None, "confirmation_queue doit etre definie"
                    self.confirmation_queue.put(
                        {
                            "brokerReply": False,
                            "exgLoad": rcvLoad,
                            "execValidation": False,
                        }
                    )
                else:
                    if "orderQty" in sender.order:
                        sender.order["orderQty"] = reducedQty
                    else:
                        sender.order["quantity"] = reducedQty
                    sender.send_order()

            except ke.InvalidOrder as io:
                self.logger.error(f"Invalid order? {rcvOrder}")
                self.log_reply()
                raise io

            except Exception as e:
                # Si on arrive ici il y a probablement un gros pb de connexion
                # que faire ?
                # vraisemblablement d'un pique au niveau de bitmex avec affluence
                # if no money handle
                self.logger.exception(f"Unknown exception. RdvOrder={rcvOrder}")
                self.log_reply()
                raise e

    def handle_load(self, rcvLoad: OrderLoad) -> None:
        sender = rcvLoad["sender"]
        timeOut = rcvLoad["timeOut"]
        symbol = rcvLoad["symbol"]

        # make a deep copy of the order to avoid changing rcvLoad
        rcvOrder = pickle.loads(pickle.dumps(rcvLoad["order"]))
        rcvOrder = normalize_order_dict(rcvOrder)
        ordType = rcvOrder.get("ordType", None)
        assert ordType, f"Should have an ordType in rcvOrder but {rcvOrder}"

        command = self.build_runtime_command(rcvOrder, symbol)
        minutes_override = timeout_override_minutes_for(command)
        if minutes_override is not None:
            timeOut = pd.Timedelta(minutes_override, unit="m")

        self.submission_ack_queue.put(
            execute_runtime_command(
                self.brg,
                command,
                amend_absdelta=PRICE_PRECISION.get(symbol, 1),
            )
        )
        valconditions = validation_conditions_for(
            command,
            trailstop_sender=isinstance(sender, TrailStop),
        )
        kwargs = {
            "timeout": timeOut,
            "rcvload": rcvLoad,
            "waitstep": 0.1,
            "valconditions": valconditions,
        }
        threadName = f'VT-{rcvLoad["order"]["clOrdID"].replace("mlk_", "")[:10]}'
        self.logger.info(f"{threadName} check validation avec {kwargs}")
        threading.Thread(
            target=self.confirm_pair_transition,
            name=threadName,
            kwargs=kwargs,
        ).start()

    def build_runtime_command(
        self,
        rcvOrder: dict[str, Any],
        symbol: str,
    ) -> RuntimeCommand:
        prepared = dict(rcvOrder)
        ordType = str(prepared["ordType"])

        if ordType in {"cancel"}:
            return runtime_command_from_order(
                symbol=symbol,
                order=prepared,
            )

        side = str(prepared["side"])
        execInst = str(prepared.get("execInst", ""))

        if ordType.startswith("amend"):
            return runtime_command_from_order(
                symbol=symbol,
                order=prepared,
            )

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

    def log_reply(self, absMsg: str = "No reply available") -> None:
        """Log the reply if available"""
        reply = self.await_submission_ack(block=False)
        if reply is None:
            self.logger.error(absMsg)
        else:
            self.logger.error(f"Reply={trim_dic(reply, trimid=12)}")
            self.submission_ack_queue.put(reply)

    # @log_args(level='DEBUG')
    def await_submission_ack(
        self,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> Optional[BrokerReply]:
        """Get the reply from the queue, return None if timeout reached."""
        reply: Optional[BrokerReply] = None
        try:
            # if block is false and nothing in queue raise queue.Empty
            reply = self.submission_ack_queue.get(block, timeout)
            if block and timeout <= 0:
                self.logger.debug(
                    f"timeout={bool(timeout)} and block={block} while "
                    f"reply={reply, type(reply)}"
                )

            if isinstance(reply, list):
                assert len(reply) <= 1, (
                    f"Reply is too long. Que choisir ?"
                    f"Reply={trim_dic(reply, trimid=12)}"
                )
                reply = reply[0]

            return reply
        except Empty:
            self.logger.error(
                f"reply={trim_dic(reply, trimid=12) if reply is not None else 'reply is None'},"
                f" timeout={timeout}, block={block}"
            )
            return None

    def extract_client_or_exchange_id(self, rcvOrder, idType: str = "clOrdID") -> str | None:
        """Return the ID from the rcvOrder (a dict containing an order)."""
        assert rcvOrder is not None, f"rcvOrder={rcvOrder} should not be None."
        try:
            return get_order_from(rcvOrder).get(idType)
        except Exception as e:
            self.logger.error(f"rcvOrder, idType={rcvOrder, idType}")
            raise (e)

    def confirm_pair_transition(
        self,
        valconditions: Optional[list[ValidationCondition]] = None,
        rcvload: Optional[OrderLoad] = None,
        timeout: Optional[pd.Timedelta] = None,
        waitstep: float = 1,
    ) -> None:
        """
        Boucle qui attend de recevoir dans order execution from ws.

        ordertype: orderstatus.
        - timeout doit être un pd.Timedelta,
        - waitStep en second
        ordertype is 'ordStatus our triggered et ordstatus ??
        """
        self.logger.debug("Thread started.")

        # defaults
        timeOut = setdef_timedelta(timeout, default=pd.Timedelta(60, unit="m"))
        conds = (
            valconditions
            if valconditions is not None
            else [{"exectype": "New", "orderstatus": "New"}]
        )
        assert rcvload is not None, "rcvload doit etre defini"

        clOrdID = (
            self.brg.crypto_api.dummyID
            if self.brg.crypto_api.dummy
            else self.extract_client_or_exchange_id(rcvload, "clOrdID")
        )

        startTime = now()

        def update_timeleft(timeout=timeOut, starttime=startTime, now_=None):
            """
            Update time left

            -now_: is sometime set by default.
            """
            _now = now_ if now_ else now()
            # convert to seconds +/-
            _timeleft = (timeout + starttime - _now).total_seconds()

            return _timeleft if _timeleft > 0 else 0

        timeLeft = update_timeleft()

        while timeLeft > 0 and not self.matches_confirmation_rule(
            clOrdID, conds, validateCancel=False
        ):
            # #### matches_confirmation_rule important !
            # validating cancel enable resubminting orders ?

            sleep(waitstep)
            timeLeft = update_timeleft()
            if timeLeft % 298 == 0:
                # logging every 4:58
                self.logger.info(f"timeLeft={timeLeft}s. Still waiting...")
                sleep(1)  # avoid too much logging and throwtlle the system

        # block until next reply
        reply = self.await_submission_ack(block=True, timeout=timeLeft)

        # problème avec les reply None

        if rcvload["order"]["ordType"].lower() == "cancel":
            self.logger.info(f"*ordType is canceled* rcvload={rcvload}")
            replyID = rcvload["order"]["clOrdID"]
        elif reply is None:
            # mais si ce n'est pas la bonne reply ? on va tester après
            replyID = rcvload["order"]["clOrdID"]
        else:
            replyID = self.extract_client_or_exchange_id(reply, "clOrdID")

        seenReplyIDs: Dict[Any, Any] = {}

        while replyID != clOrdID and timeLeft:
            # in case it's not the reply ID we are waiting for
            # saving ID already seen to avoid clutering logs
            if replyID is not None:
                seenReplyIDs[replyID] = seenReplyIDs.get(replyID, 0) + 1

            if seenReplyIDs.get(replyID, 0) < 2:
                self.logger.debug(
                    f"*No match ID* for clOrdID={clOrdID}"
                    f" in {trim_dic(reply, trimid=12)}!"
                )
            if reply:
                # to be sure not to put Nones back in the loop
                self.submission_ack_queue.put(reply)

            try:
                # can get None here, not good... if timeout..
                reply = self.await_submission_ack(timeout=timeLeft)
                replyID = (
                    clOrdID if reply is None else self.extract_client_or_exchange_id(reply, "clOrdID")
                )
            except Exception as e:
                self.logger.error(
                    f"reply={reply}, timeLeft={timeLeft}, replyID={replyID}"
                    f"clOrdID={clOrdID}"
                )
                raise (e)

            timeLeft = update_timeleft()

            self.logger.debug(
                f"Getting out of the loop. seenReplyIds={seenReplyIDs},"
                f" timeLeft={timeLeft}, reply={reply}."
            )

        # ici il y a en fait le cas des reply error d'ammending
        if timeLeft and reply is not None and not reply.get("error", False):
            validation = reply
        else:
            validation = False

        self.logger.info(
            f"_Attendu {timeOut - pd.Timedelta(timeLeft, unit='s')}_  "
            f"Validation for {replyID} is {bool(validation)}."
        )
        # self.logger.debug(f"Détail validation {validation} et reply")

        assert self.confirmation_queue is not None, "confirmation_queue doit etre definie"
        self.confirmation_queue.put(
            {"brokerReply": reply, "exgLoad": rcvload, "execValidation": validation}
        )

    def matches_confirmation_rule(
        self,
        ID,
        valconditions: Optional[list[ValidationCondition]] = None,
        validateCancel: bool = True,
    ) -> bool:
        """
        Return test if order with ID is exec or canceled and compare to val.

        handles a list of validation conditions.
        """
        statusType = {}
        conds = (
            valconditions
            if valconditions is not None
            else [{"exectype": "New", "orderstatus": "New"}]
        )
        for dic in conds:
            exectype, orderstatus = dic["exectype"], dic["orderstatus"]
            statusType[f"is_{exectype}-{orderstatus}"] = self.latest_execution_for_status(
                ID, exectype, orderstatus
            )

        # #### by default we handle cancel orders ####
        if validateCancel:
            # #### so cancel will act as condition validated
            statusType["is_canceled"] = self.latest_execution_for_status(
                ID, "Canceled", "Canceled"
            )

        # self.logger.debug(f'ID=set({ID[:10]}, statusType={statusType})')

        return any(statusType.values())

    #    @log_args(LOGNAME)
    def latest_execution_for_status(
        self,
        ID,
        exectype: str = "New",
        orderstatus: str = "New",
    ):
        """
        Renvois un order avec ID et dont le status type est status.

        Défault status Filled et statustype 'ordStatus, exectype default New
        """
        # self.logger.debug(f'ID={ID}, status={status}, statustype={statustype}')
        # on récupère les orders de type voulu
        execOrders = [
            o for o in self.brg.crypto_api.exec_orders() if o["execType"] == exectype
        ]
        ordWstatus = [
            o
            for o in execOrders
            if o["ordStatus"] == orderstatus and o["ordStatus"] != "PartiallyFilled"
        ]

        # logOrders = [trim_dic(o, trimid=12) for o in ordWstatus]
        # self.logger.debug(f'logOrders={logOrders}')

        # on gère le cas test avec dummy brg
        ID = self.brg.crypto_api.dummyID if self.brg.crypto_api.dummy else ID

        # On filtre les orders par ID
        oidWstatus = [o for o in ordWstatus if ID in [o["orderID"], o["clOrdID"]]]

        def latest(ordList):
            """given a list of orders return the one with latest transtime"""
            assert isinstance(ordList, list), f"{ordList} should be a list of orders"
            return sort_dic_list(ordList, "transactTime")

        if len(oidWstatus) == 0:
            return {}
        elif len(oidWstatus) == 1:
            return oidWstatus[0]
        else:
            self.logger.warning(
                f"{len(oidWstatus)} previous orders with ID={ID}. Returning latest only"
            )
            try:
                return latest(oidWstatus)
            except TypeError:
                self.logger.exception(f"Returning -1 of oidWstatus={oidWstatus},")
                return oidWstatus[-1]

    def pop_price_from_(
        self,
        rcvOrder: OrderDict,
        side: str,
        execInst: str,
        symbol: Optional[str] = None,
    ) -> float:
        """
        Get price from rcvOrder.

        else get the market price using execInst and side.
        By default get lastMidprice
        """
        if "price" in rcvOrder:
            return rcvOrder.pop("price")
        return get_execPrice(self.brg, side, {"execInst": execInst}, symbol)

    def pop_stopPx_from_(
        self,
        rcvOrder: OrderDict,
        price: float,
        side: str,
        ordtype: str,
        absdelta=None,
        symbol: Optional[str] = None,
    ) -> Optional[float]:
        """
        Pop the stopPx from the rcvOrder.

        Use class method to facilitate eventual logging.
        if stopPx not in rcvOrder,
        set de default stopPx based on price side, ordType and absdelta
        """
        # absdelta = PRICE_PRECISION.get(symbol,1) if absdelta is None else absdelta
        stopPx = rcvOrder.pop("stopPx", None)
        if stopPx is None:
            stopPx = rcvOrder.pop("stopPrice", None)
        if stopPx is None and contains(["Stop", "Touched"], ordtype):
            # probably not necessary as stop should be set
            # defaut to 2 for XBTUSD
            stopPx = setdef_stopPrice(
                entryPrice=price,
                side=side,
                ordtype=ordtype,
                absdelta=rcvOrder.pop("oDelta", PRICE_PRECISION[symbol]),
            )

        return stopPx

    # Backward-compatible aliases for legacy call sites.
    def wait_for_change(self, *args: Any, **kwargs: Any) -> None:
        self.confirm_pair_transition(*args, **kwargs)

    def wait_for_reply(
        self,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> Optional[BrokerReply]:
        return self.await_submission_ack(block=block, timeout=timeout)

    def get_ID_from(self, rcvOrder: object, idType: str = "clOrdID") -> str | None:
        return self.extract_client_or_exchange_id(rcvOrder, idType=idType)

    def is_changed_(
        self,
        ID: object,
        valconditions: Optional[list[ValidationCondition]] = None,
        validateCancel: bool = True,
    ) -> bool:
        return self.matches_confirmation_rule(ID, valconditions, validateCancel)

    def ID_type_status_exec(
        self,
        ID: object,
        exectype: str = "New",
        orderstatus: str = "New",
    ) -> object:
        return self.latest_execution_for_status(ID, exectype, orderstatus)
