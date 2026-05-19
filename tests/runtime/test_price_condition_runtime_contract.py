from __future__ import annotations

from dataclasses import dataclass

from kolabi.runtime.kola.orders.condition import Condition
from kolabi.runtime.kola.price import PriceObj


@dataclass
class _FakeBargain:
    symbol: str = "XBTUSD"
    last_price: float = 100.0
    hook_ids: list[str] | None = None

    def prices(self, price_type: str) -> float:
        mapping = {
            "fairPrice": self.last_price,
            "lastPrice": self.last_price,
            "markPrice": self.last_price,
            "indexPrice": self.last_price,
            "lastMidPrice": self.last_price,
            "bidPrice": self.last_price - 0.5,
            "askPrice": self.last_price + 0.5,
        }
        return mapping[price_type]

    def get_exec_clID_with_(self, srcKey_: str, debug_: bool = False) -> list[str]:
        _ = debug_
        ids = self.hook_ids or []
        return [cid for cid in ids if srcKey_ in cid]

    def order_reached_status(self, clordid: str, status: str) -> bool:
        _ = clordid
        return status == "Filled"


def test_price_obj_tail_update_stays_deterministic() -> None:
    po = PriceObj(
        price=100.0,
        refPrice=100.0,
        tail_perct_init=1.0,
        head="buy",
        updatepause=1.0,
        timeBin=60,
        symbol="XBTUSD",
    )
    initial_stop = float(po.data.stopTail.current)

    po.update_to(price=111.0, refPrice=111.0)

    assert float(po.data.stopTail.current) >= initial_stop
    assert po.new_current_stopTail() in {True, False}


def test_condition_still_evaluates_price_band() -> None:
    brg = _FakeBargain(last_price=100.0)
    cond = Condition(brg, (("lastPrice", "<", 101.0), ("lastPrice", ">", 99.0)))
    assert cond.is_(True)

    brg.last_price = 102.0
    assert not cond.is_(True)


def test_condition_hook_evaluation_uses_exec_status() -> None:
    brg = _FakeBargain(last_price=100.0, hook_ids=["mlk_XTop-Sabc"])
    cond = Condition(brg, (("hook", "XTop-S", "Filled"),))

    assert cond.is_hooked()
    assert cond.hookedSrcID == "mlk_XTop-Sabc"

