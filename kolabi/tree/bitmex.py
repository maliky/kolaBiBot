from __future__ import annotations

import argparse
import asyncio
import json
import signal
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

import websockets
from sqlalchemy import select

from kolabi.shared.bitmex_futures import (
    bitmex_futures_environment,
    bitmex_futures_public_db_url,
)
from kolabi.shared.persistence import ExchangeInstrument
from kolabi.shared.redaction import redact_url
from kolabi.tree.kraken import (
    BookPayload,
    KrakenConfig,
    KrakenTree,
    MarketSnapshot,
    PendingBook,
    TickerPrices,
    first_present,
    format_market_status,
    format_market_status_header,
    format_trace_message,
    is_due,
    optional_float,
    parse_kraken_time,
)


@dataclass(frozen=True)
class BitmexConfig(KrakenConfig):
    """Configuration for the BitMEX public orderbook feed."""

    pair: str = "XBTUSD"
    depth: int = 25
    ws_url: str = "wss://testnet.bitmex.com/realtime"
    db_url: str = "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    private_db_url: str = "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
    rest_url: str = "https://testnet.bitmex.com/api/v1"
    exchange: str = "bitmex"
    instrument_refresh_on_start: bool = True


@dataclass(frozen=True)
class BitmexBookLevel:
    """One BitMEX orderBookL2 level keyed by exchange level id."""

    level_id: str
    symbol: str
    side: str
    price: float
    size: float
    source_timestamp: datetime | None = None


@dataclass(frozen=True)
class BitmexBookState:
    """Immutable local image of the BitMEX orderBookL2 table."""

    symbol: str
    levels: tuple[BitmexBookLevel, ...]
    version: int = 0
    source_timestamp: datetime | None = None


