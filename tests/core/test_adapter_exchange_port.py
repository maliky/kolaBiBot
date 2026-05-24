from __future__ import annotations

import asyncio
from typing import Any

from kolabi.bot.service import AdapterExchangePort
from kolabi.shared.config import ExchangeConfig
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
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
