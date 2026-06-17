from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Iterable, TypedDict

import pandas as pd

from kolabi.bot.domain import (
    HeadSpec,
    OrderPairSpec,
    StrategySpec,
    TailSpec,
    TimeWindow,
    normalize_side,
    opposite_side,
)
from kolabi.bot.exchange_routes import parse_exchange_code
from kolabi.bot.order_codes import validate_order_code

NumberPair = tuple[float, float]
PERCENT_OPEN_BOUND = 90.0
DIFFERENTIAL_OPEN_BOUND = 1_000_000.0
ABSOLUTE_PRICE_OPEN_MAX = 1_000_000_000.0


class _LegacyRow(TypedDict):
    tps_run: NumberPair
    essais: int | str | None
    dr_pause: float | None
    timeout: float | None
    side: str
    prix: NumberPair
    q: int | None
    tp: float | None
    atype: str
    oType: str
    oDelta: float | None
    tDelta: float | None
    tUblk: float | None
    tUblk_type: str
    tType: str
    hook: str
    symbol: str | None
    exchange: str | None


def read_strategy_file(path: str | Path) -> StrategySpec:
    """Charge un fichier TSV vers une StrategySpec canonique."""
    strategy_path = Path(path)
    df = pd.read_csv(
        filepath_or_buffer=strategy_path,
        sep="\t",
        comment="#",
        skip_blank_lines=True,
    )
    df = _drop_empty_columns(df)
    if "name" not in df.columns:
        raise ValueError("Strategy TSV is missing required column 'name'.")
    if df["name"].duplicated().any():
        duplicates = tuple(str(name) for name in df.loc[df["name"].duplicated(), "name"])
        raise ValueError(f"Duplicate pair name(s) in strategy TSV: {', '.join(duplicates)}")
    df = df.set_index([df.index, df["name"]]).drop(columns="name")
    pairs: list[OrderPairSpec] = []
    for idx in df.index:
        pair_name = str(idx[1])
        row = normalize_legacy_row(df.loc[idx])
        pairs.append(order_pair_from_legacy_values(name=pair_name, **row))
    return StrategySpec(name=strategy_path.stem, pairs=tuple(pairs))


def order_pair_from_legacy_values(
    *,
    name: str,
    tps_run: NumberPair,
    essais: int | str | None,
    dr_pause: float | None,
    timeout: float | None,
    side: str,
    prix: NumberPair,
    q: int | None,
    tp: float | None,
    atype: str,
    oType: str,
    oDelta: float | None,
    tDelta: float | None,
    tType: str,
    hook: str,
    symbol: str | None = None,
    exchange: str | None = None,
    tUblk: float | None = None,
    tUblk_type: str = "uD",
) -> OrderPairSpec:
    """Normalise une ligne legacy vers une paire canonique."""
    head_quantity_type, tail_price_type, head_price_type = split_amount_type(atype)
    head_delta_type = extract_head_delta_type(atype)
    normalized_side = normalize_side(side)
    exchange_name, market_type = parse_exchange_code(exchange)
    window = TimeWindow(start_minutes=float(tps_run[0]), end_minutes=float(tps_run[1]))
    attempts = _validate_attempts(essais, name=name)
    timeout_minutes = _resolve_timeout_minutes(
        timeout=timeout,
        attempts=attempts,
        window=window,
        pause_minutes=dr_pause,
        name=name,
    )
    validate_order_code(oType, role="head")
    validate_order_code(tType, role="tail")
    validate_price_interval(prix)
    validate_quantity(q)

    return OrderPairSpec(
        name=name,
        window=window,
        try_num=attempts,
        dr_pause=dr_pause,
        timeout=timeout_minutes,
        head=HeadSpec(
            side=normalized_side,
            order_type=oType.strip(),
            delta=oDelta,
            delta_type=head_delta_type,
        ),
        head_price=prix,
        head_price_type=head_price_type,
        head_quantity=q,
        head_quantity_type=head_quantity_type,
        tail=TailSpec(
            side=opposite_side(normalized_side),
            order_type=tType.strip(),
            delta=tDelta,
        ),
        tail_price_spec=tp,
        tail_price_spec_type=tail_price_type,
        amount_type=atype.strip(),
        hook_name=hook.strip() or None,
        symbol=None if symbol is None or not symbol.strip() else symbol.strip(),
        exchange=exchange_name,
        market_type=market_type,
        tail_unblock_spec=tUblk,
        tail_unblock_spec_type=tUblk_type,
    )


