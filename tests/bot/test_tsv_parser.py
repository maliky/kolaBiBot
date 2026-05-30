from __future__ import annotations

from pathlib import Path

import pytest
from kolabi.bot.tsv.parser import read_strategy_file


def _write_strategy(path: Path, rows: list[str]) -> Path:
    path.write_text(
        "\n".join(
            [
                "name\ttps_run\tessais\ttOut\tpause\tside\toType\toDelta\ttType\ttDelta\tatype\tquantity\ttp\tprix\thook",
                *rows,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_head_limit_price_suffix_is_valid(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "valid.tsv",
            ["BUY_LM\t0 60\t1\t4\t\tbuy\tLm\t\tS-\t\tqAtDpD\t3\t8\t- -5\t"],
        )
    )

    assert strategy.pairs[0].head.order_type == "Lm"
    assert strategy.pairs[0].head_price == (-1_000_000.0, -5.0)


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
