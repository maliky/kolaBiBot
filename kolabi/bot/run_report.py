"""Build operator-facing run-state reports from Kolabi runtime logs.

The report uses the runtime log as the lifecycle timeline and, by default,
uses the local private account DB as the canonical source for fills, fees, and
maker/taker roles.  This keeps post-run journals reproducible from data already
on disk, without depending on an exchange UI or other external witness.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO

from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kolabi.shared.persistence import ExchangeFill, ExchangeOrder


class ReportError(RuntimeError):
    """Raised when a run report cannot be built from local data."""


@dataclass(frozen=True, order=True)
class PairKey:
    """Stable identity for one pair attempt in compact runtime logs."""

    name: str
    attempt: int


@dataclass(frozen=True)
class FillLeg:
    """One log-derived fill leg for a head or tail order."""

    side: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime


@dataclass
class PairLifecycle:
    """Log-derived lifecycle facts needed to recognise terminated pairs."""

    key: PairKey
    head_client_id: str | None = None
    tail_client_id: str | None = None
    head_fill: FillLeg | None = None
    tail_fill: FillLeg | None = None
    initial_tail_stop: Decimal | None = None
    latest_tail_stop: Decimal | None = None
    amend_count: int = 0
    tail_amend_times: list[datetime] = field(default_factory=list)

    @property
    def terminated(self) -> bool:
        return self.head_fill is not None and self.tail_fill is not None


@dataclass(frozen=True)
class TailTelemetry:
    """Latest log-derived tail telemetry for one flying tail."""

    key: PairKey
    recorded_at: datetime
    reference_price: Decimal
    stop_price: Decimal
    current_distance: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    """Latest log-derived public price snapshot seen in tail telemetry."""

    recorded_at: datetime
    source: str
    bid_price: Decimal | None
    ask_price: Decimal | None
    mid_price: Decimal | None
    last_price: Decimal | None
    mark_price: Decimal | None
    index_price: Decimal | None


@dataclass
class LatentAttempt:
    """Latest log-derived state for one not-yet-filled head attempt."""

    key: PairKey
    last_event_at: datetime
    last_event: str
    status: str = ""
    gate: str = ""
    reference_price: Decimal | None = None
    head_price: Decimal | None = None
    quantity: Decimal | None = None
    order_type: str = ""
    deadline_at: datetime | None = None
    head_client_id: str | None = None
    ended: bool = False


@dataclass(frozen=True)
class RunLogSnapshot:
    """Parsed report state from one runtime log."""

    lifecycles: dict[PairKey, PairLifecycle]
    tail_telemetry: dict[PairKey, TailTelemetry]
    latent_attempts: dict[PairKey, LatentAttempt]
    market_snapshot: MarketSnapshot | None
    last_log_at: datetime | None


@dataclass(frozen=True)
class DbFillSummary:
    """DB-derived fill summary for one client order id."""

    client_order_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str | None
    liquidity_role: str | None


@dataclass(frozen=True)
class DbOrderSummary:
    """DB-derived latest order state for one client order id."""

    client_order_id: str
    side: str
    status: str
    price: Decimal | None
    quantity: Decimal
    filled_quantity: Decimal


@dataclass(frozen=True)
class ReportRow:
    """One rendered terminated-pair row.

    `amend_diff` is a price difference in quote units per base unit, for
    example USD per ADA on PF_ADAUSD.  Gross and net are quote-currency amounts.
    """

    key: PairKey
    head_fill_at: datetime
    tail_fill_at: datetime
    life_seconds: int
    side: str
    head_price: Decimal
    tail_price: Decimal
    quantity: Decimal
    liquidity: str
    amend_count: int
    tail_amend_1_at: datetime | None
    tail_amend_2_at: datetime | None
    amend_diff: Decimal | None
    gross_usd: Decimal
    net_usd: Decimal | None
    roi_percent: Decimal | None
    cumulative_net: Decimal | None


@dataclass(frozen=True)
class LivingTailRow:
    """One head-filled pair whose tail is still flying."""

    key: PairKey
    head_fill_at: datetime
    age_seconds: int
    side: str
    head_price: Decimal
    quantity: Decimal
    head_liquidity: str
    tail_stop: Decimal | None
    reference_price: Decimal | None
    current_distance: Decimal | None
    tail_status: str
    tail_filled_quantity: Decimal | None


@dataclass(frozen=True)
class LatentRow:
    """One latest active latent/head-pending attempt."""

    key: PairKey
    time: datetime
    status: str
    gate: str
    reference_price: Decimal | None
    head_price: Decimal | None
    quantity: Decimal | None
    order_type: str
    deadline_at: datetime | None
    last_event: str


@dataclass(frozen=True)
class ReportOptions:
    """Presentation options for the Org report table."""

    price_places: int = 5
    diff_places: int = 5
    money_places: int = 6
    pct_places: int = 4


@dataclass
class _FillAccumulator:
    client_order_id: str
    side: str
    total_quantity: Decimal = Decimal("0")
    weighted_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    fee_currencies: set[str] = field(default_factory=set)
    liquidity_roles: list[str] = field(default_factory=list)
    seen_fill_ids: set[int] = field(default_factory=set)

    def add_fill(
        self,
        *,
        fill_id: int,
        price: Decimal,
        quantity: Decimal,
        fee: Decimal | None,
        fee_currency: str | None,
        liquidity_role: str | None,
    ) -> None:
        if fill_id in self.seen_fill_ids:
            return
        self.seen_fill_ids.add(fill_id)
        self.total_quantity += quantity
        self.weighted_price += price * quantity
        if fee is not None:
            self.fee += fee
        if fee_currency:
            self.fee_currencies.add(fee_currency)
        if liquidity_role:
            self.liquidity_roles.append(liquidity_role)

    def summary(self) -> DbFillSummary:
        price = (
            self.weighted_price / self.total_quantity
            if self.total_quantity
            else Decimal("0")
        )
        fee_currency = None
        if len(self.fee_currencies) == 1:
            fee_currency = next(iter(self.fee_currencies))
        elif len(self.fee_currencies) > 1:
            fee_currency = "mixed"
        return DbFillSummary(
            client_order_id=self.client_order_id,
            side=self.side,
            quantity=self.total_quantity,
            price=price,
            fee=self.fee,
            fee_currency=fee_currency,
            liquidity_role=_summarise_liquidity(self.liquidity_roles),
        )


_EVENT_RE = re.compile(
    r"^(?P<log_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) .*?/ "
    r"(?P<event>[A-Z0-9_-]+) \((?P<pair>[^)]+)\): (?P<body>.*)$"
)
_ENV_REF_RE = re.compile(r"\$\{([^}]+)\}")
_USD_FEE_CURRENCIES = {"", "usd", "usdt", "zfusd"}


def parse_log_file(path: str | Path) -> dict[PairKey, PairLifecycle]:
    """Parse a Kolabi runtime log file into pair lifecycles."""

    return parse_run_log_file(path).lifecycles


def parse_log_text(text: str) -> dict[PairKey, PairLifecycle]:
    """Parse a Kolabi runtime log text into pair lifecycles."""

    return parse_run_log_text(text).lifecycles


def parse_run_log_file(path: str | Path) -> RunLogSnapshot:
    """Parse a Kolabi runtime log file into all reportable run state."""

    return parse_run_log_text(Path(path).read_text(encoding="utf-8", errors="replace"))


def parse_run_log_text(text: str) -> RunLogSnapshot:
    """Parse compact runtime lifecycle lines from text.

    The parser intentionally ignores unrelated log lines.  Required events are
    `HEAD_SENT`, `UPDATE`, and `AMEND_SENT`; private DB rows fill in exact
    prices, fees, and liquidity when the final report is built.  `METRICS`
    rows provide living-tail distance, and latent-head events provide the
    latest active not-yet-filled attempts.  Terminal `closed--living` and
    `closed--closed` updates can carry `0.0000` stop placeholders after the
    exchange has already filled the tail; those placeholders are ignored for
    amendment-diff calculations.
    """

    lifecycles: dict[PairKey, PairLifecycle] = {}
    tail_telemetry: dict[PairKey, TailTelemetry] = {}
    latent_attempts: dict[PairKey, LatentAttempt] = {}
    market_snapshot: MarketSnapshot | None = None
    last_log_at: datetime | None = None
    for raw_line in text.splitlines():
        match = _EVENT_RE.match(raw_line)
        if match is None:
            continue
        log_time = _parse_log_utc(match.group("log_ts"))
        if last_log_at is None or log_time > last_log_at:
            last_log_at = log_time
        key = _parse_pair_key(match.group("pair"))
        if key is None:
            continue
        lifecycle = lifecycles.setdefault(key, PairLifecycle(key=key))
        event = match.group("event")
        body = match.group("body")
        if event == "HEAD_SENT":
            _parse_head_sent(lifecycle, body)
            _parse_latent_head_sent(latent_attempts, key, body, log_time)
        elif event == "UPDATE":
            _parse_update(lifecycle, body)
        elif event == "AMEND_SENT":
            _parse_amend_sent(lifecycle, body, log_time)
        elif event == "METRICS":
            parsed_market = _parse_tail_metrics(tail_telemetry, key, body, log_time)
            if parsed_market is not None and (
                market_snapshot is None
                or parsed_market.recorded_at >= market_snapshot.recorded_at
            ):
                market_snapshot = parsed_market
        elif event == "REPEAT_READY":
            _parse_repeat_ready(latent_attempts, key, body, log_time)
        elif event == "LATENT_TIMEOUT_ARMED":
            _parse_latent_timeout_armed(latent_attempts, key, body, log_time)
        elif event.startswith("GATE_WAIT"):
            _parse_gate_wait(latent_attempts, key, event, body, log_time)
        elif event in {
            "COMMAND_FAILED",
            "HEAD_CANCELLED",
            "HEAD_CANCEL_SENT",
            "HEAD_TIMEOUT",
        }:
            _mark_latent_ended(latent_attempts, key, event, log_time)
    return RunLogSnapshot(
        lifecycles=lifecycles,
        tail_telemetry=tail_telemetry,
        latent_attempts=latent_attempts,
        market_snapshot=market_snapshot,
        last_log_at=last_log_at,
    )


def fetch_fill_summaries(
    db_url: str,
    client_order_ids: Iterable[str],
) -> dict[str, DbFillSummary]:
    """Fetch exact fill facts for the requested client order ids.

    Multiple fills for the same order are combined with a quantity-weighted
    average price and summed fees.  Liquidity is summarised as taker if any fill
    took liquidity, otherwise maker if any fill made liquidity.
    """

    ids = sorted({client_id for client_id in client_order_ids if client_id})
    if not ids:
        return {}
    engine = create_engine(db_url, echo=False, future=True)
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(ExchangeOrder, ExchangeFill)
                .join(ExchangeFill, ExchangeFill.order_id == ExchangeOrder.id)
                .where(ExchangeOrder.client_order_id.in_(ids))
                .order_by(ExchangeFill.local_timestamp, ExchangeFill.id)
            ).all()
    except SQLAlchemyError as exc:
        raise ReportError(f"could not read local account DB: {_compact_error(exc)}") from exc
    finally:
        engine.dispose()

    accumulators: dict[str, _FillAccumulator] = {}
    for order, fill in rows:
        client_id = order.client_order_id
        if not client_id:
            continue
        accumulator = accumulators.setdefault(
            client_id,
            _FillAccumulator(client_order_id=client_id, side=order.side),
        )
        accumulator.add_fill(
            fill_id=fill.id,
            price=_decimal(fill.price),
            quantity=_decimal(fill.quantity),
            fee=_optional_decimal(fill.fee),
            fee_currency=fill.fee_currency,
            liquidity_role=fill.liquidity_role,
        )
    return {
        client_id: accumulator.summary()
        for client_id, accumulator in accumulators.items()
    }


def fetch_order_summaries(
    db_url: str,
    client_order_ids: Iterable[str],
) -> dict[str, DbOrderSummary]:
    """Fetch latest local order state for requested client order ids."""

    ids = sorted({client_id for client_id in client_order_ids if client_id})
    if not ids:
        return {}
    engine = create_engine(db_url, echo=False, future=True)
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(ExchangeOrder)
                .where(ExchangeOrder.client_order_id.in_(ids))
                .order_by(ExchangeOrder.local_timestamp, ExchangeOrder.id)
            ).scalars()
            summaries: dict[str, DbOrderSummary] = {}
            for order in rows:
                client_id = order.client_order_id
                if not client_id:
                    continue
                summaries[client_id] = DbOrderSummary(
                    client_order_id=client_id,
                    side=order.side,
                    status=order.status,
                    price=_optional_decimal(order.price),
                    quantity=_decimal(order.quantity),
                    filled_quantity=_decimal(order.filled_quantity),
                )
    except SQLAlchemyError as exc:
        raise ReportError(f"could not read local account DB: {_compact_error(exc)}") from exc
    finally:
        engine.dispose()
    return summaries


def build_report_rows(
    lifecycles: Mapping[PairKey, PairLifecycle],
    *,
    fill_summaries: Mapping[str, DbFillSummary] | None = None,
    require_db: bool = False,
) -> tuple[ReportRow, ...]:
    """Build sorted report rows and a running cumulative net value."""

    fill_summaries = fill_summaries or {}
    rows: list[ReportRow] = []
    cumulative_net: Decimal | None = Decimal("0")
    for lifecycle in sorted(
        (item for item in lifecycles.values() if item.terminated),
        key=lambda item: (
            item.tail_fill.filled_at
            if item.tail_fill
            else datetime.max.replace(tzinfo=timezone.utc)
        ),
    ):
        row = _build_report_row(lifecycle, fill_summaries, require_db=require_db)
        if row.net_usd is None:
            cumulative = None
            cumulative_net = None
        elif cumulative_net is None:
            cumulative = None
        else:
            cumulative_net += row.net_usd
            cumulative = cumulative_net
        rows.append(
            ReportRow(
                key=row.key,
                head_fill_at=row.head_fill_at,
                tail_fill_at=row.tail_fill_at,
                life_seconds=row.life_seconds,
                side=row.side,
                head_price=row.head_price,
                tail_price=row.tail_price,
                quantity=row.quantity,
                liquidity=row.liquidity,
                amend_count=row.amend_count,
                tail_amend_1_at=row.tail_amend_1_at,
                tail_amend_2_at=row.tail_amend_2_at,
                amend_diff=row.amend_diff,
                gross_usd=row.gross_usd,
                net_usd=row.net_usd,
                roi_percent=row.roi_percent,
                cumulative_net=cumulative,
            )
        )
    return tuple(rows)


def render_org_table(
    rows: Sequence[ReportRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render report rows as an aligned Org table."""

    options = options or ReportOptions()
    headers = (
        "H fill UTC",
        "T fill UTC",
        "Life",
        "Pair",
        "h/t side",
        "Hfill",
        "Tfill",
        "Qty",
        "H/T liq",
        "A#",
        "T amend1 UTC",
        "T amend2 UTC",
        "A.diff",
        "Gross USD",
        "Net USD",
        "ROI %",
        "Cum net",
    )
    pair_name_width = max((len(row.key.name) for row in rows), default=4)
    pair_attempt_width = max((len(f"#{row.key.attempt}") for row in rows), default=2)
    body = [
        (
            _format_time(row.head_fill_at),
            _format_time(row.tail_fill_at),
            _format_life(row.life_seconds),
            _format_pair(row.key, pair_name_width, pair_attempt_width),
            row.side,
            _format_decimal(row.head_price, options.price_places),
            _format_decimal(row.tail_price, options.price_places),
            _format_quantity(row.quantity),
            row.liquidity,
            str(row.amend_count),
            _format_optional_time(row.tail_amend_1_at),
            _format_optional_time(row.tail_amend_2_at),
            _format_signed_optional(row.amend_diff, options.diff_places),
            _format_signed(row.gross_usd, options.money_places),
            _format_signed_optional(row.net_usd, options.money_places),
            _format_signed_optional(row.roi_percent, options.pct_places),
            _format_signed_optional(row.cumulative_net, options.money_places),
        )
        for row in rows
    ]
    align_right = {
        "Hfill",
        "Tfill",
        "Qty",
        "A#",
        "A.diff",
        "Gross USD",
        "Net USD",
        "ROI %",
        "Cum net",
    }
    return _format_table(headers, body, align_right=align_right)


