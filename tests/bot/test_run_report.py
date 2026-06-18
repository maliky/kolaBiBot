from __future__ import annotations

from decimal import Decimal
from io import StringIO
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kolabi.bot.run_report import (
    PairKey,
    ReportOptions,
    build_latent_rows,
    build_living_tail_rows,
    build_report_table,
    build_report_rows,
    fetch_fill_summaries,
    fetch_order_summaries,
    main,
    parse_run_log_text,
    parse_log_text,
    render_latent_table,
    render_living_tail_table,
    render_market_snapshot_line,
    render_org_table,
)
from kolabi.shared.persistence import Base, ExchangeFill, ExchangeOrder


SAMPLE_LOG = "\n".join(
    (
        "2026-06-17 23:05:38,000 MainThread~20 /strategy_runtime.py@1@x/ "
        "HEAD_SENT (MM_BUY#4): H4alpha buy L 12.00 0.1667 -",
        "2026-06-17 23:05:39,483 MainThread~20 /strategy_runtime.py@1@x/ "
        "UPDATE (MM_BUY#4): closed--hooked 12.0 0.1645 buy 12.00 0.1667 "
        "2026-06-17T23:05:39.422000+00:00",
        "2026-06-17 23:05:39,753 MainThread~20 /strategy_runtime.py@1@x/ "
        "UPDATE (MM_BUY#4): closed--living 12.0 0.1645 0.1645 T4beta "
        "a20c4f50 2026-06-17T23:05:39.505000+00:00",
        "2026-06-17 23:19:21,864 MainThread~20 /strategy_runtime.py@1@x/ "
        "AMEND_SENT (MM_BUY#4): 0.1645 0.1669 0.1671 last T4beta a20c4f50",
        "2026-06-17 23:20:00,001 MainThread~20 /strategy_runtime.py@1@x/ "
        "AMEND_SENT (MM_BUY#4): 0.1669 0.1669 0.1671 last T4beta a20c4f50",
        "2026-06-17 23:21:32,325 MainThread~20 /strategy_runtime.py@1@x/ "
        "UPDATE (MM_BUY#4): closed--closed 12.0 0.1669 0.1669 sell 12.00 "
        "0.1669 2026-06-17T23:21:31.902000+00:00",
        "2026-06-17 23:21:32,555 MainThread~20 /strategy_runtime.py@1@x/ "
        "UPDATE (MM_BUY#4): closed--closed 12.0 0.1669 0.1669 sell 12.00 "
        "0.1669 2026-06-17T23:21:31.902000+00:00",
    )
)


def test_parse_log_collects_terminated_pair_once() -> None:
    lifecycles = parse_log_text(SAMPLE_LOG)
    lifecycle = lifecycles[PairKey("MM_BUY", 4)]

    assert lifecycle.head_client_id == "H4alpha"
    assert lifecycle.tail_client_id == "T4beta"
    assert lifecycle.amend_count == 2
    assert [item.isoformat() for item in lifecycle.tail_amend_times] == [
        "2026-06-17T23:19:21.864000+00:00",
        "2026-06-17T23:20:00.001000+00:00",
    ]
    assert lifecycle.terminated
    assert lifecycle.tail_fill is not None
    assert lifecycle.tail_fill.price == Decimal("0.1669")
    assert lifecycle.tail_fill.filled_at.isoformat() == "2026-06-17T23:21:31.902000+00:00"


def test_terminal_zero_stop_placeholders_do_not_overwrite_last_amend() -> None:
    log = "\n".join(
        (
            "2026-06-18 15:26:47,467 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_SEL#2): H2nimble sell L 5.00 0.1627 -",
            "2026-06-18 15:27:01,780 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--hooked 5.0 0.1656 sell 5.00 0.1627 "
            "2026-06-18T15:27:01.600000+00:00",
            "2026-06-18 15:27:02,051 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--living 5.0 0.1656 0.1656 T2tail "
            "tail-order 2026-06-18T15:27:01.817000+00:00",
            "2026-06-18 15:29:27,898 MainThread~20 /strategy_runtime.py@1@x/ "
            "AMEND_SENT (MM_SEL#2): 0.1656 0.1624 0.1621 last T2tail tail-order",
            "2026-06-18 15:29:28,153 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--living 5.0 0.1624 0.1624 T2tail "
            "tail-order 2026-06-18T15:29:27.919000+00:00",
            "2026-06-18 15:31:25,723 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--living 5.0 0.0000 0.0000 T2tail "
            "tail-order 2026-06-18T15:31:25.535000+00:00",
            "2026-06-18 15:31:45,102 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--closed 5.0 0.0000 0.0000 buy 5.00 "
            "0.1624 2026-06-18T15:31:44.826000+00:00",
        )
    )

    lifecycle = parse_log_text(log)[PairKey("MM_SEL", 2)]
    rows = build_report_rows({lifecycle.key: lifecycle})
    table = render_org_table(rows)

    assert lifecycle.initial_tail_stop == Decimal("0.1656")
    assert lifecycle.latest_tail_stop == Decimal("0.1624")
    assert lifecycle.tail_fill is not None
    assert lifecycle.tail_fill.price == Decimal("0.1624")
    assert rows[0].amend_diff == Decimal("-0.0032")
    assert "| -0.00320 | +0.001500 |" in table


