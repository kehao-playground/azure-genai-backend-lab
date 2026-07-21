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

Failures before the stream starts keep their HTTP status codes (the envelope applies as usual); failures after the 200 travel as an `error` event. A normally closed stream ends with exactly one terminal event; clients must treat EOF without a terminal as a failure and must ignore unknown event names (future events are additive). Ordering and cardinality invariants are enforced by `tests/bdd/features/streaming_response.feature`.

## Correlation ID

The middleware in `azgenai_lab.core.correlation`:

- reads `X-Correlation-Id` from the request, or generates a UUID when absent,
- stores it on `request.state.correlation_id`,
- always returns it as the `X-Correlation-Id` response header.

It appears in every error body and, as the series progresses, in structured logs and traces (audit logging and Application Insights articles).

## Placeholder policy

Endpoints that are not implemented yet return an explicit `501 Not Implemented` with the envelope above, rather than fake success.
