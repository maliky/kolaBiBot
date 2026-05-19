import pandas as pd
from kolabi.runtime.legacy.kola.utils.argfunc import price_type_trad
from kolabi.runtime.legacy.kola.utils.orderfunc import create_order


def test_price_type_trad_accepts_index_price_shorthand() -> None:
    price_type, order_type, exec_inst = price_type_trad("Si", "sell")

    assert price_type == "indexPrice"
    assert order_type == "Stop"
    assert exec_inst == "indexPrice"


def test_timedelta_total_seconds_compatibility() -> None:
    timeout = pd.Timedelta(minutes=2)
    start_time = pd.Timestamp("2026-05-13T15:24:00")
    current_time = pd.Timestamp("2026-05-13T15:25:30")

    remaining = (timeout + start_time - current_time).total_seconds()

    assert remaining == 30.0


def test_create_order_uses_runtime_min_quantity() -> None:
    order = create_order(
        "sell",
        20,
        "lastMidPrice",
        "Limit",
        "",
        prices=(79319.5, 79320.5),
        min_qty=1,
    )

    assert order["orderQty"] == 20