def test_render_log_only_table_aligns_pair_attempt() -> None:
    rows = build_report_rows(parse_log_text(SAMPLE_LOG))
    table = render_org_table(rows, options=ReportOptions())

    assert (
        "| H fill UTC  | T fill UTC  | Life     | Pair      | h/t side |"
        in table
    )
    assert "T amend1 UTC" in table
    assert "T amend2 UTC" in table
    assert (
        "| 06-17 23:05 | 06-17 23:21 | 00:15:52 | MM_BUY #4 | B/S      |"
        in table
    )
    assert "|  2 | 06-17 23:19  | 06-17 23:20  | +0.00240 |" in table
    assert "| 0.16670 | 0.16690 |" in table
    assert "| +0.00240 | +0.002400 |" in table
    assert "| +0.002400 |         | +0.1200 |" in table


def test_render_log_only_table_leaves_second_amend_time_blank() -> None:
    single_amend_log = "\n".join(
        line for line in SAMPLE_LOG.splitlines() if "23:20:00,001" not in line
    )
    rows = build_report_rows(parse_log_text(single_amend_log))
    table = render_org_table(rows, options=ReportOptions())

    assert "|  1 | 06-17 23:19  |              | +0.00240 |" in table


def test_fetch_fill_summaries_aggregates_local_db_rows(postgres_url_factory) -> None:
    db_url = postgres_url_factory("run-report")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        head = ExchangeOrder(
            local_uuid="order-head",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="head-order",
            client_order_id="H4alpha",
            side="buy",
            order_type="limit",
            status="filled",
            price=0.1667,
            quantity=12,
            filled_quantity=12,
        )
        tail = ExchangeOrder(
            local_uuid="order-tail",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="tail-order",
            client_order_id="T4beta",
            side="sell",
            order_type="stop",
            status="filled",
            price=0.1669,
            quantity=12,
            filled_quantity=12,
        )
        session.add_all([head, tail])
        session.flush()
        session.add_all(
            [
                ExchangeFill(
                    local_uuid="fill-head",
                    order_id=head.id,
                    exchange="kraken",
                    exchange_fill_id="head-fill",
                    price=0.16671,
                    quantity=12,
                    fee=0.0006,
                    fee_currency="USD",
                    liquidity_role="maker",
                ),
                ExchangeFill(
                    local_uuid="fill-tail",
                    order_id=tail.id,
                    exchange="kraken",
                    exchange_fill_id="tail-fill",
                    price=0.16685,
                    quantity=12,
                    fee=0.000801,
                    fee_currency="USD",
                    liquidity_role="taker",
                ),
            ]
        )
        session.commit()
    engine.dispose()

    fills = fetch_fill_summaries(db_url, ("H4alpha", "T4beta"))
    rows = build_report_rows(parse_log_text(SAMPLE_LOG), fill_summaries=fills, require_db=True)
    table = render_org_table(rows)

    assert "| 0.16671 | 0.16685 |" in table
    assert "| M/T     |" in table
    assert "| +0.001680 | +0.000279 | +0.0139 | +0.000279 |" in table


