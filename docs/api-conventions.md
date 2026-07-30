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
- `message.done` — `{"status": "completed" | "incomplete", "incomplete_reason"?, "usage"?, "correlation_id"}`, sole success terminal (`usage` added in Day 9 — an additive change clients must tolerate);
- `error` — the error envelope above, verbatim, sole failure terminal.

Failures before the stream starts keep their HTTP status codes (the envelope applies as usual); failures after the 200 travel as an `error` event. A normally closed stream ends with exactly one terminal event; clients must treat EOF without a terminal as a failure and must ignore unknown event names (future events are additive). Ordering and cardinality invariants are enforced by `tests/bdd/features/streaming_response.feature` together with the streaming unit tests (the EOF-without-terminal fallback and nothing-after-terminal rules live in `tests/unit/test_streaming_api.py`).

## Conversation state

The LLM API is stateless (`store=False` upstream); conversation history is owned by this application behind the `ConversationStore` protocol (Day 7):

- `POST /api/v1/chat` and `POST /api/v1/chat/stream` accept an optional `conversation_id`. Omitting it starts a new conversation; the id comes back in the JSON body (`/chat`) or in the `X-Conversation-Id` response header (`/chat/stream` — a header because SSE clients need it at response time, not from an event). On a first streaming turn that header id is **provisional**: it becomes real only with a keepable terminal (`message.done` completed or `max_output_tokens`); after `error`, `content_filter`/`other`, or a disconnect the client must discard it.
- Unknown ids are rejected with `404 conversation_not_found` through the envelope. "Unknown" covers never-issued, expired, and lost-on-restart ids alike; the client reaction is the same — start a new conversation.
- Each committed turn stores two representations: the visible transcript (user + assistant messages) and the provider **replay items** — the user input item plus every response output item, including encrypted reasoning items (`include=["reasoning.encrypted_content"]`). The replay items, not the transcript, are what the next request resends: with `store=False` and a reasoning model, replaying only visible text silently drops reasoning context.
- A turn commits atomically only after a reply the client keeps: non-streaming success, stream `completed`, or `incomplete`/`max_output_tokens`. Failed turns, `content_filter`/`other` truncations, and disconnects **before the upstream terminal is consumed** leave no trace, so retries cannot corrupt history. Once the terminal is consumed, the commit happens whether or not delivery of `message.done` can be proven — the one-way invariant is that a client which received `message.done` can rely on the history existing. An empty non-streaming reply maps to `502 upstream_error`, never a 200 carrying an id that does not exist.
- Turns on one conversation are serialized (per-conversation critical section with reference-counted lock entries), and every commit is **conditional**: `append` presents the revision read at the start of the turn and the store rejects stale writers (`ConversationConflictError`) — the version/ETag contract a multi-replica persistent adapter enforces natively. `append` is all-or-nothing: everything that can fail happens before the first mutation.
- Storage failures map to `500 storage_error` (envelope) before a response is out, or an SSE `error` terminal after the 200. By that point inference has already consumed tokens; retrying repeats it.

The executable contract is `tests/bdd/features/conversation_state.feature` plus `tests/unit/test_conversation_service.py`.

## Token usage and budget (Day 9)

Cost is metered, not estimated: every turn that returns a usage-bearing terminal surfaces the provider-reported token counts (a request-level usage signal for attribution and guardrails — not a billing record; the invoice and Cost Management meters remain the source of truth), and a per-conversation budget is enforced before inference.

