# Diagrams

Mermaid-first diagrams: the reference architecture and the boundary maps, then
sequence diagrams for the main flows, then the operational diagrams — deployment,
observability, and the gates.

Diagrams with a `.zh-tw.mmd` sibling have a second, publication source: the
Traditional Chinese figure rendered into the iThome series. The two are the same
topology in two languages, and they move together.

## Architecture and boundaries

- [Reference Architecture](reference-architecture.md)
- [App to Platform: Which Boundaries Move](app-to-platform-boundaries.md) — which contracts could move out of this process, and which stay
- [Azure OpenAI Resource Hierarchy](aoai-resource-hierarchy.md)
- [Infrastructure Ownership Axes](infra-ownership-axes.md) — provisioning ownership versus full lifecycle ownership
## Flows and sequences

- [RAG: Two Pipelines](rag-two-pipelines.md)
- [One Document, Many Search Documents](document-to-chunks.md)
- [Entra ID Authentication Sequence](entra-auth-sequence.md)
- [Chat Sequence](chat-sequence.md)
- [Streaming Sequence](streaming-sequence.md)
- [Retrieval Stages](retrieval-stages.md)
- [RAG Query Sequence](rag-query-sequence.md) (placeholder)
- [Agent Decision Flow](agent-decision-flow.md)
- [Agent turn sequence](agent-turn-sequence.md) — one run, one turn, committed atomically
- [Agent Tool-Call Sequence](agent-tool-call-sequence.md) (placeholder)
- [Audit Event Exit Paths](audit-exit-paths.md)

## Deployment and operations

- [Container Shutdown Timeline](container-shutdown-timeline.md)
- [Container Apps Teardown Order](aca-teardown-order.md) — the order is the contract: assignments before principals
- [CI/CD Defense Boundaries](cicd-defense-boundaries.md) — what each of the five gates actually checks
- [Three Checks Pass, the New Revision Never Becomes Ready](revision-false-pass.md) — the false pass the readiness gate used to allow
- [The Checklist's Dividing Line](readiness-dividing-line.md) — machine-checkable versus a decision someone made

## Observability and evaluation

- [One RAG Request's Span Tree](request-lifecycle.md)
- [Three Timings of One Streamed Call](streaming-latency.md) — why a transport span is not generation latency
- [Eval: Two Assertion Layers, One Exit Code](eval-two-layers.md)
- [Eval: The Adjudication Evidence Chain](eval-evidence-chain.md)
