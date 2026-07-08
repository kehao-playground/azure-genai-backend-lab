Feature: Chat API contract
  Scenario: Chat API is not implemented in the initial skeleton
    Given a valid chat request
    When I submit the request to the chat endpoint
    Then the response status code should be 501
    And the response JSON should contain error "not_implemented"
