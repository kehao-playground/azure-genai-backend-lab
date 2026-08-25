# Roadmap

Where this lab goes next, and what would have to be true first.

Today it is one process. Everything below is about moving a boundary *out* of
that process — not about adding parts. Each entry names the contract that would
have to survive the move, the evidence this repo already has for that contract,
and the condition that would make the move worth its cost.

**Nothing below has been built or measured here.** The days after the move are
outside this lab's evidence: no gateway was selected, no cluster was run, no
governance plane was operated. These are directions with receipts for their
starting point, not results.

## 1. Model gateway — move the provider adapter out

- **What moves:** the provider adapter seam. `ChatService`
  ([`services/azure_openai.py:191`](../src/azgenai_lab/services/azure_openai.py))
  is a `Protocol` this app defines, not a copy of an SDK surface; `SearchClient`,
  `EmbeddingClient`, and `RagChatService` are shaped the same way.
- **Evidence for the boundary:** the contract has had two implementations since
  the day it was drawn (2026-07-13, day 4) — a fake and a real one, selected at
  one composition point, never with `if use_fake` in a handler. Both are
  exercised by the unit suite; the fake path is what makes the whole
  business-logic chain testable in milliseconds.
- **Trigger:** the platform — not this app — owns model routing, quota, or
  cross-team spend attribution. Until one of those has an owner outside the
  service, a gateway adds a hop and moves no decision.
- **Already anticipated:** [architecture.md](architecture.md) said so on
  2026-07-09, on day 2 of the series: *"organizations with a platform-provided
  LLM gateway (the adapter layer moves out of the app)"*. That sentence was
  written as a scope exclusion — the shape of deployment this lab does **not**
  fit. It reads as a roadmap entry only in hindsight.

## 2. Agent workflow — move the `AgentService` Protocol out

- **What moves:** `AgentService`
  ([`services/agent_framework.py:175`](../src/azgenai_lab/services/agent_framework.py)),
  and only that. It is the framework seam: an app-owned `Protocol` that an agent
  runtime satisfies.
- **What does not move:** `AgentTurnService`
  ([`services/agent_turn.py`](../src/azgenai_lab/services/agent_turn.py)) is this
  app's *consumer* of that Protocol. Conversation and RAG
  ([`services/conversation.py`](../src/azgenai_lab/services/conversation.py),
  [`services/rag.py`](../src/azgenai_lab/services/rag.py)) are parallel app-side
  orchestration services, not layers underneath it. Conversation scope, token
  budget admission, turn commit, and audit attribution stay here — they are
  answers this application owes its callers, not capabilities a runtime provides.
- **Evidence for the boundary:** drawn 2026-08-02 (day 17) as a contract before
  any runtime was wired to it, and pinned by
  [`tests/unit/test_agent_framework_api_surface.py`](../tests/unit/test_agent_framework_api_surface.py),
  which asserts the framework's API surface directly — iteration cap, client
  construction, per-run tools, multiple tool calls — so an upstream change to any
  of them fails here rather than in production.
- **Trigger:** more than one application needs the same agent runtime, or the
  runtime's release cadence stops matching this service's. One app with one agent
  does not need a workflow platform; it needs a Protocol, which it has.

## 3. Governance plane — move three authorities out

Three governance authorities live in this process today. None of them is a
Python `Protocol`, and that is the useful part: each has a different contract
form, and each is enforced rather than documented.

| Authority | Contract form today | Evidence | Trigger |
|---|---|---|---|
| Prompt versioning | Closed YAML front matter, validated fail-fast at startup: only `name`/`version`/`description`/`changelog` are accepted, `version` must be an `int` ≥ 1 — and a literal `true`/`false` is rejected, because `bool` is an `int` subclass ([`prompts/loader.py`](../src/azgenai_lab/prompts/loader.py)) | Drawn 2026-07-21 (day 8); 18 loader tests plus the logging tests that pin `prompt_name`/`prompt_version`/`prompt_sha256` on every upstream call | Prompts are edited by people who do not deploy this service, or the same prompt is served to more than one application |
| Token budget | An admission/ledger invariant. Admission reads only committed totals (`_check_budget`); the ledger commits *with the turn*, in the same all-or-nothing `append(... usage_tokens ...)`, so usage can never drift from the turn that incurred it ([`services/conversation_store.py`](../src/azgenai_lab/services/conversation_store.py)) | Drawn 2026-07-22 (day 9); 13 budget tests and 15 store tests | Budget is enforced per user or per team rather than per conversation — which needs an identity this service does not own |
| Audit | A closed schema plus an emission boundary: `emit_audit_event` validates through `AUDIT_EVENT_ADAPTER.validate_python` before anything is written, so a type hint is not the boundary — the call is ([`core/audit.py`](../src/azgenai_lab/core/audit.py)) | Drawn 2026-08-10 (day 22); 20 schema tests plus per-producer suites, and a JSON Schema export with a CI drift check | The log must outlive the process that wrote it, or be queried by someone who cannot read this service's stdout |

## 4. Deployment scale — Container Apps to Kubernetes

- **What moves:** the hosting boundary, not application code. The image already
  runs as a non-root user with a read-only virtualenv and answers `/health`; what
  changes is who schedules it.
- **Trigger:** something Container Apps does not express — node-level placement,
  a sidecar the platform team owns, or a scaling signal outside HTTP and KEDA's
  built-in scalers.
- **Not covered here:** cluster design, ingress, and workload identity are
  outside this lab's scope entirely. The one concrete thing this repo can say is
  a caveat, not a plan: the container's `HEALTHCHECK` serves local `docker run`
  and Compose only — Kubernetes does not execute it, and an operator has to point
  a probe at the same `/health` themselves. See [docker.md](docker.md).

## What "movable" means here

A boundary is movable when it has an explicit contract that tests exercise.
The form differs: a Python `Protocol`, a closed schema, typed validation, or an
admission/ledger invariant. Not every authority above is a `Protocol` — and that
is the point. The test is whether something outside can depend on the contract
without depending on the implementation, not whether the contract is spelled
with a particular Python construct.

The inverse is the useful warning. A capability with no contract does not become
movable by being wanted somewhere else; it becomes a rewrite.
