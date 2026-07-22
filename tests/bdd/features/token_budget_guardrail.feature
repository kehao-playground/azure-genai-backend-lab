Feature: Token budget guardrail
  Provider-reported token usage is surfaced on every turn that returns a
  usage-bearing terminal, and a per-conversation budget is enforced before
  inference: an exhausted conversation is rejected without spending
  anything further upstream.

  Scenario: A chat turn surfaces its provider-reported token usage
    Given a valid chat request
    When I submit the request to the chat endpoint
    Then the response status code should be 200
    And the response JSON should report token usage

  Scenario: A streaming turn reports usage on the terminal event
    Given a valid streaming chat request
    When I submit the request to the streaming endpoint
    Then the response status code should be 200
    And the message.done event should report token usage

  Scenario: An exhausted conversation is rejected with the error envelope
    Given a conversation token budget of 10 tokens
    And a conversation with one completed turn
    When I submit a follow-up message in the same conversation
    Then the response status code should be 429
    And the response JSON should contain error "token_budget_exceeded"
    And the response JSON should contain a non-empty "correlation_id"

  Scenario: An exhausted conversation is rejected before the stream starts
    Given a conversation token budget of 10 tokens
    And a conversation with one completed turn
    When I stream a follow-up message in the same conversation
    Then the response status code should be 429
    And the response JSON should contain error "token_budget_exceeded"

  Scenario: A new conversation is unaffected by an exhausted one
    Given a conversation token budget of 10 tokens
    And a conversation with one completed turn
    When I submit a follow-up message in the same conversation
    Then the response status code should be 429
    When I submit a new chat message "hello"
    Then the response status code should be 200