def build_living_tail_rows(
    lifecycles: Mapping[PairKey, PairLifecycle],
    *,
    fill_summaries: Mapping[str, DbFillSummary] | None = None,
    order_summaries: Mapping[str, DbOrderSummary] | None = None,
    tail_telemetry: Mapping[PairKey, TailTelemetry] | None = None,
    snapshot_at: datetime | None = None,
) -> tuple[LivingTailRow, ...]:
    """Build rows for head-filled pairs whose tail is still flying."""

    fill_summaries = fill_summaries or {}
    order_summaries = order_summaries or {}
    tail_telemetry = tail_telemetry or {}
    rows: list[LivingTailRow] = []
    for lifecycle in sorted(
        (
            item
            for item in lifecycles.values()
            if item.head_fill is not None
            and item.tail_fill is None
            and item.tail_client_id is not None
        ),
        key=lambda item: item.head_fill.filled_at if item.head_fill else datetime.min,
    ):
        head_summary = _fill_summary_for(lifecycle.head_client_id, fill_summaries)
        tail_order = _order_summary_for(lifecycle.tail_client_id, order_summaries)
        telemetry = tail_telemetry.get(lifecycle.key)
        head_fill = lifecycle.head_fill
        if head_fill is None:
            continue
        head_price = head_summary.price if head_summary is not None else head_fill.price
        quantity = (
            head_summary.quantity
            if head_summary is not None and head_summary.quantity
            else head_fill.quantity
        )
        tail_stop = lifecycle.latest_tail_stop
        if tail_stop is None and telemetry is not None:
            tail_stop = telemetry.stop_price
        age_seconds = 0
        if snapshot_at is not None:
            age_seconds = int(
                (
                    snapshot_at.replace(microsecond=0)
                    - head_fill.filled_at.replace(microsecond=0)
                ).total_seconds()
            )
        rows.append(
            LivingTailRow(
                key=lifecycle.key,
                head_fill_at=head_fill.filled_at,
                age_seconds=age_seconds,
                side=_side_abbrev(head_fill.side, _opposite_side(head_fill.side)),
                head_price=head_price,
                quantity=quantity,
                head_liquidity=_liquidity_abbrev(
                    head_summary.liquidity_role if head_summary is not None else None
                ),
                tail_stop=tail_stop,
                reference_price=telemetry.reference_price if telemetry else None,
                current_distance=telemetry.current_distance if telemetry else None,
                tail_status=tail_order.status if tail_order is not None else "",
                tail_filled_quantity=(
                    tail_order.filled_quantity if tail_order is not None else None
                ),
            )
        )
    return tuple(rows)


