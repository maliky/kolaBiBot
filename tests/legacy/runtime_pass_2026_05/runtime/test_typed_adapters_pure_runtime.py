"""Coverage for extracted pure runtime helpers and typed adapters.

Purpose: validate pure decision helpers and thin typed adapter surface added in
the migration pass.
Inputs: deterministic literals and minimal order payloads.
Outputs: assertions for pure logic outputs and adapter normalization behaviour.
Side effects: none.
Important types: `OrderDict`, pure runtime helper outputs.
Role: test module (pure logic and adapter coverage gate).
"""
from __future__ import annotations

from datetime import datetime, timezone

from kolabi.runtime.kola.pure_runtime import (
    build_order_payload,
    condition_truth_value,
    derive_hooked_order_update,
    normalize_amend_order_type,
)
from kolabi.runtime.kola.typed_adapters import OrderFuncTypedAdapter


def test_pure_condition_truth_value_price_and_time() -> None:
    now = datetime.now(timezone.utc)
    assert condition_truth_value(
        genre="lastPrice",
        op=">",
        value=100.0,
        current_price=101.0,
        current_time=now,
        hook_matched=None,
    )
    assert condition_truth_value(
        genre="temps",
        op="<",
        value=now,
        current_price=None,
        current_time=now,
        hook_matched=None,
    ) is False


def test_pure_hooked_order_update_preserves_price_delta() -> None:
    update = derive_hooked_order_update(
        side="buy",
        old_price=100.0,
        old_stop_px=99.0,
        condition_high_price=110.0,
        condition_low_price=105.0,
    )
    assert update.price == 105.0
    assert update.stop_px == 104.0


def test_pure_order_payload_builder_supports_limit() -> None:
    payload = build_order_payload(
        side="buy",
        quantity=2,
        op_type="lastPrice",
        ord_type="Limit",
        exec_inst="",
        prices=(100.0, 101.0),
        absdelta=0.5,
        text="hello",
    )
    assert payload["ordType"] == "Limit"
    assert payload["orderQty"] == 2
    assert "price" in payload


def test_adapter_surface_exists() -> None:
    normalized = OrderFuncTypedAdapter.normalize({"side": "buy", "quantity": 1})
    assert normalized["orderQty"] == 1


def test_normalize_amend_order_type() -> None:
    assert normalize_amend_order_type("Stop") == "amendStop"
    assert normalize_amend_order_type("amendStop") == "amendStop"
