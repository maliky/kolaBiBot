from __future__ import annotations

from pathlib import Path

import pytest
from kolabi.bot.tsv.parser import read_strategy_file


def _write_strategy(
    path: Path,
    rows: list[str],
    *,
    symbol_column: bool = False,
    exchange_column: bool = False,
) -> Path:
    header = "name\ttps_run\tessais\ttOut\tpause\tside\toType\toDelta\ttType\ttDelta\tatype\tqty\ttp\tprix\thook"
    if exchange_column:
        header += "\texchg"
    if symbol_column:
        header += "\tsymbol"
    path.write_text(
        "\n".join(
            [
                header,
                *rows,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_new_typed_field_grammar_parses_and_ignores_empty_columns(tmp_path: Path) -> None:
    path = tmp_path / "typed.tsv"
    path.write_text(
        "\n".join(
            [
                "exchg\tsymbol\t\tname\ttps_run\tessais\ttOut\tpause\tside\toType\toDelta\tqty\ttType\ttDelta\tprix\ttp\thook",
                "BTX\tXBTUSD\t\tXBTC_SEL\t0 1440\t1\t6\t\tsell\tL\tD6\tA1\tS-\t\tD- +\tD20\t",
                "KRKF\tPI_ADAUSD\t\tKADA_SEL\t0 1440\t1\t6\t\tsell\tL\t%0.20\tA1\tS-\t\tD- +\t%0.10\tXBTC_SEL-head-filled",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    strategy = read_strategy_file(path)

    assert [pair.name for pair in strategy.pairs] == ["XBTC_SEL", "KADA_SEL"]
    assert [
        (pair.exchange, pair.market_type, pair.symbol)
        for pair in strategy.pairs
    ] == [
        ("bitmex", "futures", "XBTUSD"),
        ("kraken", "futures", "PI_ADAUSD"),
    ]
    assert strategy.pairs[0].head.delta == 6.0
    assert strategy.pairs[0].head.delta_type == "oD"
    assert strategy.pairs[0].head_quantity == 1
    assert strategy.pairs[0].head_quantity_type == "qA"
    assert strategy.pairs[0].tail_price_spec == 20.0
    assert strategy.pairs[0].tail_price_spec_type == "tD"
    assert strategy.pairs[1].head.delta == 0.2
    assert strategy.pairs[1].head.delta_type == "o%"
    assert strategy.pairs[1].tail_price_spec == 0.1
    assert strategy.pairs[1].tail_price_spec_type == "t%"


def test_new_typed_field_grammar_rejects_untyped_non_empty_values() -> None:
    with pytest.raises(ValueError, match="oDelta value '6' must start"):
        read_strategy_file(Path("orders/new_parse_grammar.tsv"))


def test_new_typed_field_grammar_allows_empty_optional_values(tmp_path: Path) -> None:
    path = tmp_path / "empty_optional.tsv"
    path.write_text(
        "\n".join(
            [
                "exchg\tsymbol\tname\ttps_run\tessais\ttOut\tpause\tside\toType\toDelta\tqty\ttType\ttDelta\tprix\ttp\thook",
                "BINS\tADAUSDT\tNADA_BUY\t0 1440\t1\t6\t\tbuy\tM\t\tA3\tL!\t\t\t\t",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    strategy = read_strategy_file(path)

    pair = strategy.pairs[0]
    assert pair.head.delta is None
    assert pair.head.delta_type == "oD"
    assert pair.head_price == (-1_000_000.0, 1_000_000.0)
    assert pair.head_price_type == "pD"
    assert pair.tail_price_spec is None
    assert pair.tail_price_spec_type == "tD"


def test_head_limit_price_suffix_is_valid(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "valid.tsv",
            ["BUY_LM\t0 60\t1\t4\t\tbuy\tLm\t\tS-\t\tqAtDpD\t3\t8\t- -5\t"],
        )
    )

    assert strategy.pairs[0].head.order_type == "Lm"
    assert strategy.pairs[0].head_price == (-1_000_000.0, -5.0)
    assert strategy.pairs[0].head.delta_type == "oD"


def test_head_percent_offset_type_is_valid(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "valid.tsv",
            ["BUY_LM\t0 60\t1\t4\t\tbuy\tLm!\t1.5\tS-\t\tqAtDpDo%\t3\t8\t- +\t"],
        )
    )

    assert strategy.pairs[0].head.order_type == "Lm!"
    assert strategy.pairs[0].head.delta == 1.5
    assert strategy.pairs[0].head.delta_type == "o%"


def test_invalid_head_offset_type_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid o token"):
        read_strategy_file(
            _write_strategy(
                tmp_path / "invalid.tsv",
                ["BAD_O\t0 60\t1\t4\t\tbuy\tLm!\t1.5\tS-\t\tqAtDpDoA\t3\t8\t- +\t"],
            )
        )


def test_market_head_price_suffix_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="price suffix 'm'.*M head order type"):
        read_strategy_file(
            _write_strategy(
                tmp_path / "invalid.tsv",
                ["BAD_M\t0 60\t1\t4\t\tbuy\tMm\t\tS-\t\tqAtDpD\t3\t8\t- -5\t"],
            )
        )


def test_f_price_suffix_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported order type suffix 'f'"):
        read_strategy_file(
            _write_strategy(
                tmp_path / "invalid.tsv",
                ["BAD_F\t0 60\t1\t4\t\tbuy\tLf\t\tS-\t\tqAtDpD\t3\t8\t- -5\t"],
            )
        )


def test_tail_limit_price_suffix_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="price suffix 'm'.*L tail order type"):
        read_strategy_file(
            _write_strategy(
                tmp_path / "invalid.tsv",
                ["BAD_TAIL\t0 60\t1\t4\t\tbuy\tL\t\tLm\t\tqAtDpD\t3\t8\t- -5\t"],
            )
        )


def test_post_only_market_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="! is only allowed"):
        read_strategy_file(
            _write_strategy(
                tmp_path / "invalid.tsv",
                ["BAD_POST\t0 60\t1\t4\t\tbuy\tM!\t\tS-\t\tqAtDpD\t3\t8\t- -5\t"],
            )
        )


def test_semio_open_price_bounds_are_fixed_large_intervals(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "bounds.tsv",
            [
                "DOWN\t0 60\t1\t4\t\tbuy\tL\t\tS-\t\tqAtDpD\t3\t8\t- -5\t",
                "UP\t0 60\t1\t4\t\tsell\tL\t\tS-\t\tqAtDpD\t4\t8\t5 +\t",
            ],
        )
    )

    assert strategy.pairs[0].head_price == (-1_000_000.0, -5.0)
    assert strategy.pairs[1].head_price == (5.0, 1_000_000.0)


def test_fractional_timeout_and_extra_interval_spaces_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "fractional.tsv",
            ["FAST\t0   10\t1\t.5\t\tbuy\tL\t\tS-\t\tqAtDpD\t3\t8\t-   +\t"],
        )
    )

    assert strategy.pairs[0].timeout == 0.5
    assert strategy.pairs[0].window.start_minutes == 0.0
    assert strategy.pairs[0].window.end_minutes == 10.0
    assert strategy.pairs[0].head_price == (-1_000_000.0, 1_000_000.0)


def test_optional_symbol_column_is_parsed(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "symbol.tsv",
            [
                "XBT\t0 60\t1\t4\t\tbuy\tL\t\tS-\t\tqAtDpD\t3\t8\t- -5\t\tPI_XBTUSD",
                "ETH\t0 60\t1\t4\t\tbuy\tL\t\tS-\t\tqAtDpD\t5\t8\t- -5\t\tPI_ETHUSD",
            ],
            symbol_column=True,
        )
    )

    assert [pair.symbol for pair in strategy.pairs] == ["PI_XBTUSD", "PI_ETHUSD"]


