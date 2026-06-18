Feature: Monolithic strategy specification layer
  As an operator
  I want one typed strategy layer
  So that Org strategy tables and run-once are normalized once before runtime

  Scenario: Org strategy table row maps to canonical StrategySpec
    Given a valid Org strategy table row payload
    When the payload is normalized into the canonical strategy layer
    Then a typed StrategySpec should be produced

  Scenario: run-once arguments map to canonical StrategySpec
    Given a valid run-once argument payload
    When the payload is normalized into the canonical strategy layer
    Then a typed StrategySpec should be produced

  Scenario: Org strategy table and run-once equivalent intent produce same canonical pair
    Given equivalent Org strategy table and run-once payloads
    When both payloads are normalized into the canonical strategy layer
    Then the normalized OrderPairSpec values should match

  Scenario: Strict price interval validation
    Given an invalid price interval payload with equal bounds
    When the payload is normalized into the canonical strategy layer
    Then normalization should fail with a deterministic validation error

  Scenario: Lifecycle vocabulary contracts remain stable
    Given the domain lifecycle enums
    When I read exported string values
    Then they should match the agreed strategy vocabulary contracts
