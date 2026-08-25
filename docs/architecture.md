# Architecture

The reference architecture for this lab. It is shaped by three design questions rather than by a list of services:

1. **Where is nondeterminism isolated?** The smaller the isolation surface, the larger the testable surface.
2. **What data is allowed into prompts?** Anything that enters a prompt leaves your organization's boundary — a governance decision, not an implementation detail.
3. **What behavior must be observable?** Token usage, latency, and who-asked-what cannot be reconstructed after the fact.

## Layers

See the [reference architecture diagram](diagrams/reference-architecture.md).

### API layer (`api/`)

Authentication, input validation, rate limiting, and the `X-Correlation-Id` middleware. Its single job: **the public API contract must be more stable than the model.** Models, prompts, and retrieval strategies change; the request/response schemas and the error envelope (`{"error": {"code", "message"}, "correlation_id"}`) do not. Input length limits live here — the first cost gate.

### Orchestration services (`services/`, with DTOs in `models/` and prompt assets in `prompts/`)

Conversation state handling, prompt assembly, and routing decisions (plain chat vs. retrieval vs. tool calls), in `conversation.py`, `rag.py`, and `agent_turn.py`. This code is **fully deterministic**: same input and state produce the same prompt and the same routing. It is covered by ordinary unit tests with no real model involved.

Prompt assembly is centralized here on purpose — it is the only way to answer question 2, and the single place to apply data masking or filtering.

`agent_turn.py` is worth naming separately: `AgentTurnService` is this application's *consumer* of the agent framework, not a layer beneath conversation and RAG. All three are parallel orchestration services, and the answers this application owes its callers — conversation scope, budget admission, turn commit, audit attribution — belong to them, not to the runtime behind the adapter.

### Adapters (`services/`, same package, different responsibility)

`services/` holds two kinds of code, and the distinction matters more than the directory: the orchestration services above, and the adapters below. LLM calls, vector retrieval, and the agent runtime sit behind thin adapters whose interfaces we define (Python `Protocol`s — `ChatService`, `SearchClient`, `EmbeddingClient`, `AgentService`), not copies of SDK surfaces. Fake and real implementations are selected at one composition point (never `if use_fake` in handlers). Consequences:

- **Swappable**: changing model version or provider touches one file.
- **Testable**: fake adapters return fixed answers; the whole business-logic chain runs in milliseconds.
- **Measurable**: timeout, retry, and circuit-breaking are implemented once, here.

This is the cage for nondeterminism: only adapter internals are unpredictable; everything outside is conventional, testable code.

**Dependency rule: `services/` never depends on `api/`.** An adapter answers a protocol-shaped question (e.g. *was this token signed by a key this tenant publishes?*) and forms no opinion about HTTP — no FastAPI imports, no `HTTPException`, no request/response models. Mapping a failure to a status code, or claims to an application identity, is the API layer's job, done by depending on the adapter's return value, never the other way round. This keeps every adapter testable with plain data and no ASGI app in the loop (see `services/entra_jwt.py` and [entra-id-auth.md](entra-id-auth.md) for a worked example).

### Cross-cutting primitives (`core/`)

`core/` is not a layer in the request path; it is the primitives every layer uses: audit event schema, settings, correlation context, the error contract, structured logging, telemetry assembly, a keyed lock, and tenant context. Nothing here decides what to say to a model.

The token budget is the clearest example of why the distinction is worth keeping. It is one guardrail spread across three owners: **policy** is configuration (`core/config.py`), **admission and the ledger** belong to the orchestration services (`_check_budget` reads only committed totals; the ledger commits with the turn in the same all-or-nothing `ConversationStore.append(... usage_tokens ...)`), and the **HTTP and audit projection** — turning `TokenBudgetExceededError` into a 429 envelope and one audit event — belongs to `api/`. Input length limits, mentioned above, are a separate and simpler gate; they do not read the ledger.

### External dependencies and state

Azure OpenAI (model), Azure AI Search (retrieval), and conversation state storage sit outside the system boundary. The LLM API is stateless — "conversation memory" is an illusion the backend assembles from its own state store, and its location, retention, and access are the backend's responsibility.

### Observability plane (cross-cutting)

The correlation ID enters at the API layer, travels through every layer, and lands in telemetry (Application Insights) together with token usage, latency, and model version. Cost anomalies, quality regressions, and security incidents are all reconstructed from this line. Telemetry has a single assembly point (`core/telemetry.py`); `correlation_id` stays authoritative and a trace id is transport-level baggage attached to it, never a replacement. See [observability.md](observability.md) — including why an HTTP client span cannot stand in for generation latency while streaming.

### Audit plane (cross-cutting, and deliberately not the same thing)

Every validated request that reaches a terminal state inside the contract emits exactly one terminal event. The events carry ids, counts, and outcomes — never content: the system of record for what was said is the conversation store, not the log. The schema is closed and validated at the emission boundary rather than at a type hint, and it is exported to JSON Schema with a CI drift check. See [audit-logging.md](audit-logging.md).

Telemetry answers *how did this behave*; audit answers *what happened, and to whom it is attributable*. Collapsing them loses both.

### What is deliberately outside the application

Two things this repo ships are not layers of the service, and saying so is part of the architecture:

- **Evaluation** lives in `tools/`, not `src/`. The runner seeds its own corpus and asserts in two structurally separated layers, so a judged assertion cannot change an exit code. Nothing in the application imports it. See [evaluation.md](evaluation.md).
- **Deployment** is a boundary, not a layer: a multi-stage image running as a non-root user, one Container Apps revision, and a four-job pipeline that deploys by digest. The application's only contract with it is `/health` and its environment. See [docker.md](docker.md), [container-apps.md](container-apps.md), and [ci-cd.md](ci-cd.md).

## Rejected alternatives

- **Calling the SDK directly from handlers** — fastest path to a demo, and the path to a rewrite: nondeterminism everywhere (untestable), prompts unmanaged (ungovernable), observability bolted on too late.
- **Adopting an orchestration framework up front** — frameworks like LangChain solve real problems, but they outsource the boundary-drawing decision to someone else's abstractions. This lab draws its own boundaries with `Protocol` + adapters first (a few dozen lines) and re-evaluates frameworks when a concrete pain point appears.

## Scope

Fits: a single team, a single deployable, features growing from chat to RAG to agents — i.e., this lab. Does not fit: organizations with a platform-provided LLM gateway (the adapter layer moves out of the app), or one-off demos (three boxes and two arrows are genuinely enough).

Which of these boundaries would move first if this stopped being one team's single deployable — and what would have to be true before it was worth doing — is [roadmap.md](roadmap.md).

The layering itself adds no cloud cost (it is code structure, not deployment topology). The observability plane does have real cost — telemetry ingestion — managed within free-tier limits (see [cost-and-monitoring](cost-and-monitoring.md)).
