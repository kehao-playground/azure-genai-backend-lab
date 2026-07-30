Feature: RAG no-answer policy
  Retrieved sources flow into generation with citations that map to them;
  when retrieval returns zero hits, the API answers no_answer deterministically
  without calling the LLM, instead of letting the model guess.

  Scenario: Retrieved sources flow into generation and citations map to returned sources
    Given an indexed corpus that covers the question
    When I ask the RAG endpoint the question
    Then the response status code should be 200
    And the RAG status should be "answered"
    And the response should list at least one numbered source
    And the response JSON should contain a non-empty "answer"
    And every citation number in the answer should reference a returned source

  Scenario: Zero retrieval hits produce a no-answer response without calling the LLM
    Given retrieval that returns zero hits
    When I ask the RAG endpoint the question
    Then the response status code should be 200
    And the RAG status should be "no_answer"
    And the response should list no sources
    And the LLM should not have been called

  Scenario: A whitespace-only question is rejected before retrieval runs
    Given an indexed corpus that covers the question
    When I ask the RAG endpoint a whitespace-only question
    Then the response status code should be 422
    And the response JSON should contain error "validation_error"
