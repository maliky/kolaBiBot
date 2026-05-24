from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kolabi.bot.service import AdapterExchangePort
from kolabi.shared.config import ExchangeConfig
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
)


class _FakeAdapter:
    last: tuple[str, dict[str, Any]] | None = None

    def __init__(self, **_kwargs: Any) -> None:
        type(self).last = None

    def place_order(self, *_args: Any, **_kwargs: Any) -> OrderAck:
        raise AssertionError("not used")

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        type(self).last = (order_id, params)
        return OrderAck(order_id=order_id, status="Replaced")

    def cancel_order(self, order_id: str) -> OrderAck:
        raise AssertionError("not used")


class _TailAdapter:
    placed: tuple[tuple[Any, ...], dict[str, Any]] | None = None
    trigger_orders: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        type(self).placed = None

    def place_order(self, *args: Any, **kwargs: Any) -> OrderAck:
        type(self).placed = (args, kwargs)
        return OrderAck(order_id="OID-T", status="New", orig_qty=1.0, side="sell")

    def live_trigger_orders(self) -> list[dict[str, Any]]:
        return type(self).trigger_orders

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        raise AssertionError("not used")

    def cancel_order(self, order_id: str) -> OrderAck:
        raise AssertionError("not used")


def _config() -> ExchangeConfig:
    return ExchangeConfig(
        api_key="key",
        api_secret="secret",
        base_url="https://example.invalid",
        symbol="PI_XBTUSD",
    )


def test_amend_tail_maps_new_price_to_stop_px(monkeypatch) -> None:
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _FakeAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    asyncio.run(
        port.amend_tail(
            AmendTailCommand(
                kind=RuntimeCommandKind.AMEND,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=AmendOrderCommandRequest(
                    pair_name="pair-a",
                    side="buy",
                    ordType="Stop",
                    orderID="OID-T",
                    clOrdID="CID-T",
                    newPrice=101.5,
                ),
            )
        )
    )

    assert _FakeAdapter.last == ("OID-T", {"clOrdID": "CID-T", "stopPx": 101.5})


def test_amend_head_keeps_new_price_as_limit_price(monkeypatch) -> None:
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _FakeAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    asyncio.run(
        port.amend_head(
            AmendHeadCommand(
                kind=RuntimeCommandKind.AMEND,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=AmendOrderCommandRequest(
                    pair_name="pair-a",
                    side="sell",
                    ordType="Limit",
                    orderID="OID-H",
                    newPrice=100.5,
                ),
            )
        )
    )

    assert _FakeAdapter.last == ("OID-H", {"price": 100.5})


def test_place_tail_requires_matching_live_trigger_order(monkeypatch) -> None:
    _TailAdapter.trigger_orders = [
        {
            "order_id": "OID-T",
            "client_order_id": "CID-T",
            "symbol": "PI_XBTUSD",
            "side": "sell",
            "qty": 1.0,
            "stop_price": 99.0,
            "reduce_only": True,
        }
    ]
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _TailAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    ack = asyncio.run(
        port.place_tail(
            PlaceTailCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="sell",
                    ordType="S",
                    orderQty=1.0,
                    stopPx=99.0,
                    execInst="ReduceOnly,LastPrice",
                    clOrdID="CID-T",
                ),
            )
        )
    )

    assert ack.order_id == "OID-T"
    assert _TailAdapter.placed == (
        ("sell", 1.0),
        {
            "type_": "S",
            "stopPx": 99.0,
            "clOrdID": "CID-T",
            "execInst": "ReduceOnly,LastPrice",
        },
    )


def test_place_tail_fails_when_trigger_order_is_not_visible(monkeypatch) -> None:
    _TailAdapter.trigger_orders = []
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _TailAdapter)
    monkeypatch.setattr("kolabi.bot.service.asyncio.sleep", _raise_timeout)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    with pytest.raises(RuntimeError, match="tail trigger order not visible"):
        asyncio.run(
            port.place_tail(
                PlaceTailCommand(
                    kind=RuntimeCommandKind.PLACE,
                    symbol=Symbol("PI_XBTUSD"),
                    pair_name="pair-a",
                    request=PlaceOrderCommandRequest(
                        pair_name="pair-a",
                        side="sell",
                        ordType="S",
                        orderQty=1.0,
                        stopPx=99.0,
                        execInst="ReduceOnly,LastPrice",
                        clOrdID="CID-T",
                    ),
                )
            )
        )


async def _raise_timeout(_seconds: float) -> None:
    raise RuntimeError("tail trigger order not visible")
