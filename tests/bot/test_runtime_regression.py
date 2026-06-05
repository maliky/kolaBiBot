from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from kolabi.bot.domain import (
    HeadState,
    OrderIdentity,
    PairCycleState,
    StrategySpec,
    TailState,
)
from kolabi.bot.indicators import DummyIndicatorClient
from kolabi.bot.service import AdapterExchangePort, BotConfig, BotService
from kolabi.bot.strategy_runtime import StrategyRunResult, StrategyRuntime
from kolabi.bot.tsv import read_strategy_file
from kolabi.shared.config import ExchangeConfig
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.core.runtime_types import (
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
)
from kolabi.shared.runtime_state import (
    PrivateFeedState,
    PublicMarketState,
    StrategyRuntimeState,
)


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


def test_bot_service_keeps_audit_and_telemetry_lanes_off_account_db(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_FUTURE_DEMO_API_SECRET", "s")
    account_db_url = f"sqlite:///{tmp_path / 'account.sqlite'}"
    audit_db_url = f"sqlite:///{tmp_path / 'audit.sqlite'}"
    telemetry_db_url = f"sqlite:///{tmp_path / 'telemetry.sqlite'}"
    service = BotService(
        BotConfig(
            symbol="PI_XBTUSD",
            exchange="kraken",
            require_ready=False,
            account_db_url=account_db_url,
            audit_db_url=audit_db_url,
            telemetry_db_url=telemetry_db_url,
            account_scope="advers",
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    assert service._account_db_url == account_db_url
    assert service._audit_db_url == audit_db_url
    assert service._telemetry_db_url == telemetry_db_url
    service._ensure_exchange_config()

    assert service.exchange_config is not None
    assert service.exchange_config.adapter_kwargs["account_db_url"] == account_db_url
    assert service.exchange_config.adapter_kwargs["audit_db_url"] == audit_db_url
    assert service.exchange_config.adapter_kwargs["account_scope"] == "advers"


def test_bot_service_uses_scoped_kolabi_db_env_lanes(monkeypatch) -> None:
    monkeypatch.setenv("KOLABI_MARKET_DB_URL", "postgresql+psycopg://x/market")
    monkeypatch.setenv("KOLABI_ACCOUNT_DB_URL", "postgresql+psycopg://x/main_account")
    monkeypatch.setenv("KOLABI_CRITICAL_DB_URL", "postgresql+psycopg://x/main_critical")
    monkeypatch.setenv("KOLABI_AUDIT_DB_URL", "postgresql+psycopg://x/main_audit")
    monkeypatch.setenv("KOLABI_TELEMETRY_DB_URL", "postgresql+psycopg://x/main_telemetry")
    monkeypatch.setenv(
        "KOLABI_ADVERS_ACCOUNT_DB_URL",
        "postgresql+psycopg://x/advers_account",
    )
    monkeypatch.setenv(
        "KOLABI_ADVERS_CRITICAL_DB_URL",
        "postgresql+psycopg://x/advers_critical",
    )
    monkeypatch.setenv(
        "KOLABI_ADVERS_AUDIT_DB_URL",
        "postgresql+psycopg://x/advers_audit",
    )
    monkeypatch.setenv(
        "KOLABI_ADVERS_TELEMETRY_DB_URL",
        "postgresql+psycopg://x/advers_telemetry",
    )
    service = BotService(
        BotConfig(
            symbol="PI_XBTUSD",
            exchange="kraken",
            require_ready=False,
            account_scope="advers",
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    assert service._market_db_url == "postgresql+psycopg://x/market"
    assert service._account_db_url == "postgresql+psycopg://x/advers_account"
    assert service._critical_account_db_url == "postgresql+psycopg://x/advers_critical"
    assert service._audit_db_url == "postgresql+psycopg://x/advers_audit"
    assert service._telemetry_db_url == "postgresql+psycopg://x/advers_telemetry"


def test_bot_service_dry_run_preserves_pair_symbols() -> None:
    base_pair = read_strategy_file(Path("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv")).pairs[0]
    strategy = StrategySpec(
        name="multi-symbol",
        pairs=(
            replace(base_pair, name="xbt", symbol="PI_XBTUSD"),
            replace(base_pair, name="eth", symbol="PI_ETHUSD"),
        ),
    )
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    result = service.run_strategy(strategy, dry_run=True)

    assert {str(command.symbol) for command in result.commands} == {
        "PI_XBTUSD",
        "PI_ETHUSD",
    }
    assert service._required_symbols == ("PI_ETHUSD", "PI_XBTUSD")


def test_bot_service_requires_shared_market_db_for_active_multi_symbol_strategy(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KOLABI_MARKET_DB_URL", raising=False)
    base_pair = read_strategy_file(Path("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv")).pairs[0]
    strategy = StrategySpec(
        name="multi-symbol",
        pairs=(
            replace(base_pair, name="xbt", symbol="PI_XBTUSD"),
            replace(base_pair, name="eth", symbol="PI_ETHUSD"),
        ),
    )
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    with pytest.raises(ValueError, match="shared market DB URL"):
        service.run_strategy(strategy, dry_run=False, simulate=False)


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


def test_adapter_exchange_port_derives_stop_limit_price_from_tail_offset(monkeypatch) -> None:
    class FakeAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.calls: list[dict[str, object]] = []

        def place_order(self, side: str, orderQty: object, **params: object) -> OrderAck:
            self.calls.append({"side": side, "orderQty": orderQty, **params})
            return OrderAck(order_id="OID-1", status="New")

        def live_trigger_orders(self) -> list[dict[str, object]]:
            return []

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
        verify_tail_on_place=False,
    )
    command = PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="SL",
            orderQty=11,
            stopPx=Decimal("100"),
            oDelta=Decimal("0.5"),
            clOrdID="CID-T",
        ),
    )

    ack = asyncio.run(port.place_tail(command))

    assert ack.order_id == "OID-1"
    assert adapter_holder["adapter"].calls == [
        {
            "side": "sell",
            "orderQty": 11,
            "price": Decimal("99.5"),
            "stopPx": Decimal("100"),
            "type_": "SL",
            "clOrdID": "CID-T",
            "oDelta": Decimal("0.5"),
        }
    ]


def test_interrupt_cleanup_cancels_tail_by_exchange_id_and_reverses_played_qty(monkeypatch) -> None:
    strategy = read_strategy_file(Path("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv"))
    pair = strategy.pairs[0]
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    runtime = StrategyRuntime(strategy=strategy, symbol="PI_XBTUSD", simulate=True)
    runtime.state = replace(
        runtime.state,
        pairs={
            pair.name: PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                played_quantity=Decimal("10"),
                tail_identity=OrderIdentity(
                    pair_name=pair.name,
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
            )
        },
    )
    expected_close_side = "sell" if pair.head.side.value == "buy" else "buy"
    initial_position = 12.0 if expected_close_side == "sell" else -12.0
    expected_after = initial_position - 10.0 if expected_close_side == "sell" else initial_position + 10.0

    class FakeAdapter:
        def __init__(self, position: float) -> None:
            self.position = position
            self.cancelled: list[str] = []
            self.closed: list[tuple[str, float]] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

        def place_order(self, side: str, orderQty: object, **_params: object) -> OrderAck:
            qty = float(orderQty) if isinstance(orderQty, (int, float)) else float(str(orderQty))
            self.closed.append((side, qty))
            if side == "sell":
                self.position -= qty
            else:
                self.position += qty
            return OrderAck(order_id=f"CLOSE-{len(self.closed)}", status="New", side=side)

        def get_position(self) -> Position:
            return Position(symbol="PI_XBTUSD", qty=self.position)

        def live_open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders(self) -> list[dict[str, object]]:
            return []

        def open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders_db(self) -> list[dict[str, object]]:
            return []

    adapter = FakeAdapter(initial_position)
    monkeypatch.setattr(
        service,
        "_build_admin_port",
        lambda: type("Port", (), {"adapter": adapter})(),
    )

    summary = service.cleanup_interrupted_pairs(runtime)

    assert adapter.cancelled == ["OID-T"]
    assert adapter.closed == [(expected_close_side, 10.0)]
    assert summary["tail_cancelled"] == 1
    assert summary["close_orders"] == 1
    assert summary["position_before_qty"] == initial_position
    assert summary["position_after_qty"] == expected_after


def test_interrupt_cleanup_resolves_tail_exchange_id_from_client_id(monkeypatch) -> None:
    strategy = read_strategy_file(Path("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv"))
    pair = strategy.pairs[0]
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    runtime = StrategyRuntime(strategy=strategy, symbol="PI_XBTUSD", simulate=True)
    runtime.state = replace(
        runtime.state,
        pairs={
            pair.name: PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.SUBMITTED,
                played_quantity=Decimal("7"),
                tail_identity=OrderIdentity(
                    pair_name=pair.name,
                    role="tail",
                    client_order_id="CID-T-ONLY",
                    exchange_order_id=None,
                ),
            )
        },
    )
    expected_close_side = "sell" if pair.head.side.value == "buy" else "buy"
    initial_position = 5.0 if expected_close_side == "sell" else -5.0

    class FakeAdapter:
        def __init__(self, position: float) -> None:
            self.position = position
            self.cancelled: list[str] = []
            self.closed: list[tuple[str, float]] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

        def place_order(self, side: str, orderQty: object, **_params: object) -> OrderAck:
            qty = float(orderQty) if isinstance(orderQty, (int, float)) else float(str(orderQty))
            self.closed.append((side, qty))
            if side == "sell":
                self.position -= qty
            else:
                self.position += qty
            return OrderAck(order_id=f"CLOSE-{len(self.closed)}", status="New", side=side)

        def get_position(self) -> Position:
            return Position(symbol="PI_XBTUSD", qty=self.position)

        def live_open_orders(self) -> list[dict[str, object]]:
            return [
                {
                    "client_order_id": "CID-T-ONLY",
                    "order_id": "OID-FOUND",
                }
            ]

        def live_trigger_orders(self) -> list[dict[str, object]]:
            return []

        def open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders_db(self) -> list[dict[str, object]]:
            return []

    adapter = FakeAdapter(initial_position)
    monkeypatch.setattr(
        service,
        "_build_admin_port",
        lambda: type("Port", (), {"adapter": adapter})(),
    )

    summary = service.cleanup_interrupted_pairs(runtime)

    assert adapter.cancelled == ["OID-FOUND"]
    assert adapter.closed == [(expected_close_side, 5.0)]
    assert summary["tail_cancelled"] == 1
    assert summary["close_orders"] == 1
    assert summary["position_before_qty"] == initial_position
    assert summary["position_after_qty"] == 0.0


def test_runtime_error_triggers_live_cleanup_before_reraising(monkeypatch) -> None:
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    cleanup_calls: list[object] = []

    class FailingRuntime:
        async def run(self) -> StrategyRunResult:
            raise RuntimeError("head fill reference price missing")

    def cleanup(runtime: object) -> dict[str, object]:
        cleanup_calls.append(runtime)
        return {
            "pairs": 1,
            "tail_cancelled": 1,
            "close_orders": 1,
            "position_before_qty": 2.0,
            "position_after_qty": 0.0,
            "errors": 0,
        }

    monkeypatch.setattr(service, "cleanup_interrupted_pairs", cleanup)
    runtime = FailingRuntime()

    with pytest.raises(RuntimeError, match="head fill reference"):
        service._run_runtime_with_cleanup(runtime, simulate=False)  # type: ignore[arg-type]

    assert cleanup_calls == [runtime]


def test_wait_timeout_message_includes_runtime_diagnostics() -> None:
    service = BotService(BotConfig(exchange="kraken", ready_timeout_seconds=45.0))
    state = StrategyRuntimeState(
        symbol="PI_XBTUSD",
        public=PublicMarketState(
            symbol="PI_XBTUSD",
            best_bid=None,
            best_ask=None,
            mid_price=None,
            last_price=None,
            mark_price=None,
            index_price=None,
            tick_size=None,
            spread=None,
            imbalance=None,
            avg_bid=None,
            avg_ask=None,
            recorded_at=None,
            source_timestamp=None,
            age_seconds=9.5,
            source_age_seconds=None,
            indicators={},
            ready=False,
            reason="public market data is stale",
        ),
        private_ws=PrivateFeedState(
            stream_kind="private_ws",
            status="healthy",
            updated_at="2026-05-29T16:04:46.183674",
            last_heartbeat_at="2026-05-29T16:04:46.183674",
            age_seconds=266.1,
            ready=False,
            last_error=None,
            reason="private_ws state is stale",
        ),
        rest_reconciler=PrivateFeedState(
            stream_kind="rest_reconciler",
            status="healthy",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=True,
            last_error=None,
            reason=None,
        ),
        open_order_count=0,
        fill_count=0,
        position_size=None,
        position_entry_price=None,
        ready=False,
        reasons=("private_ws state is stale",),
    )

    message = service._format_wait_timeout(state)

    assert "private_ws state is stale" in message
    assert "public_age=9.50s" in message
    assert "private_status=healthy" in message
    assert "private_age=266.10s" in message
    assert "private_last_heartbeat=2026-05-29T16:04:46.183674" in message
    assert "account_scope=default" in message


def test_wait_timeout_message_hints_private_feeder_for_missing_schema() -> None:
    service = BotService(
        BotConfig(exchange="kraken", account_scope="advers", ready_timeout_seconds=5.0)
    )
    state = StrategyRuntimeState(
        symbol="PI_XBTUSD",
        public=PublicMarketState(
            symbol="PI_XBTUSD",
            best_bid=100.0,
            best_ask=101.0,
            mid_price=100.5,
            last_price=100.0,
            mark_price=100.0,
            index_price=100.0,
            tick_size=0.5,
            spread=1.0,
            imbalance=0.5,
            avg_bid=100.0,
            avg_ask=101.0,
            recorded_at="2026-06-01T00:00:00+00:00",
            source_timestamp="2026-06-01T00:00:00+00:00",
            age_seconds=1.0,
            source_age_seconds=1.0,
            indicators={},
            ready=True,
            reason=None,
        ),
        private_ws=PrivateFeedState(
            stream_kind="private_ws",
            status="missing_schema",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=False,
            last_error=None,
            reason="private_ws DB schema missing",
        ),
        rest_reconciler=PrivateFeedState(
            stream_kind="rest_reconciler",
            status="missing_schema",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=True,
            last_error=None,
            reason=None,
        ),
        open_order_count=0,
        fill_count=0,
        position_size=None,
        position_entry_price=None,
        ready=False,
        reasons=("private_ws DB schema missing",),
    )

    message = service._format_wait_timeout(state)

    assert "private_ws DB schema missing" in message
    assert "account_scope=advers" in message
    assert "scripts/kolabidb private start --account-scope advers" in message
