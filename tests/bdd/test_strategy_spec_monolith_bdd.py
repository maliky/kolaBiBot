from __future__ import annotations

import pytest
from pytest_bdd import given, scenario, then, when


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
def given_valid_tsv_payload() -> dict[str, object]:
    return {"source": "tsv"}


@given("a valid run-once argument payload", target_fixture="payload")
def given_valid_run_once_payload() -> dict[str, object]:
    return {"source": "run-once"}


@given("equivalent TSV and run-once payloads", target_fixture="payloads")
def given_equivalent_payloads() -> tuple[dict[str, object], dict[str, object]]:
    return {"source": "tsv"}, {"source": "run-once"}


@given("an invalid price interval payload with equal bounds", target_fixture="payload")
def given_invalid_interval_payload() -> dict[str, object]:
    return {"source": "invalid-interval"}


@given("the domain lifecycle enums")
def given_domain_lifecycle_enums() -> None:
    return None


@when("the payload is normalized into the canonical strategy layer")
def when_payload_is_normalized(payload: dict[str, object], request: pytest.FixtureRequest) -> None:
    if payload["source"] in {"tsv", "run-once"}:
        request.node._normalized = None  # type: ignore[attr-defined]
        return
    request.node._normalization_error = "not implemented"  # type: ignore[attr-defined]


@when("both payloads are normalized into the canonical strategy layer")
def when_both_payloads_are_normalized(
    payloads: tuple[dict[str, object], dict[str, object]], request: pytest.FixtureRequest
) -> None:
    del payloads
    request.node._normalized_pair = None  # type: ignore[attr-defined]


@when("I read exported string values")
def when_i_read_exported_values(request: pytest.FixtureRequest) -> None:
    request.node._enum_values = None  # type: ignore[attr-defined]


@then("a typed StrategySpec should be produced")
def then_typed_strategy_spec_is_produced(request: pytest.FixtureRequest) -> None:
    assert getattr(request.node, "_normalized", None) is not None


@then("the normalized OrderPairSpec values should match")
def then_normalized_pair_values_should_match(request: pytest.FixtureRequest) -> None:
    assert getattr(request.node, "_normalized_pair", None) is not None


@then("normalization should fail with a deterministic validation error")
def then_normalization_should_fail_with_validation_error(request: pytest.FixtureRequest) -> None:
    assert getattr(request.node, "_normalization_error", None) is not None


@then("they should match the agreed strategy vocabulary contracts")
def then_contract_values_match(request: pytest.FixtureRequest) -> None:
    assert getattr(request.node, "_enum_values", None) is not None

