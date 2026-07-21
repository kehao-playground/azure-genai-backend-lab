Feature: Conversation state
  Conversation history is owned by this application (store=false upstream).
  A turn (user message + assistant reply) commits atomically after success;
  failed turns leave no trace, so retries cannot corrupt the history.

  Scenario: First turn opens a new conversation
    Given a valid chat request
    When I submit the request to the chat endpoint
    Then the response status code should be 200
    And the response JSON should contain a non-empty "conversation_id"

  Scenario: A follow-up turn carries the conversation history to the model
    Given a conversation with one completed turn
    When I submit a follow-up message in the same conversation
    Then the response status code should be 200
    And the reply should include the marker "(history=2)"

  Scenario: An unknown conversation id maps to the error envelope
    Given a chat request with an unknown conversation id
    When I submit the request to the chat endpoint
    Then the response status code should be 404
    And the response JSON should contain error "conversation_not_found"

  Scenario: A failed turn leaves no trace in the history
    Given a conversation with one completed turn
    And the upstream model rejects the input
    When I submit a follow-up message in the same conversation
    Then the response status code should be 400
    And the response JSON should contain error "invalid_input"
    When the upstream model recovers
    And I submit a follow-up message in the same conversation
    Then the response status code should be 200
    And the reply should include the marker "(history=2)"

  Scenario: A streaming turn continues the same conversation
    Given a conversation with one completed turn
    When I stream a follow-up message in the same conversation
    Then the response status code should be 200
    And the streaming response should include header "X-Conversation-Id"
    And the streamed text should include the marker "(history=2)"
