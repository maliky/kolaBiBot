from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest

from kolabi.runtime.kola.ogun_executor import execute_runtime_command
from kolabi.shared.core.runtime_types import RuntimeCommand, RuntimeCommandKind, Symbol


def place_command(ord_type: str, **order: object) -> RuntimeCommand:
    payload = {"ordType": ord_type, "side": "sell", "orderQty": Decimal("2")}
    payload.update(order)
    return RuntimeCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        order=payload,
        reason="tail",
    )


def amend_command(**order: object) -> RuntimeCommand:
    payload = {
        "ordType": "Stop",
        "side": "sell",
        "orderID": "OID-T",
        "newPrice": Decimal("99.0"),
    }
    payload.update(order)
    return RuntimeCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        order=payload,
        reason="tail",
    )


def cancel_command(**order: object) -> RuntimeCommand:
    payload = {"clOrdID": "CID-T"}
    payload.update(order)
    return RuntimeCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol("PI_XBTUSD"),
        order=payload,
        reason="tail",
    )


def test_execute_place_market_dispatches_to_market_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_place_at_market(brg: object, order_qty: float, side: str, **opts: object) -> dict[str, object]:
        captured.update({"brg": brg, "order_qty": order_qty, "side": side, "opts": opts})
        return {"ok": True}

    monkeypatch.setattr("kolabi.runtime.kola.ogun_executor.place_at_market", fake_place_at_market)

    result = execute_runtime_command(
        object(),
        place_command("Market"),
        amend_absdelta=0.5,
    )

    assert result == {"ok": True}
    assert captured == {"brg": captured["brg"], "order_qty": 2.0, "side": "sell", "opts": {}}


def test_execute_place_limit_dispatches_to_limit_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_place(brg: object, side: str, order_qty: float, price: float, **opts: object) -> dict[str, object]:
        captured.update(
            {"brg": brg, "side": side, "order_qty": order_qty, "price": price, "opts": opts}
        )
        return {"orderID": "OID-1"}

    monkeypatch.setattr("kolabi.runtime.kola.ogun_executor.place", fake_place)

    result = execute_runtime_command(
        object(),
        place_command("Limit", price=Decimal("99.5"), text="limit"),
        amend_absdelta=0.5,
    )

    assert result == {"orderID": "OID-1"}
    assert captured == {
        "brg": captured["brg"],
        "side": "sell",
        "order_qty": 2.0,
        "price": 99.5,
        "opts": {"text": "limit"},
    }


@pytest.mark.parametrize(
    ("ord_type", "helper_name", "expected_args"),
    [
        ("Stop", "place_stop", (2.0, 99.0)),
        ("StopLimit", "place_SL", (2.0, 99.0, 100.0)),
        ("MarketIfTouched", "place_MIT", (2.0, 99.0)),
        ("LimitIfTouched", "place_LIT", (2.0, 99.0, 100.0)),
    ],
)
def test_execute_stop_family_dispatches_to_the_right_helper(
    monkeypatch: pytest.MonkeyPatch,
    ord_type: str,
    helper_name: str,
    expected_args: tuple[float, ...],
) -> None:
    captured: dict[str, object] = {}

    def fake_helper(brg: object, side: str, order_qty: float, stop_px: float, *rest: object, **opts: object) -> str:
        captured.update(
            {
                "brg": brg,
                "side": side,
                "order_qty": order_qty,
                "stop_px": stop_px,
                "rest": rest,
                "opts": opts,
            }
        )
        return helper_name

    monkeypatch.setattr(f"kolabi.runtime.kola.ogun_executor.{helper_name}", fake_helper)
    command = place_command(ord_type, stopPx=Decimal("99.0"))
    if ord_type in {"StopLimit", "LimitIfTouched"}:
        command.order["price"] = Decimal("100.0")

    result = execute_runtime_command(object(), command, amend_absdelta=0.5)

    assert result == helper_name
    assert captured["side"] == "sell"
    assert captured["order_qty"] == expected_args[0]
    assert captured["stop_px"] == expected_args[1]
    if len(expected_args) == 3:
        assert captured["rest"] == (expected_args[2],)
    else:
        assert captured["rest"] == ()


