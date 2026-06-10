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

from kolabi.shared.binance_futures import (
    binance_futures_environment,
    binance_futures_public_db_url,
)
from kolabi.shared.persistence import ExchangeInstrument
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
    is_due,
    optional_float,
    parse_optional_int,
)


@dataclass(frozen=True)
class BinanceConfig(KrakenConfig):
    """Configuration for the Binance USD-M Futures public feed."""

    pair: str = "BTCUSDT"
    ws_url: str = "wss://stream.binancefuture.com/stream"
    db_url: str = "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    private_db_url: str = "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
    rest_url: str = "https://testnet.binancefuture.com"
    exchange: str = "binance"


class BinanceTree(KrakenTree):
    """Binance public websocket reader writing the shared market schema."""

    config: BinanceConfig

    def __init__(self, config: BinanceConfig) -> None:
        super().__init__(config)
        self._refresh_instrument_rules()

    async def run(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "binance_tree reconnecting in %ss after error: %s",
                    self.config.reconnect_seconds,
                    exc,
                )
                await self._wait_or_stop(float(self.config.reconnect_seconds))

    async def run_once(self) -> None:
        url = public_stream_url(self.config)
        async with websockets.connect(url, ping_interval=20) as ws:
            self.logger.info(
                "binance_tree subscribed pair=%s depth=%s env=%s db=%s ws=%s",
                self.config.pair,
                self.config.depth,
                self.config.environment,
                self.config.db_url,
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
        message = unwrap_combined_stream(json.loads(payload))
        self._raw_message_count += 1
        normalised_message = with_binance_symbol_alias(message)
        self.record_raw_event(normalised_message, stream_kind="public_ws")
        self.trace_message(normalised_message, payload)
        ticker = ticker_prices_from_message(normalised_message)
        if ticker is not None:
            self._latest_ticker_prices = ticker
            self._last_ticker_fetch_at = datetime.now(timezone.utc)
        parsed = extract_book_payload(normalised_message)
        if parsed is None:
            return None
        self._book_message_count += 1
        return self.ingest_payload(parsed, datetime.now(timezone.utc))

    def _fetch_ticker_prices(self) -> TickerPrices:
        mark_response = self._rest_session.get(
            f"{self.config.rest_url.rstrip('/')}/fapi/v1/premiumIndex",
            params={"symbol": self.config.pair},
            timeout=max(0.2, self.config.ticker_timeout_seconds),
        )
        mark_response.raise_for_status()
        mark_payload = mark_response.json()
        ticker_response = self._rest_session.get(
            f"{self.config.rest_url.rstrip('/')}/fapi/v1/ticker/24hr",
            params={"symbol": self.config.pair},
            timeout=max(0.2, self.config.ticker_timeout_seconds),
        )
        ticker_response.raise_for_status()
        ticker_payload = ticker_response.json()
        return TickerPrices(
            last_price=optional_float(ticker_payload.get("lastPrice")),
            mark_price=optional_float(mark_payload.get("markPrice")),
            index_price=optional_float(mark_payload.get("indexPrice")),
        )

    def _refresh_instrument_rules(self) -> None:
        try:
            payload = self._rest_session.get(
                f"{self.config.rest_url.rstrip('/')}/fapi/v1/exchangeInfo",
                params={"symbol": self.config.pair},
                timeout=3,
            )
            payload.raise_for_status()
            data = payload.json()
        except Exception as exc:
            self.logger.debug("binance_tree instrument refresh skipped: %s", exc)
            return
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        for item in symbols:
            if not isinstance(item, Mapping) or item.get("symbol") != self.config.pair:
                continue
            filters = {
                str(entry.get("filterType")): entry
                for entry in item.get("filters", [])
                if isinstance(entry, Mapping)
            }
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
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
                row.tick_size = optional_float(price_filter.get("tickSize"))
                row.min_quantity = optional_float(lot_filter.get("minQty")) or 0.0
                row.raw_payload = dict(item)
                if existing is None:
                    session.add(row)
                session.commit()
            return

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
        ).replace("kraken_tree", "binance_tree", 1)
        if status_line == self._last_status_log_line:
            return
        self._last_status_log_line = status_line
        if self._status_rows_logged % 50 == 0:
            self.logger.info(
                format_market_status_header().replace("kraken_tree", "binance_tree", 1)
            )
        self.logger.info(status_line)
        self._status_rows_logged += 1


def public_stream_url(config: BinanceConfig) -> str:
    symbol = config.pair.lower()
    streams = "/".join(
        (
            f"{symbol}@depth{max(5, min(config.depth, 20))}@500ms",
            f"{symbol}@markPrice@1s",
            f"{symbol}@ticker",
        )
    )
    base = config.ws_url.rstrip("/")
    if base.endswith("/stream"):
        return f"{base}?streams={streams}"
    return f"{base}/stream?streams={streams}"


def unwrap_combined_stream(message: object) -> dict[str, object]:
    if not isinstance(message, dict):
        return {}
    data = message.get("data")
    if isinstance(data, dict):
        payload = dict(data)
        if "stream" in message:
            payload.setdefault("stream", message["stream"])
        return payload
    return dict(message)