def render_living_tail_table(
    rows: Sequence[LivingTailRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render living tail-flying rows as an aligned Org table."""

    options = options or ReportOptions()
    headers = (
        "H fill UTC",
        "Age",
        "Pair",
        "h/t side",
        "Hfill",
        "Qty",
        "Hliq",
        "Tstop",
        "Dist",
        "T status",
        "T filled",
    )
    pair_name_width = max((len(row.key.name) for row in rows), default=4)
    pair_attempt_width = max((len(f"#{row.key.attempt}") for row in rows), default=2)
    body = [
        (
            _format_time(row.head_fill_at),
            _format_life(row.age_seconds),
            _format_pair(row.key, pair_name_width, pair_attempt_width),
            row.side,
            _format_decimal(row.head_price, options.price_places),
            _format_quantity(row.quantity),
            row.head_liquidity,
            _format_optional_decimal(row.tail_stop, options.price_places),
            _format_optional_decimal(row.current_distance, options.diff_places),
            row.tail_status,
            _format_optional_quantity(row.tail_filled_quantity),
        )
        for row in rows
    ]
    return _format_table(
        headers,
        body,
        align_right={"Hfill", "Qty", "Tstop", "Dist", "T filled"},
    )


def build_latent_rows(
    lifecycles: Mapping[PairKey, PairLifecycle],
    latent_attempts: Mapping[PairKey, LatentAttempt],
) -> tuple[LatentRow, ...]:
    """Build rows for latest active latent/head-pending attempts."""

    latest_by_name: dict[str, LatentAttempt] = {}
    for attempt in latent_attempts.values():
        current = latest_by_name.get(attempt.key.name)
        if current is None or attempt.key.attempt > current.key.attempt:
            latest_by_name[attempt.key.name] = attempt

    rows: list[LatentRow] = []
    for attempt in sorted(latest_by_name.values(), key=lambda item: item.key):
        lifecycle = lifecycles.get(attempt.key)
        if attempt.ended:
            continue
        if lifecycle is not None and lifecycle.head_fill is not None:
            continue
        rows.append(
            LatentRow(
                key=attempt.key,
                time=attempt.last_event_at,
                status=_latent_status(attempt),
                gate=attempt.gate,
                reference_price=attempt.reference_price,
                head_price=attempt.head_price,
                quantity=attempt.quantity,
                order_type=attempt.order_type,
                deadline_at=attempt.deadline_at,
                last_event=attempt.last_event,
            )
        )
    return tuple(rows)


def render_latent_table(
    rows: Sequence[LatentRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render latest active latent rows as an aligned Org table."""

    options = options or ReportOptions()
    headers = (
        "Time",
        "Pair",
        "Status",
        "Gate",
        "H price",
        "Qty",
        "Type",
        "Deadline",
        "Last event",
    )
    pair_name_width = max((len(row.key.name) for row in rows), default=4)
    pair_attempt_width = max((len(f"#{row.key.attempt}") for row in rows), default=2)
    body = [
        (
            _format_time(row.time),
            _format_pair(row.key, pair_name_width, pair_attempt_width),
            row.status,
            row.gate,
            _format_optional_decimal(row.head_price, options.price_places),
            _format_optional_quantity(row.quantity),
            row.order_type,
            _format_optional_time(row.deadline_at),
            row.last_event,
        )
        for row in rows
    ]
    return _format_table(
        headers,
        body,
        align_right={"H price", "Qty"},
    )


def render_market_snapshot_line(
    snapshot: MarketSnapshot | None,
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render the latest parsed mark and market prices as one compact line."""

    options = options or ReportOptions()
    if snapshot is None:
        return "Latest prices: unavailable."
    return " ".join(
        (
            f"Latest prices: {_format_time(snapshot.recorded_at)}",
            f"mark={_format_optional_price_word(snapshot.mark_price, options.price_places)}",
            f"bid={_format_optional_price_word(snapshot.bid_price, options.price_places)}",
            f"ask={_format_optional_price_word(snapshot.ask_price, options.price_places)}",
            f"mid={_format_optional_price_word(snapshot.mid_price, options.price_places)}",
            f"last={_format_optional_price_word(snapshot.last_price, options.price_places)}",
            f"index={_format_optional_price_word(snapshot.index_price, options.price_places)}",
            f"src={snapshot.source or '-'}",
        )
    )


def render_run_report(
    terminated_rows: Sequence[ReportRow],
    living_rows: Sequence[LivingTailRow],
    latent_rows: Sequence[LatentRow],
    *,
    market_snapshot: MarketSnapshot | None = None,
    options: ReportOptions | None = None,
) -> str:
    """Render the full operator report as Org sections."""

    options = options or ReportOptions()
    sections = [
        "* Terminated pairs",
        _render_section_table(render_org_table, terminated_rows, options=options),
        "",
        "* Living tail-flying pairs",
        _render_section_table(render_living_tail_table, living_rows, options=options),
        "",
        "* Latest latent pairs",
        _render_section_table(render_latent_table, latent_rows, options=options),
        render_market_snapshot_line(market_snapshot, options=options),
    ]
    return "\n".join(sections)


def build_report_table(
    log_path: str | Path,
    *,
    db_url: str | None = None,
    log_only: bool = False,
    options: ReportOptions | None = None,
) -> str:
    """Build the full report table from a runtime log and optional DB URL."""

    snapshot = parse_run_log_file(log_path)
    fill_summaries: Mapping[str, DbFillSummary] = {}
    order_summaries: Mapping[str, DbOrderSummary] = {}
    if not log_only:
        if not db_url:
            raise ReportError(
                "account DB URL is required for exact reports; pass --account-db-url "
                "or --log-only"
            )
        client_ids = _client_ids(snapshot.lifecycles.values())
        fill_summaries = fetch_fill_summaries(db_url, client_ids)
        order_summaries = fetch_order_summaries(db_url, client_ids)
    terminated_rows = build_report_rows(
        snapshot.lifecycles,
        fill_summaries=fill_summaries,
        require_db=not log_only,
    )
    living_rows = build_living_tail_rows(
        snapshot.lifecycles,
        fill_summaries=fill_summaries,
        order_summaries=order_summaries,
        tail_telemetry=snapshot.tail_telemetry,
        snapshot_at=snapshot.last_log_at,
    )
    latent_rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    return render_run_report(
        terminated_rows,
        living_rows,
        latent_rows,
        market_snapshot=snapshot.market_snapshot,
        options=options,
    )


def resolve_account_db_url(
    explicit_url: str | None,
    *,
    env_file: str | Path | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the account DB URL from CLI, environment, then env file."""

    if explicit_url:
        return explicit_url
    env_mapping = env or os.environ
    if env_mapping.get("KOLABI_ACCOUNT_DB_URL"):
        return env_mapping["KOLABI_ACCOUNT_DB_URL"]
    if env_file is None:
        return None
    values = _load_env_file(Path(env_file), env=env_mapping)
    return values.get("KOLABI_ACCOUNT_DB_URL")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser for `kolabi-run-report`."""

    parser = argparse.ArgumentParser(
        prog="kolabi-run-report",
        description=(
            "Generate aligned Org report tables for terminated, living "
            "tail-flying, and latest latent Kolabi pairs."
        ),
        epilog=(
            "Source order: logs provide lifecycle timing, amendments, tail "
            "telemetry, and latent gate events; the local account DB provides "
            "exact fills, fees, maker/taker roles, and open-tail state. Use "
            "--log-only only when DB rows are unavailable. ROI is return on "
            "head notional."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("log_file", help="Kolabi runtime log file to parse.")
    parser.add_argument(
        "--account-db-url",
        help="Local account DB URL used for exact fill, fee, and liquidity data.",
    )
    parser.add_argument(
        "--env-file",
        default=".env.postgres",
        help="Env file to read KOLABI_ACCOUNT_DB_URL from when needed.",
    )
    parser.add_argument(
        "--log-only",
        action="store_true",
        help="Use only log data. DB-only columns are left blank where needed.",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Write the Org table to this file instead of stdout.",
    )
    parser.add_argument(
        "--price-dp",
        type=int,
        default=5,
        help="Decimal places for Hfill and Tfill.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        db_url = None
        if not args.log_only:
            db_url = resolve_account_db_url(args.account_db_url, env_file=args.env_file)
        table = build_report_table(
            args.log_file,
            db_url=db_url,
            log_only=args.log_only,
            options=ReportOptions(price_places=args.price_dp),
        )
        if args.output:
            Path(args.output).write_text(table + "\n", encoding="utf-8")
        else:
            print(table, file=out)
    except ReportError as exc:
        print(f"kolabi-run-report: {exc}", file=err)
        return 2
    except OSError as exc:
        print(f"kolabi-run-report: {exc}", file=err)
        return 2
    return 0


def _parse_pair_key(raw: str) -> PairKey | None:
    if "#" not in raw:
        return None
    name, attempt = raw.rsplit("#", 1)
    try:
        return PairKey(name=name, attempt=int(attempt))
    except ValueError:
        return None


def _parse_head_sent(lifecycle: PairLifecycle, body: str) -> None:
    fields = body.split()
    if fields:
        lifecycle.head_client_id = fields[0]


def _parse_update(lifecycle: PairLifecycle, body: str) -> None:
    fields = body.split()
    if not fields:
        return
    state = fields[0]
    if state == "closed--hooked" and len(fields) >= 7:
        initial_tail_stop = _positive_decimal(fields[2])
        if lifecycle.initial_tail_stop is None and initial_tail_stop is not None:
            lifecycle.initial_tail_stop = initial_tail_stop
        lifecycle.head_fill = FillLeg(
            side=fields[3],
            quantity=_decimal(fields[4]),
            price=_decimal(fields[5]),
            filled_at=_parse_iso_utc(fields[6]),
        )
    elif state == "closed--living" and len(fields) >= 7:
        confirmed_stop = _positive_decimal(fields[2])
        desired_stop = _positive_decimal(fields[3])
        if lifecycle.initial_tail_stop is None and confirmed_stop is not None:
            lifecycle.initial_tail_stop = confirmed_stop
        if desired_stop is not None:
            lifecycle.latest_tail_stop = desired_stop
        lifecycle.tail_client_id = fields[4]
    elif state == "closed--closed" and len(fields) >= 8:
        if lifecycle.tail_fill is None:
            lifecycle.tail_fill = FillLeg(
                side=fields[4],
                quantity=_decimal(fields[5]),
                price=_decimal(fields[6]),
                filled_at=_parse_iso_utc(fields[7]),
            )
        latest_tail_stop = _positive_decimal(fields[2])
        if latest_tail_stop is not None:
            lifecycle.latest_tail_stop = latest_tail_stop


def _parse_amend_sent(
    lifecycle: PairLifecycle,
    body: str,
    log_time: datetime,
) -> None:
    fields = body.split()
    if len(fields) < 6:
        return
    lifecycle.amend_count += 1
    lifecycle.tail_amend_times.append(log_time)
    lifecycle.latest_tail_stop = _decimal(fields[1])
    lifecycle.tail_client_id = fields[4]


def _parse_tail_metrics(
    tail_telemetry: dict[PairKey, TailTelemetry],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> MarketSnapshot | None:
    fields = body.split()
    market_snapshot = _parse_market_snapshot(fields, log_time)
    if len(fields) < 7:
        return market_snapshot
    if not fields[0].endswith("--living"):
        return market_snapshot
    tail_telemetry[key] = TailTelemetry(
        key=key,
        recorded_at=log_time,
        reference_price=_decimal(fields[1]),
        stop_price=_decimal(fields[2]),
        current_distance=_decimal(fields[4]),
    )
    return market_snapshot


def _parse_market_snapshot(
    fields: Sequence[str],
    log_time: datetime,
) -> MarketSnapshot | None:
    if len(fields) < 15:
        return None
    return MarketSnapshot(
        recorded_at=log_time,
        source=fields[8],
        bid_price=_field_decimal(fields[9]),
        ask_price=_field_decimal(fields[10]),
        mid_price=_field_decimal(fields[11]),
        last_price=_field_decimal(fields[12]),
        mark_price=_field_decimal(fields[13]),
        index_price=_field_decimal(fields[14]),
    )


def _parse_repeat_ready(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, "REPEAT_READY")
    fields = body.split()
    attempt.status = fields[0] if fields else "repeat_ready"
    attempt.last_event_at = log_time
    attempt.last_event = "REPEAT_READY"


def _parse_latent_timeout_armed(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, "LATENT_TIMEOUT_ARMED")
    fields = body.split()
    if fields:
        attempt.deadline_at = _parse_iso_utc(fields[0])
    attempt.last_event_at = log_time
    attempt.last_event = "LATENT_TIMEOUT_ARMED"


def _parse_gate_wait(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    event: str,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, event)
    fields = body.split()
    if fields:
        attempt.status = fields[0]
    if event == "GATE_WAIT-2" and len(fields) >= 8:
        attempt.gate = f"{fields[0]} {fields[1]}"
        attempt.reference_price = _optional_positive_decimal(fields[2])
        attempt.order_type = fields[6]
        attempt.head_price = _optional_positive_decimal(fields[7])
    elif event == "GATE_WAIT-1" and fields:
        attempt.gate = fields[0]
        if len(fields) >= 4:
            attempt.order_type = fields[2]
            attempt.head_price = _optional_positive_decimal(fields[3])
    attempt.last_event_at = log_time
    attempt.last_event = event


def _parse_latent_head_sent(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, "HEAD_SENT")
    fields = body.split()
    if len(fields) >= 5:
        attempt.head_client_id = fields[0]
        attempt.status = "head_sent"
        attempt.order_type = fields[2]
        attempt.quantity = _decimal(fields[3])
        attempt.head_price = _optional_positive_decimal(fields[4])
    attempt.last_event_at = log_time
    attempt.last_event = "HEAD_SENT"


def _mark_latent_ended(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    event: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, event)
    attempt.ended = True
    attempt.status = event.lower()
    attempt.last_event_at = log_time
    attempt.last_event = event


def _latent_attempt(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    log_time: datetime,
    event: str,
) -> LatentAttempt:
    return latent_attempts.setdefault(
        key,
        LatentAttempt(key=key, last_event_at=log_time, last_event=event),
    )


def _build_report_row(
    lifecycle: PairLifecycle,
    fill_summaries: Mapping[str, DbFillSummary],
    *,
    require_db: bool,
) -> ReportRow:
    if lifecycle.head_fill is None or lifecycle.tail_fill is None:
        raise ReportError(f"pair {lifecycle.key} is not terminated")
    head_summary = _fill_summary_for(lifecycle.head_client_id, fill_summaries)
    tail_summary = _fill_summary_for(lifecycle.tail_client_id, fill_summaries)
    if require_db and (head_summary is None or tail_summary is None):
        missing = []
        if lifecycle.head_client_id and head_summary is None:
            missing.append(lifecycle.head_client_id)
        if lifecycle.tail_client_id and tail_summary is None:
            missing.append(lifecycle.tail_client_id)
        if not lifecycle.head_client_id:
            missing.append(f"{lifecycle.key.name}#{lifecycle.key.attempt}:head")
        if not lifecycle.tail_client_id:
            missing.append(f"{lifecycle.key.name}#{lifecycle.key.attempt}:tail")
        raise ReportError("missing local DB fill rows for " + ", ".join(missing))

    head_price = head_summary.price if head_summary is not None else lifecycle.head_fill.price
    tail_price = tail_summary.price if tail_summary is not None else lifecycle.tail_fill.price
    quantity = (
        tail_summary.quantity
        if tail_summary is not None and tail_summary.quantity
        else lifecycle.tail_fill.quantity
    )
    side = _side_abbrev(lifecycle.head_fill.side, lifecycle.tail_fill.side)
    gross = _gross_usd(lifecycle.head_fill.side, head_price, tail_price, quantity)
    net = None
    if head_summary is not None and tail_summary is not None:
        net = _net_usd(gross, head_summary, tail_summary)
    roi = _roi_percent(net if net is not None else gross, head_price, quantity)
    amend_diff = None
    if (
        lifecycle.amend_count > 0
        and lifecycle.initial_tail_stop is not None
        and lifecycle.latest_tail_stop is not None
    ):
        amend_diff = lifecycle.latest_tail_stop - lifecycle.initial_tail_stop

    return ReportRow(
        key=lifecycle.key,
        head_fill_at=lifecycle.head_fill.filled_at,
        tail_fill_at=lifecycle.tail_fill.filled_at,
        life_seconds=int(
            (
                lifecycle.tail_fill.filled_at.replace(microsecond=0)
                - lifecycle.head_fill.filled_at.replace(microsecond=0)
            ).total_seconds()
        ),
        side=side,
        head_price=head_price,
        tail_price=tail_price,
        quantity=quantity,
        liquidity=_liquidity_pair(head_summary, tail_summary),
        amend_count=lifecycle.amend_count,
        tail_amend_1_at=_tail_amend_time(lifecycle, 0),
        tail_amend_2_at=_tail_amend_time(lifecycle, 1),
        amend_diff=amend_diff,
        gross_usd=gross,
        net_usd=net,
        roi_percent=roi,
        cumulative_net=None,
    )


def _client_ids(lifecycles: Iterable[PairLifecycle]) -> tuple[str, ...]:
    ids: set[str] = set()
    for lifecycle in lifecycles:
        if lifecycle.head_client_id:
            ids.add(lifecycle.head_client_id)
        if lifecycle.tail_client_id:
            ids.add(lifecycle.tail_client_id)
    return tuple(sorted(ids))


def _fill_summary_for(
    client_id: str | None,
    fill_summaries: Mapping[str, DbFillSummary],
) -> DbFillSummary | None:
    if not client_id:
        return None
    return fill_summaries.get(client_id)


def _order_summary_for(
    client_id: str | None,
    order_summaries: Mapping[str, DbOrderSummary],
) -> DbOrderSummary | None:
    if not client_id:
        return None
    return order_summaries.get(client_id)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _field_decimal(value: object) -> Decimal | None:
    text = str(value).strip()
    if not text or text == "-":
        return None
    return _decimal(text)


def _positive_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    if parsed <= 0:
        return None
    return parsed


def _optional_positive_decimal(value: object) -> Decimal | None:
    try:
        return _positive_decimal(value)
    except Exception:
        return None


def _parse_iso_utc(raw: str) -> datetime:
    value = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_log_utc(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)


def _tail_amend_time(lifecycle: PairLifecycle, index: int) -> datetime | None:
    if index >= len(lifecycle.tail_amend_times):
        return None
    return lifecycle.tail_amend_times[index]


def _side_abbrev(head_side: str, tail_side: str) -> str:
    return f"{head_side[:1].upper()}/{tail_side[:1].upper()}"


def _opposite_side(side: str) -> str:
    normalized = side.lower()
    if normalized == "buy":
        return "sell"
    if normalized == "sell":
        return "buy"
    return ""


def _gross_usd(
    head_side: str,
    head_price: Decimal,
    tail_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    if head_side.lower() == "buy":
        return (tail_price - head_price) * quantity
    return (head_price - tail_price) * quantity


def _net_usd(
    gross: Decimal,
    head_summary: DbFillSummary,
    tail_summary: DbFillSummary,
) -> Decimal | None:
    if not _fee_is_usd(head_summary.fee_currency):
        return None
    if not _fee_is_usd(tail_summary.fee_currency):
        return None
    return gross - head_summary.fee - tail_summary.fee


def _roi_percent(
    basis: Decimal | None,
    head_price: Decimal,
    quantity: Decimal,
) -> Decimal | None:
    if basis is None:
        return None
    notional = head_price * quantity
    if notional == 0:
        return None
    return basis / notional * Decimal("100")


def _fee_is_usd(currency: str | None) -> bool:
    return (currency or "").strip().lower() in _USD_FEE_CURRENCIES


def _liquidity_pair(
    head_summary: DbFillSummary | None,
    tail_summary: DbFillSummary | None,
) -> str:
    head = _liquidity_abbrev(head_summary.liquidity_role if head_summary else None)
    tail = _liquidity_abbrev(tail_summary.liquidity_role if tail_summary else None)
    if not head and not tail:
        return ""
    return f"{head}/{tail}"


def _summarise_liquidity(roles: Sequence[str]) -> str | None:
    abbreviations = [_liquidity_abbrev(role) for role in roles]
    if "T" in abbreviations:
        return "T"
    if "M" in abbreviations:
        return "M"
    return abbreviations[0] if abbreviations else None


def _liquidity_abbrev(role: str | None) -> str:
    normalized = (role or "").strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("m"):
        return "M"
    if normalized.startswith("t"):
        return "T"
    return normalized[:1].upper()


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%m-%d %H:%M")


def _format_optional_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return _format_time(value)


def _format_life(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_pair(key: PairKey, name_width: int, attempt_width: int) -> str:
    return f"{key.name:<{name_width}} {f'#{key.attempt}':>{attempt_width}}"


def _format_quantity(value: Decimal) -> str:
    integral = value.to_integral_value()
    if value == integral:
        return str(int(integral))
    return format(value.normalize(), "f")


def _format_optional_quantity(value: Decimal | None) -> str:
    if value is None:
        return ""
    return _format_quantity(value)


def _format_decimal(value: Decimal, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):.{places}f}"


def _format_optional_decimal(value: Decimal | None, places: int) -> str:
    if value is None:
        return ""
    return _format_decimal(value, places)


def _format_optional_price_word(value: Decimal | None, places: int) -> str:
    if value is None:
        return "-"
    return _format_decimal(value, places)


def _format_signed(value: Decimal, places: int) -> str:
    formatted = _format_decimal(value, places)
    return formatted if formatted.startswith("-") else f"+{formatted}"


def _format_signed_optional(value: Decimal | None, places: int) -> str:
    if value is None:
        return ""
    return _format_signed(value, places)


def _format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    align_right: set[str],
) -> str:
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        if rows
        else len(headers[index])
        for index in range(len(headers))
    ]
    lines = [_format_table_row(headers, widths, align_right=align_right, headers=headers)]
    lines.append("|" + "+".join("-" * (width + 2) for width in widths) + "|")
    for row in rows:
        lines.append(_format_table_row(row, widths, align_right=align_right, headers=headers))
    return "\n".join(lines)


def _format_table_row(
    cells: Sequence[str],
    widths: Sequence[int],
    *,
    align_right: set[str],
    headers: Sequence[str],
) -> str:
    formatted = []
    for index, cell in enumerate(cells):
        header = headers[index]
        if header in align_right:
            formatted.append(f" {cell:>{widths[index]}} ")
        else:
            formatted.append(f" {cell:<{widths[index]}} ")
    return "|" + "|".join(formatted) + "|"


def _render_section_table(
    renderer,
    rows: Sequence[object],
    *,
    options: ReportOptions,
) -> str:
    if not rows:
        return "No rows."
    return renderer(rows, options=options)


def _latent_status(attempt: LatentAttempt) -> str:
    if attempt.status:
        return attempt.status
    if attempt.head_client_id:
        return "head_sent"
    if attempt.gate:
        return "gate_wait"
    return attempt.last_event.lower()


def _load_env_file(path: Path, *, env: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip().strip('"').strip("'")

        def expand(match: re.Match[str]) -> str:
            name = match.group(1)
            return values.get(name, env.get(name, ""))

        values[key] = _ENV_REF_RE.sub(expand, raw_value)
    return values


def _compact_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())


if __name__ == "__main__":
    raise SystemExit(main())
