from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Protocol, TypedDict, cast

from kolabi.bot.indicators import (
    DummyIndicatorClient,
    IndicatorClient,
    KrakenDbIndicatorClient,
)
from kolabi.bot.domain import OrderPairSpec, StrategySpec
from kolabi.bot.persistence import OrderRecorder, PersistenceConfig
from kolabi.bot.strategy_runtime import (
    KrakenPrivateOrderPollingSource,
    KrakenPublicTriggerSource,
    LegacyOgunExecutor,
    SimulatedExecutor,
    StrategyRunResult,
    StrategyRuntime,
    StaticHookSource,
    plan_strategy_once,
)
from kolabi.shared.core.bargain import Bargain
from kolabi.shared.config import ExchangeConfig, load_exchange_config
from kolabi.shared.exchanges import get_adapter
from kolabi.shared.kraken_futures import kraken_futures_environment
from kolabi.shared.logging import setup_logging
from kolabi.shared.runtime_state import KrakenRuntimeStateClient, StrategyRuntimeState
from kolabi.tree.account import (
    AccountStateStore,
    AccountStreamConfig,
    KrakenFuturesPrivateStream,
    credentials_from_env,
)


class InstrumentRulesExchange(Protocol):
    def instrument_rules(self, symbol: str | None = None) -> dict[str, object]: ...


@dataclass
class BotConfig:
    """Runtime configuration for kolaBiBot."""

    exchange: str = "kraken"
    symbol: str = "PI_XBTUSD"
    environment: str = "demo"
    updatepause: int = 10
    logpause: int = 60
    dummy: bool = False
    log_level: str = "INFO"
    db_url: Optional[str] = None
    market_db_url: Optional[str] = None
    account_db_url: Optional[str] = None
    require_ready: bool = True
    ready_timeout_seconds: float = 45.0
    ready_poll_seconds: float = 1.0
    max_public_age_seconds: float = 15.0
    max_private_age_seconds: float = 30.0
    max_reconcile_age_seconds: float = 300.0