def test_optional_exchange_column_parses_futures_codes(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "exchange.tsv",
            [
                "KRK\t0 60\t1\t4\t\tbuy\tL\t\tS-\t\tqAtDpD\t3\t8\t- -5\t\tKRKF",
                "BIN\t0 60\t1\t4\t\tbuy\tL\t\tS-\t\tqAtDpD\t5\t8\t- -5\t\tBINF",
            ],
            exchange_column=True,
        )
    )

    assert [(pair.exchange, pair.market_type) for pair in strategy.pairs] == [
        ("kraken", "futures"),
        ("binance", "futures"),
    ]


def test_binance_spot_and_margin_exchange_codes_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "spot_margin.tsv",
            [
                "SPOT\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tBINS",
                "MARGIN\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tBINM",
                "ISO\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tBINI",
            ],
            exchange_column=True,
        )
    )

    assert [(pair.exchange, pair.market_type) for pair in strategy.pairs] == [
        ("binance", "spot"),
        ("binance", "margin"),
        ("binance", "isolated_margin"),
    ]


def test_kraken_spot_and_margin_exchange_codes_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "kraken_spot_margin.tsv",
            [
                "SPOT\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tKRKS",
                "MARGIN\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tKRKM",
            ],
            exchange_column=True,
        )
    )

    assert [(pair.exchange, pair.market_type) for pair in strategy.pairs] == [
        ("kraken", "spot"),
        ("kraken", "margin"),
    ]


