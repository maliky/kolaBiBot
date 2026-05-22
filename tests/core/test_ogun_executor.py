from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest

from kolabi.runtime.kola.ogun_executor import execute_runtime_command
from kolabi.shared.core.runtime_types import (
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
)


def place_command(
    ord_type: str,
    *,
    price: Decimal | None = None,
    stopPx: Decimal | None = None,
    text: str | None = None,
    oDelta: Decimal | None = None,
) -> PlaceTailCommand:
    request = PlaceOrderCommandRequest(
        pair_name="pair-a",
        side="sell",
        ordType=ord_type,
        orderQty=Decimal("2"),
        price=price,
        stopPx=stopPx,
        text=text,
        oDelta=oDelta,
    )
    return PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=request,
    )


def amend_command(
    *,
    side: str = "sell",
    ordType: str = "Stop",
    orderID: str = "OID-T",
    clOrdID: str | None = None,
    newPrice: Decimal | None = Decimal("99.0"),
    newQty: Decimal | None = None,
    text: str | None = None,
) -> AmendTailCommand:
    request = AmendOrderCommandRequest(
        pair_name="pair-a",
        side=side,
        ordType=ordType,
        orderID=orderID,
        clOrdID=clOrdID,
        newPrice=newPrice,
        newQty=newQty,
        text=text,
    )
    return AmendTailCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=request,
    )


def cancel_command(
    *,
    clOrdID: str = "CID-T",
) -> CancelCommand:
    request = CancelOrderCommandRequest(
        pair_name="pair-a",
        clOrdID=clOrdID,
    )
    return CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=request,
    )


class _FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _record(self, name: str, *args: object, **kwargs: object) -> dict[str, object] | str:
        self.calls.append((name, args, kwargs))
        if name.startswith("place_"):
            return name
        return {"status": name}

    def place_at_market(self, brg: object, order_qty: float, side: str, **opts: object) -> dict[str, object]:
        return cast(dict[str, object], self._record("place_at_market", brg, order_qty, side, **opts))

    def place(self, brg: object, side: str, order_qty: float, price: float, **opts: object) -> dict[str, object]:
        return cast(dict[str, object], self._record("place", brg, side, order_qty, price, **opts))

    def place_stop(self, brg: object, side: str, order_qty: float, stop_px: float, **opts: object) -> str:
        return cast(str, self._record("place_stop", brg, side, order_qty, stop_px, **opts))

    def place_sl(self, brg: object, side: str, order_qty: float, stop_px: float, price: float, **opts: object) -> str:
        return cast(str, self._record("place_sl", brg, side, order_qty, stop_px, price, **opts))

    def place_mit(self, brg: object, side: str, order_qty: float, stop_px: float, **opts: object) -> str:
        return cast(str, self._record("place_mit", brg, side, order_qty, stop_px, **opts))

    def place_lit(self, brg: object, side: str, order_qty: float, stop_px: float, price: float, **opts: object) -> str:
        return cast(str, self._record("place_lit", brg, side, order_qty, stop_px, price, **opts))

    def amend_prices(
        self,
        brg: object,
        order_id: str,
        new_price: float,
        ord_type: str,
        side: str,
        *,
        absdelta: float,
        text: str,
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self._record(
                "amend_prices",
                brg,
                order_id,
                new_price,
                ord_type,
                side,
                absdelta=absdelta,
                text=text,
            ),
        )

    def amend_order_qty(self, brg: object, order: dict[str, object], new_qty: float) -> dict[str, object]:
        return cast(dict[str, object], self._record("amend_order_qty", brg, order, new_qty))

    def cancel_order(self, brg: object, order: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], self._record("cancel_order", brg, order))


class _FakeCryptoApi:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def amend(self, order: dict[str, object], **params: object) -> dict[str, object]:
        self.calls.append((order, params))
        return {"status": "amended", "order": order, "params": params}


class _FakeBargain:
    def __init__(self) -> None:
        self.crypto_api = _FakeCryptoApi()


def test_execute_place_market_dispatches_to_market_helper() -> None:
    adapter = _FakeAdapter()

    result = execute_runtime_command(
        object(),
        place_command("Market"),
        amend_absdelta=0.5,
        adapter=adapter,
    )

    assert result == "place_at_market"
    assert adapter.calls == [
        ("place_at_market", (adapter.calls[0][1][0], 2.0, "sell"), {"pair_name": "pair-a"})
    ]


def test_execute_place_limit_dispatches_to_limit_helper() -> None:
    adapter = _FakeAdapter()

    result = execute_runtime_command(
        object(),
        place_command("Limit", price=Decimal("99.5"), text="limit"),
        amend_absdelta=0.5,
        adapter=adapter,
    )

    assert result == {"status": "place"}
    assert adapter.calls[0][0] == "place"
    assert adapter.calls[0][1][1:] == ("sell", 2.0, 99.5)
    assert adapter.calls[0][2] == {"pair_name": "pair-a", "text": "limit"}