class BotService:
    """High-level orchestrator for the single active `kolabi.bot` strategy path."""

    def __init__(
        self,
        config: BotConfig,
        indicators: IndicatorClient | None = None,
    ) -> None:
        self.config = config
        self.logger = setup_logging(config.log_level)
        self.exchange_config: ExchangeConfig | None = None
        market_db_url = config.market_db_url
        account_db_url = config.account_db_url
        if config.exchange.lower() == "kraken":
            env_cfg = kraken_futures_environment(config.environment)
            market_db_url = market_db_url or env_cfg.public_db_url
            account_db_url = account_db_url or env_cfg.private_db_url
        self.indicators: IndicatorClient = indicators or (
            KrakenDbIndicatorClient(
                # > should probably use global constant here instead of string
                db_url=market_db_url or "sqlite:///pub-futures-demo.sqlite",
                environment=config.environment,
                # > Same here for the moment we swith to spot
                market_type="futures",
            )
            # > Note that kraken is not the only target for this bot.
            if config.exchange.lower() == "kraken"
            else DummyIndicatorClient()
        )
        self.recorder: OrderRecorder | None = (
            OrderRecorder(PersistenceConfig(config.db_url))
            if config.db_url
            else None
        )
        self._server_started = False
        self._account_thread: Any = None
        self._account_db_url = account_db_url
        self._market_db_url = market_db_url
        self.runtime_state: KrakenRuntimeStateClient | None = None
        if (
            config.exchange.lower() == "kraken"
            and market_db_url is not None
            and account_db_url is not None
        ):
            self.runtime_state = KrakenRuntimeStateClient(
                market_db_url=market_db_url,
                account_db_url=account_db_url,
                symbol=config.symbol,
                exchange=config.exchange.lower(),
                environment=config.environment,
                market_type="futures",
                max_public_age_seconds=config.max_public_age_seconds,
                max_private_age_seconds=config.max_private_age_seconds,
                max_reconcile_age_seconds=config.max_reconcile_age_seconds,
            )

    def start(self) -> None:
        if not self._server_started:
            self._start_kraken_private_stream()
            self._wait_until_ready()
            self._server_started = True

    def preflight(self) -> dict[str, object]:
        """Return the current runtime readiness payload."""
        if self.runtime_state is None:
            return {
                "exchange": self.config.exchange,
                "symbol": self.config.symbol,
                "ready": True,
                "reasons": (),
                "status": "not_applicable",
            }
        state = self.runtime_state.fetch_runtime_state()
        payload = state.as_dict()
        payload["status"] = "ok" if state.ready else "waiting"
        return payload

    def _start_kraken_private_stream(self) -> None:
        if (
            self.config.exchange.lower() != "kraken"
            or self._account_thread is not None
        ):
            return
        env_cfg = kraken_futures_environment(self.config.environment)
        stream_config = AccountStreamConfig(
            db_url=self._account_db_url or env_cfg.private_db_url,
            environment=self.config.environment,
            market_type="futures",
            ws_url=env_cfg.private_ws_url,
            rest_url=env_cfg.rest_url,
            api_key_env=env_cfg.api_key_env,
            api_secret_env=env_cfg.api_secret_env,
        )
        store = AccountStateStore(stream_config)
        credentials = credentials_from_env(stream_config)

        def _run_stream() -> None:
            store.record_connection_status("rest_reconciler", "starting")
            asyncio.run(_run_private_stack(stream_config, store, credentials))

        self._account_thread = threading.Thread(
            target=_run_stream,
            name="kraken-private-stream",
            daemon=True,
        )
        self._account_thread.start()

    def _wait_until_ready(self) -> None:
        """Wait for fresh Kraken public/private state before starting the runtime."""
        if (
            self.runtime_state is None
            or self.config.exchange.lower() != "kraken"
            or not self.config.require_ready
        ):
            return
        self.logger.info(
            "kraken runtime preflight symbol=%s env=%s market_db=%s account_db=%s",
            self.config.symbol,
            self.config.environment,
            self._market_db_url,
            self._account_db_url,
        )
        state = self.runtime_state.wait_until_ready(
            timeout_seconds=self.config.ready_timeout_seconds,
            poll_seconds=self.config.ready_poll_seconds,
        )
        if not state.ready:
            raise TimeoutError(self._format_wait_timeout(state))
        self.logger.info(
            "kraken runtime ready symbol=%s public_age=%.2fs private_age=%.2fs",
            state.symbol,
            state.public.age_seconds or 0.0,
            state.private_ws.age_seconds or 0.0,
        )

    def _format_wait_timeout(self, state: StrategyRuntimeState) -> str:
        """Format a short readiness timeout message for CLI users."""
        reasons = ", ".join(state.reasons) if state.reasons else "unknown readiness failure"
        return (
            "Kraken runtime did not become ready within "
            f"{self.config.ready_timeout_seconds:.0f}s: {reasons}"
        )

    def run_strategy(
        self,
        strategy: StrategySpec,
        *,
        dry_run: bool = False,
        simulate: bool = False,
    ) -> StrategyRunResult:
        """Execute the active typed runtime path in the foreground."""
        pair_list = list(strategy.pairs)
        if not dry_run or self.exchange_config is not None:
            self._validate_pairs(pair_list)
        if not dry_run and not simulate:
            self.start()
        for pair in pair_list:
            kwargs = self._pair_to_kwargs(pair)
            snapshot = self.indicators.fetch_snapshot(self.config.symbol)
            run_id: Optional[int] = None
            if self.recorder:
                run = self.recorder.start_run(pair, snapshot)
                run_id = run.id
                self.logger.info(f"[{pair.name}] submitted run #{run_id}")
            del kwargs, run_id
        runtime = StrategyRuntime(
            strategy=strategy,
            symbol=self.config.symbol,
            executor=None if dry_run else self._build_executor(simulate=simulate),
            public_source=self._build_public_source(simulate=simulate),
            private_source=self._build_private_source(simulate=simulate),
            simulate=simulate,
        )
        if dry_run:
            return plan_strategy_once(strategy=strategy, symbol=self.config.symbol)
        return asyncio.run(runtime.run())

    def run_orders(self, pairs: Iterable[OrderPairSpec], *, dry_run: bool = False, simulate: bool = False) -> StrategyRunResult:
        """Compatibilite: accepte directement une liste de paires canoniques."""
        return self.run_strategy(
            StrategySpec(name="compat", pairs=tuple(pairs)),
            dry_run=dry_run,
            simulate=simulate,
        )

    def _validate_pairs(self, pairs: Iterable[OrderPairSpec]) -> None:
        """Valide les contraintes instrument Kraken avant envoi."""
        if self.config.exchange.lower() != "kraken":
            return
        if self.exchange_config is None:
            self._ensure_exchange_config()
        assert self.exchange_config is not None
        adapter_cls = get_adapter("kraken")
        adapter = cast(
            InstrumentRulesExchange,
            adapter_cls(
            api_key=self.exchange_config.api_key,
            api_secret=self.exchange_config.api_secret,
            base_url=self.exchange_config.base_url,
            symbol=self.exchange_config.symbol,
            **self.exchange_config.adapter_kwargs,
            ),
        )
        rules = adapter.instrument_rules(self.config.symbol)
        raw_min_qty = rules.get("minQuantity")
        min_qty = (
            float(raw_min_qty)
            if isinstance(raw_min_qty, (int, float, str)) and raw_min_qty
            else 1.0
        )
        for pair in pairs:
            if (
                pair.head_quantity_type == "qA"
                and pair.head_quantity is not None
                and float(pair.head_quantity) < min_qty
            ):
                raise ValueError(
                    f"Strategy '{pair.name}' quantity {pair.head_quantity} is below "
                    f"the minimum quantity {min_qty:g} for {self.config.symbol}."
                )

    def _pair_to_kwargs(self, pair: OrderPairSpec) -> dict[str, object]:
        """Conserve un resume local pour journaux/tests de transition."""
        return {
            "nameT": pair.name,
            "side": pair.head.side.value,
            "prix": pair.head_price,
        }

    def _build_executor(self, *, simulate: bool):
        if simulate:
            return SimulatedExecutor()
        self._ensure_exchange_config()
        if self.exchange_config is None:
            raise RuntimeError("Exchange configuration is required for active execution")
        bargain = Bargain(self.config.exchange, self.exchange_config)
        return LegacyOgunExecutor(bargain)

    def _build_public_source(self, *, simulate: bool):
        if simulate:
            return StaticHookSource()
        if self.runtime_state is not None:
            return KrakenPublicTriggerSource(self.runtime_state)
        return StaticHookSource()

    def _build_private_source(self, *, simulate: bool):
        if simulate or self.runtime_state is None:
            return None
        return KrakenPrivateOrderPollingSource(self.runtime_state)

    def _ensure_exchange_config(self) -> None:
        if self.exchange_config is not None:
            return
        self.exchange_config = load_exchange_config(
            self.config.exchange,
            symbol=self.config.symbol,
            environment=self.config.environment,
        )
        if self._market_db_url is not None:
            self.exchange_config.adapter_kwargs.setdefault("public_db_url", self._market_db_url)
        if self._account_db_url is not None:
            self.exchange_config.adapter_kwargs.setdefault("account_db_url", self._account_db_url)


async def _run_private_stack(
    config: AccountStreamConfig,
    store: AccountStateStore,
    credentials: Any,
) -> None:
    """Start one reconcile pass, then keep the private websocket alive."""
    from kolabi.tree.account import KrakenFuturesRestReconciler

    reconciler = KrakenFuturesRestReconciler(config, store, credentials)
    try:
        reconciler.reconcile_once()
    except Exception as exc:
        store.record_connection_status("rest_reconciler", "error", last_error=str(exc))
    stream = KrakenFuturesPrivateStream(config, store, credentials)
    await stream.run()
