from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional, Protocol, TypeVar, cast

from kolabi.bot.domain import OrderPairSpec, StrategySpec
from kolabi.bot.indicators import (
    DummyIndicatorClient,
    IndicatorClient,
    KrakenDbIndicatorClient,
)
from kolabi.bot.ogun_executor import OgunExecutor, RestFlightPolicy
from kolabi.bot.order_codes import parse_order_code
from kolabi.bot.persistence import (
    OrderRecorder,
    PersistenceConfig,
    TailTelemetryRecorder,
)
from kolabi.bot.strategy_runtime import (
    KrakenPrivateOrderPollingSource,
    KrakenPublicTriggerSource,
    PublicRuntimeStateReader,
    SimulatedExecutor,
    StaticHookSource,
    StrategyRunResult,
    StrategyRuntime,
    plan_strategy_once,
)
from kolabi.shared.binance_futures import (
    binance_futures_audit_db_url,
    binance_futures_critical_db_url,
    binance_futures_private_db_url,
    binance_futures_public_db_url,
    binance_futures_telemetry_db_url,
)
from kolabi.shared.config import ExchangeConfig, load_exchange_config
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    ExchangePort,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
)
from kolabi.shared.exchanges import get_adapter
from kolabi.shared.kraken_futures import (
    kraken_futures_audit_db_url,
    kraken_futures_environment,
    kraken_futures_public_db_url,
    kraken_futures_telemetry_db_url,
)
from kolabi.shared.logging import setup_logging
from kolabi.shared.pruning import DEFAULT_PRUNING, TimeCountPruning
from kolabi.shared.runtime_state import KrakenRuntimeStateClient, StrategyRuntimeState

_LOGGER = logging.getLogger("kola")
_T = TypeVar("_T")


def _env_scope_key(account_scope: str) -> str:
    """Return the env-var-safe account scope key used for Kolabi DB lanes."""

    raw = account_scope.strip() or "default"
    return "".join(ch if ch.isalnum() else "_" for ch in raw).upper()


def _kolabi_scoped_db_url(lane: str, account_scope: str) -> str | None:
    """Resolve KOLABI_* DB lane URLs without mixing scoped accounts by accident."""

    lane_key = lane.upper()
    if (account_scope.strip() or "default") == "default":
        return os.environ.get(f"KOLABI_{lane_key}_DB_URL")
    return os.environ.get(f"KOLABI_{_env_scope_key(account_scope)}_{lane_key}_DB_URL")


class InstrumentRulesExchange(Protocol):
    def instrument_rules(self, symbol: str | None = None) -> dict[str, object]: ...


class ExchangeAdapterLike(Protocol):
    def place_order(
        self,
        side: str,
        orderQty: object,
        price: object | None = None,
        stopPx: object | None = None,
        type_: str = "LIMIT",
        **params: object,
    ) -> OrderAck: ...
    def amend_order(self, order_id: str, **params: object) -> OrderAck: ...
    def cancel_order(self, order_id: str) -> OrderAck: ...
    def get_position(self) -> Any: ...


class TriggerOrderReader(Protocol):
    def live_trigger_orders(self) -> list[dict[str, Any]]: ...
    def live_trigger_orders_db(self) -> list[dict[str, Any]]: ...


