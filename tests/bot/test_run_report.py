from __future__ import annotations

from decimal import Decimal
from io import StringIO
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kolabi.bot.run_report import (
    PairKey,
    ReportOptions,
    build_report_rows,
    fetch_fill_summaries,
    main,
    parse_log_text,
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
    assert "| +0.001680 | +0.000279 | +0.000279 |" in table


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
