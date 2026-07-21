Feature: Streaming chat contract (SSE)
  The wire vocabulary is ours, not the upstream's: message.delta,
  message.done, error. When the client stays connected and the stream ends
  normally, it receives exactly one terminal event (message.done or error);
  EOF without a terminal event must be treated as a failure.

  Scenario: Successful stream delivers deltas and exactly one message.done
    Given a valid streaming chat request
    When I submit the request to the streaming endpoint
    Then the response status code should be 200
    And the response content type should be "text/event-stream"
    And the stream should contain at least 2 "message.delta" events
    And the stream should end with exactly one terminal "message.done" event
    And the terminal event should carry status "completed" and a correlation_id

  Scenario: Upstream failure before the stream starts keeps its HTTP status
    Given the upstream fails with throttling before the stream starts
    And a valid streaming chat request
    When I submit the request to the streaming endpoint
    Then the response status code should be 503
    And the response JSON should contain error "upstream_throttled"
    And the response JSON should contain a non-empty "correlation_id"

  Scenario: Upstream failure mid-stream ends the stream with an error event
    Given the upstream fails after streaming part of the answer
    And a valid streaming chat request
    When I submit the request to the streaming endpoint
    Then the response status code should be 200
    And the stream should end with exactly one terminal "error" event
    And the error event data should use the error envelope shape

  Scenario: Content filter truncation is reported as incomplete with a reason
    Given the upstream truncates the stream with reason "content_filter"
    And a valid streaming chat request
    When I submit the request to the streaming endpoint
    Then the response status code should be 200
    And the stream should end with exactly one terminal "message.done" event
    And the terminal event should carry status "incomplete" and reason "content_filter"

  Scenario: Invalid streaming input is rejected with the error envelope
    Given a streaming chat request with an empty message
    When I submit the request to the streaming endpoint
    Then the response status code should be 422
    And the response JSON should contain error "validation_error"
    And the response JSON should contain a non-empty "correlation_id"
