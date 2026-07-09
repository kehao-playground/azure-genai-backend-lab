# Architecture

The reference architecture for this lab. It is shaped by three design questions rather than by a list of services:

1. **Where is nondeterminism isolated?** The smaller the isolation surface, the larger the testable surface.
2. **What data is allowed into prompts?** Anything that enters a prompt leaves your organization's boundary — a governance decision, not an implementation detail.
3. **What behavior must be observable?** Token usage, latency, and who-asked-what cannot be reconstructed after the fact.

## Layers

See the [reference architecture diagram](diagrams/reference-architecture.md).

### API layer (`api/`)

Authentication, input validation, rate limiting, and the `X-Correlation-Id` middleware. Its single job: **the public API contract must be more stable than the model.** Models, prompts, and retrieval strategies change; the request/response schemas and the error envelope (`{"error": {"code", "message"}, "correlation_id"}`) do not. Input length limits live here — the first cost gate.

### Orchestration layer (`core/`, with DTOs in `models/` and prompt assets in `prompts/`)

Conversation state handling, prompt assembly, and routing decisions (plain chat vs. retrieval vs. tool calls). This layer is **fully deterministic**: same input and state produce the same prompt and the same routing. It is covered by ordinary unit tests with no real model involved.

Prompt assembly is centralized here on purpose — it is the only way to answer question 2, and the single place to apply data masking or filtering.

### Adapter layer (`services/`)

LLM calls and vector retrieval sit behind thin adapters whose interfaces we define (Python `Protocol`s), not copies of SDK surfaces. Fake and real implementations are selected at one composition point (never `if use_fake` in handlers). Consequences:

- **Swappable**: changing model version or provider touches one file.
- **Testable**: fake adapters return fixed answers; the whole business-logic chain runs in milliseconds.
- **Measurable**: timeout, retry, and circuit-breaking are implemented once, here.

This is the cage for nondeterminism: only adapter internals are unpredictable; everything outside is conventional, testable code.

### External dependencies and state

Azure OpenAI (model), Azure AI Search (retrieval), and conversation state storage sit outside the system boundary. The LLM API is stateless — "conversation memory" is an illusion the backend assembles from its own state store, and its location, retention, and access are the backend's responsibility.

### Observability plane (cross-cutting)

The correlation ID enters at the API layer, travels through every layer, and lands in telemetry (Application Insights) together with token usage, latency, and model version. Cost anomalies, quality regressions, and security incidents are all reconstructed from this line.

## Rejected alternatives

- **Calling the SDK directly from handlers** — fastest path to a demo, and the path to a rewrite: nondeterminism everywhere (untestable), prompts unmanaged (ungovernable), observability bolted on too late.
- **Adopting an orchestration framework up front** — frameworks like LangChain solve real problems, but they outsource the boundary-drawing decision to someone else's abstractions. This lab draws its own boundaries with `Protocol` + adapters first (a few dozen lines) and re-evaluates frameworks when a concrete pain point appears.

## Scope

Fits: a single team, a single deployable, features growing from chat to RAG to agents — i.e., this lab. Does not fit: organizations with a platform-provided LLM gateway (the adapter layer moves out of the app), or one-off demos (three boxes and two arrows are genuinely enough).

The layering itself adds no cloud cost (it is code structure, not deployment topology). The observability plane does have real cost — telemetry ingestion — managed within free-tier limits (see [cost-and-monitoring](cost-and-monitoring.md)).