def test_bitmex_spot_exchange_code_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "bitmex_spot.tsv",
            ["SPOT\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tBMXS"],
            exchange_column=True,
        )
    )

    assert (strategy.pairs[0].exchange, strategy.pairs[0].market_type) == (
        "bitmex",
        "spot",
    )


def test_bitmex_btx_exchange_codes_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "bitmex_btx.tsv",
            [
                "FUT_A\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tBTX",
                "FUT_B\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tBTXF",
                "SPOT\t0 60\t1\t4\t\tbuy\tL\t\tS\t\tqAtDpD\t3\t8\t- -5\t\tBTXS",
            ],
            exchange_column=True,
        )
    )

    assert [(pair.exchange, pair.market_type) for pair in strategy.pairs] == [
        ("bitmex", "futures"),
        ("bitmex", "futures"),
        ("bitmex", "spot"),
    ]


def test_demo_cross_exchange_chain_parses_route_symbols() -> None:
    strategy = read_strategy_file(Path("orders/demo_cross_exchange_chain.tsv"))

    assert strategy.name == "demo_cross_exchange_chain"
    assert [pair.name for pair in strategy.pairs] == [
        "XBTC_SEL",
        "KADA_SEL",
        "NADA_BUY",
    ]
    assert [
        (pair.exchange, pair.market_type, pair.symbol)
        for pair in strategy.pairs
    ] == [
        ("bitmex", "futures", "XBTUSD"),
        ("kraken", "futures", "PI_ADAUSD"),
        ("binance", "spot", "ADAUSDT"),
    ]
    assert strategy.pairs[1].hook_name == "XBTC_SEL-head-filled"
    assert strategy.pairs[2].hook_name == "KADA_SEL-tail-closed"
    assert strategy.pairs[2].tail.order_type == "L!"
    assert strategy.pairs[2].tail_price_spec == 0.0


def test_duplicate_pair_names_fail_at_tsv_parse_time(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Duplicate pair name"):
        read_strategy_file(
            _write_strategy(
                tmp_path / "duplicate.tsv",
                [
                    "SAME\t0 60\t1\t4\t\tbuy\tL\t\tS-\t\tqAtDpD\t3\t8\t- -5\t",
                    "SAME\t0 60\t1\t4\t\tsell\tL\t\tS-\t\tqAtDpD\t4\t8\t5 +\t",
                ],
            )
        )