def strategy_from_pairs(name: str, pairs: Iterable[OrderPairSpec]) -> StrategySpec:
    """Construit une StrategySpec a partir de paires deja canoniques."""
    return StrategySpec(name=name, pairs=tuple(pairs))


def strategy_from_run_once_args(args: object) -> StrategySpec:
    """Construit une StrategySpec canonique depuis les arguments CLI legacy."""
    tail_unblock = _parse_optional_tail_unblock(getattr(args, "tUblk", None))
    pair = order_pair_from_legacy_values(
        name=str(getattr(args, "name")),
        tps_run=(float(getattr(args, "tps_run")[0]), float(getattr(args, "tps_run")[1])),
        essais=int(getattr(args, "nbEssais")),
        dr_pause=getattr(args, "drPause"),
        timeout=getattr(args, "tOut"),
        side=str(getattr(args, "side")),
        prix=(float(getattr(args, "prix")[0]), float(getattr(args, "prix")[1])),
        q=getattr(args, "quantity"),
        tp=getattr(args, "tailPrice"),
        atype=str(getattr(args, "aType")),
        oType=str(getattr(args, "oType")),
        oDelta=getattr(args, "oDelta"),
        tDelta=getattr(args, "tDelta"),
        tUblk=tail_unblock[0],
        tUblk_type=tail_unblock[1],
        tType=str(getattr(args, "tType")),
        hook=str(getattr(args, "Hook")),
        symbol=None,
        exchange=None,
    )
    return StrategySpec(name=pair.name, pairs=(pair,))


def strategy_to_pretty_dict(strategy: StrategySpec) -> dict[str, object]:
    """Retourne une structure dry-run stable avec alias canoniques."""
    payload = asdict(strategy)
    pairs = payload.get("pairs", [])
    if isinstance(pairs, (list, tuple)):
        normalized_pairs = []
        for pair in pairs:
            if not isinstance(pair, dict):
                normalized_pairs.append(pair)
                continue
            if "head_price" in pair:
                pair["head_price_spec"] = pair["head_price"]
            if "head_price_type" in pair:
                pair["head_price_spec_type"] = pair["head_price_type"]
            if "head_quantity" in pair:
                pair["head_quantity_spec"] = pair["head_quantity"]
            if "head_quantity_type" in pair:
                pair["head_quantity_spec_type"] = pair["head_quantity_type"]
            normalized_pairs.append(pair)
        payload["pairs"] = normalized_pairs
    return payload


def normalize_legacy_row(row: pd.Series) -> _LegacyRow:
    """Convertit une ligne TSV legacy en champs intermediaires stables."""
    atype = _optional_text(_row_value(row, "atype"))
    if atype:
        return _normalize_compact_atype_row(row, atype)
    return _normalize_typed_field_row(row)


def _normalize_compact_atype_row(row: pd.Series, atype: str) -> _LegacyRow:
    """Normalise the old compact-atype row grammar."""
    tail_unblock = _parse_optional_tail_unblock(_row_value(row, "tUblk"))
    return {
        "tps_run": _parse_legacy_interval(str(row.tps_run)),
        "essais": _parse_essais(_row_value(row, "essais")),
        "dr_pause": _coerce_to("float", _row_value(row, "pause")),
        "timeout": _coerce_to("float", _row_value(row, "tOut")),
        "side": str(row.side).strip(),
        "prix": _parse_legacy_interval(str(row.prix), atype),
        "q": _coerce_int(_row_value(row, "qty", "quantity", "q")),
        "tp": _coerce_to("float", _row_value(row, "tp")),
        "atype": atype,
        "oType": str(row.oType).strip(),
        "oDelta": _coerce_to("float", _row_value(row, "oDelta")),
        "tDelta": _coerce_to("float", _row_value(row, "tDelta")),
        "tUblk": tail_unblock[0],
        "tUblk_type": tail_unblock[1],
        "tType": str(row.tType).strip(),
        "hook": _optional_text(_row_value(row, "hook")) or "",
        "symbol": _optional_text(_row_value(row, "symbol")),
        "exchange": _optional_text(_row_value(row, "exchg", "exchange")),
    }