- `POST /api/v1/chat` responses carry `usage: {input_tokens, output_tokens, total_tokens, reasoning_tokens?}` (nullable — only if the provider omitted its usage block; `reasoning_tokens` is the hidden-reasoning subset of `output_tokens`, from `usage.output_tokens_details`). Streaming turns report the same object on the `message.done` terminal; deltas never carry usage because only the terminal response settles this turn's count.
- `POST /api/v1/chat` responses also carry `status` (`completed` | `incomplete`) and a nullable `incomplete_reason` — the non-streaming mirror of the `message.done` terminal. Client rules are identical to the Day 6 vocabulary: keep the partial text for `max_output_tokens`, discard or mask it for `content_filter`, treat it as unusable for `other`. Commit rules mirror too: `content_filter`/`other` turns are not committed, and on a first turn the returned `conversation_id` never comes into existence.
- Every upstream call sends `max_output_tokens` (config `LLM_MAX_OUTPUT_TOKENS`, default 1000; must be positive — zero/negative values fail startup validation). A capped stream ends with `message.done` `incomplete`/`max_output_tokens`; a capped non-streaming call returns `status: incomplete` — the Day 6 contract, now enforced on both endpoints.
- Each conversation has a lifetime budget in provider-reported tokens (`CONVERSATION_TOKEN_BUDGET`, default 50000; `None` is the only way to disable — zero/negative values fail startup validation). The ledger accumulates with each committed turn (atomically, in the same `append`), and the check runs **before** inference: an exhausted conversation is rejected with `429 token_budget_exceeded` through the envelope — for streams, before the stream starts, so it is always a plain HTTP response. The budget does not replenish; there is no `Retry-After`. The remedy is a new conversation.
- Known gap, by design: a failed turn may have incurred billable processing upstream but leaves no ledger trace — turn-commit semantics (Day 7) win over accounting completeness. The authoritative spend record is Azure Cost Management, not this ledger; the ledger exists to bound spend, not to account for it.

Usage is also logged (`llm usage input_tokens=… output_tokens=… reasoning_tokens=… total_tokens=… correlation_id=…`) for every call that returned a usage-bearing terminal — non-streaming success and stream `completed`/`incomplete`. Failed events, SDK exceptions and client disconnects may still have incurred billable processing with no line logged; a missing line is not zero cost. The line is joinable with the prompt-attribution line (Day 8) on `correlation_id`.

The executable contract is `tests/bdd/features/token_budget_guardrail.feature` plus `tests/unit/test_token_budget.py` and `tests/unit/test_chat_incomplete.py` (non-streaming truncation contract, also covered by a `chat_api_contract.feature` scenario).

## RAG (Retrieval-Augmented Generation)

`POST /api/v1/rag` (Day 14) is a single-turn read path, not a `/chat` variant: no
`conversation_id`, no history, no lifetime token-budget ledger — those belong to the
stateful `/chat` machinery above (Day 7/Day 9) and RAG does not opt into them.
`store=False` still applies per call, and `LLM_MAX_OUTPUT_TOKENS` still caps this
call exactly as it caps a `/chat` turn.

```json
// request
{ "question": "..." }

// response
{
  "answer": "... [1]" ,
  "status": "answered",
  "incomplete_reason": null,
  "sources": [
    {
      "number": 1,
      "chunk_id": "...",
      "title": "...",
      "heading_path": "...",
      "score": 0.0166,
      "reranker_score": null
    }
  ],
  "usage": { "input_tokens": 812, "output_tokens": 96, "total_tokens": 908, "reasoning_tokens": null },
  "correlation_id": "5f0d2c9e-..."
}
```

`incomplete_reason` and `usage` are always present in the response body — never omitted — but
their value is nullable: both are `null` on the `"no_answer"` short-circuit and on a fully
completed generation; `usage` is `null` only if the provider omitted its usage block.

- `status` is `"answered"` or `"no_answer"`. `"no_answer"` is a structural
  short-circuit — retrieval returned zero hits, so the request never reaches the
  LLM (`answer`, `usage`, and `incomplete_reason` are `null`, `sources` is empty).
  There is no score-based gate: Day 13's live probe showed hybrid RRF scores
  cannot distinguish an answer-present corpus from an answer-absent one, so
  nothing downstream of "were there hits at all" gets to threshold on a number.
- **`status: "answered"` does not mean grounded.** Grounding past the zero-hits
  gate is instructional, not structural: the `rag_answer` prompt tells the model
  to cite sources and to say plainly when the sources don't cover the question,
  but a model-level refusal is still a successful generation from the pipeline's
  view and comes back as `"answered"`. Clients that need to tell "cited answer"
  from "polite refusal" apart must inspect `answer` and `sources`, not `status`
  alone.
