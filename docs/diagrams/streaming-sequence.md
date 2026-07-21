# Streaming Sequence

Two-phase error boundary: the upstream stream is opened eagerly, before any
byte reaches the client, so pre-stream failures keep their HTTP status codes.
After the 200, the serializer guarantees exactly one terminal event
(`message.done` or `error`) on a normally closed stream.

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI Backend
    participant LLM as Azure OpenAI (Responses API)

    Client->>API: POST /api/v1/chat/stream
    API->>LLM: responses.create(stream=True) — eager open
    alt upstream fails before the stream starts
        LLM-->>API: SDK exception (401 / 429 / timeout ...)
        API-->>Client: HTTP 4xx/5xx + error envelope (Day 5 mapping)
    else stream established
        API-->>Client: HTTP 200 (text/event-stream)
        loop typed events → owned vocabulary
            LLM-->>API: response.output_text.delta
            API-->>Client: event: message.delta {"text"}
        end
        alt upstream completes or truncates
            LLM-->>API: response.completed / response.incomplete
            API-->>Client: event: message.done {"status", "incomplete_reason"?, "correlation_id"}
        else upstream fails mid-stream
            LLM-->>API: response.failed / error event / SDK exception
            API-->>Client: event: error {error envelope}
        else upstream EOF without terminal
            API-->>Client: event: error (synthesized upstream_error)
        end
    end
```

Terminal guarantee: when the client stays connected and the stream ends
normally, it receives exactly one terminal event; EOF without a terminal must
be treated as a failure. On client disconnect the adapter closes the upstream
stream in a `finally` block.

Conversation state (Day 7) wraps this flow without changing it: the
conversation is resolved before the eager open (an unknown `conversation_id`
is a pre-stream 404), the issued id travels in the `X-Conversation-Id`
response header (provisional on a first turn until a keepable terminal), and
the turn — transcript plus provider replay items — commits to the
`ConversationStore` just before the `message.done` terminal is delivered.
`error`, `content_filter`/`other`, and disconnects before the upstream
terminal is consumed commit nothing; a storage failure after the 200 becomes
an SSE `error` terminal with code `storage_error` (see
[Conversation Turn Lifecycle](../state-models/conversation-session-fsm.md)).
