Feature: Streaming response
  Scenario: Streaming API is not implemented in the initial skeleton
    Given a valid streaming chat request
    When I submit the request to the streaming endpoint
    Then the response status code should be 501
    And the response JSON should contain error "not_implemented"
