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
- Every store/service key is `(tenant_id, conversation_id)` (Day 15), `tenant_id` coming from the request's `Principal` — there is no conversation lookup that skips tenant scoping. Tenant B presenting tenant A's `conversation_id` gets the same `404 conversation_not_found` as an unknown id, never a 403: existence is not leaked across tenants any more than it is across never-issued ids. B's failed cross-tenant attempt performs no mutation, so A can continue that same conversation afterward.
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

## Identity and tenancy

Four endpoints require a `Principal`: `POST /api/v1/chat`,
`POST /api/v1/chat/stream`, `POST /api/v1/rag`, and `POST /api/v1/agent`.
`/health` does not.

`Principal(tenant_id, user_id, group_ids)` is the whole authorization context.
`tenant_id` and `user_id` are required strings; `group_ids` is a tuple that may be
empty. All three identifiers match `[A-Za-z0-9_-]{1,64}` (a canonical GUID is 36
characters and fits), at most 100 group ids are accepted, and group ids are
deduplicated and sorted so the derived ACL filter is deterministic.

### Two modes behind one dependency

`require_principal` is the only identity boundary, and which adapter sits behind it
is chosen **once at startup** from `AUTH_MODE` — never per request, and never by
what a request happens to carry.

- **`AUTH_MODE=headers`** (default, development). `HeaderPrincipalResolver` reads
  exactly one `X-Tenant-Id`, exactly one `X-User-Id`, and zero-or-one
  `X-Group-Ids` (a comma-separated list; absent or whitespace-only means
  `group_ids=()`). The raw `X-Group-Ids` value is capped at 4096 ASCII bytes and
  at 100 tokens before deduplication.
- **`AUTH_MODE=entra`** (Day 19). `EntraJwtPrincipalResolver` reads
  `Authorization: Bearer <token>`, verifies it cryptographically against the
  configured tenant's published signing keys, and builds the `Principal` from the
  `tid`, `oid` and `groups` claims. The `X-*` identity headers are read by nothing
  in this mode. Full contract, configuration and provisioning:
  [entra-id-auth.md](entra-id-auth.md).

`X-User-Id` is required in headers mode — a **breaking change** from Day 15, where
a `Principal` was `(tenant_id, group_ids)` only. A request that carried working
identity headers before Day 19 now gets a 401 until it also sends `X-User-Id`.

In Entra mode the raw `Authorization` header value is capped at 16 KiB, checked
**before** any splitting and **including** the `Bearer ` prefix; bounding the token
body instead would mean splitting unbounded attacker input first.

### 401 versus 403

- **`401 unauthorized`**, with a `WWW-Authenticate: Bearer` challenge (RFC 9110
  §15.5.2) — the caller could not be authenticated. Every parsing violation lands
  here: duplicate `X-Tenant-Id` or `X-User-Id` lines, more than one `X-Group-Ids`
  line, an empty token inside a non-empty CSV (`a,,b`, `,a`, `a,`), an identifier
  outside the charset/length rule, too many or too-long group values — and, in
  Entra mode, a malformed `Authorization` header, an unaccepted algorithm, an
  unknown key id, a failed signature/issuer/audience/expiry check, a `tid` that is
  not the configured tenant, or a group-overage claim. Never `422`: header syntax
  is not request-body validation, and a 422 here would leak the distinction
  between "who are you" and "what did you ask for". The message
  (`Missing or invalid credentials.`) deliberately names neither the mechanism nor
  the reason — two resolvers share this dependency, and telling an unauthenticated
  caller which check failed is free reconnaissance.
- **`403 insufficient_scope`**, with `WWW-Authenticate: Bearer error="insufficient_scope"`
  — Entra mode only. The token verified and the caller *is* authenticated; it just
  lacks the required delegated scope or application role. Retrying with the same
  credential is pointless, which is what RFC 6750's `insufficient_scope` challenge
  says. Identity is resolved before permissions, so an untrusted `tid` or a group
  overage is 401 even when the scope would also have been refused.

On `/chat/stream`, both rejections are pre-stream JSON responses through the
standard envelope (the Day 6 two-stage error boundary), never SSE `error` events:
`require_principal` runs before the `StreamingResponse` is constructed.