- `sources` is the ranked hit list the model was given, numbered to match the
  `[1]`/`[2]` citation markers the prompt asks the model to use; `score` and
  `reranker_score` follow the [two-scores contract](rag-retrieval.md#two-scores-and-only-one-of-them-has-a-rubric) — hybrid mode never populates `reranker_score`.
- Retrieved chunk content is untrusted data, not instructions: it travels inside
  the *user* message, while the citation/refusal rules live only in the prompt
  template's instructions, which explicitly tell the model to treat source text
  as non-instructions. That is mitigation, not immunity — corpus poisoning
  crafted to look like an instruction stays on the threat model.
- Retrieval is unscoped by tenant or user; per-tenant/per-user authorization on
  retrieval is deferred to Day 15.
- Errors follow the standard envelope; `incomplete_reason` mirrors the `/chat`
  vocabulary (`max_output_tokens`, `content_filter`, `other`) for the same
  client rules (keep/discard/treat-as-unusable) — it only ever appears when
  `status` is `"answered"`.
- See [diagrams/rag-query-sequence.md](diagrams/rag-query-sequence.md) for the
  full request flow.
- **Question contract (Day 14 review findings 1-2):** `question` must be 1-2,000
  characters after trimming leading/trailing whitespace; the trimmed value is
  what flows to retrieval and generation. A whitespace-only question (e.g.
  `"   "`) is rejected as `422 validation_error`, not accepted and then failed
  downstream. 2,000 chars is the Day 12 conservative char proxy, bounded
  universally rather than by sample: a UTF-8 character is at most 4 bytes, so
  2,000 chars <= 8,000 bytes, and since a BPE token decodes to at least one
  UTF-8 byte, token count <= byte count — 2,000 chars is therefore <= 8,000
  tokens, under the embedding model's 8,192-token input ceiling for any
  input, so a within-contract question can never trigger an upstream
  embedding-size 400. `RAG_TOP` is bounded to 1-50: 50 is `DEFAULT_VECTOR_K`,
  the vector leg's own candidate pool, so a larger value would ask generation
  to read more chunks than retrieval ever offers.
- **Assembled-prompt budget (Day 14 r04 residual A; headroom added r06
  residual 2):** the worst legal case (`RAG_TOP=50` hits of
  `chunk_max_chars=2000` chars each, plus the 2,000-char question) can exceed
  the model's documented input limit before this guard existed. `RagService`
  enforces `MAX_PROMPT_BYTES = 267,904` (`272,000 - 4,096`) — the same
  byte-bound argument as above (token_count <= utf8_byte_count), applied to
  the whole assembled prompt (instructions + rendered sources + question)
  instead of the question alone. That byte bound is a conservative guardrail
  on the counted *text*, not a full proof of the complete provider-input
  ceiling: the Responses API's message framing (roles, field wrappers,
  protocol overhead) sits outside the counted text and has no documented
  bound of its own. `PROMPT_FRAMING_HEADROOM_BYTES = 4,096` reserves budget
  for that unbounded overhead (well beyond any observed framing cost); if a
  provider-side overflow ever occurred despite this headroom, it would remain
  a server-owned failure (the corpus is server-selected) and is in fact
  reclassified: the adapter translates the provider's
  `context_length_exceeded` into a dedicated `ContextLengthExceededError`
  subtype, and `RagService` rethrows exactly that subtype as
  `500 rag_context_overflow` (r08). On `/chat` the same subtype keeps its
  inherited `400 invalid_input` contract — there the caller composed the
  prompt, so the ownership genuinely differs per route.
  Selection includes retrieved hits in rank order, stopping at
  the first hit that would not fit rather than skipping it for a lower-ranked
  one. Truncation semantics: the `sources` list in the response is exactly
  the set of hits actually sent to the model for generation, never the full
  retrieved set; a stage log line reports `dropped_source_count` when hits
  were excluded.
- **Context overflow (Day 14 r06 residual 1):** live `SearchHit.content`/
  `heading_path` carry no runtime maximum (only the offline chunker bounds
  our own corpus), so the query boundary does not trust that indexing-side
  invariant. If even the rank-1 hit alone cannot fit `MAX_PROMPT_BYTES`,
  `RagService` raises `500 rag_context_overflow` through the standard
  envelope — server-owned, since the corpus is server-selected, not user
  input — instead of an unhandled error. Zero provider calls happen on this
  path. The same error code also covers the post-call case above (provider
  `context_length_exceeded` despite the headroom), which by definition made
  one provider call before failing.

## Correlation ID

The middleware in `azgenai_lab.core.correlation`:

- reads `X-Correlation-Id` from the request, or generates a UUID when absent,
- stores it on `request.state.correlation_id`,
- always returns it as the `X-Correlation-Id` response header.

It appears in every error body and, as the series progresses, in structured logs and traces (audit logging and Application Insights articles).

## Placeholder policy

Endpoints that are not implemented yet return an explicit `501 Not Implemented` with the envelope above, rather than fake success.