class BitmexTree(KrakenTree):
    """BitMEX public websocket reader writing the shared market schema."""

    config: BitmexConfig

    def __init__(self, config: BitmexConfig) -> None:
        super().__init__(config)
        self._bitmex_book_state: BitmexBookState | None = None
        if config.instrument_refresh_on_start:
            self._refresh_instrument_rules()

    async def run(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "bitmex_tree reconnecting in %ss after error: %s",
                    self.config.reconnect_seconds,
                    exc,
                )
                await self._wait_or_stop(float(self.config.reconnect_seconds))

    async def run_once(self) -> None:
        url = public_stream_url(self.config)
        async with websockets.connect(url, ping_interval=20) as ws:
            self.logger.info(
                "bitmex_tree subscribed pair=%s depth=%s env=%s db=%s ws=%s",
                self.config.pair,
                self.config.depth,
                self.config.environment,
                redact_url(self.config.db_url),
                url,
            )
            while self._running:
                try:
                    raw_message = await asyncio.wait_for(ws.recv(), timeout=0.25)
                    self.handle_message(raw_message)
                except TimeoutError:
                    pass
                now = datetime.now(timezone.utc)
                self.flush_due(now)
                self._maintenance_due(now)

    def handle_message(self, raw_message: str | bytes) -> PendingBook | None:
        payload = (
            raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message
        )
        message = normalise_public_message(json.loads(payload), symbol=self.config.pair)
        self._raw_message_count += 1
        self.record_raw_event(message, stream_kind="public_ws")
        self.trace_message(message, payload)
        ticker = ticker_prices_from_message(message)
        if ticker is not None:
            self._latest_ticker_prices = ticker
            self._last_ticker_fetch_at = datetime.now(timezone.utc)

        next_state, changed = apply_bitmex_book_message(
            self._bitmex_book_state,
            message,
            symbol=self.config.pair,
        )
        self._bitmex_book_state = next_state
        if not changed or next_state is None:
            return None
        parsed = book_payload_from_state(next_state)
        if parsed is None:
            return None
        self._book_message_count += 1
        now = datetime.now(timezone.utc)
        try:
            return self.ingest_payload(parsed, now)
        except ValueError as exc:
            self._log_invalid_book(parsed, now, exc)
            return None

    def _fetch_ticker_prices(self) -> TickerPrices:
        response = self._rest_session.get(
            bitmex_rest_url(self.config, "/instrument"),
            params={"symbol": self.config.pair},
            timeout=max(0.2, self.config.ticker_timeout_seconds),
        )
        response.raise_for_status()
        payload = first_mapping(response.json())
        return ticker_prices_from_instrument(payload)

    def _refresh_instrument_rules(self) -> None:
        try:
            response = self._rest_session.get(
                bitmex_rest_url(self.config, "/instrument"),
                params={"symbol": self.config.pair},
                timeout=3,
            )
            response.raise_for_status()
            payload = first_mapping(response.json())
        except Exception as exc:
            self.logger.debug("bitmex_tree instrument refresh skipped: %s", exc)
            return
        if not payload:
            return
        tick_size = optional_float(payload.get("tickSize"))
        contract_size = optional_float(
            first_present(payload, "contractSize", "multiplier", "settlCurrencyScale")
        )
        min_quantity = optional_float(first_present(payload, "lotSize", "minOrderQty")) or 1.0
        state = str(payload.get("state") or "")
        with self.sessionmaker() as session:
            existing = (
                session.execute(
                    select(ExchangeInstrument).where(
                        ExchangeInstrument.exchange == self.config.exchange,
                        ExchangeInstrument.environment == self.config.environment,
                        ExchangeInstrument.market_type == self.config.market_type,
                        ExchangeInstrument.symbol == self.config.pair,
                    )
                )
                .scalars()
                .first()
            )
            row = existing or ExchangeInstrument(
                exchange=self.config.exchange,
                environment=self.config.environment,
                market_type=self.config.market_type,
                symbol=self.config.pair,
            )
            row.instrument_type = str(first_present(payload, "typ", "type", "root") or "")
            row.tradeable = state in {"Open", "Unlisted"} if state else True
            row.tick_size = tick_size
            row.contract_size = contract_size
            row.min_quantity = min_quantity
            row.raw_payload = dict(payload)
            row.updated_at = datetime.now(timezone.utc)
            if existing is None:
                session.add(row)
            session.commit()

    def log_due(self, now: datetime, snapshot: MarketSnapshot | None) -> None:
        if not is_due(self._last_log_at, now, self.config.log_interval_seconds):
            return
        self._last_log_at = now
        if self._latest_book is None:
            return
        tick_size = self._status_tick_size(self.config.pair)
        status_line = format_market_status(
            config=self.config,
            metrics=self._latest_book.metrics,
            stored_count=self._stored_count,
            persisted=snapshot is not None,
            raw_message_count=self._raw_message_count,
            book_message_count=self._book_message_count,
            tick_size=tick_size,
            ticker_prices=self._latest_ticker_prices,
        ).replace("kraken_tree", "bitmex_tree", 1)
        if status_line == self._last_status_log_line:
            return
        self._last_status_log_line = status_line
        if self._status_rows_logged % 50 == 0:
            self.logger.info(
                format_market_status_header().replace("kraken_tree", "bitmex_tree", 1)
            )
        self.logger.info(status_line)
        self._status_rows_logged += 1

    def trace_message(self, message: object, raw_payload: str) -> None:
        if not self.config.trace_ws:
            return
        if self.config.trace_ws_max_lines > 0 and self._trace_count >= self.config.trace_ws_max_lines:
            if not self._last_trace_cap_logged:
                self.logger.info(
                    "bitmex_ws_trace cap reached lines=%s",
                    self.config.trace_ws_max_lines,
                )
                self._last_trace_cap_logged = True
            return
        self._trace_count += 1
        self.logger.info(
            "bitmex_ws_raw %s",
            format_bitmex_trace_message(message, raw_payload, self.config.trace_ws_format),
        )


def public_stream_url(config: BitmexConfig) -> str:
    subscriptions = ",".join(
        (
            f"orderBookL2_25:{config.pair}",
            f"instrument:{config.pair}",
            f"trade:{config.pair}",
        )
    )
    base = config.ws_url.rstrip("/")
    if base.endswith("/realtime"):
        return f"{base}?subscribe={subscriptions}"
    return f"{base}/realtime?subscribe={subscriptions}"


