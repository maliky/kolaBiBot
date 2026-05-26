from __future__ import annotations

from pathlib import Path
import asyncio

from kolabi.bot.indicators import DummyIndicatorClient
from kolabi.bot.service import AdapterExchangePort, BotConfig, BotService
from kolabi.bot.strategy_runtime import StrategyRunResult
from kolabi.bot.tsv import read_strategy_file
from kolabi.shared.config import ExchangeConfig
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import PlaceHeadCommand, PlaceOrderCommandRequest, RuntimeCommandKind, Symbol


def test_demo_ada_strategy_parsed_and_planned_on_active_runtime() -> None:
    strategy = read_strategy_file(Path("orders/demo_ada.tsv"))
    assert len(strategy.pairs) >= 2

    service = BotService(
        BotConfig(symbol="XBTUSD", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    result = service.run_strategy(strategy, dry_run=True)

    assert isinstance(result, StrategyRunResult)
    assert len(result.commands) == len(strategy.pairs)
    first = result.commands[0]
    first_pair = strategy.pairs[0]
    assert first.pair_name == first_pair.name
    assert first.role is not None and first.role.value == "head"
    assert first.request is not None


def test_kraken_run_strategy_rejects_too_small_absolute_quantity(monkeypatch) -> None:
    class FakeKrakenAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def instrument_rules(self, symbol: str):
            return {"symbol": symbol, "minQuantity": 30.0}

    strategy = read_strategy_file(Path("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv"))
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.exchange_config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        adapter_kwargs={},
    )
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _: FakeKrakenAdapter)

    try:
        service.run_strategy(strategy, dry_run=True)
    except ValueError as exc:
        assert "below the minimum quantity 30" in str(exc)
    else:
        raise AssertionError("Expected quantity validation to fail before dispatch")


def test_adapter_exchange_port_forwards_execinst_once(monkeypatch) -> None:
    class FakeAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.calls: list[dict[str, object]] = []

        def place_order(self, side: str, orderQty: object, **params: object) -> OrderAck:
            self.calls.append({"side": side, "orderQty": orderQty, **params})
            return OrderAck(order_id="OID-1", status="New")

    adapter_holder: dict[str, FakeAdapter] = {}

    def build_adapter(**kwargs) -> FakeAdapter:
        adapter = FakeAdapter(**kwargs)
        adapter_holder["adapter"] = adapter
        return adapter

    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _: build_adapter)
    port = AdapterExchangePort(
        exchange="kraken",
        exchange_config=ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url="https://demo-futures.kraken.com",
            symbol="PI_XBTUSD",
            adapter_kwargs={},
        ),
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Limit",
            orderQty=11,
            price=75000.0,
            execInst="ParticipateDoNotInitiate",
            clOrdID="CID-1",
        ),
    )

    ack = asyncio.run(port.place_head(command))

    assert ack.order_id == "OID-1"
    assert adapter_holder["adapter"].calls == [
        {
            "side": "sell",
            "orderQty": 11,
            "price": 75000.0,
            "type_": "Limit",
            "clOrdID": "CID-1",
            "execInst": "ParticipateDoNotInitiate",
        }
    ]
