from __future__ import annotations

from pathlib import Path
from typing import List

from kolabi.bot.indicators import DummyIndicatorClient
from kolabi.bot.runtime.auditor import MarketAuditor
from kolabi.bot.service import BotConfig, BotService
from kolabi.bot.tsv import read_strategy_file
from kolabi.runtime.kola.multi_kola import KolaMarketAuditor
from kolabi.shared.config import ExchangeConfig


class RecordingAuditor:
    def __init__(self) -> None:
        self.started = False
        self.calls: List[dict] = []

    def start_server(self) -> None:
        self.started = True

    def go(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_demo_ada_strategy_parsed_and_dispatched(tmp_path: Path) -> None:
    strategy = read_strategy_file(Path("orders/demo_ada.tsv"))
    assert len(strategy.pairs) >= 2

    auditor = RecordingAuditor()
    service = BotService(
        BotConfig(symbol="XBTUSD", require_ready=False),
        auditor=auditor,  # type: ignore[arg-type]
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.run_strategy(strategy, asynchronous=False)

    assert auditor.started
    assert len(auditor.calls) == len(strategy.pairs)
    first = auditor.calls[0]
    first_pair = strategy.pairs[0]
    assert set(first.keys()) == {
        "tps_run",
        "prix",
        "essais",
        "side",
        "q",
        "tp",
        "atype",
        "oType",
        "nameT",
        "updatepause",
        "logpause",
        "dr_pause",
        "tType",
        "timeout",
        "oDelta",
        "tDelta",
        "hook",
    }
    assert first["nameT"] == first_pair.name
    assert first["side"] == first_pair.head.side.value
    assert first["prix"] == first_pair.head_price


def test_kraken_run_strategy_rejects_too_small_absolute_quantity(monkeypatch) -> None:
    class FakeKrakenAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def instrument_rules(self, symbol: str):
            return {"symbol": symbol, "minQuantity": 30.0}

    strategy = read_strategy_file(Path("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv"))
    auditor = RecordingAuditor()
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        auditor=auditor,  # type: ignore[arg-type]
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
        service.run_strategy(strategy, asynchronous=False)
    except ValueError as exc:
        assert "below the minimum quantity 30" in str(exc)
    else:
        raise AssertionError("Expected quantity validation to fail before dispatch")


def test_active_market_auditor_no_longer_subclasses_multi_kola() -> None:
    assert KolaMarketAuditor not in MarketAuditor.__mro__
