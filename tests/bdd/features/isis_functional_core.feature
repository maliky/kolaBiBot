Feature: Isis functional core
  Isis owns strategy-state replacement and ordered intent emission for already-targeted events.

  Scenario: Routed event changes only one pair
    Given a strategy state with three pairs for Isis
    When Isis processes a targeted event for pair B
    Then only pair B state should change in Isis
    And Isis should emit one ordered head intent

  Scenario: Unresolved event is a deterministic no-op
    Given a strategy state with three pairs for Isis
    When Isis processes an event without a target pair
    Then Isis should return a fresh unchanged strategy state
    And Isis should emit no intents

  Scenario: Private routed event updates private metadata only
    Given a strategy state with three pairs for Isis
    When Isis processes a targeted private event for pair B
    Then Isis should update private metadata for pair B
    And Isis should not update private metadata for other pairs

  Scenario: Isis preserves reducer intent order
    Given a strategy state with three pairs for Isis
    When Isis receives ordered intents from the pair reducer
    Then Isis should preserve the reducer intent order