def _normalize_typed_field_row(row: pd.Series) -> _LegacyRow:
    """Normalise the explicit typed-field TSV grammar."""
    raw_q, quantity_type = _parse_required_typed_number(
        _row_value(row, "qty", "quantity", "q"),
        field="qty",
        token_prefix="q",
        allowed_suffixes={"A", "%"},
        as_int=True,
    )
    if not isinstance(raw_q, int):
        raise ValueError("Invalid qty value; expected an integer payload.")
    tp, tail_price_type = _parse_optional_typed_number(
        _row_value(row, "tp"),
        field="tp",
        token_prefix="t",
        allowed_suffixes={"A", "D", "%"},
        default_type="tD",
    )
    prix, head_price_type = _parse_optional_typed_interval(
        _row_value(row, "prix"),
        field="prix",
        token_prefix="p",
        allowed_suffixes={"A", "D", "%"},
        default_type="pD",
    )
    o_delta, head_delta_type = _parse_optional_typed_number(
        _row_value(row, "oDelta"),
        field="oDelta",
        token_prefix="o",
        allowed_suffixes={"D", "%"},
        default_type="oD",
    )
    t_delta = _parse_optional_tail_delta(_row_value(row, "tDelta"))
    tail_unblock = _parse_optional_tail_unblock(_row_value(row, "tUblk"))
    atype = f"{quantity_type}{tail_price_type}{head_price_type}{head_delta_type}"

    return {
        "tps_run": _parse_legacy_interval(str(row.tps_run)),
        "essais": _parse_essais(_row_value(row, "essais")),
        "dr_pause": _coerce_to("float", _row_value(row, "pause")),
        "timeout": _coerce_to("float", _row_value(row, "tOut")),
        "side": str(row.side).strip(),
        "prix": prix,
        "q": raw_q,
        "tp": tp,
        "atype": atype,
        "oType": str(row.oType).strip(),
        "oDelta": o_delta,
        "tDelta": t_delta,
        "tUblk": tail_unblock[0],
        "tUblk_type": tail_unblock[1],
        "tType": str(row.tType).strip(),
        "hook": _optional_text(_row_value(row, "hook")) or "",
        "symbol": _optional_text(_row_value(row, "symbol")),
        "exchange": _optional_text(_row_value(row, "exchg", "exchange")),
    }


