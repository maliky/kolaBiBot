from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from kolabi.shared.config import ExchangeConfig
from kolabi.shared.core.bargain import Bargain
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.core.runtime_types import OrderQty, Price, StopPrice
from kolabi.shared.core.types import ExchangeABC
from kolabi.shared.persistence import Base, ExchangeConnection, MarketSnapshot
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


class _FakeAdapter(ExchangeABC):
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        symbol: str,
        **kwargs: object,
    ) -> None:
        super().__init__(api_key, api_secret, base_url, symbol)
        self.params = {
            "api_key": api_key,
            "api_secret": api_secret,
            "base_url": base_url,
            "symbol": symbol,
            **kwargs,
        }

    def place_order(
        self,
        side: str,
        orderQty: OrderQty | float,
        price: Price | float | None = None,
        stopPx: StopPrice | float | None = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        raise NotImplementedError

    def amend_order(self, order_id: str, **params: float) -> OrderAck:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> OrderAck:
        raise NotImplementedError

    def get_position(self) -> Position:
        raise NotImplementedError

    def get_balance(self) -> float:
        raise NotImplementedError

    def exec_orders(self) -> list[dict[str, object]]:
        return []


def test_bargain_instantiates_adapter(monkeypatch) -> None:
    config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="PI_XBTUSD",
    )

    monkeypatch.setattr("kolabi.shared.core.bargain.get_adapter", lambda _: _FakeAdapter)
    bargain = Bargain("kraken", config)
    assert isinstance(bargain.crypto_api, _FakeAdapter)
    assert bargain.describe() == "kraken:PI_XBTUSD"


def test_bargain_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_SECRET", "s")
    monkeypatch.setattr("kolabi.shared.core.bargain.get_adapter", lambda _: _FakeAdapter)
    bargain = Bargain.from_env("kraken", symbol="PI_XBTUSD", environment="demo")
    assert bargain.config.api_key == "k"


def test_bargain_initialization_error(monkeypatch) -> None:
    class BrokenAdapter(_FakeAdapter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("boom")

    config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="PI_XBTUSD",
    )
    monkeypatch.setattr("kolabi.shared.core.bargain.get_adapter", lambda _: BrokenAdapter)
    with pytest.raises(RuntimeError):
        Bargain("kraken", config)


def test_bargain_reads_kraken_prices_from_market_db(monkeypatch, tmp_path) -> None:
    market_db = f"sqlite:///{tmp_path / 'pub.sqlite'}"
    account_db = f"sqlite:///{tmp_path / 'prv.sqlite'}"
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(market_engine) as session:
        session.add(
            MarketSnapshot(
                local_uuid="snap-1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                symbol="PI_XBTUSD",
                best_bid=99.0,
                best_ask=101.0,
                avg_bid=98.5,
                avg_ask=101.5,
                mid_price=100.0,
                spread=2.0,
                imbalance=0.52,
                source_timestamp=now - timedelta(seconds=1),
                local_timestamp=now - timedelta(seconds=1),
            )
        )
        session.commit()
    with Session(account_engine) as session:
        session.add_all(
            [
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="private_ws",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=1),
                    updated_at=now - timedelta(seconds=1),
                ),
                ExchangeConnection(
                    exchange="kraken",
                    environment="demo",
                    market_type="futures",
                    stream_kind="rest_reconciler",
                    status="healthy",
                    last_heartbeat_at=now - timedelta(seconds=5),
                    updated_at=now - timedelta(seconds=5),
                ),
            ]
        )
        session.commit()

    config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="PI_XBTUSD",
        adapter_kwargs={
            "environment": "demo",
            "public_db_url": market_db,
            "account_db_url": account_db,
        },
    )

    monkeypatch.setattr("kolabi.shared.core.bargain.get_adapter", lambda _: _FakeAdapter)
    bargain = Bargain("kraken", config)

    assert bargain.prices("askPrice") == 101.0
    assert bargain.prices("bidPrice") == 99.0
    assert bargain.prices("midPrice") == 100.0
    assert bargain.prices("lastPrice", side="buy") == 101.0
    assert bargain.prices("lastPrice", side="sell") == 99.0


def test_bargain_execution_uses_exec_orders_for_kraken(monkeypatch) -> None:
    class ExecAdapter(_FakeAdapter):
        def exec_orders(self) -> list[dict[str, object]]:
            return [
                {
                    "orderID": "OID-1",
                    "clOrdID": "mlk_Test-PO123",
                    "side": "Buy",
                    "orderQty": 2,
                    "price": 100.0,
                    "execType": "New",
                    "ordStatus": "New",
                    "transactTime": "2026-05-13T15:24:00+00:00",
                },
                {
                    "orderID": "OID-1",
                    "clOrdID": "mlk_Test-PO123",
                    "side": "Buy",
                    "orderQty": 2,
                    "price": 100.0,
                    "execType": "Trade",
                    "ordStatus": "Filled",
                    "transactTime": "2026-05-13T15:24:01+00:00",
                },
            ]

    config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://example",
        symbol="PI_XBTUSD",
        adapter_kwargs={"environment": "demo"},
    )
    monkeypatch.setattr("kolabi.shared.core.bargain.get_adapter", lambda _: ExecAdapter)
    bargain = Bargain("kraken", config)

    df = bargain.execution()

    assert list(df["clOrdID"]) == ["mlk_Test-PO123", "mlk_Test-PO123"]
    assert list(df["execType"]) == ["New", "Trade"]
