from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import pandas as pd

TupleFloat = Tuple[float, float]


@dataclass(frozen=True)
class OrderSpec:
    """Canonical representation of one TSV-defined order pair."""

    name: str
    tps_run: TupleFloat
    essais: int | None
    dr_pause: float | None
    timeout: int | None
    side: str
    prix: TupleFloat
    q: int | None
    tp: float | None
    atype: str
    oType: str
    oDelta: float | None
    tDelta: float | None
    tType: str
    hook: str


def read_strategy_file(path: str | Path) -> List[OrderSpec]:
    """Load a TSV strategy file (demo_ada.tsv, etc.) into OrderSpec rows."""
    df = pd.read_csv(
        filepath_or_buffer=path,
        sep="\t",
        comment="#",
        skip_blank_lines=True,
    )
    df = df.set_index([df.index, df.name]).drop(columns="name")
    specs: List[OrderSpec] = []
    for idx in df.index:
        name = idx[1]
        row = coerce_types(df.loc[idx])
        specs.append(OrderSpec(name=name, **row))
    return specs


def coerce_types(row: pd.Series) -> dict[str, object]:
    """Convert dataframe row to the structure Bot runtime expects."""

    def handle_tuple(raw: str, atype: str | None = None) -> TupleFloat:
        el1, el2 = raw.strip().split(" ")
        a = atype or ""
        if "p%" in a:
            el1 = "-90" if el1 == "-" else el1
            el2 = "90" if el2 == "+" else el2
        if "pD" in a:
            el1 = str(float(el2) * 10) if el1 == "-" else el1
            el2 = str(float(el1) * 10) if el2 == "+" else el2
        if "pA" in a:
            if el1 == "-":
                el1 = str(int(float(el2) / 10))
            if el2 == "+":
                el2 = str(int(float(el1) * 10))
        return float(el1), float(el2)

    def coerce_to(kind: str, value: object) -> int | float | None:
        if pd.isna(value):
            return None
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        raise ValueError(f"Unsupported coercion kind '{kind}'")

    atype = row.atype.strip()
    return {
        "tps_run": handle_tuple(str(row.tps_run)),
        "essais": coerce_to("int", row.essais),
        "dr_pause": coerce_to("float", row.pause),
        "timeout": coerce_to("int", row.tOut),
        "side": row.side.strip(),
        "prix": handle_tuple(str(row.prix), atype),
        "q": coerce_to("int", row["quantity"] if "quantity" in row else row.q),
        "tp": coerce_to("float", row.tp),
        "atype": atype,
        "oType": row.oType.strip(),
        "oDelta": coerce_to("float", row.oDelta),
        "tDelta": coerce_to("float", row.tDelta),
        "tType": row.tType.strip(),
        "hook": "" if pd.isna(row.hook) else row.hook.strip(),
    }
