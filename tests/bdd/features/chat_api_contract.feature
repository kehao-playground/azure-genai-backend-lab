Feature: Chat API contract
  Scenario: Valid chat request returns a reply with correlation id
    Given a valid chat request
    When I submit the request to the chat endpoint
    Then the response status code should be 200
    And the response JSON should contain a non-empty "message"
    And the response JSON should contain a "correlation_id"
