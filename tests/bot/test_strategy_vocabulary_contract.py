from __future__ import annotations

import importlib

from kolabi.bot import domain


def test_head_state_string_contract() -> None:
    assert [state.value for state in domain.HeadState] == [
        "latent",
        "hooked",
        "submitted",
        "unadmitted",
        "admitted",
        "confirmed",
        "new",
        "living",
        "closed",
        "failed",
    ]


def test_tail_state_string_contract() -> None:
    assert [state.value for state in domain.TailState] == [
        "latent",
        "hooked",
        "submitted",
        "unadmitted",
        "admitted",
        "confirmed",
        "new",
        "living",
        "closed",
        "failed",
    ]


def test_transition_event_contract() -> None:
    transition_values = [
        "latent_to_hooked",
        "hooked_to_submitted",
        "submitted_to_unadmitted",
        "submitted_to_admitted",
        "admitted_to_confirmed",
        "confirmed_to_new",
        "confirmed_to_living",
        "confirmed_to_closed",
        "confirmed_to_failed",
        "new_to_failed",
        "new_to_living",
        "new_to_new",
        "new_to_closed",
        "living_to_failed",
        "living_to_living",
        "living_to_close",
    ]
    if hasattr(domain, "OrderMove"):
        enum_values = [member.value for member in domain.OrderMove]
    elif hasattr(domain, "EggMoveKind"):
        enum_values = [member.value for member in domain.EggMoveKind]
    else:
        raise AssertionError("No movement/transition enum found in kolabi.bot.domain")
    assert enum_values == transition_values


def test_order_role_exists_for_head_tail_distinction() -> None:
    assert hasattr(domain, "OrderRole"), "OrderRole must represent head/tail distinction."
    role_values = [role.value for role in domain.OrderRole]
    assert role_values == ["head", "tail"]


def test_common_order_state_is_present() -> None:
    assert hasattr(
        domain, "OrderState"
    ), "OrderState should factor common lifecycle values for head and tail."
    values = [state.value for state in domain.OrderState]
    assert values == [
        "latent",
        "hooked",
        "submitted",
        "unadmitted",
        "admitted",
        "confirmed",
        "new",
        "living",
        "closed",
        "failed",
    ]


def test_orderspec_deleted_from_tsv_public_api() -> None:
    tsv = importlib.import_module("kolabi.bot.tsv")
    assert not hasattr(tsv, "OrderSpec"), "OrderSpec should be removed from tsv public API."

