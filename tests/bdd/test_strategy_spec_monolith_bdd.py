from __future__ import annotations

import argparse
from pathlib import Path

from pytest_bdd import given, scenario, then, when

from kolabi.bot.__main__ import build_single_strategy
from kolabi.bot.domain import OrderMove, OrderState, StrategySpec
from kolabi.bot.tsv import order_pair_from_legacy_values, read_strategy_file


@scenario("features/strategy_spec_monolith.feature", "TSV row maps to canonical StrategySpec")
def test_tsv_row_maps_to_canonical_strategy_spec() -> None:
    pass


@scenario("features/strategy_spec_monolith.feature", "run-once arguments map to canonical StrategySpec")
def test_run_once_args_map_to_canonical_strategy_spec() -> None:
    pass


@scenario(
    "features/strategy_spec_monolith.feature",
    "TSV and run-once equivalent intent produce same canonical pair",
)
def test_tsv_and_run_once_equivalence() -> None:
    pass


@scenario("features/strategy_spec_monolith.feature", "Strict price interval validation")
def test_strict_price_interval_validation() -> None:
    pass


@scenario("features/strategy_spec_monolith.feature", "Lifecycle vocabulary contracts remain stable")
def test_lifecycle_vocabulary_contracts() -> None:
    pass


@given("a valid TSV strategy row payload", target_fixture="payload")
def given_valid_tsv_payload() -> Path:
    return Path("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv")


@given("a valid run-once argument payload", target_fixture="payload")
def given_valid_run_once_payload() -> argparse.Namespace:
    return argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        nbEssais=1,
        drPause=None,
        tOut=60,
        side="sell",
        prix=[1.0, 2.0],
        quantity=1,
        tailPrice=0.5,
        aType="qAt%pD",
        oType="L",
        oDelta=None,
        tDelta=None,
        tType="S-",
        Hook="",
    )


@given("equivalent TSV and run-once payloads", target_fixture="payloads")
def given_equivalent_payloads() -> tuple[Path, argparse.Namespace]:
    return (
        Path("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv"),
        argparse.Namespace(
            name="XSellTail",
            tps_run=[0.0, 1440.0],
            nbEssais=1,
            drPause=None,
            tOut=60,
            side="sell",
            prix=[1.0, 2.0],
            quantity=1,
            tailPrice=0.5,
            aType="qAt%pD",
            oType="L",
            oDelta=None,
            tDelta=None,
            tType="S-",
            Hook="",
        ),
    )


@given("an invalid price interval payload with equal bounds", target_fixture="payload")
def given_invalid_interval_payload() -> dict[str, object]:
    return {
        "name": "BadPair",
        "tps_run": (0.0, 60.0),
        "essais": 1,
        "dr_pause": None,
        "timeout": 60,
        "side": "sell",
        "prix": (1.0, 1.0),
        "q": 1,
        "tp": 0.5,
        "atype": "qAt%pD",
        "oType": "L",
        "oDelta": None,
        "tDelta": None,
        "tType": "S-",
        "hook": "",
    }


@given("the domain lifecycle enums", target_fixture="payload")
def given_domain_lifecycle_enums() -> None:
    return None


@when("the payload is normalized into the canonical strategy layer", target_fixture="result")
def when_payload_is_normalized(payload: object) -> object:
    if isinstance(payload, Path):
        return read_strategy_file(payload)
    if isinstance(payload, argparse.Namespace):
        return build_single_strategy(payload)
    try:
        return order_pair_from_legacy_values(**payload)
    except ValueError as exc:
        return exc


@when("both payloads are normalized into the canonical strategy layer", target_fixture="result")
def when_both_payloads_are_normalized(payloads: tuple[Path, argparse.Namespace]) -> tuple[StrategySpec, StrategySpec]:
    return read_strategy_file(payloads[0]), build_single_strategy(payloads[1])


@when("I read exported string values", target_fixture="result")
def when_i_read_exported_values(payload: None) -> tuple[list[str], list[str]]:
    del payload
    return [state.value for state in OrderState], [move.value for move in OrderMove]


@then("a typed StrategySpec should be produced")
def then_typed_strategy_spec_is_produced(result: object) -> None:
    assert isinstance(result, StrategySpec)
    assert len(result.pairs) == 1


@then("the normalized OrderPairSpec values should match")
def then_normalized_pair_values_should_match(result: tuple[StrategySpec, StrategySpec]) -> None:
    left, right = result
    assert len(left.pairs) == 1
    assert len(right.pairs) == 1
    assert left.pairs[0] == right.pairs[0]


@then("normalization should fail with a deterministic validation error")
def then_normalization_should_fail_with_validation_error(result: object) -> None:
    assert isinstance(result, Exception)


@then("they should match the agreed strategy vocabulary contracts")
def then_contract_values_match(result: tuple[list[str], list[str]]) -> None:
    order_states, order_moves = result
    assert order_states == [
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
    assert order_moves == [
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
