Feature: Agent endpoint
  An agent turn is a conversation turn: it sees prior history, its outcome
  commits atomically with its usage, and limit stops surface as structured
  incomplete reasons — never as framework fallback prose.

  Scenario: An agent turn opens a new conversation with an execution trace
    When I submit an agent task "check the runtime configuration"
    Then the response status code should be 200
    And the response JSON should contain a non-empty "conversation_id"
    And the response JSON should contain a "tool_calls"

  Scenario: An agent turn sees earlier chat turns in the same conversation
    Given a conversation with one completed turn
    When I submit an agent task in the same conversation
    Then the response status code should be 200
    And the agent answer should include the marker "history=2"

  Scenario: An unknown conversation id maps to the error envelope
    When I submit an agent task in an unknown conversation
    Then the response status code should be 404
    And the response JSON should contain error "conversation_not_found"

  Scenario: A different group set cannot continue the conversation
    Given a conversation with one completed turn
    When I submit an agent task in the same conversation as group "other"
    Then the response status code should be 404
    And the response JSON should contain error "conversation_not_found"

  Scenario: An exhausted budget rejects the agent turn before it runs
    Given a conversation whose token budget is exhausted
    When I submit an agent task in the same conversation
    Then the response status code should be 429
    And the response JSON should contain error "token_budget_exceeded"

  Scenario: A tool-call limit surfaces as a structured incomplete reason
    Given the agent service stops at the tool-call limit
    When I submit an agent task "loop forever"
    Then the response status code should be 200
    And the response JSON field "status" should be "incomplete"
    And the response JSON field "incomplete_reason" should be "tool_call_limit"
    And the response JSON field "answer" should be empty
