# App to Platform: Which Boundaries Move

Three boundaries could move out of this process, and one thing they have in
common is why: each has a contract that tests exercise. Entries, evidence, and
triggers are in [roadmap.md](../roadmap.md).

```mermaid
flowchart LR
    TODAY["Today: one process<br/>every filled node lives in it"]
    API["api/<br/>contract · projection of errors"]
    ORCH["orchestration services<br/>conversation · rag · AgentTurnService<br/>scope · budget admission · commit · audit attribution"]
    PROV["provider adapters<br/>ChatService · SearchClient · EmbeddingClient"]
    AGT["agent adapter<br/>AgentService"]
    GOV["governance authorities<br/>prompt versions · token budget · audit"]
    API --> ORCH
    ORCH --> PROV
    ORCH --> AGT
    ORCH -.-> GOV
    PROV ==>|"contract: Protocol<br/>two implementations since day 4"| GW["Model gateway<br/>platform-owned"]
    AGT ==>|"contract: AgentService Protocol<br/>the consumer stays behind"| WF["Agent workflow runtime"]
    GOV ==>|"contracts: closed schema · typed validation<br/>admission/ledger invariant"| GP["Governance plane"]
    API -.->|"no contract to move — this is the service"| STAY["stays"]
    ORCH -.->|"owes callers answers, not capabilities"| STAY
    %% No subgraph container on purpose: mermaid clips edges that cross a
    %% cluster border, which hid the source node of every arrow here — and
    %% which node moves is the whole claim (Day 30 review R15). Membership is
    %% carried by fill instead.
    classDef today fill:#efe8ff,stroke:#6b4fbb,stroke-width:2px
    classDef out fill:#ffffff,stroke:#6b4fbb,stroke-width:2px
    classDef marker fill:none,stroke:#999,stroke-dasharray: 4 4
    classDef lbl fill:none,stroke:none
    class API,ORCH,PROV,AGT,GOV today
    class GW,WF,GP out
    class STAY marker
    class TODAY lbl
```

Filled nodes are inside today's single process; unfilled ones are where a
boundary would land. There is no container box around the process on purpose:
mermaid clips edges at a cluster border, which hid the source node of every
arrow — and which node moves is the whole claim.

Double arrows are moves this repo has evidence for the *starting point* of —
the contract exists and tests exercise it. That is a prerequisite, not a proof:
it says nothing yet about the network boundary, partial failure, transactions
that no longer share a process, or the operational owner a move introduces. This
repo has not made any of these moves, and the world after one is outside its
evidence entirely.

Dotted arrows to "stays" are the other half of the claim: `api/` is the service's
own contract, and the orchestration services own answers the application owes
its callers — conversation scope, budget admission (conversation and agent
turns; `/rag` has no conversation ledger), turn commit, audit attribution.
Neither becomes movable by being wanted elsewhere.
