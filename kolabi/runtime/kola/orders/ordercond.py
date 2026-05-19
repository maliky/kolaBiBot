# -*- coding: utf-8 -*-
"""Legacy conditioned-order interpreter.

Purpose: run one conditioned order thread, wait for condition truth, submit to
Chronos, and route broker validation replies back to the order owner.
Inputs: send/validation queues, `Condition`, and `OrderDict` payload.
Outputs: broker validation reply (`BrokerReply | bool`) and lifecycle logs.
Side effects: thread lifecycle, queue blocking IO, mutable order state.
Important types: `OrderDict`, `OrderLoad`, `ValidationLoad`, `BrokerReply`.
Role: interpreter shell.
Transitional: yes, queue protocol is legacy and intentionally preserved.
"""
from queue import Queue
from threading import Thread
from time import sleep
from typing import Optional

import pandas as pd
from kolabi.runtime.kola.orders.condition import Condition
from kolabi.runtime.kola.utils.datefunc import now, setdef_timedelta
from kolabi.runtime.kola.utils.general import compteur, trim_dic
from kolabi.runtime.kola.utils.logfunc import (
    get_logfunc,
    get_logger,
    throttled_log,
)
from kolabi.runtime.kola.utils.orderfunc import (
    get_order_from,
    newClID,
    remove_execInst,
    toggle_order,
)
from kolabi.shared.core.runtime_types import (
    BrokerReply,
    OrderDict,
    OrderLoad,
    ValidationLoad,
)


