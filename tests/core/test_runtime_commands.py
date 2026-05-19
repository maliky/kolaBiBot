from __future__ import annotations

from queue import Queue
from typing import Any

import pandas as pd
from kolabi.runtime.kola.chronos import Chronos
from kolabi.shared.core.runtime_commands import (
    command_payload_for_role,
    execute_runtime_command,
    runtime_command_from_order,
    timeout_override_minutes_for,
    validation_conditions_for,
)
from kolabi.shared.core.runtime_types import OrderRole, RuntimeCommandKind


class _FakeBargain:
    symbol = "PI_XBTUSD"


def test_runtime_command_from_order_classifies_command_kind() -> None:
    place_command = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "Limit", "side": "buy", "orderQty": 1},
    )
    amend_command = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "amendStop", "side": "buy", "orderID": "OID-1", "newPrice": 1.0},
    )
    cancel_command = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "cancel", "clOrdID": "CID-1"},
    )

    assert place_command.kind == RuntimeCommandKind.PLACE
    assert amend_command.kind == RuntimeCommandKind.AMEND
    assert cancel_command.kind == RuntimeCommandKind.CANCEL


def test_validation_and_timeout_rules_match_legacy_runtime() -> None:
    market = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "Market", "side": "buy", "orderQty": 1},
    )
    stop_tail = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "Stop", "side": "sell", "orderQty": 1, "stopPx": 99.0},
    )
    amend = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "amendStop", "side": "buy", "orderID": "OID-1", "newPrice": 1.0},
    )

    assert timeout_override_minutes_for(market) == 5
    assert validation_conditions_for(stop_tail, trailstop_sender=True) == (
        {"exectype": "New", "orderstatus": "New"},
    )
    assert validation_conditions_for(amend) == (
        {"exectype": "Replaced", "orderstatus": "New"},
    )


def test_execute_runtime_command_uses_legacy_place_functions(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_place(brg: object, side: str, orderQty: object, price: object, **opts: object) -> dict[str, object]:
        seen["args"] = (brg, side, orderQty, price, opts)
        return {"orderID": "OID-1", "clOrdID": "CID-1"}

    monkeypatch.setattr("kolabi.shared.core.runtime_commands.place", fake_place)
    command = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={
            "ordType": "Limit",
            "side": "buy",
            "orderQty": 2,
            "price": 100.5,
            "clOrdID": "CID-1",
        },
    )

    reply = execute_runtime_command(_FakeBargain(), command, amend_absdelta=0.5)

    assert reply["orderID"] == "OID-1"
    assert seen["args"][1:] == ("buy", 2, 100.5, {"clOrdID": "CID-1"})


def test_chronos_handle_load_dispatches_through_runtime_command_layer(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_execute(brg: object, command: object, *, amend_absdelta: float) -> dict[str, object]:
        captured["command"] = command
        captured["absdelta"] = amend_absdelta
        return {"orderID": "OID-1", "clOrdID": "mlk_CID-1"}

    class _FakeThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            if "target" in kwargs:
                captured["thread"] = {
                    "target": kwargs["target"],
                    "name": kwargs["name"],
                    "kwargs": kwargs["kwargs"],
                }

        def start(self) -> None:
            captured["thread_started"] = True

    monkeypatch.setattr("kolabi.runtime.kola.chronos.execute_runtime_command", fake_execute)
    monkeypatch.setattr("kolabi.runtime.kola.chronos.threading.Thread", _FakeThread)

    chronos = Chronos(_FakeBargain(), Queue(), valid_queue=Queue())
    load = {
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

    chronos.handle_load(load)
    reply = chronos.reply_queue.get_nowait()

    assert captured["command"].kind == RuntimeCommandKind.PLACE
    assert captured["command"].order["ordType"] == "Limit"
    assert captured["absdelta"] == 0.5
    assert reply["orderID"] == "OID-1"
    assert captured["thread_started"] is True


def test_command_payload_for_role_maps_new_amend_cancel_requests() -> None:
    head_place = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "Limit", "side": "buy", "orderQty": 2, "price": 100.0},
    )
    tail_amend = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "amendStop", "side": "sell", "orderID": "OID-2", "newPrice": 98.5},
    )
    cancel = runtime_command_from_order(
        symbol="PI_XBTUSD",
        order={"ordType": "cancel", "clOrdID": "CID-9"},
    )

    head_payload = command_payload_for_role(head_place, role=OrderRole.PRIMARY)
    tail_payload = command_payload_for_role(tail_amend, role=OrderRole.TAIL)
    cancel_payload = command_payload_for_role(cancel, role=OrderRole.PRIMARY)

    assert head_payload["request"]["ordType"] == "Limit"
    assert tail_payload["request"]["orderID"] == "OID-2"
    assert cancel_payload["request"]["clOrdID"] == "CID-9"
