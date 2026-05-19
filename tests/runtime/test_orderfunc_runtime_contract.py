from __future__ import annotations

from kolabi.runtime.legacy.kola.utils.orderfunc import (
    create_order,
    get_order_from,
    normalize_order_dict,
    split_ids,
)


def test_create_order_limit_keeps_runtime_keys() -> None:
    order = create_order(
        side="sell",
        _q=42,
        opType="lastMidPrice",
        ordtype="Limit",
        execinst="ReduceOnly",
        prices=(81570.0, 81572.0),
        min_qty=1,
    )

    assert set(order.keys()) == {"side", "orderQty", "price", "ordType", "execInst", "text"}
    assert order["side"] == "sell"
    assert order["orderQty"] == 42
    assert order["ordType"] == "Limit"


def test_normalize_order_dict_accepts_quantity_and_stop_price() -> None:
    payload = {"side": "buy", "quantity": 3, "stopPrice": 81000.5}
    normalized = normalize_order_dict(payload)

    assert normalized["orderQty"] == 3
    assert normalized["stopPx"] == 81000.5
    assert "quantity" not in normalized
    assert "stopPrice" not in normalized


def test_get_order_from_accepts_runtime_shapes() -> None:
    order = {"clOrdID": "mlk_abc", "orderQty": 1}

    assert get_order_from([order]) == order
    assert get_order_from({"order": order}) == order
    assert get_order_from({"orders": [order]}) == order


def test_split_ids_still_partitions_client_and_exchange_ids() -> None:
    out = split_ids(["mlk_A", "abcd-123", "mlk_B"])

    assert out["clIDList"] == ["mlk_A", "mlk_B"]
    assert out["oIDList"] == ["abcd-123"]