class OrderConditionned(Thread):
    # Un thread qui court pour un temps donné.
    # Il lance un ordre prédéfini si la condition associée est validée.
    def __init__(
        self,
        send_queue: Queue,
        order: OrderDict,
        cond: Condition,
        valid_queue: Optional[Queue] = None,
        logName_: Optional[str] = __name__,
        logLevel_: str = "INFO",
        nameT: Optional[str] = None,
        timeout=None,
        symbol: str = "XBTUSD",
    ) -> None:
        """
        Une queue, pour passer les ordres, un ordre à passer si la condition est validée.
        - l'ordre (order) peut être stopé prématurement en mettant stop=True
        - order est un dict avec keys: side, orderQty..
        - un timeout pendant lequel l'ordre (dont l'évaluation de sa condition) est actif
        def 2 jours
        - hook : nom du hook, ou de l'abbrevation qui sert de hook.
        - sLL= debug level
        -symbol: symbol for this order, def. XBTUSD
        """
        Thread.__init__(self, name=nameT)

        self.cpt_call = compteur()
        self.logLevel = logLevel_
        _logName = __name__ if logName_ is None else logName_
        self.logger = get_logger(sLL=self.logLevel, name=_logName)
        self.symbol = symbol
        self.send_queue: Queue = send_queue
        self.valid_queue: Optional[Queue] = valid_queue
        self._canceled = False
        self.condition: Condition = cond
        self.stop = False
        self.orderIDPrefix = "mlk_"
        self.order: OrderDict = (
            order  # a dict ex. {'side': 'buy', 'orderQty': 100, 'options'...}
        )
        self.oclid = newClID(abbv_=nameT)
        self.order["clOrdID"] = self.oclid

        # default for timeOut (2 days)
        # could add a timecond eg cVraieTpsDiffA(timeOut.seconds or delta)
        self.timeOut = setdef_timedelta(timeout, pd.Timedelta(2, unit="D"))
        self.startTime = now()

        self.logger.debug(f"#### Init fini for {self.__repr__(short=False)}")

    def __repr__(self, short: bool = True, suffix: str = "----") -> str:
        """Représenation."""
        rep = f"{suffix}OrderCond: {self.oclid[:10]}"
        if not short:
            rep += "\n" + f"----Détails: {self.order}"
            rep += "\n" + f"----{self.condition.__repr__(short)}"
        return rep

    def elapsed_time(self):
        """Return the elapsed time in seconds."""
        return now() - self.startTime

    def timed_out(self) -> bool:
        """
        Say if ordre timed_out or not.

        Deux raisons peuvent le time-out.
        1) Il y a une condition de temps qui ne peut plus être vraie,
        2) Le paramètre timeOut de l'orderCondition est arrivé à expiration
        """
        return (self.elapsed_time() >= self.timeOut) or self.condition.timed_out()

    def canceled(self) -> bool:
        return self._canceled

    def add_condition(self, condition: Condition) -> None:
        """Ajoute une ou des conditions à la condition existante."""
        self.condition.add_condition(condition)

    def run(self):
        """Run until truth exit condition."""
        self.logger.info(f"#### Starting {self.__repr__(False)}")

        execValidation: BrokerReply | bool | dict = {}
        while not self.stop and not self.timed_out():

            if self.condition.is_(True) or (self.order["ordType"] != "Market"):
                # Envoi l'ordre à chronos qui gère le suivi de la bonne execution
                condition_sortie = (
                    self.condition
                    if self.condition.is_(True)
                    else f'Immediately placing a {self.order["ordType"]} Order'
                )
                self.logger.info(f"Déclenchement {self} '{condition_sortie}'")
                execValidation = self.send_order()
                if isinstance(execValidation, dict):
                    if execValidation.get("ordStatus", False) == "Canceled":
                        self._canceled = True
                break

            # sleep(2+randint(5))  # mitigate rate limite
            sleep(1.05)

        reason = self.explain()

        msg = f'"{reason}" & 1st execValidation={trim_dic(execValidation, trimid=12)}.'
        # at order leave do not close the order if finished.

        self.finalise(close=False, reason=msg)

        # on renvois les informations sur cet ordre pour les chained orders
        return execValidation

    def get_logfunc(self, level_="INFO"):
        """Return the object logger with level_."""
        return get_logfunc(self.logger, level_)

    def log(self, msg_, level_=None, one_in_: int = 10):
        """a throttled log a message after one_in_ self.call"""
        _level = self.logLevel if level_ is None else level_
        log_func = self.get_logfunc(_level)
        i = self.cpt_call()
        return throttled_log(i, log_func, msg_, one_in_)

    def explain(self) -> str:
        """Find reason."""
        reason = ""
        if self.stop:
            reason = "Stop received"
        elif self.condition.is_(True):
            reason = f"{self.condition}"
        elif self.timed_out():
            reason = f"Timed_out: timeout={self.timeOut} while elapsed_time={self.elapsed_time()}"
            rep = self.cancel_order()
            reason += f"\n---- Détails: {rep}"
        elif self.order["ordType"] in ["Limit", "Stop"]:
            # ? pourquoi ce test
            reason = "Self is Limit or Stop order"
        elif self._canceled:
            reason = "Self has been canceled on exec time"

        return reason

    def cancel_order(self):
        """Envois une demande pour annuler l'ordre en cours via chronos."""
        load, _order = self.get_load()
        _order["ordType"] = "cancel"
        self.send_queue.put(load)
        return self.wait_for_broker_reply()

    def get_load(self, order: Optional[OrderDict] = None) -> tuple[OrderLoad, OrderDict]:
        """Set the default load pour this order."""
        # un identifiant pour le suivi
        assert self.symbol is not None, f"order={order}"
        load: OrderLoad = {
            "sender": self,
            "timeOut": self.timeOut,
            "symbol": self.symbol,
            "order": {},
        }
        _order = order if order else self.order
        load["order"] = _order
        return load, _order

    def send_order(self, order: Optional[OrderDict] = None):
        """
        Passe l'ordre au serveur chronos chargé de le faire executer.

        l'ordre et d'en vérifier la validation.
        """
        load, _order = self.get_load(order)

        ordType = _order.get("ordType", None)
        assert ordType, f"Should have an order Type here but order={order}"

        # check the execInst:
        execInst = _order.get("execInst", None)
        if execInst:
            _order["execInst"] = remove_execInst(execInst, "lastMidPrice")

        self.logger.debug(f"Envoi à Chronos du load={load}")
        self.send_queue.put(load)

        return self.wait_for_broker_reply()

    def wait_for_broker_reply(self) -> BrokerReply | bool:
        """
        Wait for the borker reply.

        Should only get a validated orders but if we get error we could cancel."""
        self.logger.debug(f"{self} waiting for validation")

        # Ce routage est fragile en concurrence; cette passe le documente sans le modifier.
        assert self.valid_queue is not None, "valid_queue doit etre definie pour attendre la validation"
        while True:
            rcvLoad: ValidationLoad = self.valid_queue.get(block=True)
            execValidation = rcvLoad["execValidation"]
            try:
                order = get_order_from(rcvLoad["exgLoad"])
            except Exception as e:
                self.logger.error(f"{self}, rcvLoad={rcvLoad}")
                raise (e)

            if self.oclid == order["clOrdID"]:
                self.logger.debug(
                    f"_Validation {bool(execValidation)}_ for order={order},"
                )
                # Normalement execValidation est une reply
                return execValidation
            else:
                # received something that's not for us.  replace in queue
                self.valid_queue.put(rcvLoad)
                sleep(0.1)

    def finalise(self, close: bool = False, reason: Optional[str] = None) -> None:
        """Finalise somme values depending on reason."""
        reason = f"{self}" if reason is None else f"{reason}"
        # the closing order.  Will reduce only.. Attention si execInst dans order
        if close:
            closing_order = {
                "side": toggle_order(self.order),
                "execInst": "Close",
                "ordType": "Market",
                "ordQty": None,
            }
            self.send_order({"order": closing_order})
            reason += " ... with order closing."

        if self.canceled():
            reason += ">>>> IS CANCELED <<<<"

        self.logger.info(f"with {reason}")
