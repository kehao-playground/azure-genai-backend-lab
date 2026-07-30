Feature: RAG no-answer policy
  Grounded answers cite retrieved sources; when retrieval finds nothing,
  the API says so deterministically instead of letting the model guess.

  Scenario: A question covered by the indexed corpus gets a grounded answer
    Given an indexed corpus that covers the question
    When I ask the RAG endpoint the question
    Then the response status code should be 200
    And the RAG status should be "answered"
    And the response should list at least one numbered source
    And the response JSON should contain a non-empty "answer"

  Scenario: A question with no matching documents gets a no-answer response
    Given an indexed corpus with no matching documents
    When I ask the RAG endpoint the question
    Then the response status code should be 200
    And the RAG status should be "no_answer"
    And the response should list no sources
    And the LLM should not have been called
