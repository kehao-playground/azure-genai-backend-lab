# API Conventions

These conventions apply series-wide and are fixed from the first commit.

## Path versioning

Business endpoints live under `/api/v1/`. The health endpoint stays unversioned at `/health`.

## Error envelope

All HTTP errors share one shape, produced by a custom exception handler — never FastAPI's default `{"detail": ...}`:

```json
{
  "error": {
    "code": "not_implemented",
    "message": "Chat API will be implemented in Day 5."
  },
  "correlation_id": "5f0d2c9e-..."
}
```

Endpoints raise `HTTPException` with a `{"code", "message"}` dict as `detail`; the handler in `azgenai_lab.core.errors` wraps it into the envelope.

Validation errors (HTTP 422) use the envelope too: a `RequestValidationError` handler maps FastAPI's default `{"detail": [...]}` shape into the envelope with code `validation_error` (Day 5). Clients only ever need to parse one error shape.

## Streaming events (SSE)

Streaming endpoints use Server-Sent Events with an owned vocabulary — upstream event names never reach clients (Day 6):

- `message.delta` — `{"text"}`, one increment of output text;
- `message.done` — `{"status": "completed" | "incomplete", "incomplete_reason"?, "correlation_id"}`, sole success terminal;
- `error` — the error envelope above, verbatim, sole failure terminal.

Failures before the stream starts keep their HTTP status codes (the envelope applies as usual); failures after the 200 travel as an `error` event. A normally closed stream ends with exactly one terminal event; clients must treat EOF without a terminal as a failure and must ignore unknown event names (future events are additive). Ordering and cardinality invariants are enforced by `tests/bdd/features/streaming_response.feature` together with the streaming unit tests (the EOF-without-terminal fallback and nothing-after-terminal rules live in `tests/unit/test_streaming_api.py`).

## Conversation state

The LLM API is stateless (`store=False` upstream); conversation history is owned by this application behind the `ConversationStore` protocol (Day 7):

- `POST /api/v1/chat` and `POST /api/v1/chat/stream` accept an optional `conversation_id`. Omitting it starts a new conversation; the id comes back in the JSON body (`/chat`) or in the `X-Conversation-Id` response header (`/chat/stream` — a header because SSE clients need it at response time, not from an event). On a first streaming turn that header id is **provisional**: it becomes real only with a keepable terminal (`message.done` completed or `max_output_tokens`); after `error`, `content_filter`/`other`, or a disconnect the client must discard it.
- Unknown ids are rejected with `404 conversation_not_found` through the envelope. "Unknown" covers never-issued, expired, and lost-on-restart ids alike; the client reaction is the same — start a new conversation.
- Each committed turn stores two representations: the visible transcript (user + assistant messages) and the provider **replay items** — the user input item plus every response output item, including encrypted reasoning items (`include=["reasoning.encrypted_content"]`). The replay items, not the transcript, are what the next request resends: with `store=False` and a reasoning model, replaying only visible text silently drops reasoning context.
- A turn commits atomically only after a reply the client keeps: non-streaming success, stream `completed`, or `incomplete`/`max_output_tokens`. Failed turns, `content_filter`/`other` truncations, and disconnects **before the upstream terminal is consumed** leave no trace, so retries cannot corrupt history. Once the terminal is consumed, the commit happens whether or not delivery of `message.done` can be proven — the one-way invariant is that a client which received `message.done` can rely on the history existing. An empty non-streaming reply maps to `502 upstream_error`, never a 200 carrying an id that does not exist.
- Turns on one conversation are serialized (per-conversation critical section); a persistent store must provide equivalent conditional-write semantics across replicas, and its `append` must be all-or-nothing.
- Storage failures map to `500 storage_error` (envelope) before a response is out, or an SSE `error` terminal after the 200. By that point inference has been billed; retrying repeats it.

The executable contract is `tests/bdd/features/conversation_state.feature` plus `tests/unit/test_conversation_service.py`.

## Correlation ID

The middleware in `azgenai_lab.core.correlation`:

- reads `X-Correlation-Id` from the request, or generates a UUID when absent,
- stores it on `request.state.correlation_id`,
- always returns it as the `X-Correlation-Id` response header.

It appears in every error body and, as the series progresses, in structured logs and traces (audit logging and Application Insights articles).

## Placeholder policy

Endpoints that are not implemented yet return an explicit `501 Not Implemented` with the envelope above, rather than fake success.
