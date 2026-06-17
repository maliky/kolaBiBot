from __future__ import annotations

import pytest

from kolabi.kraken_contract import (
    KrakenFuturesOrderType,
    KrakenFuturesStandardOrder,
    KrakenTrailingDeviationUnit,
    build_send_order_contract,
    normalize_standard_order,
)


def test_build_send_order_contract_maps_limit_to_lmt():
    contract = build_send_order_contract(
        ord_type="Limit",
        symbol="PI_XBTUSD",
        side="buy",
        size=2,
        price=80000.0,
        stop_price=None,
        fallback_market_price=None,
        cli_ord_id="CID-1",
        reduce_only=False,
        post_only=False,
        trigger_signal=None,
        trailing_stop_max_deviation=None,
        trailing_stop_deviation_unit=None,
        tag="tag-1",
    )

    assert contract.order_type == KrakenFuturesOrderType.LIMIT
    assert contract.as_params()[0] == ("orderType", "lmt")
    assert "postOnly" not in dict(contract.as_params())


def test_build_send_order_contract_maps_post_only_limit_to_post():
    contract = build_send_order_contract(
        ord_type="Limit",
        symbol="PI_XBTUSD",
        side="buy",
        size=2,
        price=80000.0,
        stop_price=None,
        fallback_market_price=None,
        cli_ord_id="CID-1",
        reduce_only=False,
        post_only=True,
        trigger_signal=None,
        trailing_stop_max_deviation=None,
        trailing_stop_deviation_unit=None,
        tag="tag-1",
    )

    assert contract.order_type == KrakenFuturesOrderType.POST_ONLY_LIMIT
    assert contract.as_params()[0] == ("orderType", "post")
    assert "postOnly" not in dict(contract.as_params())


def test_build_send_order_contract_rejects_post_only_trigger_order():
    with pytest.raises(ValueError, match="post-only is only supported for limit"):
        build_send_order_contract(
            ord_type="StopLossLimit",
            symbol="PI_XBTUSD",
            side="buy",
            size=2,
            price=80000.0,
            stop_price=79900.0,
            fallback_market_price=None,
            cli_ord_id="CID-1",
            reduce_only=False,
            post_only=True,
            trigger_signal="mark",
            trailing_stop_max_deviation=None,
            trailing_stop_deviation_unit=None,
            tag=None,
        )


def test_build_send_order_contract_uses_native_market_type():
    contract = build_send_order_contract(
        ord_type="Market",
        symbol="PI_XBTUSD",
        side="sell",
        size=1,
        price=None,
        stop_price=None,
        fallback_market_price=79000.0,
        cli_ord_id=None,
        reduce_only=False,
        post_only=False,
        trigger_signal=None,
        trailing_stop_max_deviation=None,
        trailing_stop_deviation_unit=None,
        tag=None,
    )

    assert contract.order_type == KrakenFuturesOrderType.MARKET
    assert ("orderType", "mkt") in contract.as_params()
    assert ("limitPrice", None) in contract.as_params()


def test_build_send_order_contract_ignores_market_price_protection_inputs():
    contract = build_send_order_contract(
        ord_type="Market",
        symbol="PI_XBTUSD",
        side="buy",
        size=1,
        price=79123.0,
        stop_price=None,
        fallback_market_price=79000.0,
        cli_ord_id=None,
        reduce_only=False,
        post_only=False,
        trigger_signal=None,
        trailing_stop_max_deviation=None,
        trailing_stop_deviation_unit=None,
        tag=None,
    )

    assert contract.order_type == KrakenFuturesOrderType.MARKET
    assert ("limitPrice", None) in contract.as_params()


