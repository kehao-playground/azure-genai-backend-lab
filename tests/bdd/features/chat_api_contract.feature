Feature: Chat API contract
  Scenario: Valid chat request returns a reply with correlation id
    Given a valid chat request
    When I submit the request to the chat endpoint
    Then the response status code should be 200
    And the response JSON should contain a non-empty "message"
    And the response JSON should contain a "correlation_id"

  Scenario: Invalid chat request maps to the error envelope
    Given a chat request with an empty message
    When I submit the request to the chat endpoint
    Then the response status code should be 422
    And the response JSON should contain error "validation_error"

  Scenario: Upstream input rejection maps to the error envelope
    Given a valid chat request
    And the upstream model rejects the input
    When I submit the request to the chat endpoint
    Then the response status code should be 400
    And the response JSON should contain error "invalid_input"
    And the response JSON should contain a "correlation_id"

  Scenario: Truncated reply is reported as incomplete, not disguised as success
    Given a valid chat request
    And the upstream truncates the reply at the output token cap
    When I submit the request to the chat endpoint
    Then the response status code should be 200
    And the response JSON should report status "incomplete" with reason "max_output_tokens"
    And the response JSON should contain a non-empty "message"
