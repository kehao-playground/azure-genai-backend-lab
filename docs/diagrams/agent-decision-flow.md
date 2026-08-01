# Agent Decision Flow

The decision procedure from the [Agent Decision Guide](../agent-decision-guide.md), as a flowchart. Deliberately self-referential: every edge in this graph is decided by a program rule — no node asks a model to select an action — which is exactly why this decision is a checklist, not an agent.

This English diagram is the semantic companion to the article's published figure. The publication PNG is rendered from a localized (zh-TW) Mermaid source tracked in the planning repo alongside the article assets; changes here must be mirrored there.

```mermaid
flowchart TB
    req["Request arrives"] --> q1{"Solved by one model call<br/>plus a good prompt?"}
    q1 -- "yes" --> direct["Direct model call<br/>(Days 5-10: /chat)"]
    q1 -- "no" --> q2{"Can program rules decide<br/>every edge at every step?<br/>(no model action selection)"}
    q2 -- "yes" --> pipe["Code-owned pipeline<br/>(Days 11-15: /rag)"]
    q2 -- "no" --> q3{"All five checklist answers<br/>written down?<br/>(concrete request / least-privilege tools /<br/>iteration limit / cost bound / guarantee trade)"}
    q3 -- "yes" --> agent["Single agent + tools<br/>(Days 17-18)"]
    q3 -- "no" --> back["The requirement is not real yet:<br/>go write the edge-selection rules"]
    back -.-> q2
    style direct fill:#d3f0d8,stroke:#2e7d32
    style pipe fill:#d3f0d8,stroke:#2e7d32
    style agent fill:#fff3cd,stroke:#b26a00
    style back fill:#fde3e0,stroke:#c62828
```

Reading notes:

- The first diamond is the Azure Architecture Center's lowest complexity level (direct model call); the guide cites the instruction to use the lowest level that reliably meets requirements (checked 2026-08).
- The second diamond is the guide's main criterion: edge/action-selection ownership. "Can you draw the flow as a fixed graph?" is deliberately **not** the question — an agent loop is also a fixed cyclic graph (candidate tools, `model → tool → model` edges, iteration limit, all drawable up front). What is dynamic at runtime is who picks the edge.
- The third diamond is the guide's five-item checklist; failing it routes back to writing edge-selection rules, not to an agent.
