from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Iterable

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
from kolabi.bot.order_codes import validate_order_code

NumberPair = tuple[float, float]
PERCENT_OPEN_BOUND = 90.0
DIFFERENTIAL_OPEN_BOUND = 1_000_000.0
ABSOLUTE_PRICE_OPEN_MAX = 1_000_000_000.0


def read_strategy_file(path: str | Path) -> StrategySpec:
    """Charge un fichier TSV vers une StrategySpec canonique."""
    strategy_path = Path(path)
    df = pd.read_csv(
        filepath_or_buffer=strategy_path,
        sep="\t",
        comment="#",
        skip_blank_lines=True,
    )
    df = df.set_index([df.index, df.name]).drop(columns="name")
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
    essais: int | None,
    dr_pause: float | None,
    timeout: int | None,
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
) -> OrderPairSpec:
    """Normalise une ligne legacy vers une paire canonique."""
    head_quantity_type, tail_price_type, head_price_type = split_amount_type(atype)
    head_delta_type = extract_head_delta_type(atype)
    normalized_side = normalize_side(side)
    validate_order_code(oType, role="head")
    validate_order_code(tType, role="tail")
    validate_price_interval(prix)
    validate_quantity(q)

    return OrderPairSpec(
        name=name,
        window=TimeWindow(start_minutes=float(tps_run[0]), end_minutes=float(tps_run[1])),
        try_num=1 if essais is None else essais,
        dr_pause=dr_pause,
        timeout=timeout,
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
    )


def strategy_from_pairs(name: str, pairs: Iterable[OrderPairSpec]) -> StrategySpec:
    """Construit une StrategySpec a partir de paires deja canoniques."""
    return StrategySpec(name=name, pairs=tuple(pairs))


def strategy_from_run_once_args(args: object) -> StrategySpec:
    """Construit une StrategySpec canonique depuis les arguments CLI legacy."""
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
        tType=str(getattr(args, "tType")),
        hook=str(getattr(args, "Hook")),
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


def normalize_legacy_row(row: pd.Series) -> dict[str, object]:
    """Convertit une ligne TSV legacy en champs intermediaires stables."""

    def handle_tuple(raw: str, atype: str | None = None) -> NumberPair:
        el1, el2 = raw.strip().split(" ")
        amount_type = atype or ""
        if "p%" in amount_type:
            el1 = str(-PERCENT_OPEN_BOUND) if el1 == "-" else el1
            el2 = str(PERCENT_OPEN_BOUND) if el2 == "+" else el2
        if "pD" in amount_type:
            el1 = str(-DIFFERENTIAL_OPEN_BOUND) if el1 == "-" else el1
            el2 = str(DIFFERENTIAL_OPEN_BOUND) if el2 == "+" else el2
        if "pA" in amount_type:
            el1 = "0" if el1 == "-" else el1
            el2 = str(ABSOLUTE_PRICE_OPEN_MAX) if el2 == "+" else el2
        return float(el1), float(el2)

    def coerce_to(kind: str, value: object) -> int | float | None:
        if pd.isna(value):
            return None
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        raise ValueError(f"Unsupported coercion kind '{kind}'")

    atype = str(row.atype).strip()
    return {
        "tps_run": handle_tuple(str(row.tps_run)),
        "essais": coerce_to("int", row.essais),
        "dr_pause": coerce_to("float", row.pause),
        "timeout": coerce_to("int", row.tOut),
        "side": str(row.side).strip(),
        "prix": handle_tuple(str(row.prix), atype),
        "q": coerce_to("int", row["quantity"] if "quantity" in row else row.q),
        "tp": coerce_to("float", row.tp),
        "atype": atype,
        "oType": str(row.oType).strip(),
        "oDelta": coerce_to("float", row.oDelta),
        "tDelta": coerce_to("float", row.tDelta),
        "tType": str(row.tType).strip(),
        "hook": "" if pd.isna(row.hook) else str(row.hook).strip(),
    }


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
