Feature: Token budget guardrail
  Scenario: Token budget policy is documented but not implemented in the initial skeleton
    Given the token budget policy is not implemented
    Then the token budget scenario should be marked pending
