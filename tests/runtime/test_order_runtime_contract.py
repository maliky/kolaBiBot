from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue

from kolabi.runtime.kola.orders.condition import Condition
from kolabi.runtime.kola.orders.ordercond import OrderConditionned
from kolabi.runtime.kola.orders.trailstop import TrailStop


@dataclass
class _FakeBargain:
    symbol: str = "XBTUSD"

    def prices(self, price_type: str) -> float:
        mapping = {
            "fairPrice": 100.0,
            "lastPrice": 100.0,
            "markPrice": 100.0,
            "indexPrice": 100.0,
            "lastMidPrice": 100.0,
            "bidPrice": 99.5,
            "askPrice": 100.5,
        }
        return mapping[price_type]

    def get_exec_clID_with_(self, srcKey_: str, debug_: bool = False) -> list[str]:
        _ = srcKey_, debug_
        return []

    def order_reached_status(self, clordid: str, status: str) -> bool:
        _ = clordid, status
        return False


def _build_ordercond(valid_queue: Queue) -> OrderConditionned:
    brg = _FakeBargain()
    cond = Condition(brg, (("lastPrice", ">", 90.0),))
    return OrderConditionned(
        send_queue=Queue(),
        order={"side": "buy", "orderQty": 1, "ordType": "Limit", "price": 101.0},
        cond=cond,
        valid_queue=valid_queue,
        nameT="XTest",
        symbol="XBTUSD",
    )


def test_orderconditionned_get_load_shape() -> None:
    oc = _build_ordercond(Queue())
    load, order = oc.get_load()

    assert load["sender"] is oc
    assert load["symbol"] == "XBTUSD"
    assert load["order"] is order
    assert order["clOrdID"] == oc.oclid


def test_wait_for_broker_reply_requeues_non_matching_payload() -> None:
    valid_queue: Queue = Queue()
    oc = _build_ordercond(valid_queue)

    other = {
        "brokerReply": {"clOrdID": "mlk_other"},
        "exgLoad": {"order": {"clOrdID": "mlk_other"}},
        "execValidation": {"ordStatus": "New"},
    }
    mine = {
        "brokerReply": {"clOrdID": oc.oclid},
        "exgLoad": {"order": {"clOrdID": oc.oclid}},
        "execValidation": {"ordStatus": "Filled"},
    }
    valid_queue.put(other)
    valid_queue.put(mine)

    reply = oc.wait_for_broker_reply()
    assert reply == {"ordStatus": "Filled"}

    recycled = valid_queue.get_nowait()
    assert recycled["exgLoad"]["order"]["clOrdID"] == "mlk_other"
    try:
        valid_queue.get_nowait()
    except Empty:
        pass
    else:
        raise AssertionError("La queue de validation devrait etre vide apres le recyclage.")


def test_trailstop_amend_order_type_keeps_runtime_prefix_logic() -> None:
    trail = TrailStop.__new__(TrailStop)

    assert trail.amend_order_type("Stop") == "amendStop"
    assert trail.amend_order_type("amendStop") == "amendStop"