def bitmex_rest_url(config: BitmexConfig, path: str) -> str:
    base = config.rest_url.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base}{suffix}"


def normalise_public_message(message: object, *, symbol: str) -> dict[str, object]:
    if not isinstance(message, dict):
        return {"event": "unknown", "symbol": symbol, "payload": message}
    payload = dict(message)
    table = str(payload.get("table") or "")
    action = str(payload.get("action") or payload.get("event") or "")
    if table and action:
        payload.setdefault("event", f"{table}:{action}")
    payload.setdefault("symbol", symbol_from_message(payload) or symbol)
    return payload


def apply_bitmex_book_message(
    state: BitmexBookState | None,
    message: Mapping[str, object],
    *,
    symbol: str,
) -> tuple[BitmexBookState | None, bool]:
    table = message.get("table")
    if table != "orderBookL2_25":
        return state, False
    action = str(message.get("action") or "")
    rows = tuple(row for row in message.get("data", []) if isinstance(row, Mapping))
    if action == "partial":
        levels = tuple(
            level
            for row in rows
            if (level := level_from_row(row, symbol=symbol, existing=None)) is not None
        )
        return next_state(symbol, levels, previous=state), True
    if state is None:
        return state, False
    by_id = {level.level_id: level for level in state.levels}
    if action == "insert":
        for row in rows:
            level = level_from_row(row, symbol=symbol, existing=None)
            if level is not None:
                by_id[level.level_id] = level
        return next_state(symbol, tuple(by_id.values()), previous=state), bool(rows)
    if action == "update":
        changed = False
        for row in rows:
            level_id = optional_level_id(row)
            if level_id is None:
                continue
            existing = by_id.get(level_id)
            level = level_from_row(row, symbol=symbol, existing=existing)
            if level is None:
                by_id.pop(level_id, None)
            else:
                by_id[level_id] = level
            changed = True
        return next_state(symbol, tuple(by_id.values()), previous=state), changed
    if action == "delete":
        changed = False
        for row in rows:
            level_id = optional_level_id(row)
            if level_id is None:
                continue
            changed = by_id.pop(level_id, None) is not None or changed
        return next_state(symbol, tuple(by_id.values()), previous=state), changed
    return state, False


def next_state(
    symbol: str,
    levels: tuple[BitmexBookLevel, ...],
    *,
    previous: BitmexBookState | None,
) -> BitmexBookState:
    source_timestamp = newest_timestamp(
        tuple(level.source_timestamp for level in levels if level.source_timestamp is not None)
    )
    return BitmexBookState(
        symbol=symbol,
        levels=levels,
        version=(0 if previous is None else previous.version) + 1,
        source_timestamp=source_timestamp
        or (None if previous is None else previous.source_timestamp),
    )


def level_from_row(
    row: Mapping[str, object],
    *,
    symbol: str,
    existing: BitmexBookLevel | None,
) -> BitmexBookLevel | None:
    row_symbol = str(row.get("symbol") or (existing.symbol if existing else symbol))
    if row.get("symbol") is not None and row_symbol != symbol:
        return existing
    level_id = optional_level_id(row) or (existing.level_id if existing else None)
    side = normalise_side(row.get("side") or (existing.side if existing else None))
    price = optional_float(row.get("price")) if "price" in row else (
        existing.price if existing else None
    )
    size = optional_float(first_present(dict(row), "size", "qty")) if (
        "size" in row or "qty" in row
    ) else (existing.size if existing else None)
    if level_id is None or side is None or price is None:
        return existing
    if size is None or size <= 0:
        return None
    return BitmexBookLevel(
        level_id=level_id,
        symbol=row_symbol,
        side=side,
        price=price,
        size=size,
        source_timestamp=parse_bitmex_time(first_present(dict(row), "timestamp", "transactTime")),
    )