def with_binance_symbol_alias(message: dict[str, object]) -> dict[str, object]:
    payload = dict(message)
    if "symbol" not in payload and payload.get("s") is not None:
        payload["symbol"] = payload["s"]
    return payload


def extract_book_payload(message: object) -> BookPayload | None:
    if not isinstance(message, dict):
        return None
    event_type = str(message.get("e") or "")
    if event_type not in {"depthUpdate", ""}:
        return None
    asks = tuple(parse_levels(message.get("a", []), depth=10_000))
    bids = tuple(parse_levels(message.get("b", []), depth=10_000))
    if not asks and not bids:
        return None
    return BookPayload(
        message_type="snapshot",
        symbol=str(message.get("s") or message.get("symbol") or ""),
        asks=asks,
        bids=bids,
        source_timestamp=parse_binance_time(first_present(message, "E", "T")),
        sequence=parse_optional_int(message.get("u")),
    )


def ticker_prices_from_message(message: dict[str, object]) -> TickerPrices | None:
    event_type = str(message.get("e") or "")
    if event_type == "markPriceUpdate":
        return TickerPrices(
            last_price=None,
            mark_price=optional_float(message.get("p")),
            index_price=optional_float(message.get("i")),
        )
    if event_type == "24hrTicker":
        return TickerPrices(
            last_price=optional_float(message.get("c")),
            mark_price=None,
            index_price=None,
        )
    return None


def parse_levels(levels: object, depth: int) -> list[tuple[float, float]]:
    if not isinstance(levels, list):
        return []
    parsed: list[tuple[float, float]] = []
    for level in levels[:depth]:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = optional_float(level[0])
        quantity = optional_float(level[1])
        if price is None or quantity is None or quantity <= 0:
            continue
        parsed.append((price, quantity))
    return parsed


def parse_binance_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc)
    if isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kolabi.tree.binance",
        description="Public market-data service CLI for Binance USD-M Futures.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "probe", "status"):
        cmd = subparsers.add_parser(command, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        cmd.add_argument("--pair", default=BinanceConfig.pair, help="Binance Futures symbol.")
        cmd.add_argument("--depth", type=int, default=BinanceConfig.depth, help="Orderbook depth to keep.")
        cmd.add_argument("--environment", choices=("demo", "live"), default=BinanceConfig.environment)
        cmd.add_argument("--ws-url", help="Override public websocket base URL.")
        cmd.add_argument("--rest-url", help="Override public REST base URL.")
        cmd.add_argument("--db-url", help="Override public DB URL.")
        cmd.add_argument("--private-db-url", help="Private DB URL used for correlation.")
        cmd.add_argument("--exchange", default=BinanceConfig.exchange)
        cmd.add_argument("--market-type", default=BinanceConfig.market_type)
        cmd.add_argument("--log-level", default=BinanceConfig.log_level)
        cmd.add_argument("--snapshot-interval-seconds", type=float, default=BinanceConfig.snapshot_interval_seconds)
        cmd.add_argument("--indicator-interval-seconds", type=float, default=BinanceConfig.indicator_interval_seconds)
        cmd.add_argument("--ticker-interval-seconds", type=float, default=BinanceConfig.ticker_interval_seconds)
        cmd.add_argument("--ticker-timeout-seconds", type=float, default=BinanceConfig.ticker_timeout_seconds)
        cmd.add_argument("--log-interval-seconds", type=float, default=BinanceConfig.log_interval_seconds)
        cmd.add_argument("--maintenance-seconds", type=float, default=BinanceConfig.maintenance_seconds)
        cmd.add_argument("--retention-minutes", type=int, default=BinanceConfig.retention_minutes)
        cmd.add_argument("--reconnect-seconds", type=int, default=BinanceConfig.reconnect_seconds)
        cmd.add_argument("--trace-ws", action="store_true")
        cmd.add_argument("--trace-ws-format", choices=("compact", "json"), default=BinanceConfig.trace_ws_format)
        cmd.add_argument("--trace-ws-max-lines", type=int, default=BinanceConfig.trace_ws_max_lines)
    subparsers.choices["probe"].add_argument("--seconds", type=float, default=10.0)
    return parser


def config_from_args(args: argparse.Namespace) -> BinanceConfig:
    env_cfg = binance_futures_environment(args.environment)
    return BinanceConfig(
        pair=args.pair,
        depth=args.depth,
        ws_url=args.ws_url or env_cfg.public_ws_url,
        rest_url=args.rest_url or env_cfg.rest_url,
        db_url=args.db_url or binance_futures_public_db_url(args.environment, args.pair),
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
    )


def print_status(tree: BinanceTree, pair: str) -> None:
    print(json.dumps(tree.latest_status(pair), sort_keys=True))


async def run_service(tree: BinanceTree, stop_after_seconds: float | None = None) -> None:
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


async def stop_tree_after(tree: BinanceTree, delay_seconds: float) -> None:
    await asyncio.sleep(max(delay_seconds, 0.0))
    tree.stop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tree = BinanceTree(config_from_args(args))
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