def test_living_tail_rows_include_latest_metrics_and_db_order_state(postgres_url_factory) -> None:
    log = "\n".join(
        (
            "2026-06-18 15:23:32,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_BUY#1): H1alpha buy L 6.00 0.1625 -",
            "2026-06-18 15:23:34,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--hooked 6.0 0.1594 buy 6.00 0.1625 "
            "2026-06-18T15:23:34.392000+00:00",
            "2026-06-18 15:23:35,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--living 6.0 0.1594 0.1594 T1beta "
            "tail-order 2026-06-18T15:23:34.658000+00:00",
            "2026-06-18 18:50:31,793 MainThread~20 /strategy_runtime.py@1@x/ "
            "METRICS (MM_BUY#1): closed--living 0.1622 0.1594 0.0031 0.0029 "
            "0.0004 0.0037 2026-06-18T15:23:34.658510+00:00 last "
            "0.1607 0.1608 0.1607 0.1622 0.1623 0.1623",
        )
    )
    db_url = postgres_url_factory("living-report")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        head = ExchangeOrder(
            local_uuid="living-head",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="head-order",
            client_order_id="H1alpha",
            side="buy",
            order_type="limit",
            status="filled",
            price=0.1625,
            quantity=6,
            filled_quantity=6,
        )
        tail = ExchangeOrder(
            local_uuid="living-tail",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="tail-order",
            client_order_id="T1beta",
            side="sell",
            order_type="stop",
            status="untouched",
            price=0.15937,
            quantity=6,
            filled_quantity=0,
        )
        session.add_all([head, tail])
        session.flush()
        session.add(
            ExchangeFill(
                local_uuid="living-fill-head",
                order_id=head.id,
                exchange="kraken",
                exchange_fill_id="head-fill",
                price=0.16247,
                quantity=6,
                fee=0.000243,
                fee_currency="USD",
                liquidity_role="maker",
            )
        )
        session.commit()
    engine.dispose()

    snapshot = parse_run_log_text(log)
    fills = fetch_fill_summaries(db_url, ("H1alpha", "T1beta"))
    orders = fetch_order_summaries(db_url, ("H1alpha", "T1beta"))
    rows = build_living_tail_rows(
        snapshot.lifecycles,
        fill_summaries=fills,
        order_summaries=orders,
        tail_telemetry=snapshot.tail_telemetry,
        snapshot_at=snapshot.last_log_at,
    )
    table = render_living_tail_table(rows)

    assert "Ref" not in table.splitlines()[0]
    assert "| 06-18 15:23 | 03:26:57 | MM_BUY #1 | B/S      |" in table
    assert "| 0.16247 |   6 | M    | 0.15940 | 0.00290 | untouched |        0 |" in table
    assert (
        render_market_snapshot_line(snapshot.market_snapshot)
        == "Latest prices: 06-18 18:50 mark=0.16230 bid=0.16070 "
        "ask=0.16080 mid=0.16070 last=0.16220 index=0.16230 src=last"
    )


def test_latest_latent_rows_exclude_failed_latest_attempts() -> None:
    log = "\n".join(
        (
            "2026-06-18 19:00:21,362 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (UP_BUY2#213): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:21,363 MainThread~20 /strategy_runtime.py@1@x/ "
            "LATENT_TIMEOUT_ARMED (UP_BUY2#213): 2026-06-18T19:06:21.363093+00:00",
            "2026-06-18 19:00:21,387 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (UP_BUY2#213): ready ask 0.1625 - 0.1625 "
            "0.0000..1000000000.00 pA SL! 1.40 6.0",
            "2026-06-18 19:00:22,537 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (UP_BUY2#213): H213myrtle buy SL 16.00 0.1648 0.1648",
            "2026-06-18 19:00:22,882 MainThread~30 /strategy_runtime.py@1@x/ "
            "COMMAND_FAILED (UP_BUY2#213): head Kraken Futures post-only is only supported",
            "2026-06-18 19:00:38,386 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (RB_SEL#1): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:38,560 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-1 (RB_SEL#1): chain_wait UP_BUY2-tail-closed L! 0.0001 1.0",
        )
    )

    snapshot = parse_run_log_text(log)
    rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    table = render_latent_table(rows)

    assert "Ref" not in table.splitlines()[0]
    assert "UP_BUY2" not in table
    assert "| 06-18 19:00 | RB_SEL #1 | chain_wait | chain_wait |" in table


def test_full_report_renders_three_sections_in_log_only_mode(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")

    table = build_report_table(log_path, log_only=True)

    assert "* Terminated pairs" in table
    assert "* Living tail-flying pairs\nNo rows." in table
    assert "* Latest latent pairs\nNo rows." in table
    assert "Latest prices: unavailable." in table


def test_cli_log_only_writes_stdout(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    out = StringIO()
    err = StringIO()

    result = main(["--log-only", str(log_path)], stdout=out, stderr=err)

    assert result == 0
    assert err.getvalue() == ""
    assert "| 06-17 23:05 | 06-17 23:21 | 00:15:52 | MM_BUY #4 |" in out.getvalue()


def test_cli_requires_db_unless_log_only(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    out = StringIO()
    err = StringIO()

    result = main([str(log_path), "--env-file", str(tmp_path / "missing.env")], stdout=out, stderr=err)

    assert result == 2
    assert out.getvalue() == ""
    assert "account DB URL is required" in err.getvalue()