def optional_level_id(row: Mapping[str, object]) -> str | None:
    value = row.get("id")
    if value in (None, ""):
        return None
    return str(value)


def normalise_side(value: object) -> str | None:
    side = str(value or "").strip().lower()
    if side in {"buy", "bid"}:
        return "Buy"
    if side in {"sell", "ask"}:
        return "Sell"
    return None


def book_payload_from_state(state: BitmexBookState) -> BookPayload | None:
    asks = tuple((level.price, level.size) for level in state.levels if level.side == "Sell")
    bids = tuple((level.price, level.size) for level in state.levels if level.side == "Buy")
    if not asks or not bids:
        return None
    return BookPayload(
        message_type="snapshot",
        symbol=state.symbol,
        asks=asks,
        bids=bids,
        source_timestamp=state.source_timestamp,
        sequence=state.version,
    )


def ticker_prices_from_message(message: Mapping[str, object]) -> TickerPrices | None:
    if message.get("table") == "instrument":
        for row in message.get("data", []):
            if isinstance(row, Mapping):
                return ticker_prices_from_instrument(row)
    if message.get("table") == "trade":
        rows = tuple(row for row in message.get("data", []) if isinstance(row, Mapping))
        if not rows:
            return None
        return TickerPrices(
            last_price=optional_float(rows[-1].get("price")),
            mark_price=None,
            index_price=None,
        )
    return None


def ticker_prices_from_instrument(row: Mapping[str, object]) -> TickerPrices:
    return TickerPrices(
        last_price=optional_float(row.get("lastPrice")),
        mark_price=optional_float(row.get("markPrice")),
        index_price=optional_float(
            first_present(dict(row), "indicativeSettlePrice", "indexPrice")
        ),
    )


def first_mapping(payload: object) -> dict[str, object]:
    if isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
        return dict(payload[0])
    if isinstance(payload, Mapping):
        return dict(payload)
    return {}


def symbol_from_message(message: Mapping[str, object]) -> str | None:
    data = message.get("data")
    if not isinstance(data, list):
        return None
    for row in data:
        if isinstance(row, Mapping) and row.get("symbol") not in (None, ""):
            return str(row["symbol"])
    return None


def parse_bitmex_time(value: object) -> datetime | None:
    return parse_kraken_time(value)


def newest_timestamp(values: Sequence[datetime]) -> datetime | None:
    if not values:
        return None
    return max(values)


