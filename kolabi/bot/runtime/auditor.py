"""Active MarketAuditor interpreter shell.

Purpose: run the sacred head/tail activation loop used by `kolabi.bot`,
including order creation, condition activation, hook handling, and tail
management, without routing through `multi_kola`.
Inputs: runtime config, shared `Bargain` adapter, queue-backed `Chronos`,
strategy kwargs matching `BotService._spec_to_kwargs()`.
Outputs: queued exchange commands, in-memory balance snapshots, and lifecycle
logs for each strategy attempt.
Side effects: thread startup, exchange IO, queue IO, pandas state tracking, and
process-local timing pauses.
Important types: `ExchangeConfig`, `Bargain`, `Chronos`, `OrderConditionned`,
`HookOrder`, `TrailStop`.
Role: interpreter shell.
Transitional: yes, this keeps the historic `MarketAuditor.go()` contract while
removing the active dependency on `multi_kola`.
"""

from __future__ import annotations

from queue import Queue
from time import sleep
from typing import Any, Optional, Set, cast

import numpy as np
import pandas as pd

from kolabi.runtime.kola.chronos import Chronos
from kolabi.runtime.kola.orders.hookorder import HookOrder
from kolabi.runtime.kola.orders.ordercond import OrderConditionned
from kolabi.runtime.kola.orders.trailstop import TrailStop
from kolabi.runtime.kola.settings import ordStatusTrans
from kolabi.runtime.kola.utils.argfunc import price_type_trad, set_order_args
from kolabi.runtime.kola.utils.conditions import cHook, cVraiePrixDeA, cVraieTpsDeA
from kolabi.runtime.kola.utils.constantes import PRICE_PRECISION
from kolabi.runtime.kola.utils.datefunc import now
from kolabi.shared.config import ExchangeConfig, load_exchange_config
from kolabi.shared.core.bargain import Bargain
from kolabi.shared.core.runtime_types import BargainLike
from kolabi.shared.logging import setup_logging


