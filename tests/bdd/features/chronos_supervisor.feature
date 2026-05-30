Feature: Chronos supervisor
  The Chronos supervisor owns strategy state, routes events, and forwards only typed runtime commands.

  Scenario: Event routing changes only pair B
    Given a strategy state with three pairs for Chronos
    When a private event for pair B is processed by Chronos
    Then only pair B state should change
    And pairs A and C should emit no commands

  Scenario: Duplicate private fill event is ignored
    Given a Chronos instance with a submitted pair B
    When the same private fill event is processed twice
    Then Chronos should emit commands only once for that logical event
    And Chronos should record DuplicateEventIgnored

  Scenario: Private terminal event wins over public trigger
    Given a public trigger and a private terminal event for the same pair
    When Chronos processes the same-cycle event batch
    Then the private terminal event should win
    And the public trigger should be recorded as ignored

  Scenario: REST ack without private confirmation times out
    Given a private event without enough identity for confirmation
    When the pending identity timeout expires
    Then Chronos should emit a typed pending identity timeout notice
    And no exchange command should be emitted

  Scenario: Tail chaining makes a dependent pair eligible
    Given a closed tail and a dependent latent pair
    When Chronos processes the upstream private closing event
    Then the dependent pair should remain latent but eligible
    And no exchange command should be emitted