def _drop_empty_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Drop blank TSV columns and normalise header whitespace."""
    rename: dict[object, str] = {}
    drop: list[object] = []
    for column in df.columns:
        name = str(column).strip()
        if not name or name.startswith("Unnamed:"):
            drop.append(column)
            continue
        rename[column] = name
    if drop:
        df = df.drop(columns=drop)
    if rename:
        df = df.rename(columns=rename)
    return df


def _row_value(row: pd.Series, *names: str) -> object:
    for name in names:
        if name in row:
            return row[name]
    return None


def _optional_text(value: object) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _coerce_to(kind: str, value: object) -> int | float | None:
    text = _optional_text(value)
    if text is None:
        return None
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    raise ValueError(f"Unsupported coercion kind '{kind}'")


def _coerce_int(value: object) -> int | None:
    text = _optional_text(value)
    if text is None:
        return None
    return int(text)


def _parse_essais(value: object) -> int | str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if text == "*":
        return text
    return int(text)


def _validate_attempts(essais: int | str | None, *, name: str) -> int | None:
    if essais is None:
        return 1
    if essais == "*":
        return None
    attempts = int(essais)
    if attempts < 1:
        raise ValueError(f"Invalid essais for pair '{name}'; expected a positive integer or '*'.")
    return attempts


def _resolve_timeout_minutes(
    *,
    timeout: float | None,
    attempts: int | None,
    window: TimeWindow,
    pause_minutes: float | None,
    name: str,
) -> float | None:
    if timeout is not None:
        if timeout <= 0:
            raise ValueError(f"Invalid tOut for pair '{name}'; expected a positive number.")
        return timeout
    if attempts is None:
        raise ValueError(
            f"Missing tOut for pair '{name}'; essais='*' requires an explicit per-attempt tOut."
        )
    span_minutes = window.end_minutes - window.start_minutes
    pause_total = (pause_minutes or 0.0) * attempts
    computed = (span_minutes - pause_total) / attempts
    if computed <= 0:
        raise ValueError(
            f"Invalid automatic tOut for pair '{name}'; tps_run span minus pauses must be positive."
        )
    return computed


def _parse_legacy_interval(raw: str, atype: str | None = None) -> NumberPair:
    parts = raw.strip().split()
    if len(parts) != 2:
        raise ValueError(
            f"Invalid interval '{raw}'; expected exactly two whitespace-separated bounds."
        )
    low, high = parts
    amount_type = atype or ""
    if "p%" in amount_type:
        low = str(-PERCENT_OPEN_BOUND) if low == "-" else low
        high = str(PERCENT_OPEN_BOUND) if high == "+" else high
    if "pD" in amount_type:
        low = str(-DIFFERENTIAL_OPEN_BOUND) if low == "-" else low
        high = str(DIFFERENTIAL_OPEN_BOUND) if high == "+" else high
    if "pA" in amount_type:
        low = "0" if low == "-" else low
        high = str(ABSOLUTE_PRICE_OPEN_MAX) if high == "+" else high
    return float(low), float(high)


def _parse_required_typed_number(
    value: object,
    *,
    field: str,
    token_prefix: str,
    allowed_suffixes: set[str],
    as_int: bool = False,
) -> tuple[int | float, str]:
    parsed, amount_type = _parse_optional_typed_number(
        value,
        field=field,
        token_prefix=token_prefix,
        allowed_suffixes=allowed_suffixes,
        default_type=None,
        as_int=as_int,
    )
    if parsed is None:
        raise ValueError(f"Missing required TSV field '{field}'.")
    return parsed, amount_type


def _parse_optional_typed_number(
    value: object,
    *,
    field: str,
    token_prefix: str,
    allowed_suffixes: set[str],
    default_type: str | None,
    as_int: bool = False,
) -> tuple[int | float | None, str]:
    text = _optional_text(value)
    if text is None:
        if default_type is None:
            raise ValueError(f"Missing required TSV field '{field}'.")
        return None, default_type
    suffix, payload = _split_typed_payload(
        text,
        field=field,
        allowed_suffixes=allowed_suffixes,
    )
    if not payload:
        raise ValueError(f"Invalid {field} value '{text}'; missing numeric payload.")
    number = float(payload)
    if as_int and not number.is_integer():
        raise ValueError(f"Invalid {field} value '{text}'; expected an integer payload.")
    return (int(number) if as_int else number), f"{token_prefix}{suffix}"


def _parse_optional_typed_interval(
    value: object,
    *,
    field: str,
    token_prefix: str,
    allowed_suffixes: set[str],
    default_type: str,
) -> tuple[NumberPair, str]:
    text = _optional_text(value)
    if text is None:
        return _open_interval_for_suffix(default_type[-1]), default_type
    suffix, payload = _split_typed_payload(
        text,
        field=field,
        allowed_suffixes=allowed_suffixes,
    )
    parts = payload.split()
    if len(parts) != 2:
        raise ValueError(
            f"Invalid {field} interval '{text}'; expected typed bounds like 'D- +'."
        )
    low, high = parts
    low_value, high_value = _bounds_for_suffix(suffix, low, high)
    return (float(low_value), float(high_value)), f"{token_prefix}{suffix}"


def _parse_optional_tail_delta(value: object) -> float | None:
    text = _optional_text(value)
    if text is None:
        return None
    suffix, payload = _split_typed_payload(
        text,
        field="tDelta",
        allowed_suffixes={"D"},
    )
    if suffix != "D":
        raise ValueError("Invalid tDelta type; only differential 'D' is supported.")
    if not payload:
        raise ValueError(f"Invalid tDelta value '{text}'; missing numeric payload.")
    return float(payload)


def _parse_optional_tail_unblock(value: object) -> tuple[float | None, str]:
    parsed, amount_type = _parse_optional_typed_number(
        value,
        field="tUblk",
        token_prefix="u",
        allowed_suffixes={"D", "%"},
        default_type="uD",
    )
    if parsed is not None and parsed < 0:
        raise ValueError("Invalid tUblk value; expected a non-negative distance.")
    return parsed, amount_type


def _split_typed_payload(
    text: str,
    *,
    field: str,
    allowed_suffixes: set[str],
) -> tuple[str, str]:
    suffix = text[0]
    if suffix not in allowed_suffixes:
        allowed = " or ".join(sorted(allowed_suffixes))
        raise ValueError(f"{field} value '{text}' must start with {allowed}.")
    return suffix, text[1:].strip()


def _open_interval_for_suffix(suffix: str) -> NumberPair:
    low, high = _bounds_for_suffix(suffix, "-", "+")
    return float(low), float(high)


def _bounds_for_suffix(suffix: str, low: str, high: str) -> tuple[str, str]:
    if suffix == "%":
        return (
            str(-PERCENT_OPEN_BOUND) if low == "-" else low,
            str(PERCENT_OPEN_BOUND) if high == "+" else high,
        )
    if suffix == "D":
        return (
            str(-DIFFERENTIAL_OPEN_BOUND) if low == "-" else low,
            str(DIFFERENTIAL_OPEN_BOUND) if high == "+" else high,
        )
    if suffix == "A":
        return (
            "0" if low == "-" else low,
            str(ABSOLUTE_PRICE_OPEN_MAX) if high == "+" else high,
        )
    raise ValueError(f"Unsupported interval type '{suffix}'.")


def split_amount_type(atype: str) -> tuple[str, str, str]:
    """Extrait les trois codes canoniques depuis la chaine legacy."""
    compact = atype.strip()
    quantity_type = _extract_typed_token(compact, "q")
    tail_type = _extract_typed_token(compact, "t")
    price_type = _extract_typed_token(compact, "p")
    return quantity_type, tail_type, price_type


def extract_head_delta_type(atype: str) -> str:
    """Extract optional head offset semantics from the legacy compact type."""
    return _extract_optional_typed_token(atype.strip(), "o", default="oD")


def _extract_typed_token(raw: str, prefix: str) -> str:
    """Trouve le token d'un prefixe legacy dans atype."""
    start = raw.find(prefix)
    if start < 0:
        raise ValueError(f"Missing {prefix} token in amount type '{raw}'.")
    if start + 1 >= len(raw):
        raise ValueError(f"Incomplete {prefix} token in amount type '{raw}'.")
    suffix = raw[start + 1]
    if suffix in {"A", "D", "%"}:
        return f"{prefix}{suffix}"
    raise ValueError(f"Invalid {prefix} token in amount type '{raw}'.")


def _extract_optional_typed_token(raw: str, prefix: str, *, default: str) -> str:
    start = raw.find(prefix)
    if start < 0:
        return default
    if start + 1 >= len(raw):
        raise ValueError(f"Incomplete {prefix} token in amount type '{raw}'.")
    suffix = raw[start + 1]
    if prefix == "o" and suffix in {"D", "%"}:
        return f"{prefix}{suffix}"
    raise ValueError(f"Invalid {prefix} token in amount type '{raw}'.")


def validate_price_interval(prix: NumberPair) -> None:
    """Verifie la stricte croissance de l'intervalle de prix."""
    low, high = prix
    if low >= high:
        raise ValueError(
            f"Invalid canonical price interval: low={low} high={high}; expected low < high."
        )


def validate_quantity(quantity: int | None) -> None:
    """Verifie qu'une quantite strategique est positive quand elle existe."""
    if quantity is not None and quantity <= 0:
        raise ValueError(
            f"Invalid canonical quantity: quantity={quantity}; expected a positive value."
        )
