# Audit logging (Day 22)

The audit log is a second logging channel, separate from the diagnostic lines the rest of
this codebase already writes: one JSON line per authenticated request that reaches a
terminal this contract classifies, carrying who/when/which/outcome and nothing else. The
exact scope of that guarantee — including the deliberate zero-event paths and one narrow
disclosed exception — is defined in [Exactly-once & delivery](#exactly-once--delivery), not
here. Content — the message text, the retrieved chunks, the tool
arguments — never enters it; the conversation store remains the system of record for
content, and an audit event's job is to be joinable back to it by id, not to duplicate it.

Three decision points that previously only existed as a response code and a diagnostic line
now have a durable-within-the-process record: whether a turn **committed** (Day 7), whether a
request was turned away by the **token budget** (Day 9), and whether a caller was rejected by
**authentication or authorization** (Day 15/Day 19). This document describes the schema, the
presence rules, what is guaranteed and what is not, and how to read the log. The schema
lives in `core/audit.py`; the exported, CI-drift-checked JSON Schema is
[`docs/audit/audit-events.json`](audit/audit-events.json).

## Overview

- **Reference-only.** Every field is an id, a count, a hash, a boolean, or an enum. There is
  no field that carries free text a user or the model wrote.
- **Terminal, not per-stage.** Existing diagnostic log lines are unchanged and stay
  diagnostic-only: prompt attribution (`llm call …`, Day 8), usage (`llm usage …`, Day 9),
  RAG stage latency (`rag stage=…`, Day 14), and agent-turn stage latency plus
  tool-execution logging (`agent_turn_stage stage=…` / `agent_tool_execution …`, Day 18).
  The audit log adds one additional line per classified request (scope in
  [Exactly-once & delivery](#exactly-once--delivery)), on top of all of these, emitted at
  the point the request's outcome is known.
- **One event type per route, plus one for rejected auth:** `chat.turn`, `rag.query`,
  `agent.run`, and `auth.rejected`. A budget rejection is not a separate event type — it is
  `chat.turn`/`agent.run` with `outcome="rejected"` and `error_code="token_budget_exceeded"`.
- **Separate logger namespace.** Events go through `logging.getLogger("audit")`, not a
  module-named logger, so they can be filtered, leveled, and formatted independently of
  every other log line in the process — see [Consuming the log](#consuming-the-log).
- **A validated boundary, not a convention.** `emit_audit_event()` runs every event through
  `AUDIT_EVENT_ADAPTER` (a pydantic `TypeAdapter` over the full discriminated union) before
  it is serialized. A value that doesn't fit the schema raises before anything is written —
  the never-log guarantee and the presence rules are enforced by that validation step, not
  by callers remembering to follow a convention.

## Event taxonomy

### Common envelope

Every event, regardless of type, carries:

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `1` | literal; bump when the shape changes |
| `event` | `"chat.turn"` \| `"rag.query"` \| `"agent.run"` \| `"auth.rejected"` | outer discriminator |
| `occurred_at` | timezone-aware datetime | naive datetimes are rejected outright; any offset is normalized to UTC on the way in |
| `correlation_id` | string | joins back to every diagnostic line for the same request — see [Consuming the log](#consuming-the-log) |
| `duration_ms` | float ≥ 0 | request-level, from the same monotonic start the correlation middleware stamps when the request begins. `/chat`, `/chat/stream` and `/agent` read it as `request.state.audit_start` (`duration_since`); `/rag` emits from the service layer, which has no `Request` to read, so it goes through the `ContextVar` path instead (`request_duration_ms`) — same start value, different accessor, and `core/audit.py` calls out the distinction explicitly so a future caller doesn't reach for the wrong one |

The three route events (`chat.turn`, `rag.query`, `agent.run`) additionally always carry:

| Field | Type | Notes |
|---|---|---|
| `tenant_id`, `user_id` | string | the verified `Principal` — never null on a route event (only `auth.rejected`'s 401 case omits identity) |
| `provider_call_attempted` | bool | whether the provider adapter boundary was invoked for this request — see [Presence rules](#presence-rules) |
| `prompt_name`, `prompt_version`, `prompt_sha256`, `deployment` | string / int, all nullable together | the Day 8 prompt-attribution identity, present iff `provider_call_attempted` |
| `usage` | `{input_tokens, output_tokens, reasoning_tokens?, total_tokens}` or null | the Day 9 usage block, present only on a usage-bearing terminal |
| `outcome` | `"success"` \| `"rejected"` \| `"error"` | inner discriminator — `rejected` maps to a 4xx envelope, `error` to a 5xx envelope or a client disconnect, `success` to a 2xx |
| `error_code` | per-variant closed set | **absent on `success`** (the schema forbids the field entirely on that variant, not just nulls it) |

The two-level shape — an outer union on `event`, and inside each event an inner union on
`outcome` — exists because pydantic does not support a composite `(event, outcome)`
discriminator; `ChatEvent`/`RagQueryEvent`/`AgentRunEvent` are each their own nested
discriminated union, and `AuditEvent` discriminates across the four of those.

### `chat.turn`

Adds `conversation_id` (nullable — null only on a pre-commit rejection where no id was ever
returned), `streaming`, `committed`, `model_version`, `status` (`"completed"` \|
`"incomplete"`, nullable), `incomplete_reason` (nullable, the Day 6 vocabulary).

| Variant | Adds |
|---|---|
| `ChatTurnSuccess` | nothing beyond `outcome="success"` — required invariant: `provider_call_attempted=true` and `status` set |
| `ChatTurnRejected` | `error_code`: `validation_error` \| `conversation_not_found` \| `token_budget_exceeded` \| `invalid_input` \| `content_filtered`; `spent`, `budget` (present iff `error_code="token_budget_exceeded"`) |
| `ChatTurnError` | `error_code`: `configuration_error` \| `storage_error` \| `upstream_throttled` \| `upstream_timeout` \| `upstream_error` \| `client_disconnect` (audit's own code — never an HTTP status, only the streaming disconnect path uses it) |

### `rag.query`

Adds `model_version`, `hit_count` (nullable, ≥0), `selected_chunk_ids` (nullable tuple —
never chunk content, only ids), `status` (`"answered"` \| `"no_answer"` \| `"error"`,
nullable), `failed_stage` (`"retrieve"` \| `"assemble_context"` \| `"generation"`, nullable).

| Variant | Adds |
|---|---|
| `RagQuerySuccess` | `status` narrowed to `"answered"` \| `"no_answer"` (required); invariant: `status="answered"` requires `provider_call_attempted=true`, `status="no_answer"` requires it `false` — the Day 14 structural no-answer never calls the model |
| `RagQueryRejected` | `error_code`: `validation_error` \| `content_filtered` (the only RAG 400 — `invalid_input` is deliberately absent from this route's contract); `status` is `"error"` when the pipeline ran and failed, or null when it never ran at all (a pre-pipeline 422) |
| `RagQueryError` | `error_code`: `configuration_error` \| `embedding_rejected` \| `rag_context_overflow` \| `search_unavailable` \| `search_request_rejected` \| `upstream_throttled` \| `upstream_timeout` \| `upstream_error`; `failed_stage` is required (an error always names where it happened) |

`RagService.answer()` emits this event itself (there is no per-endpoint finalizer the way
`/chat` has one) — see [Exactly-once & delivery](#exactly-once--delivery) for the guard that
keeps direct, non-request test calls from fabricating one.

### `agent.run`

Adds `conversation_id` (nullable), `committed`, `model_calls` (nullable, ≥0 — **null, not
zero, when the framework result is unavailable**), `tool_call_count`, `refused_call_count`
(same null-not-zero rule), `tools` (nullable tuple of `{name, executed, round_index}` —
`round_index` is null unless a framework trace join succeeded), `stop_reason`
(`"natural"` \| `"iteration_limit"` \| `"function_call_limit"`, nullable).

| Variant | Adds |
|---|---|
| `AgentRunSuccess` | invariant: `provider_call_attempted=true` |
| `AgentRunRejected` | `error_code`: `validation_error` \| `invalid_input` (oversized task) \| `conversation_not_found` \| `token_budget_exceeded`; `spent`, `budget` (same pairing rule as `chat.turn`) |
| `AgentRunErrorEvent` | `error_code`: `storage_error` \| `upstream_error` only — `AgentTurnService` collapses every framework/provider failure (throttle, timeout, content filter) into `upstream_error` at the HTTP boundary, so the audit code set mirrors what `/agent` actually returns rather than a finer taxonomy the route doesn't expose |

Both success and error paths report through one shape, `AgentAuditTerminalSnapshot`: an
error path carries however much of the trace the adapter had in hand before the failure,
honestly null past that point, never a fabricated zero or empty list.

### `auth.rejected`

Its field set is deliberately different from the three route events — it does not extend
the same base, because the identity rules are the opposite of the norm:

| Field | Notes |
|---|---|
| `tenant_id`, `user_id` | **null on a 401** (no identity was ever established — an unverified claim never enters the audit log); **required on a 403** (the caller *was* authenticated, just lacked permission) |
| `path` | `request.url.path` — never the query string |
| `auth_mode` | `"headers"` \| `"entra"` |
| `reason` | `bearer_missing` \| `token_invalid` \| `permission_missing` \| `headers_missing` \| `headers_invalid` — a closed enum, further constrained per mode (e.g. headers mode never produces a 403, so `permission_missing` never appears there) |
| `http_status` | `401` \| `403` |

Emitted from exactly one place: `require_principal` (`api/principal.py`) catches the
internal `AuthRejection` both resolvers raise, emits, and only then maps to the HTTP
response — so a caller never sees a response that isn't matched by an event.

(Indexing-tool-only error codes such as `duplicate_chunk_id` are not on any HTTP route
surface and do not appear in any variant's code set.)

## Presence rules

Two layers enforce what a given event may and may not carry:

**The outcome layer is structural.** `error_code` simply does not exist as a field on a
`*Success` model — not "null", *absent* — so a success event carrying one fails validation
before it can be constructed. `spent`/`budget` exist only on the `Rejected` variants. This
layer is enforced the same way ordinary pydantic field presence always is, and the exported
JSON Schema shows it directly as per-variant `$def`s.

**The attempted-, budget-, identity-, and success-layer rules are runtime validators**, not
structural presence — a field can exist on the model and still be forbidden a value by a
`model_validator`. These six constraints are surfaced verbatim as `Field(description=...)`
in the exported schema, so a reader of `docs/audit/audit-events.json` sees the same rule a
test enforces, not just a comment:

| Constant | Text |
|---|---|
| `ATTEMPTED_CONSTRAINT` | `attempted=true requires full prompt/deployment attribution; attempted=false forbids attribution and provider terminal data` |
| `BUDGET_CONSTRAINT` | `spent/budget appear as a pair and exactly with error_code=token_budget_exceeded` |
| `IDENTITY_CONSTRAINT` | `401 carries no identity; 403 requires verified identity` |
| `CHAT_SUCCESS_CONSTRAINT` | `chat.turn success requires provider_call_attempted=true` |
| `AGENT_SUCCESS_CONSTRAINT` | `agent.run success requires provider_call_attempted=true` |
| `RAG_SUCCESS_CONSTRAINT` | `rag.query answered requires attempted=true; no_answer requires attempted=false` |

`provider_call_attempted` itself needs a validator, not just a bool, because the same
`error_code` can mean different things depending on when a request failed: a bare
`storage_error` from the conversation-load path never reached the provider
(`attempted=false`), while the same code from the commit path means the provider ran and
only the write afterward failed (`attempted=true`, with the terminal data it produced
carried on the exception as an `audit_snapshot` so the event doesn't lose it). Nothing
infers this from `error_code` alone or from `__cause__`; the typed exception hierarchy in
`core/errors.py` (`StorageError` vs. `StorageCommitError`/`ChatStorageCommitError`/
`AgentStorageCommitError`) carries the answer explicitly to the call site that builds the
event.

A few more presence rules worth knowing, none of them independently named constants but all
enforced the same validator way:

- `rag.query`'s `hit_count` appears once retrieval succeeds; `selected_chunk_ids` once
  context assembly succeeds; `failed_stage` once the pipeline has actually started and the
  outcome isn't success.
- `agent.run`'s framework-owned counts (`model_calls`, `tool_call_count`,
  `refused_call_count`, `stop_reason`) are null, never zero, when the framework result is
  unavailable — a degraded run still reports the app-owned `tools` list (`name`/`executed`
  always present; `round_index` only when a framework trace join succeeded).
- A 422 splits into two states, and only one of them produces an event: a **syntactically
  malformed JSON body** fails to parse before `require_principal` ever runs, so no identity
  was established and **zero events** are emitted; a body that parses but fails schema
  validation *after* the principal was resolved produces exactly one
  `error_code="validation_error"` rejected event — identity present; the route/provider
  terminal fields the request never reached (usage, attribution, commit state) are null —
  see [Exactly-once & delivery](#exactly-once--delivery).

## Never-log

No question or message text, chunk content, tool argument text, group ids, raw or bearer
tokens, JWT claims, exception messages, upstream error detail, or validation error text ever
appears in an audit event. The enforcement is **field absence, checked recursively, not a
redaction filter applied at write time**: `tests/unit/test_audit_schema.py` walks every
variant's field set (recursing into nested models like `AuditUsage`/`AuditTool`) and asserts
none of them uses one of the approved forbidden names —

```
message, message_text, question, question_text, content, chunk_content, chunk_text,
text, answer, task, arguments, tool_arguments, detail, upstream_detail, claims, token,
raw_token, access_token, bearer_token, snippet, body, exception_message,
validation_error_message, group_ids, groups
```

— so a future field with a content-shaped name fails a test before it can ship, rather than
relying on someone remembering to scrub a value at the point of logging.

This is the same discipline the rest of the codebase already applies to diagnostic logs
(question text and chunk content never reach `services/rag.py`'s stage log, group ids never
reach any `LogRecord` — see [api-conventions.md](api-conventions.md#logging) and
[security-checklist.md](security-checklist.md#logging-and-redaction)); the audit log
inherits it and adds the field-name test as a second, independent guarantee.

This field-shape guarantee is about what *kinds* of fields exist, not about who chose their
values. `conversation_id` and `correlation_id` are both caller-controlled strings — a client
sets `conversation_id` in the request body (`ChatRequest`/`AgentRequest`, neither field length-
nor format-constrained) and echoes `X-Correlation-Id` if it sends one — and both are logged
verbatim, including on the `conversation_not_found` 404 path where the id never resolved to
anything real. `json.dumps` escapes the value, so it cannot break the log line's JSON
structure, and it is the caller's own data rather than another party's, but a reader should
not assume every value under a reference-only field was server-generated.

## Exactly-once & delivery

**In-process classification.** For every request that reaches a terminal this contract
*classifies*, exactly one of three things happens: an authenticated route request (`/chat`,
`/chat/stream`, `/rag`, `/agent`) produces exactly one route event; an auth rejection
(401/403) produces exactly one `auth.rejected`; a malformed-JSON body that fails to parse
*before* `require_principal` ran produces zero events (there is no verified identity to
attribute one to). An out-of-contract exception — a genuine bug — deliberately falls outside
this classification altogether: zero events, original exception propagated (next heading).
The guarantee is therefore scoped to classified terminals, not to "every authenticated
request" unqualified — and even within that scope it has one narrow disclosed exception, a
streaming observer closed before its first iteration
([A known gap in the delivery chain](#a-known-gap-in-the-delivery-chain)). No request falls into two of these at once — the same-endpoint emission points and the
422 handler's own emission are mutually exclusive by construction (validation failure means
the route handler body was never entered).

| Path | Emission point | Covers |
|---|---|---|
| `/chat` | endpoint handler: a `try` with three `except` branches (each emits, then raises), falling through to a single success emission after the `try` block when none fires — no literal `finally`, but every exit emits exactly once | success, 400/404/429, 5xx — one outcome, one emit |
| `/chat/stream` | two-phase: pre-stream failures owned by the endpoint finalizer (before `StreamingResponse` is built); once the iterator transfers, `_audit_observed` (the post-transfer generator) owns the terminal | mutually exclusive — a request lands in exactly one phase |
| `/rag` | `RagService.answer()`, at its three terminals, guarded by `has_audit_context()` | matches the existing `rag stage=complete` diagnostic line's terminal points |
| `/agent` | `api/agent.py::agent_turn`'s finalizer, wrapping the whole call to `service.run_turn()` | 400 (including the service's own `validate_task()`), 404, 429, run error, success |
| `auth.rejected` | `require_principal`, the one place that sees both resolvers' rejections | both `headers` and `entra` modes |
| 422 | the `RequestValidationError` handler, guarded on `request.state.principal` being set | only fires when identity was already resolved and the parsed body then failed field/schema validation (a body that never parsed as JSON fails before identity exists — zero events) |

**A non-`UpstreamError` exception is an out-of-contract bug, not a case this system
classifies — a rule that is exact for `/chat`, `/chat/stream`, and `/rag`, and holds at the
route boundary only for `/agent` (framework-scope exception, next paragraph).** At each
route's own audit boundary, exception handling is written against the `UpstreamError`
hierarchy specifically (`except UpstreamError as exc:` in `/chat`, `/chat/stream`, `/agent`;
an `isinstance(exc, UpstreamError)` guard inside `/rag`'s broader diagnostic `except
Exception`). Anything else — a genuine programming error — is not caught for audit purposes
at that boundary: **no event is emitted, and the original exception propagates** to
Starlette's default handling (an unstyled 500, no error envelope, no audit line). A reader diffing
"requests the server actually served" against "audit events on disk" needs to know this
class of gap exists: it is not a missing feature, it is the deliberate line between "a case
this system classifies" and "a bug that should be loud, not silently reclassified as a
disconnect or a clean outcome."

One known boundary qualifies this rule. It holds at each route's own audit boundary, but
`/agent`'s framework adapter (`AgentFrameworkService.run()`, a Day 17 design) wraps *any*
exception raised inside the framework run — including a genuine bug in that scope — into
`AgentRunError`, which reaches the endpoint as an in-contract `upstream_error` (502) with
exactly one audit event. A bug raised outside that adapter scope (endpoint, service, or
store code) still propagates with zero events. As stated, the rule is exact for `/chat`,
`/chat/stream`, and `/rag`; for `/agent` it is a route-boundary rule with this
framework-scope exception.

**`/rag`'s service-level emission has a context guard.** Because `RagService.answer()` is a
plain method (not a request handler) that unit tests also call directly with no request in
flight, it emits only when `has_audit_context()` is true (the correlation middleware set a
start time and a correlation id for the current async context). A direct, non-request
invocation neither fabricates request fields nor raises — it behaves exactly as it did
before this event existed, and produces zero audit events.

**No durability claim.** These are process-lifetime guarantees only. A process crash between
"the outcome is known" and "the log line was flushed" loses the event; there is no retry, no
buffering, no acknowledgment. See [Out of scope](#out-of-scope).

### Commit truth versus delivery truth

The audit log's `committed`/`usage`/`status` fields describe what the *server* did, not what
the *client* received. On `/chat/stream`, the conversation store commits a turn **before**
the `message.done` terminal is handed to the SSE serializer (`_commit_on_done` in
`services/conversation.py`); the audit observer generator (`_audit_observed`) sees that same
`StreamDone` object as it passes through on its way to the client. If the socket drops
*after* that point, the commit has already happened and cannot be undone by a client that
never actually received the bytes — the event reports the terminal outcome as if delivery
succeeded, because from the server's side it did everything it promised.

| Cutoff | Audit event |
|---|---|
| Cancellation (consumer close or task cancellation) before the terminal is observed | `outcome="error"`, `error_code="client_disconnect"`, `committed=false`, `usage=null` |
| Cancellation after `StreamDone` is observed (commit decision already made) | Built from the terminal as normal — `committed`, `usage`, `status`, `model_version` reflect the known values. Whether `message.done` actually reached the client cannot be proven either way. |

The alternative — recording `committed=false` whenever the socket dropped, regardless of
cutoff — would have the log claim a turn wasn't saved when the store, in fact, has it: a
worse lie than the one this design accepts. **The audit log records commit truth, not
delivery truth**, and that is a deliberate, disclosed trade rather than an oversight.

`client_disconnect` names the *usual* cause, not a proven one. The emitting branch catches
both `GeneratorExit` and `asyncio.CancelledError`, and a `CancelledError` can also originate
server-side — process shutdown, task cancellation — not only from a dropped client. The
event proves the stream was cancelled before its terminal; it cannot prove which side
initiated the cancellation, and a reader inferring client behavior from these events should
know that.

### A known gap in the delivery chain

A real socket disconnect's `GeneratorExit` has to travel through a three-deep async
generator chain to reach `_audit_observed`, the generator that owns the emission decision:
`_render_sse` (the outermost, SSE-serializing generator that `StreamingResponse` iterates)
wraps `_audit_observed` (the audit observer) which wraps the events produced by
`_commit_on_done` (`services/conversation.py`, which performs the actual store commit before
yielding the terminal). Python's asyncio async-generator finalization is what is relied on
to propagate `GeneratorExit` down through all three frames when the outer `StreamingResponse`
gives up on a dead connection.

This propagation is **unit-tested only at the generator level** — the test suite drives
`_audit_observed` directly with `gen.aclose()` at a controlled point (see
`tests/unit/test_audit_streaming.py`), which is deliberate: an end-to-end test that kills a
`TestClient` connection cannot pin the exact cutoff point relative to `StreamDone` the way
calling `aclose()` by hand can — `aclose()` controls *where* the consumer close/cancellation
lands relative to the terminal, not *which side* initiated it — so the generator-level test
is the more precise one for the two-state cancellation logic above. What it does **not** cover is the real ASGI/Starlette machinery that
turns an actual dropped socket into a `GeneratorExit` reaching this three-deep chain in the
first place. This is recorded here as a known gap because it is a controller requirement,
not an optional nice-to-have: a reader relying on the disconnect accounting above should
know that the server-side generator logic is verified, but the trigger path from "the OS
noticed the socket closed" to "this generator's `finally` ran" is not independently proven
by anything in this repository.

The gap is not limited to disconnects. `_render_sse` (`api/streaming.py`) `return`s
immediately after yielding the `message.done` frame, without pulling a next value from
`_audit_observed` — so on a perfectly healthy 200, `_audit_observed` is left suspended at its
own `yield`, inside the `async for` loop, and the `else` branch that would emit success on
ordinary loop completion is unreachable through this endpoint. The success event is instead
emitted from the same `except (GeneratorExit, asyncio.CancelledError)` branch as a disconnect,
once asyncio's async-generator finalizer eventually closes the still-suspended generator.
`test_stream_success_exactly_one_event` proves the event arrives, and in practice it arrives
promptly — but it arrives *after* the response body has already completed, not synchronously
with it, and it rests on the same finalization machinery this section already flags as
unproven end-to-end. A hard process kill can lose a streaming success event exactly as
readily as it can lose a disconnect event; the finalizer reliance above covers both states,
not disconnect alone.

A narrower zero-event window sits at the other end of the stream's life. `_audit_observed`
is an async generator, so nothing in its body — including its exception handling — executes
until the first iteration. If the request is abandoned after the iterator was handed to
`StreamingResponse` but before the server pulls the first frame, the generator is closed
without ever having started: no branch runs, no event is emitted. This window loses an
event; it can never mis-classify one — but it is a genuine exception to "exactly one event
per classified terminal" for a request that got that far, and it is disclosed here rather
than papered over.

## Consuming the log

`configure_logging()` calls `logging.basicConfig()` with no explicit stream, which means
Python's default: **stderr**. Every reference to reading this log in this repository
therefore says "the process log stream," never "stdout" — piping only stdout silently drops
every line. The working incantation redirects stderr first:

```sh
your-command 2>&1 | grep '^{' | jq
```

`grep '^{'` isolates the audit lines from interleaved diagnostic `key=value` lines (the
`_AuditAwareFormatter` in `core/logging.py` gives audit records a bare-JSON format and
leaves every other logger's format alone, so the two coexist on one stream without a second
handler or log file).

**Level isolation.** `configure_logging()` explicitly calls
`logging.getLogger("audit").setLevel(logging.INFO)`. This matters because the root logger's
level is whatever `LOG_LEVEL` was configured with — `LOG_LEVEL=WARNING` silences INFO-level
diagnostic lines, and without the explicit call above the `audit` logger would inherit that
same threshold and go silent too. The audit trail's verbosity is deliberately decoupled from
diagnostic verbosity: turning down chatty debug logging must never turn off the audit trail
as a side effect.

**Correlation join.** Every audit event's `correlation_id` matches the `correlation_id=`
field stamped on every diagnostic `LogRecord` for the same request (Day 5), including the
`llm usage …` line ([cost-and-monitoring.md](cost-and-monitoring.md#metering-over-estimation))
and the prompt-attribution line (`llm call …`, Day 8) that logs the same
`prompt_name`/`prompt_version`/`prompt_sha256` the audit event's attribution fields carry.
Joining on this id lets a reader go from "here is the terminal outcome" to "here is every
stage that led to it" without the audit event needing to duplicate any of that detail
itself.

## Privacy & retention

`user_id` is a directory object id in Entra mode (the token's `oid` claim) or an
opaque client-supplied string in headers mode; treat it as **pseudonymous personal data**,
not as an inert request tag, the same guidance [api-conventions.md](api-conventions.md#logging)
already gives for diagnostic logs. `tenant_id` is comparatively low-sensitivity but still
identifies an organization.

This document makes **no retention promise**. Retention is a property of whatever log
pipeline actually hosts the process log stream in a given deployment — this repository
writes JSON lines to stderr and stops there. Day 27's Application Insights integration is
where a durable, retained, queryable sink is expected to land; until then, retention is
whatever the surrounding container/host log collection does with stderr, which this codebase
does not control and does not assume.

## Out of scope

- **No durable, tamper-evident sink.** This is JSON lines to the process log stream, full
  stop — not a database, not an append-only ledger, no hash chaining, no signature. A
  process crash between outcome and flush loses the event, and nothing here detects or
  proves that an event was altered or dropped after the fact.
- **No aggregation.** Spend-per-day, spend-per-prompt-version, and similar rollups are a
  Cost Management / Application Insights job (Day 27), the same boundary
  [cost-and-monitoring.md](cost-and-monitoring.md#known-gaps-disclosed-not-hidden) already
  draws for the `llm usage` line — audit events are attribution records, not a metrics
  store, and `grep`/`jq` is a reasonable way to inspect one event, not to aggregate a fleet
  of them.
- **A fake-mode sentinel is not evidence of a real Azure call.** `provider_call_attempted`
  means the provider *adapter boundary* was invoked — in fake mode that boundary is the
  deterministic fake adapter, not a network call. Fake-mode success events therefore still
  carry `provider_call_attempted=true`, with `deployment="fake"` and `model_version="fake"`
  as the sentinel attribution values (the same fake `PromptTemplate` instance the fake
  adapter holds is what the composition point hands to the attribution builder, so the
  values describe what the fake actually used, not a parallel hard-coded string). Reading
  `provider_call_attempted=true` in a log as proof that Azure OpenAI was reached is a
  misread unless the deployment is also known to be non-fake.
- **One-way import direction stays that way.** `core/audit.py` never imports `core/errors.py`
  — its builders take only primitives (strings, bools, the module's own dataclasses) so that
  the exception-to-primitive classification (`chat_upstream_audit_args`,
  `agent_upstream_audit_args`, both in `core/errors.py`) can depend on the audit schema
  without the audit schema ever depending on the error hierarchy back. This keeps the audit
  module usable from anywhere in the codebase without pulling in the whole error taxonomy,
  and is a boundary future changes should preserve rather than route around.