class OpenOrderReader(Protocol):
    def live_open_orders(self) -> list[dict[str, Any]]: ...
    def live_trigger_orders(self) -> list[dict[str, Any]]: ...
    def open_orders(self) -> list[dict[str, Any]]: ...
    def live_trigger_orders_db(self) -> list[dict[str, Any]]: ...


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
    critical_account_db_url: Optional[str] = None
    audit_db_url: Optional[str] = None
    telemetry_db_url: Optional[str] = None
    account_scope: str = "default"
    api_key_env: Optional[str] = None
    api_secret_env: Optional[str] = None
    require_ready: bool = True
    ready_timeout_seconds: float = 45.0
    ready_poll_seconds: float = 1.0
    max_public_age_seconds: float = 15.0
    max_private_age_seconds: float = 30.0
    max_reconcile_age_seconds: float = 300.0
    tail_verify_timeout_seconds: float = 11.0
    tail_verify_poll_seconds: float = 0.5
    tail_visibility_timeout_seconds: float = 20.0
    max_active_pairs: int = 4
    rest_min_interval_seconds: float = 0.1
    rest_max_inflight: int = 1
    rest_audit_retention_minutes: int = DEFAULT_PRUNING.rest_audit.retention_minutes
    rest_audit_retention_limit: int = DEFAULT_PRUNING.rest_audit.retention_limit
    tail_telemetry_retention_minutes: int = (
        DEFAULT_PRUNING.tail_telemetry.retention_minutes
    )
    tail_telemetry_retention_limit: int = DEFAULT_PRUNING.tail_telemetry.retention_limit


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
        market_db_url = config.market_db_url or os.environ.get("KOLABI_MARKET_DB_URL")
        account_db_url = config.account_db_url or _kolabi_scoped_db_url(
            "ACCOUNT",
            config.account_scope,
        )
        critical_account_db_url = (
            config.critical_account_db_url
            or _kolabi_scoped_db_url("CRITICAL", config.account_scope)
        )
        audit_db_url = config.audit_db_url or _kolabi_scoped_db_url(
            "AUDIT",
            config.account_scope,
        )
        telemetry_db_url = config.telemetry_db_url or _kolabi_scoped_db_url(
            "TELEMETRY",
            config.account_scope,
        )
        if config.exchange.lower() == "kraken":
            env_cfg = kraken_futures_environment(config.environment)
            market_db_url = market_db_url or kraken_futures_public_db_url(
                config.environment,
                config.symbol,
            )
            account_db_url = account_db_url or env_cfg.private_db_url
            critical_account_db_url = (
                critical_account_db_url or env_cfg.critical_private_db_url
            )
            audit_db_url = audit_db_url or kraken_futures_audit_db_url(
                config.environment,
                config.account_scope,
            )
            telemetry_db_url = telemetry_db_url or kraken_futures_telemetry_db_url(
                config.environment,
                config.account_scope,
            )
        elif config.exchange.lower() == "binance":
            market_db_url = market_db_url or binance_futures_public_db_url(
                config.environment,
                config.symbol,
            )
            account_db_url = account_db_url or binance_futures_private_db_url(
                config.environment,
                config.account_scope,
            )
            critical_account_db_url = (
                critical_account_db_url
                or binance_futures_critical_db_url(
                    config.environment,
                    config.account_scope,
                )
            )
            audit_db_url = audit_db_url or binance_futures_audit_db_url(
                config.environment,
                config.account_scope,
            )
            telemetry_db_url = telemetry_db_url or binance_futures_telemetry_db_url(
                config.environment,
                config.account_scope,
            )
        self.indicators: IndicatorClient = indicators or (
            KrakenDbIndicatorClient(
                db_url=market_db_url
                or kraken_futures_public_db_url(config.environment, config.symbol),
                exchange=config.exchange.lower(),
                environment=config.environment,
                market_type="futures",
            )
            if config.exchange.lower() in {"kraken", "binance"}
            else DummyIndicatorClient()
        )
        self.recorder: OrderRecorder | None = (
            OrderRecorder(PersistenceConfig(config.db_url))
            if config.db_url
            else None
        )
        self._server_started = False
        self._account_db_url = account_db_url
        self._critical_account_db_url = critical_account_db_url
        self._market_db_url = market_db_url
        self._audit_db_url = audit_db_url
        self._telemetry_db_url = telemetry_db_url
        self.runtime_state: KrakenRuntimeStateClient | None = None
        if (
            config.exchange.lower() in {"kraken", "binance"}
            and market_db_url is not None
            and account_db_url is not None
        ):
            self.runtime_state = KrakenRuntimeStateClient(
                market_db_url=market_db_url,
                account_db_url=account_db_url,
                critical_account_db_url=critical_account_db_url,
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

    def _wait_until_ready(self) -> None:
        """Wait for fresh DB-grounded public/private state before starting runtime."""
        if (
            self.runtime_state is None
            or not self.config.require_ready
        ):
            return
        self.logger.info(
            "%s runtime preflight symbol=%s env=%s market_db=%s account_db=%s critical_account_db=%s",
            self.config.exchange.lower(),
            self.config.symbol,
            self.config.environment,
            self._market_db_url,
            self._account_db_url,
            self._critical_account_db_url or self._account_db_url,
        )
        state = self.runtime_state.wait_until_ready(
            timeout_seconds=self.config.ready_timeout_seconds,
            poll_seconds=self.config.ready_poll_seconds,
        )
        if not state.ready:
            raise TimeoutError(self._format_wait_timeout(state))
        self.logger.info(
            "%s runtime ready symbol=%s public_age=%.2fs private_age=%.2fs",
            self.config.exchange.lower(),
            state.symbol,
            state.public.age_seconds or 0.0,
            state.private_ws.age_seconds or 0.0,
        )

    def _format_wait_timeout(self, state: StrategyRuntimeState) -> str:
        """Format a short readiness timeout message for CLI users."""
        reasons = ", ".join(state.reasons) if state.reasons else "unknown readiness failure"
        public_age = (
            f"{state.public.age_seconds:.2f}s"
            if state.public.age_seconds is not None
            else "unknown"
        )
        private_age = (
            f"{state.private_ws.age_seconds:.2f}s"
            if state.private_ws.age_seconds is not None
            else "unknown"
        )
        private_last_heartbeat = state.private_ws.last_heartbeat_at or "-"
        hint = ""
        if state.private_ws.status in {"missing", "missing_schema"}:
            hint = (
                " start_private='scripts/kolabidb private start"
                f" --account-scope {self.config.account_scope}'"
            )
        return (
            f"{self.config.exchange.capitalize()} runtime did not become ready within "
            f"{self.config.ready_timeout_seconds:.0f}s: {reasons} "
            f"(public_age={public_age} private_status={state.private_ws.status} "
            f"private_age={private_age} private_last_heartbeat={private_last_heartbeat} "
            f"account_scope={self.config.account_scope}{hint})"
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
            snapshot = self.indicators.fetch_snapshot(self.config.symbol)
            run_id: Optional[int] = None
            if self.recorder:
                run = self.recorder.start_run(pair, snapshot)
                run_id = run.id
                self.logger.info(f"[{pair.name}] submitted run #{run_id}")
            del run_id
        runtime = StrategyRuntime(
            strategy=strategy,
            symbol=self.config.symbol,
            executor=None if dry_run else self._build_executor(simulate=simulate),
            public_source=self._build_public_source(simulate=simulate),
            private_source=self._build_private_source(simulate=simulate),
            public_state_reader=(
                None
                if simulate
                else cast(PublicRuntimeStateReader | None, self.runtime_state)
            ),
            tail_telemetry_writer=(
                None
                if dry_run or simulate or self._telemetry_db_url is None
                else TailTelemetryRecorder(
                    PersistenceConfig(
                        self._telemetry_db_url,
                        sqlite_busy_timeout_seconds=0.5,
                        tail_telemetry_pruning=TimeCountPruning(
                            retention_minutes=(
                                self.config.tail_telemetry_retention_minutes
                            ),
                            retention_limit=self.config.tail_telemetry_retention_limit,
                            maintenance_seconds=(
                                DEFAULT_PRUNING.tail_telemetry.maintenance_seconds
                            ),
                        ),
                    )
                )
            ),
            exchange=self.config.exchange.lower(),
            environment=self.config.environment,
            market_type="futures",
            account_scope=self.config.account_scope,
            tail_visibility_timeout_seconds=self.config.tail_visibility_timeout_seconds,
            max_active_pairs=self.config.max_active_pairs,
            simulate=simulate,
        )
        if dry_run:
            return plan_strategy_once(strategy=strategy, symbol=self.config.symbol)
        try:
            return asyncio.run(runtime.run())
        except KeyboardInterrupt:
            if not simulate:
                cleanup = self.cleanup_interrupted_pairs(runtime)
                self.logger.info(
                    "interrupt cleanup pairs=%s tail_cancelled=%s close_orders=%s qty_before=%s qty_after=%s errors=%s",
                    cleanup["pairs"],
                    cleanup["tail_cancelled"],
                    cleanup["close_orders"],
                    cleanup["position_before_qty"],
                    cleanup["position_after_qty"],
                    cleanup["errors"],
                )
            raise

    def run_orders(self, pairs: Iterable[OrderPairSpec], *, dry_run: bool = False, simulate: bool = False) -> StrategyRunResult:
        """Compatibilite: accepte directement une liste de paires canoniques."""
        return self.run_strategy(
            StrategySpec(name="compat", pairs=tuple(pairs)),
            dry_run=dry_run,
            simulate=simulate,
        )

    def _validate_pairs(self, pairs: Iterable[OrderPairSpec]) -> None:
        """Validate exchange-specific instrument and grammar constraints."""
        exchange = self.config.exchange.lower()
        if exchange not in {"kraken", "binance"}:
            return
        if self.exchange_config is None:
            self._ensure_exchange_config()
        assert self.exchange_config is not None
        adapter_cls = get_adapter(exchange)
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
            if exchange == "binance":
                _validate_binance_pair_grammar(pair)
            if (
                pair.head_quantity_type == "qA"
                and pair.head_quantity is not None
                and float(pair.head_quantity) < min_qty
            ):
                raise ValueError(
                    f"Strategy '{pair.name}' quantity {pair.head_quantity} is below "
                    f"the minimum quantity {min_qty:g} for {self.config.symbol}."
                )

    def _build_executor(self, *, simulate: bool):
        if simulate:
            return SimulatedExecutor()
        self._ensure_exchange_config()
        if self.exchange_config is None:
            raise RuntimeError("Exchange configuration is required for active execution")
        port = AdapterExchangePort(
            exchange=self.config.exchange.lower(),
            exchange_config=self.exchange_config,
            verify_timeout_seconds=self.config.tail_verify_timeout_seconds,
            verify_poll_seconds=self.config.tail_verify_poll_seconds,
            run_blocking_calls_in_thread=True,
            verify_tail_on_place=False,
        )
        return OgunExecutor(
            port,
            flight_policy=RestFlightPolicy(
                min_interval_seconds=self.config.rest_min_interval_seconds,
                max_inflight=self.config.rest_max_inflight,
            ),
        )

    def _build_public_source(self, *, simulate: bool):
        if simulate:
            return StaticHookSource()
        if self.runtime_state is not None:
            return KrakenPublicTriggerSource(
                cast(PublicRuntimeStateReader, self.runtime_state)
            )
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
            api_key_env=self.config.api_key_env,
            api_secret_env=self.config.api_secret_env,
        )
        if self.config.exchange.lower() in {"kraken", "binance"}:
            if self._market_db_url is not None:
                self.exchange_config.adapter_kwargs["public_db_url"] = (
                    self._market_db_url
                )
            if self._account_db_url is not None:
                self.exchange_config.adapter_kwargs["account_db_url"] = (
                    self._account_db_url
                )
            if self._audit_db_url is not None:
                self.exchange_config.adapter_kwargs["audit_db_url"] = self._audit_db_url
            self.exchange_config.adapter_kwargs["rest_audit_retention_minutes"] = (
                self.config.rest_audit_retention_minutes
            )
            self.exchange_config.adapter_kwargs["rest_audit_retention_limit"] = (
                self.config.rest_audit_retention_limit
            )
            self.exchange_config.adapter_kwargs["account_scope"] = (
                self.config.account_scope
            )

    def _build_admin_port(self) -> AdapterExchangePort:
        self._ensure_exchange_config()
        if self.exchange_config is None:
            raise RuntimeError("Exchange configuration is required for admin execution")
        return AdapterExchangePort(
            exchange=self.config.exchange.lower(),
            exchange_config=self.exchange_config,
            verify_timeout_seconds=self.config.tail_verify_timeout_seconds,
            verify_poll_seconds=self.config.tail_verify_poll_seconds,
            run_blocking_calls_in_thread=True,
        )

    def cancel_all_orders(self) -> list[OrderAck]:
        """Cancel all currently visible open/trigger orders via bot execution path."""
        port = self._build_admin_port()
        cancelled, _cancel_errors = self._cancel_all_orders_with_port(port)
        return cancelled

    def _cancel_all_orders_with_port(
        self,
        port: AdapterExchangePort,
    ) -> tuple[list[OrderAck], list[dict[str, str]]]:
        executor = OgunExecutor(port)
        cancelled: list[OrderAck] = []
        cancel_errors: list[dict[str, str]] = []
        seen: set[str] = set()
        for order in _safe_cancel_order_candidates(cast(OpenOrderReader, port.adapter)):
            identity = _extract_cancelable_order_id(order)
            if identity is None:
                continue
            key = str(identity)
            if key in seen:
                continue
            seen.add(key)
            command = CancelCommand(
                kind=RuntimeCommandKind.CANCEL,
                symbol=Symbol(self.config.symbol),
                pair_name="__operator__",
                request=CancelOrderCommandRequest(
                    pair_name="__operator__",
                    clOrdID=key,
                ),
            )
            try:
                cancelled.append(asyncio.run(executor.execute(command)))
            except Exception as exc:
                cancel_errors.append({"order_id": key, "error": _compact_admin_error(exc)})
                continue
        return cancelled, cancel_errors

    def close_all_orders(self) -> dict[str, object]:
        """Cancel all orders then close residual position through the bot boundary."""
        port = self._build_admin_port()
        cancelled, cancel_errors = self._cancel_all_orders_with_port(port)
        position_before = port.adapter.get_position()
        qty_before = float(position_before.qty)
        close_ack: OrderAck | None = None
        close_action = "skipped_no_position"
        close_skipped_reason: str | None = "no_position"
        if qty_before != 0.0:
            close_side = "sell" if qty_before > 0 else "buy"
            close_action = "submitted_reduce_only_market"
            close_skipped_reason = None
            close_ack = port.adapter.place_order(
                side=close_side,
                orderQty=abs(qty_before),
                type_="MARKET",
                reduceOnly=True,
            )
        position_after = port.adapter.get_position()
        audit_errors = list(getattr(port.adapter, "rest_audit_errors", ()))
        return {
            "cancelled": cancelled,
            "cancel_errors": cancel_errors,
            "close_ack": close_ack,
            "close_action": close_action,
            "close_skipped_reason": close_skipped_reason,
            "position_before": position_before,
            "position_after": position_after,
            "closed": float(position_after.qty) == 0.0,
            "audit_persistence_ok": not audit_errors,
            "audit_persistence_errors": audit_errors,
        }

    def cancel_living_tails(self, runtime: StrategyRuntime) -> list[OrderAck]:
        """Best-effort cancellation of living/submitted tails on operator interrupt."""
        port = self._build_admin_port()
        cancelled: list[OrderAck] = []
        for target in _interrupt_cleanup_targets(runtime):
            cancel_id = _resolve_tail_cancel_id(
                cast(OpenOrderReader, port.adapter),
                target.tail_exchange_order_id,
                target.tail_client_order_id,
            )
            if cancel_id is None:
                continue
            try:
                cancelled.append(port.adapter.cancel_order(cancel_id))
            except Exception:
                continue
        return cancelled

    def cleanup_interrupted_pairs(self, runtime: StrategyRuntime) -> dict[str, object]:
        """Cancel active tails and close associated head-opened exposure."""
        port = self._build_admin_port()
        adapter = port.adapter
        targets = _interrupt_cleanup_targets(runtime)
        cancelled: list[OrderAck] = []
        close_acks: list[OrderAck] = []
        errors = 0
        position_before = adapter.get_position()
        remaining_long = max(0.0, float(position_before.qty))
        remaining_short = max(0.0, -float(position_before.qty))
        for target in targets:
            cancel_id = _resolve_tail_cancel_id(
                cast(OpenOrderReader, adapter),
                target.tail_exchange_order_id,
                target.tail_client_order_id,
            )
            if cancel_id is not None:
                try:
                    cancelled.append(adapter.cancel_order(cancel_id))
                except Exception:
                    errors += 1
            close_qty = target.played_quantity
            if target.close_side == "sell":
                close_qty = min(close_qty, remaining_long)
                remaining_long = max(0.0, remaining_long - close_qty)
            else:
                close_qty = min(close_qty, remaining_short)
                remaining_short = max(0.0, remaining_short - close_qty)
            if close_qty <= 0.0:
                continue
            try:
                close_acks.append(
                    adapter.place_order(
                        side=target.close_side,
                        orderQty=close_qty,
                        type_="MARKET",
                        reduceOnly=True,
                    )
                )
            except Exception:
                errors += 1
        position_after = adapter.get_position()
        return {
            "pairs": len(targets),
            "tail_cancelled": len(cancelled),
            "close_orders": len(close_acks),
            "position_before_qty": float(position_before.qty),
            "position_after_qty": float(position_after.qty),
            "errors": errors,
        }


async def _run_private_stack(
    *_args: Any,
) -> None:
    raise RuntimeError("_run_private_stack is retired from the active bot path")


class AdapterExchangePort(ExchangePort):
    """ExchangePort adapter backed by shared exchange adapters."""

    def __init__(
        self,
        *,
        exchange: str,
        exchange_config: ExchangeConfig,
        verify_timeout_seconds: float = 11.0,
        verify_poll_seconds: float = 0.5,
        run_blocking_calls_in_thread: bool = False,
        verify_tail_on_place: bool = True,
    ) -> None:
        adapter_cls = get_adapter(exchange)
        self.adapter = cast(
            ExchangeAdapterLike,
            adapter_cls(
                api_key=exchange_config.api_key,
                api_secret=exchange_config.api_secret,
                base_url=exchange_config.base_url,
                symbol=exchange_config.symbol,
                **exchange_config.adapter_kwargs,
            ),
        )
        self.verify_timeout_seconds = verify_timeout_seconds
        self.verify_poll_seconds = verify_poll_seconds
        self.run_blocking_calls_in_thread = run_blocking_calls_in_thread
        self.verify_tail_on_place = verify_tail_on_place

    async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
        return await self._call_blocking(self._place, command.request)

    async def place_tail(self, command: PlaceTailCommand) -> OrderAck:
        ack = await self._call_blocking(self._place, command.request)
        if self.verify_tail_on_place:
            await self._verify_tail_trigger(command.request, ack)
        return ack

    async def amend_head(self, command: AmendHeadCommand) -> OrderAck:
        return await self._call_blocking(self._amend_head, command.request)

    async def amend_tail(self, command: AmendTailCommand) -> OrderAck:
        return await self._call_blocking(self._amend_tail, command.request)

    async def cancel(self, command: CancelCommand) -> OrderAck:
        return await self._call_blocking(self.adapter.cancel_order, command.request.clOrdID)

    async def _call_blocking(self, func: Callable[..., _T], *args: object) -> _T:
        if self.run_blocking_calls_in_thread:
            return await asyncio.to_thread(func, *args)
        return func(*args)

    def _place(self, request: PlaceOrderCommandRequest) -> OrderAck:
        if request.orderQty is None:
            raise ValueError(f"Missing orderQty for place request on pair '{request.pair_name}'")
        params: dict[str, Any] = {}
        if request.stopPx is not None:
            params["stopPx"] = request.stopPx
        price = request.price
        if (
            price is None
            and request.stopPx is not None
            and request.oDelta is not None
            and _order_type_uses_limit_offset(request.ordType)
        ):
            price = _limit_price_from_stop_offset(
                side=request.side,
                stop_px=request.stopPx,
                offset=request.oDelta,
            )
        _validate_order_prices(
            pair_name=request.pair_name,
            order_type=request.ordType,
            price=price,
            stop_px=request.stopPx,
        )
        if price is not None:
            params["price"] = price
        if request.clOrdID is not None:
            params["clOrdID"] = request.clOrdID
        if request.execInst is not None:
            params["execInst"] = request.execInst
        if request.text is not None:
            params["text"] = request.text
        if request.oDelta is not None:
            params["oDelta"] = request.oDelta
        return self.adapter.place_order(
            request.side,
            request.orderQty,
            type_=request.ordType,
            **params,
        )

    async def _verify_tail_trigger(
        self,
        request: PlaceOrderCommandRequest,
        ack: OrderAck,
    ) -> None:
        if request.stopPx is None or request.orderQty is None:
            return
        if not hasattr(self.adapter, "live_trigger_orders"):
            return
        reader = cast(TriggerOrderReader, self.adapter)
        deadline = asyncio.get_running_loop().time() + self.verify_timeout_seconds
        last_live_orders: list[dict[str, Any]] = []
        last_db_orders: list[dict[str, Any]] = []
        contradictory_ack = not _ack_can_rest_as_trigger(ack)
        while True:
            live_orders = await self._call_blocking(reader.live_trigger_orders)
            db_orders = await self._call_blocking(_trigger_orders_from_private_db, reader)
            last_live_orders = live_orders
            last_db_orders = db_orders
            match, source = _match_trigger_evidence(
                live_orders,
                db_orders,
                request,
                ack,
            )
            if match is not None and source is not None:
                if contradictory_ack:
                    _LOGGER.warning(
                        "tail trigger verified by %s despite contradictory ack: "
                        "pair=%s clOrdID=%s orderID=%s ack_status=%s "
                        "live_order_id=%s live_client_id=%s live_status=%s",
                        source,
                        request.pair_name,
                        request.clOrdID or "-",
                        ack.order_id,
                        ack.status,
                        match.get("order_id"),
                        match.get("client_order_id"),
                        match.get("status"),
                    )
                return
            if asyncio.get_running_loop().time() >= deadline:
                err_kind = (
                    "tail trigger order rejected by exchange"
                    if contradictory_ack
                    else "tail trigger order not visible after placement"
                )
                raise RuntimeError(
                    f"{err_kind}: "
                    f"pair={request.pair_name} clOrdID={request.clOrdID or '-'} "
                    f"orderID={ack.order_id} status={ack.status} "
                    f"stopPx={request.stopPx} qty={request.orderQty} "
                    f"live_seen={len(last_live_orders)} db_seen={len(last_db_orders)}"
                )
            await asyncio.sleep(self.verify_poll_seconds)

    def _amend_head(self, request: AmendOrderCommandRequest) -> OrderAck:
        params: dict[str, Any] = {}
        if request.newPrice is not None:
            params["price"] = request.newPrice
        return self._amend_with_params(request, params)

    def _amend_tail(self, request: AmendOrderCommandRequest) -> OrderAck:
        params: dict[str, Any] = {}
        if request.newPrice is not None:
            params["stopPx"] = request.newPrice
        return self._amend_with_params(request, params)

    def _amend_with_params(
        self,
        request: AmendOrderCommandRequest,
        params: dict[str, Any],
    ) -> OrderAck:
        if request.clOrdID is not None:
            params["clOrdID"] = request.clOrdID
        if request.newQty is not None:
            params["orderQty"] = request.newQty
        if request.text is not None:
            params["text"] = request.text
        return self.adapter.amend_order(request.orderID, **params)

def _matching_tail_trigger_order(
    orders: list[dict[str, Any]],
    request: PlaceOrderCommandRequest,
    ack: OrderAck,
) -> dict[str, Any] | None:
    for order in orders:
        client_id = str(order.get("client_order_id") or "")
        order_id = str(order.get("order_id") or "")
        clordid_match = bool(request.clOrdID) and client_id == request.clOrdID
        orderid_match = bool(ack.order_id) and order_id == str(ack.order_id)
        if not (clordid_match or orderid_match):
            continue
        if request.side and not _matches_text(order.get("side"), request.side):
            continue
        if not _matches_quantity(order.get("qty"), request.orderQty):
            continue
        # clOrdID is the strongest identity anchor; exchange may quantize stop
        # prices to tick size, so strict stop matching can be too brittle.
        if not clordid_match and not _matches_price(order.get("stop_price"), request.stopPx):
            continue
        return order
    return None


def _match_trigger_evidence(
    live_orders: list[dict[str, Any]],
    db_orders: list[dict[str, Any]],
    request: PlaceOrderCommandRequest,
    ack: OrderAck,
) -> tuple[dict[str, Any] | None, str | None]:
    live_match = _matching_tail_trigger_order(live_orders, request, ack)
    if live_match is not None:
        return live_match, "rest_live"
    db_match = _matching_tail_trigger_order(db_orders, request, ack)
    if db_match is not None:
        return db_match, "private_db"
    return None, None


def _trigger_orders_from_private_db(reader: TriggerOrderReader) -> list[dict[str, Any]]:
    if not hasattr(reader, "live_trigger_orders_db"):
        return []
    return reader.live_trigger_orders_db()


def _ack_can_rest_as_trigger(ack: OrderAck) -> bool:
    status = ack.status.replace(" ", "_").replace("-", "_").lower()
    return status in {"new", "open", "placed", "submitted"}


def _matches_text(left: object, right: str) -> bool:
    return str(left or "").lower() == right.lower()


def _matches_quantity(left: object, right: object | None) -> bool:
    if right is None:
        return True
    left_decimal = _decimal_or_none(left)
    right_decimal = _decimal_or_none(right)
    if left_decimal is None or right_decimal is None:
        return False
    return abs(left_decimal - right_decimal) <= Decimal("0.00000001")


def _matches_price(left: object, right: object | None) -> bool:
    if right is None:
        return True
    left_decimal = _decimal_or_none(left)
    right_decimal = _decimal_or_none(right)
    if left_decimal is None or right_decimal is None:
        return False
    tolerance = max(Decimal("0.01"), abs(right_decimal) * Decimal("0.000001"))
    return abs(left_decimal - right_decimal) <= tolerance


def _decimal_or_none(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    return None


def _safe_cancel_order_candidates(adapter: OpenOrderReader) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source_name in (
        "live_open_orders",
        "live_trigger_orders",
        "open_orders",
        "live_trigger_orders_db",
    ):
        source = getattr(adapter, source_name, None)
        if not callable(source):
            continue
        try:
            rows = source()
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        candidates.extend([row for row in rows if isinstance(row, dict)])
    return candidates


def _extract_cancelable_order_id(order: dict[str, object]) -> str | None:
    for key in (
        "orderID",
        "orderId",
        "order_id",
        "id",
        "clOrdID",
        "cliOrdId",
        "cli_ord_id",
        "client_order_id",
    ):
        value = order.get(key)
        if value:
            return str(value)
    return None


def _compact_admin_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _limit_price_from_stop_offset(
    *,
    side: str,
    stop_px: object,
    offset: object,
) -> Decimal:
    stop = Decimal(str(stop_px))
    distance = abs(Decimal(str(offset)))
    if side.lower() == "buy":
        return stop + distance
    return stop - distance


def _order_type_uses_limit_offset(order_type: str) -> bool:
    return parse_order_code(order_type).base_key in {"SL", "LT"}


def _validate_order_prices(
    *,
    pair_name: str,
    order_type: str,
    price: object | None,
    stop_px: object | None,
) -> None:
    base = parse_order_code(order_type).base_key
    if base == "L" and price is None:
        raise ValueError(f"Order pair '{pair_name}' limit order needs a price")
    if base in {"S", "SL", "MT", "LT"} and stop_px is None:
        raise ValueError(f"Order pair '{pair_name}' trigger order needs a stopPx")
    if base in {"SL", "LT"} and price is None:
        raise ValueError(f"Order pair '{pair_name}' trigger-limit order needs a limit price")


def _validate_binance_pair_grammar(pair: OrderPairSpec) -> None:
    """Fail unsupported Binance grammar at preflight, before REST submission."""

    for role, raw in (("head", pair.head.order_type), ("tail", pair.tail.order_type)):
        code = parse_order_code(raw)
        if code.base_key not in {"M", "L", "S"}:
            raise ValueError(
                f"Binance Futures does not support {code.base} {role} orders in v1; "
                "use M, L, or S."
            )
        if code.price_suffix == "i":
            raise ValueError(
                f"Binance Futures does not support index-price triggers for {role} "
                f"order type '{raw}'. Use last/contract or mark price."
            )
        if code.post_only and code.base_key != "L":
            raise ValueError(
                f"Binance Futures post-only is only supported on limit orders; got {raw}."
            )


@dataclass(frozen=True)
class _InterruptCleanupTarget:
    pair_name: str
    close_side: str
    played_quantity: float
    tail_client_order_id: str | None
    tail_exchange_order_id: str | None


def _interrupt_cleanup_targets(runtime: StrategyRuntime) -> tuple[_InterruptCleanupTarget, ...]:
    targets: list[_InterruptCleanupTarget] = []
    for pair_name, pair_state in runtime.state.pairs.items():
        if pair_state.head_state.value != "closed":
            continue
        if pair_state.tail_state is None:
            continue
        if pair_state.tail_state.value not in {"hooked", "submitted", "living"}:
            continue
        played_quantity = float(pair_state.played_quantity or 0.0)
        if played_quantity <= 0.0:
            continue
        close_side = "sell" if pair_state.pair.head.side.value == "buy" else "buy"
        identity = pair_state.tail_identity
        targets.append(
            _InterruptCleanupTarget(
                pair_name=pair_name,
                close_side=close_side,
                played_quantity=played_quantity,
                tail_client_order_id=None if identity is None else identity.client_order_id,
                tail_exchange_order_id=None if identity is None else identity.exchange_order_id,
            )
        )
    return tuple(targets)


def _resolve_tail_cancel_id(
    adapter: OpenOrderReader,
    tail_exchange_order_id: str | None,
    tail_client_order_id: str | None,
) -> str | None:
    if tail_exchange_order_id:
        return tail_exchange_order_id
    if not tail_client_order_id:
        return None
    for order in _safe_cancel_order_candidates(adapter):
        client_id = str(
            order.get("client_order_id")
            or order.get("cli_ord_id")
            or order.get("clOrdID")
            or order.get("cliOrdId")
            or ""
        )
        if client_id != tail_client_order_id:
            continue
        order_id = _extract_cancelable_order_id(order)
        if order_id:
            return order_id
    return tail_client_order_id