def format_bitmex_trace_message(message: object, raw_payload: str, trace_format: str) -> str:
    if trace_format == "json":
        return raw_payload
    if isinstance(message, dict) and message.get("table"):
        rows = message.get("data")
        row_count = len(rows) if isinstance(rows, list) else 0
        return (
            f"table={message.get('table')} action={message.get('action')} "
            f"symbol={message.get('symbol')} rows={row_count}"
        )
    return format_trace_message(message, raw_payload, trace_format)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.tree.bitmex",
        description="Public market-data service CLI for BitMEX derivatives.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "probe", "status"):
        cmd = subparsers.add_parser(command, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        cmd.add_argument("--pair", default=BitmexConfig.pair, help="BitMEX instrument symbol.")
        cmd.add_argument("--depth", type=int, default=BitmexConfig.depth, help="Orderbook depth to keep.")
        cmd.add_argument("--environment", choices=("demo", "live"), default=BitmexConfig.environment)
        cmd.add_argument("--ws-url", help="Override public websocket URL.")
        cmd.add_argument("--rest-url", help="Override public REST base URL.")
        cmd.add_argument("--db-url", help="Override public DB URL.")
        cmd.add_argument("--private-db-url", help="Private DB URL used for correlation.")
        cmd.add_argument("--exchange", default=BitmexConfig.exchange)
        cmd.add_argument("--market-type", default=BitmexConfig.market_type)
        cmd.add_argument("--log-level", default=BitmexConfig.log_level)
        cmd.add_argument("--snapshot-interval-seconds", type=float, default=BitmexConfig.snapshot_interval_seconds)
        cmd.add_argument("--indicator-interval-seconds", type=float, default=BitmexConfig.indicator_interval_seconds)
        cmd.add_argument("--ticker-interval-seconds", type=float, default=BitmexConfig.ticker_interval_seconds)
        cmd.add_argument("--ticker-timeout-seconds", type=float, default=BitmexConfig.ticker_timeout_seconds)
        cmd.add_argument("--log-interval-seconds", type=float, default=BitmexConfig.log_interval_seconds)
        cmd.add_argument("--maintenance-seconds", type=float, default=BitmexConfig.maintenance_seconds)
        cmd.add_argument("--retention-minutes", type=int, default=BitmexConfig.retention_minutes)
        cmd.add_argument("--reconnect-seconds", type=int, default=BitmexConfig.reconnect_seconds)
        cmd.add_argument("--trace-ws", action="store_true")
        cmd.add_argument("--trace-ws-format", choices=("compact", "json"), default=BitmexConfig.trace_ws_format)
        cmd.add_argument("--trace-ws-max-lines", type=int, default=BitmexConfig.trace_ws_max_lines)
        cmd.add_argument(
            "--skip-instrument-refresh",
            action="store_true",
            help="Skip REST instrument metadata refresh on startup.",
        )
    subparsers.choices["probe"].add_argument("--seconds", type=float, default=10.0)
    return parser


def config_from_args(args: argparse.Namespace) -> BitmexConfig:
    env_cfg = bitmex_futures_environment(args.environment)
    return BitmexConfig(
        pair=args.pair,
        depth=args.depth,
        ws_url=args.ws_url or env_cfg.public_ws_url,
        rest_url=args.rest_url or env_cfg.rest_url,
        db_url=args.db_url or bitmex_futures_public_db_url(args.environment, args.pair),
        private_db_url=args.private_db_url or env_cfg.private_db_url,
        exchange=args.exchange,
        environment=args.environment,
        market_type=args.market_type,
        log_level=args.log_level,
        snapshot_interval_seconds=args.snapshot_interval_seconds,
        indicator_interval_seconds=args.indicator_interval_seconds,
        ticker_interval_seconds=args.ticker_interval_seconds,
        ticker_timeout_seconds=args.ticker_timeout_seconds,
        log_interval_seconds=args.log_interval_seconds,
        maintenance_seconds=args.maintenance_seconds,
        retention_minutes=args.retention_minutes,
        reconnect_seconds=args.reconnect_seconds,
        trace_ws=args.trace_ws,
        trace_ws_format=args.trace_ws_format,
        trace_ws_max_lines=args.trace_ws_max_lines,
        instrument_refresh_on_start=not args.skip_instrument_refresh,
    )


def print_status(tree: BitmexTree, pair: str) -> None:
    print(json.dumps(tree.latest_status(pair), sort_keys=True))


async def run_service(tree: BitmexTree, stop_after_seconds: float | None = None) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def _request_stop() -> None:
        tree.stop()
        if task is not None:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass
    stopper: asyncio.Task[None] | None = None
    if stop_after_seconds is not None:
        stopper = asyncio.create_task(stop_tree_after(tree, stop_after_seconds))
    try:
        await tree.run()
    except asyncio.CancelledError:
        if tree._running:
            raise
    finally:
        if stopper is not None:
            stopper.cancel()
            with suppress(asyncio.CancelledError):
                await stopper


async def stop_tree_after(tree: BitmexTree, delay_seconds: float) -> None:
    await asyncio.sleep(max(delay_seconds, 0.0))
    tree.stop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tree = BitmexTree(config_from_args(args))
    if args.command == "status":
        print_status(tree, args.pair)
        return 0
    if args.command == "probe":
        try:
            asyncio.run(run_service(tree, stop_after_seconds=args.seconds))
        except KeyboardInterrupt:
            tree.stop()
        print_status(tree, args.pair)
        return 0
    try:
        asyncio.run(run_service(tree))
    except KeyboardInterrupt:
        tree.stop()
        print("public market stream stopped by operator")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
