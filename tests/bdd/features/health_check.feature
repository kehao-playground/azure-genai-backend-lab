Feature: Health check
  Scenario: Service is healthy
    Given the API service is running
    When I request the health endpoint
    Then the response status code should be 200
    And the response JSON should contain status "ok"