def test_normalize_standard_order_supports_legacy_and_short_aliases():
    assert normalize_standard_order("L") == KrakenFuturesStandardOrder.LIMIT
    assert normalize_standard_order("M") == KrakenFuturesStandardOrder.MARKET
    assert normalize_standard_order("S") == KrakenFuturesStandardOrder.STOP_LOSS_MARKET
    assert normalize_standard_order("SL") == KrakenFuturesStandardOrder.STOP_LOSS_LIMIT
    assert normalize_standard_order("MT") == KrakenFuturesStandardOrder.TAKE_PROFIT_MARKET
    assert normalize_standard_order("LT") == KrakenFuturesStandardOrder.TAKE_PROFIT_LIMIT
    assert (
        normalize_standard_order("StopLoss")
        == KrakenFuturesStandardOrder.STOP_LOSS_MARKET
    )
    assert (
        normalize_standard_order("TriggerEntryLimit")
        == KrakenFuturesStandardOrder.STOP_LOSS_LIMIT
    )
    assert (
        normalize_standard_order("TakeProfitLimit")
        == KrakenFuturesStandardOrder.TAKE_PROFIT_LIMIT
    )


def test_build_send_order_contract_maps_standard_stop_loss_limit():
    contract = build_send_order_contract(
        ord_type="StopLossLimit",
        symbol="PI_XBTUSD",
        side="buy",
        size=3,
        price=80100.0,
        stop_price=80050.0,
        fallback_market_price=None,
        cli_ord_id=None,
        reduce_only=False,
        post_only=False,
        trigger_signal="mark",
        trailing_stop_max_deviation=None,
        trailing_stop_deviation_unit=None,
        tag=None,
    )

    assert contract.order_type == KrakenFuturesOrderType.STOP
    assert ("limitPrice", 80100.0) in contract.as_params()
    assert ("stopPrice", 80050.0) in contract.as_params()


def test_build_send_order_contract_maps_standard_stop_loss_market_without_limit():
    contract = build_send_order_contract(
        ord_type="StopLoss",
        symbol="PI_XBTUSD",
        side="buy",
        size=3,
        price=None,
        stop_price=80050.0,
        fallback_market_price=None,
        cli_ord_id=None,
        reduce_only=False,
        post_only=False,
        trigger_signal="mark",
        trailing_stop_max_deviation=None,
        trailing_stop_deviation_unit=None,
        tag=None,
    )

    assert contract.order_type == KrakenFuturesOrderType.STOP
    assert ("limitPrice", None) in contract.as_params()
    assert ("stopPrice", 80050.0) in contract.as_params()


def test_build_send_order_contract_maps_take_profit_limit():
    contract = build_send_order_contract(
        ord_type="TakeProfitLimit",
        symbol="PI_XBTUSD",
        side="sell",
        size=3,
        price=82000.0,
        stop_price=81950.0,
        fallback_market_price=None,
        cli_ord_id=None,
        reduce_only=True,
        post_only=False,
        trigger_signal="last",
        trailing_stop_max_deviation=None,
        trailing_stop_deviation_unit=None,
        tag=None,
    )

    assert contract.order_type == KrakenFuturesOrderType.TAKE_PROFIT
    assert ("limitPrice", 82000.0) in contract.as_params()
    assert ("stopPrice", 81950.0) in contract.as_params()
    assert ("reduceOnly", True) in contract.as_params()


def test_build_send_order_contract_maps_trailing_stop():
    contract = build_send_order_contract(
        ord_type="TrailingStop",
        symbol="PI_XBTUSD",
        side="sell",
        size=1,
        price=None,
        stop_price=None,
        fallback_market_price=None,
        cli_ord_id=None,
        reduce_only=True,
        post_only=False,
        trigger_signal="mark",
        trailing_stop_max_deviation=20.0,
        trailing_stop_deviation_unit="PERCENT",
        tag=None,
    )

    assert contract.order_type == KrakenFuturesOrderType.TRAILING_STOP
    assert contract.trailing_stop_deviation_unit == KrakenTrailingDeviationUnit.PERCENT
    assert ("trailingStopMaxDeviation", 20.0) in contract.as_params()
    assert ("trailingStopDeviationUnit", "PERCENT") in contract.as_params()