@pytest.mark.parametrize(
    ("ord_type", "helper_name", "request_overrides"),
    [
        ("Stop", "place_stop", {"stopPx": Decimal("99.0")}),
        ("StopLimit", "place_sl", {"stopPx": Decimal("99.0"), "price": Decimal("100.0")}),
        ("MarketIfTouched", "place_mit", {"stopPx": Decimal("99.0")}),
        ("LimitIfTouched", "place_lit", {"stopPx": Decimal("99.0"), "price": Decimal("100.0")}),
    ],
)
def test_execute_stop_family_dispatches_to_the_right_helper(
    ord_type: str,
    helper_name: str,
    request_overrides: dict[str, object],
) -> None:
    adapter = _FakeAdapter()
    price = request_overrides.get("price")
    stop_px = request_overrides.get("stopPx")

    result = execute_runtime_command(
        object(),
        place_command(
            ord_type,
            price=price if isinstance(price, Decimal) else None,
            stopPx=stop_px if isinstance(stop_px, Decimal) else None,
        ),
        amend_absdelta=0.5,
        adapter=adapter,
    )

    assert result == helper_name
    assert adapter.calls[0][0] == helper_name


def test_execute_amend_dispatches_to_amend_prices() -> None:
    adapter = _FakeAdapter()

    result = execute_runtime_command(
        object(),
        amend_command(text="repriced"),
        amend_absdelta=0.5,
        adapter=adapter,
    )

    assert result == {"status": "amend_prices"}
    assert adapter.calls[0][0] == "amend_prices"


def test_execute_quantity_only_amend_dispatches_to_amend_orderqty() -> None:
    adapter = _FakeAdapter()

    result = execute_runtime_command(
        object(),
        amend_command(newPrice=None, newQty=Decimal("3")),
        amend_absdelta=0.5,
        adapter=adapter,
    )

    assert result == {"status": "amend_order_qty"}
    assert adapter.calls[0][0] == "amend_order_qty"
    assert adapter.calls[0][1][1] == {"orderID": "OID-T", "orderQty": 3.0}


def test_execute_price_and_quantity_amend_uses_combined_adapter_call() -> None:
    brg = _FakeBargain()

    result = execute_runtime_command(
        brg,
        amend_command(ordType="StopLimit", newPrice=Decimal("101.0"), newQty=Decimal("3")),
        amend_absdelta=0.5,
        adapter=_FakeAdapter(),
    )

    assert result["status"] == "amended"
    assert brg.crypto_api.calls == [
        (
            {"orderID": "OID-T"},
            {"orderID": "OID-T", "orderQty": 3.0, "price": 101.0, "stopPx": 100.5},
        )
    ]


def test_execute_cancel_dispatches_to_cancel_order() -> None:
    adapter = _FakeAdapter()

    result = execute_runtime_command(
        object(),
        cancel_command(),
        amend_absdelta=0.5,
        adapter=adapter,
    )

    assert result == {"status": "cancel_order"}
    assert adapter.calls[0][1][1] == {"clOrdID": "CID-T"}


def test_missing_required_field_raises_before_helper_call() -> None:
    adapter = _FakeAdapter()

    with pytest.raises(KeyError, match="price"):
        execute_runtime_command(
            object(),
            place_command("Limit"),
            amend_absdelta=0.5,
            adapter=adapter,
        )

    assert adapter.calls == []


def test_amend_requires_at_least_one_change() -> None:
    with pytest.raises(ValueError, match="at least one planned change"):
        execute_runtime_command(
            object(),
            amend_command(newPrice=None),
            amend_absdelta=0.5,
            adapter=_FakeAdapter(),
        )


def test_unsupported_command_kind_raises() -> None:
    command = PlaceTailCommand(
        kind=cast(RuntimeCommandKind, "explode"),
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Market",
            orderQty=Decimal("1"),
        ),
    )

    with pytest.raises(ValueError, match="Unsupported runtime command kind"):
        execute_runtime_command(object(), command, amend_absdelta=0.5, adapter=_FakeAdapter())


def test_unsupported_order_type_raises() -> None:
    with pytest.raises(ValueError, match="Action type 'Iceberg' pas prise en compte"):
        execute_runtime_command(
            object(),
            place_command("Iceberg"),
            amend_absdelta=0.5,
            adapter=_FakeAdapter(),
        )


def test_decimal_price_is_normalized_at_execution_edge() -> None:
    adapter = _FakeAdapter()

    execute_runtime_command(
        object(),
        place_command("Limit", price=Decimal("101.25")),
        amend_absdelta=0.5,
        adapter=adapter,
    )

    assert adapter.calls[0][1][3] == 101.25


def test_execute_runtime_command_does_not_mutate_input_command() -> None:
    adapter = _FakeAdapter()
    command = place_command("Limit", price=Decimal("100.5"), text="keep")
    original_request = command.request
    original_legacy_order = command.legacy_order

    execute_runtime_command(object(), command, amend_absdelta=0.5, adapter=adapter)

    assert command.request == original_request
    assert command.legacy_order == original_legacy_order