class MarketAuditor:
    """Run one or more sacred head/tail order cycles on the active runtime path."""

    def __init__(
        self,
        *,
        exchange: str = "binance",
        symbol: str = "BTCUSDT",
        live: bool = False,
        dbo: Bargain | None = None,
        logger=None,
        config: Optional[ExchangeConfig] = None,
    ) -> None:
        self.live = live
        self.symbol = symbol
        self.trading_plateform = exchange
        self.dbo = dbo
        self._config = config
        self.logger = logger or setup_logging("INFO")
        self.stop = False
        self.hookedIDs: Set[str] = set()
        self.fileDattente: Queue[dict[str, Any]] | None = None
        self.fileDeConfirmation: Queue[dict[str, Any]] | None = None
        self.brg: Bargain | None = None
        self.chrs: Chronos | None = None
        self.resultats = pd.DataFrame(
            index=pd.DatetimeIndex(data=[], name="start_time"),
            columns=["balance", "benef"],
        )

    def __repr__(self) -> str:
        rep = (
            f"Live={self.live}-{self.symbol}({self.trading_plateform}), "
            f"log to {self.logger}"
        )
        if len(self.resultats):
            rep += f" Resultats={self.resultats}"
        return rep

    def start_server(self) -> None:
        """Instantiate the active shared `Bargain` and `Chronos` runtime services."""
        self.fileDattente = Queue()
        self.fileDeConfirmation = Queue()
        if self.dbo is not None:
            self.brg = self.dbo
        else:
            config = self._config or load_exchange_config(
                self.trading_plateform, symbol=self.symbol
            )
            self._config = config
            self.brg = Bargain(self.trading_plateform, config)
        self.chrs = Chronos(
            cast(BargainLike, self.brg),
            self.fileDattente,
            self.fileDeConfirmation,
            logger=self.logger,
        )
        self.chrs.start()
        try:
            self.resultats.loc[now(), :] = (self.balance(), np.nan)
        except ValueError:
            self.logger.warning("Unable to log initial balance for MarketAuditor")

    def stop_server(self) -> None:
        """Stop launching new attempts."""
        self.stop = True

    def go(
        self,
        tps_run,
        prix,
        essais: int,
        side: str,
        q,
        tp,
        atype: str,
        oType=False,
        nameT: Optional[str] = None,
        updatepause=None,
        logpause=None,
        dr_pause=None,
        tType=None,
        timeout=None,
        oDelta=None,
        tDelta=None,
        hook: Optional[str] = None,
    ) -> None:
        """Run the active sacred head/tail lifecycle for one strategy row."""
        if self.brg is None or self.fileDattente is None or self.fileDeConfirmation is None:
            raise RuntimeError("MarketAuditor.start_server() must run before go().")

        self.logger.debug(
            "#### Go with args :\n%s",
            {
                "tps_run": tps_run,
                "prix": prix,
                "essais": essais,
                "side": side,
                "q": q,
                "tp": tp,
                "atype": atype,
                "oType": oType,
                "tType": tType,
                "oDelta": oDelta,
                "tDelta": tDelta,
                "dr_pause": dr_pause,
                "timeout": f"{timeout}m",
                "balance": self.balance(),
            },
        )

        self.tpsDeb = now() + pd.Timedelta(tps_run[0], unit="m")
        self.tpsFin = now() + pd.Timedelta(tps_run[1], unit="m")
        repTpsDeb = self.tpsDeb.strftime("%Y-%m-%dT%H:%M")
        repTpsFin = self.tpsFin.strftime("%Y-%m-%dT%H:%M")

        if pd.isna(dr_pause):
            dr_pause = 0
            dr_moy = (self.tpsFin - self.tpsDeb) / essais
        else:
            dr_moy = pd.Timedelta(dr_pause, unit="m")
        dr_essai_theo = dr_moy + pd.Timedelta(30, unit="s")

        if pd.isna(timeout):
            timeOut = (self.tpsFin - self.tpsDeb) / essais
        else:
            timeOut = pd.Timedelta(timeout, unit="m")

        if pd.isna(oDelta):
            oDelta = PRICE_PRECISION[self.symbol]
        if pd.isna(tDelta):
            tDelta = PRICE_PRECISION[self.symbol]

        opType, ordType, execInst = price_type_trad(oType, side)
        tpType, tOrdType, tExecInst = price_type_trad(tType, side)

        i = 0
        while i < essais and not self.stop:
            self.tpsDebEssai = now()
            self.logger.info("oType=%s, tType=%s", (opType, ordType, execInst), (tpType, tOrdType, tExecInst))
            oPrices, _q, _tp, tailPrices = set_order_args(
                prix,
                q,
                tp,
                atype,
                self.brg,
                (opType, ordType, execInst),
                (tpType, tOrdType, tExecInst),
                recompute=True,
                side=side,
                symbol=self.symbol,
            )
            self.logger.info(
                "### Essais %s/%s, (%s):\n%s",
                i + 1,
                essais,
                nameT,
                {
                    "Balance": f"{self.balance()}$",
                    "tps_run": (repTpsDeb, repTpsFin),
                    "debut": self.tpsDebEssai,
                    "pause": dr_moy,
                    "hook": hook,
                    "oPrices": oPrices,
                    "tPrice": tailPrices,
                    "side": side,
                    "q": _q,
                    "tp": round(_tp, 4),
                    "oDelta": oDelta,
                    "tDelta": tDelta,
                    "opType": (opType, ordType, execInst),
                    "tpType": (tpType, tOrdType, tExecInst),
                    "timeOut": timeOut,
                },
            )
            self._run_attempt(
                nameT=nameT,
                side=side,
                hook=hook,
                essais=essais,
                index=i,
                dr_pause=dr_pause,
                dr_essai_theo=dr_essai_theo,
                opType=opType,
                ordType=ordType,
                execInst=execInst,
                order_qty=_q,
                oPrices=oPrices,
                oDelta=oDelta,
                tpType=tpType,
                tOrdType=tOrdType,
                tExecInst=tExecInst,
                _tp=_tp,
                timeOut=timeOut,
                updatepause=updatepause,
                logpause=logpause,
                tDelta=tDelta,
            )
            i += 1

        self.fin_des_essais(essais, close=False)

    def _run_attempt(
        self,
        *,
        nameT: Optional[str],
        side: str,
        hook: Optional[str],
        essais: int,
        index: int,
        dr_pause,
        dr_essai_theo,
        opType: str,
        ordType: str,
        execInst: str,
        order_qty,
        oPrices,
        oDelta,
        tpType: str,
        tOrdType: str,
        tExecInst: str,
        _tp,
        timeOut,
        updatepause,
        logpause,
        tDelta,
    ) -> None:
        assert self.brg is not None
        assert self.fileDattente is not None
        assert self.fileDeConfirmation is not None

        from kolabi.runtime.kola.utils.orderfunc import create_order

        order = create_order(
            side,
            order_qty,
            opType,
            ordType,
            execInst,
            oPrices,
            oDelta,
            min_qty=self.brg.minimum_order_quantity(),
        )
        kwargs: dict[str, Any] = {
            "send_queue": self.fileDattente,
            "order": order,
            "cond": cVraieTpsDeA(self.brg, self.tpsDeb, self.tpsFin),
            "valid_queue": self.fileDeConfirmation,
            "nameT": f"{nameT}-PO",
            "timeout": timeOut,
            "symbol": self.symbol,
        }
        if hook:
            hook_src, hook_status = hook.split("_")
            translated_status = ordStatusTrans[hook_status]
            ocp: OrderConditionned | HookOrder = HookOrder(
                hSrc=hook_src,
                hStatus=translated_status,
                excludeIDs_=self.hookedIDs,
                brg=cast(BargainLike, self.brg),
                **kwargs,
            )
            hook_condition = cHook(
                self.brg,
                hook_src,
                translated_status,
                exclIDs=self.hookedIDs,
            )
            ocp.add_condition(hook_condition)
            ocp.condition.set_excludeClIDs(tuple(self.hookedIDs))
        else:
            ocp = OrderConditionned(**kwargs)

        ocp.add_condition(cVraiePrixDeA(self.brg, opType, oPrices[0], oPrices[1]))
        oct_ = TrailStop(
            ocp,
            cast(BargainLike, self.brg),
            pegOffset_perc=_tp,
            updatepause=updatepause,
            logLevel_="INFO",
            logpause=logpause,
            nameT=f"{nameT}-SO",
            refPrice=tpType,
            execinst=tExecInst,
            ordtype=tOrdType,
            tDelta=tDelta,
        )
        try:
            oct_.start()
            oct_.join()
        except Exception as exc:
            self.logger.warning("#### Exception %s. Stopping -->\n%s", exc, oct_.condition)
            self.fin_des_essais("ERROR", close=False)
            return

        self.fin_essai(
            index,
            essais,
            oct_,
            close=False,
            dr_pause=dr_pause,
            dr_essai_theo=dr_essai_theo,
        )
        if hook:
            self.hookedIDs |= {ocp.condition.hookedSrcID}
            self.logger.info(">>>> Updating hookedIDs=%s", self.hookedIDs)

    def fin_essai(
        self,
        i: int,
        n: int,
        oct_,
        close: bool = False,
        dr_pause=None,
        dr_essai_theo=None,
    ) -> None:
        """Record the attempt result and sleep before the next attempt if needed."""
        del close
        self.resultats.loc[now(), "balance"] = self.balance()
        res_delta = self.resultats.iloc[-1] - self.resultats.iloc[-2]
        self.resultats.loc[self.resultats.index[-1], "benef"] = res_delta.loc["balance"]
        self.logger.info("\n\n#### Fin de l'essai %s/%s, Resultats:\n%s", i + 1, n, self.resultats.iloc[-1, :])
        if not oct_.main_oc.timed_out() and i + 1 < n:
            self.pause(dr_pause, dr_essai_theo)

    def balance(self) -> float:
        """Return the current USD balance from the active `Bargain` boundary."""
        assert self.brg is not None
        return float(self.brg.get_balance("usd"))

    def fin_des_essais(self, essais: int | str, close: bool = False) -> None:
        """Log end-of-run state without terminating the parent process."""
        del close
        self.logger.info(
            "\n\n################ Fin des %s essais:%s\n################ close and cancel ################\n\n",
            essais,
            self.resultats,
        )

    def pause(self, dr_pause, dr_essai_theo) -> None:
        """Sleep between attempts with the historic varying wait rule."""
        pause_seconds = 10 if dr_pause is None else dr_pause * 60
        try:
            dr_essai = now() - self.tpsDebEssai
            dr_delta = dr_essai - dr_essai_theo
            if dr_delta.seconds > 0:
                pause = pause_seconds
            else:
                pause = dr_delta.seconds + pause_seconds
            rnd_wait = np.floor(np.random.exponential(pause))
            sleep_window = pd.Timedelta(10 + rnd_wait, unit="s")
            self.logger.info(
                "Temps de l'essai %s (theo: %s). Going to sleep for %s.\n****************",
                dr_essai,
                dr_essai_theo,
                sleep_window,
            )
            sleep(sleep_window.seconds)
        except Exception as exc:
            self.logger.error(
                "%s with dr_pause=%s, dr_essai_theo=%s pause=%s",
                exc,
                pause_seconds,
                dr_essai_theo,
                pause,
            )


def go_multi(ma: MarketAuditor, arg_file=None, updatepause=None, logpause=None):
    """Legacy helper kept for archive scripts; active bot path uses `BotService`."""
    del ma, arg_file, updatepause, logpause
    raise RuntimeError("go_multi is no longer part of the active runtime path.")