The generated OpenAPI declares a `bearerAuth` security scheme on all four protected
operations **unconditionally**, including under `AUTH_MODE=headers` where the token
is ignored and the identity headers are documented as no parameter at all — see
[entra-id-auth.md §7](entra-id-auth.md#what-the-generated-openapi-says-and-where-it-deviates).

### Trust boundary (read before deploying past a lab environment)

In **headers mode**, these headers are trusted-gateway input, not
end-user-verifiable credentials: a gateway in front of this backend must strip or
override every client-supplied identity header before forwarding a request, and the
backend must only be reachable through that gateway. Sent directly to the backend,
`X-Tenant-Id`/`X-User-Id`/`X-Group-Ids` are impersonation knobs, not authentication
— anyone who can reach the backend can claim any tenant or group.

In **Entra mode** the backend verifies the credential itself, so that particular
gap closes; the gateway should still strip all three `X-*` headers, since a future
change or a misread `AUTH_MODE` should not be one line away from accepting them.

Unchanged in both modes: there is no "authorization denied" signal anywhere in this
pipeline. A document outside the caller's ACL is filtered at query time and is
indistinguishable from a document that does not exist, so silent filtering *is* the
absence contract, by design, not a gap to close later. And isolation is **logical,
not physical** — every tenant's chunks live in the same shared Azure AI Search
index, separated only by the query-time ACL filter; a bug in that filter, not a
hardware or network boundary, is what stands between tenants.

### Logging

Every application `LogRecord` carries `tenant_id` and `user_id` fields once
`require_principal` succeeds, using the same `ContextVar` + `LogRecordFactory`
mechanism as the Day 5 correlation id (`core/logging.py`), and the context stays
set for the entire response — including a streamed body. Startup, background tasks,
and any request that never reached a valid principal log `-` for both. Group ids
are never logged. In Entra mode `user_id` is a directory object ID: treat it as
pseudonymous personal data, not as an opaque request tag.

Two conventions coexist on the same process log stream (Day 22 adds the second one; it does
not replace the first): **diagnostic** lines are human-readable `key=value` text — the stage
lines, the `llm usage` line, the prompt-attribution line, all of the above — and **audit**
lines are one machine-readable JSON object per authenticated request that reaches a
terminal the audit contract classifies (the zero-event exceptions — an out-of-contract bug,
a malformed-JSON body that never established identity, a streaming observer closed before
its first iteration — are in
[audit-logging.md](audit-logging.md#exactly-once--delivery)), schema-validated before they
are written. One custom `Formatter` (`_AuditAwareFormatter`) routes by logger
name (`audit` vs. everything else) so both share one root handler and one output stream
without a second file or a second process. The outcome vocabulary is a single set of words
across both layers — `agent_turn.py`'s stage log and every audit `outcome` field both use
`success`/`error` (`rejected` is audit-only, describing a 4xx the diagnostic stage log
doesn't classify at that granularity) — so a reader never has to remember that one layer
calls a clean result `ok` and the other calls it `success`. `extra=` fields on individual
diagnostic calls are used ad hoc where a call site already passes one, not applied
uniformly; that is the current state, not a target this codebase is working toward. See
[audit-logging.md](audit-logging.md) for the full schema, presence rules, and honest limits.

See [entra-id-auth.md](entra-id-auth.md) for the Entra ID integration in full,
[rag-retrieval.md](rag-retrieval.md#access-control-is-a-query-time-filter-not-a-separate-check)
for how a `Principal` becomes an OData filter, and
[rag-indexing.md](rag-indexing.md#tenant-scoped-keys) for how tenancy is
encoded on the write side.

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
- Retrieval is scoped by tenant and group through the `Principal` resolved
  above (Day 15): every `/rag` call requires a principal, and a query-time
  ACL filter — never a raw, unfiltered query — is the only shape
  `AzureSearchClient.search()` can send. A document outside the caller's
  tenant/group scope is filtered out and comes back indistinguishable from a
  document that was never retrieved, so a cross-tenant question resolves the
  same way an answer-absent one does: `status: "no_answer"`, not a denial
  signal. See [rag-retrieval.md](rag-retrieval.md#access-control-is-a-query-time-filter-not-a-separate-check).
- Retrieved chunk content is fenced with start/end markers carrying a
  per-request random nonce, on top of the template-level warning that
  source text is data, not instructions (Day 15; nonce Day 21 G1) — a
  poisoned chunk cannot forge the closing marker without guessing it. This
  is mitigation, not a sandbox: a poisoned corpus entry crafted to look
  like an instruction stays on the threat model regardless of the fence.
  Citation markers (`[n]`) in the model's answer are validated
  syntactically against the actually-included source numbers — an
  out-of-range or invented `[n]` is stripped, logged by
  number only, and never fails the request or changes `status`; this proves
  the citation *points somewhere real*, not that the cited text actually
  supports the sentence it is attached to.
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

## Agent endpoint

`POST /api/v1/agent` runs a tool-using agent turn inside a conversation.
The request carries `task` (422 when blank; 400 `invalid_input` beyond
4,000 UTF-8 bytes) and an optional `conversation_id` with the same
semantics as `/chat` — omit to start, unknown ids are 404
`conversation_not_found`.

**Authorization scope is part of session identity.** A conversation is
bound at creation to the creating principal's exact group set; every
continuation — `/chat`, `/chat/stream`, `/agent` — under a different set
is indistinguishable from a conversation that does not exist (404, same
shape as unknown and cross-tenant ids). The remedy after a group change
is a new conversation.

**One run is one turn.** The whole run's provider-reported usage commits
atomically with the turn; a failed run leaves no trace in the ledger even
though the completed calls were billed upstream (the invoice and Cost
Management remain the authority). The budget gate is a single pre-run
check: an admitted run may overshoot the threshold by up to its entire
usage — the complete ceiling is 6 × the deployed model's context window.

**Incomplete vocabulary (additive).** `status: incomplete` with
`incomplete_reason: tool_call_limit` (the tool budget forced the final
answer) or `iteration_limit` (the model-call budget did). For both, the
answer is keepable and may be empty — the framework's hardcoded fallback
sentence never reaches the wire. `natural` stops with an empty answer are
502 `upstream_error`, mirroring `/chat`'s empty-reply contract.

**Execution trace.** `tool_calls` lists tool names, parsed arguments
(null when unparseable) and 1-based rounds, including refused calls
(`executed: false`). Tool outputs are deliberately absent from the wire:
they reached the model, and re-publishing them would bypass the
document-ACL reasoning that shaped them.

See [diagrams/agent-turn-sequence.md](diagrams/agent-turn-sequence.md) for
the full request flow.

## Correlation ID

The middleware in `azgenai_lab.core.correlation`:

- reads `X-Correlation-Id` from the request, or generates a UUID when absent,
- stores it on `request.state.correlation_id`,
- always returns it as the `X-Correlation-Id` response header.

It appears in every error body and in every diagnostic log line; it is also the join key across the audit log (Day 22 — see [audit-logging.md](audit-logging.md)) and, as the series progresses, traces (the Day 27 Application Insights article).

## Placeholder policy

Endpoints that are not implemented yet return an explicit `501 Not Implemented` with the envelope above, rather than fake success.
