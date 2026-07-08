Feature: RAG no-answer policy
  Scenario: RAG API is not implemented in the initial skeleton
    Given a valid RAG request
    When I submit the request to the RAG endpoint
    Then the response status code should be 501
    And the response JSON should contain error "not_implemented"