def test_execute_amend_dispatches_to_amend_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_amend_prices(
        brg: object,
        order_id: str,
        new_price: float,
        ord_type: str,
        side: str,
        *,
        absdelta: float,
        text: str,
    ) -> dict[str, object]:
        captured.update(
            {
                "brg": brg,
                "order_id": order_id,
                "new_price": new_price,
                "ord_type": ord_type,
                "side": side,
                "absdelta": absdelta,
                "text": text,
            }
        )
        return {"status": "amended"}

    monkeypatch.setattr("kolabi.runtime.kola.ogun_executor.amend_prices", fake_amend_prices)

    result = execute_runtime_command(
        object(),
        amend_command(text="repriced"),
        amend_absdelta=0.5,
    )

    assert result == {"status": "amended"}
    assert captured == {
        "brg": captured["brg"],
        "order_id": "OID-T",
        "new_price": 99.0,
        "ord_type": "Stop",
        "side": "sell",
        "absdelta": 0.5,
        "text": "repriced",
    }


def test_execute_cancel_dispatches_to_cancel_order(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_cancel_order(brg: object, order: dict[str, object]) -> dict[str, object]:
        captured.update({"brg": brg, "order": order})
        return {"status": "cancelled"}

    monkeypatch.setattr("kolabi.runtime.kola.ogun_executor.cancel_order", fake_cancel_order)

    result = execute_runtime_command(
        object(),
        cancel_command(),
        amend_absdelta=0.5,
    )

    assert result == {"status": "cancelled"}
    assert captured["order"] == {"clOrdID": "CID-T"}


def test_missing_required_field_raises_before_helper_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_place(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("kolabi.runtime.kola.ogun_executor.place", fake_place)

    with pytest.raises(KeyError, match="price"):
        execute_runtime_command(
            object(),
            place_command("Limit"),
            amend_absdelta=0.5,
        )

    assert called is False


def test_unsupported_command_kind_raises() -> None:
    command = RuntimeCommand(
        kind=cast(RuntimeCommandKind, "explode"),
        symbol=Symbol("PI_XBTUSD"),
        order={"ordType": "Market", "side": "sell", "orderQty": Decimal("1")},
        reason="tail",
    )

    with pytest.raises(ValueError, match="Unsupported runtime command kind"):
        execute_runtime_command(object(), command, amend_absdelta=0.5)


def test_unsupported_order_type_raises() -> None:
    with pytest.raises(ValueError, match="Action type 'Iceberg' pas prise en compte"):
        execute_runtime_command(
            object(),
            place_command("Iceberg"),
            amend_absdelta=0.5,
        )


def test_decimal_price_is_normalized_at_execution_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_place(brg: object, side: str, order_qty: float, price: float, **opts: object) -> None:
        captured.update({"side": side, "order_qty": order_qty, "price": price, "opts": opts})

    monkeypatch.setattr("kolabi.runtime.kola.ogun_executor.place", fake_place)

    execute_runtime_command(
        object(),
        place_command("Limit", price=Decimal("101.25")),
        amend_absdelta=0.5,
    )

    assert captured["order_qty"] == 2.0
    assert captured["price"] == 101.25


def test_execute_runtime_command_does_not_mutate_input_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_place(brg: object, side: str, order_qty: float, price: float, **opts: object) -> dict[str, object]:
        return {"ok": True, "side": side, "order_qty": order_qty, "price": price, "opts": opts}

    monkeypatch.setattr("kolabi.runtime.kola.ogun_executor.place", fake_place)
    command = place_command("Limit", price=Decimal("100.5"), text="keep")
    original_order = dict(cast(dict[str, Any], command.order))

    execute_runtime_command(object(), command, amend_absdelta=0.5)

    assert command.order == original_order
