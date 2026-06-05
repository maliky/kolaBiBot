"""Pure helpers for legacy order-type suffixes.

The TSV grammar allows compact order codes such as ``Lm`` or ``Sm-``.  This
module keeps that parsing local and deterministic before exchange adapters see
ordinary base order types.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OrderRoleName = Literal["head", "tail"]

PRICE_SUFFIXES = frozenset({"i", "l", "m"})
SHORT_BASES = ("SL", "LT", "MT", "M", "L", "S")
TRIGGER_BASES = frozenset({"S", "SL", "MT", "LT"})
LIMIT_BASES = frozenset({"L", "SL", "LT"})
HEAD_PRICE_SUFFIX_BASES = frozenset({"L", "S", "SL", "MT", "LT"})
TAIL_PRICE_SUFFIX_BASES = TRIGGER_BASES

LONG_BASES: dict[str, tuple[str, str]] = {
    "limit": ("Limit", "L"),
    "market": ("Market", "M"),
    "stop": ("Stop", "S"),
    "stoploss": ("StopLoss", "S"),
    "stoplossmarket": ("StopLossMarket", "S"),
    "stoplimit": ("StopLimit", "SL"),
    "stoplosslimit": ("StopLossLimit", "SL"),
    "marketiftouched": ("MarketIfTouched", "MT"),
    "takeprofit": ("TakeProfit", "MT"),
    "takeprofitmarket": ("TakeProfitMarket", "MT"),
    "limitiftouched": ("LimitIfTouched", "LT"),
    "takeprofitlimit": ("TakeProfitLimit", "LT"),
}


@dataclass(frozen=True)
class OrderCode:
    raw: str
    base: str
    base_key: str
    price_suffix: str | None = None
    post_only: bool = False
    reduce_only: bool = False


def parse_order_code(raw: str) -> OrderCode:
    """Parse a legacy compact order code without validating its role."""
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("empty order type")

    long_base = LONG_BASES.get(cleaned.replace("_", "").replace("-", "").lower())
    if long_base is not None:
        base, base_key = long_base
        return OrderCode(raw=cleaned, base=base, base_key=base_key)

    base_candidate = next((candidate for candidate in SHORT_BASES if cleaned.startswith(candidate)), None)
    if base_candidate is None:
        raise ValueError(f"unsupported order type '{raw}'")
    base = base_candidate

    rest = cleaned[len(base) :]
    price_suffix: str | None = None
    post_only = False
    reduce_only = False
    for char in rest:
        if char in PRICE_SUFFIXES:
            if price_suffix is not None:
                raise ValueError(
                    f"order type '{raw}' has more than one price suffix"
                )
            price_suffix = char
            continue
        if char == "!":
            if post_only:
                raise ValueError(f"order type '{raw}' repeats !")
            post_only = True
            continue
        if char == "-":
            if reduce_only:
                raise ValueError(f"order type '{raw}' repeats -")
            reduce_only = True
            continue
        raise ValueError(f"unsupported order type suffix '{char}' in '{raw}'")

    return OrderCode(
        raw=cleaned,
        base=base,
        base_key=base,
        price_suffix=price_suffix,
        post_only=post_only,
        reduce_only=reduce_only,
    )


def validate_order_code(raw: str, *, role: OrderRoleName) -> OrderCode:
    """Return the parsed code or raise a deterministic grammar error."""
    code = parse_order_code(raw)
    if code.price_suffix is not None:
        allowed = HEAD_PRICE_SUFFIX_BASES if role == "head" else TAIL_PRICE_SUFFIX_BASES
        if code.base_key not in allowed:
            raise ValueError(
                f"price suffix '{code.price_suffix}' is not allowed for "
                f"{code.base} {role} order type"
            )
    if code.post_only and code.base_key not in LIMIT_BASES:
        raise ValueError(
            f"! is only allowed for limit-capable order types; got {code.base}"
        )
    if code.reduce_only and code.base_key == "M":
        raise ValueError("- is not allowed for market order type")
    return code


def base_order_type(raw: str) -> str:
    return parse_order_code(raw).base


def order_price_source(raw: str, *, default: str | None = None) -> str | None:
    """Map legacy price suffixes to runtime price sources."""
    suffix = parse_order_code(raw).price_suffix
    if suffix == "m":
        return "mark"
    if suffix == "i":
        return "index"
    if suffix == "l":
        return "last"
    return default


def order_exec_inst(raw: str, *, role: OrderRoleName) -> str | None:
    """Translate valid order-code flags into adapter execInst tokens."""
    code = validate_order_code(raw, role=role)
    flags: list[str] = []
    if code.post_only:
        flags.append("ParticipateDoNotInitiate")
    if code.reduce_only:
        flags.append("ReduceOnly")
    if code.base_key in TRIGGER_BASES:
        source = order_price_source(raw, default="last")
        if source == "mark":
            flags.append("MarkPrice")
        elif source == "index":
            flags.append("IndexPrice")
        elif source == "last":
            flags.append("LastPrice")
    return ",".join(flags) if flags else None
