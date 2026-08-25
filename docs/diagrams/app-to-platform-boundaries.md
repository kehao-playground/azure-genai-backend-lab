# App to Platform: Which Boundaries Move

Three boundaries could move out of this process, and one thing they have in
common is why: each has a contract that tests exercise. Entries, evidence, and
triggers are in [roadmap.md](../roadmap.md).

```mermaid
flowchart LR
    subgraph App["Today: one process"]
        direction TB
        API["api/<br/>contract · projection of errors"]
        ORCH["orchestration services<br/>conversation · rag · AgentTurnService<br/>scope · budget admission · commit · audit attribution"]
        PROV["provider adapters<br/>ChatService · SearchClient · EmbeddingClient"]
        AGT["agent adapter<br/>AgentService"]
        GOV["governance authorities<br/>prompt versions · token budget · audit"]
        API --> ORCH
        ORCH --> PROV
        ORCH --> AGT
        ORCH -.-> GOV
    end

    PROV ==>|"contract: Protocol<br/>two implementations since day 4"| GW["Model gateway<br/>platform-owned"]
    AGT ==>|"contract: AgentService Protocol<br/>the consumer stays behind"| WF["Agent workflow runtime"]
    GOV ==>|"contracts: closed schema · typed validation<br/>admission/ledger invariant"| GP["Governance plane"]

    API -.->|"no contract to move — this is the service"| STAY["stays"]
    ORCH -.->|"owes callers answers, not capabilities"| STAY
```

Double arrows are moves this repo has evidence for the *starting point* of —
the contract exists and tests exercise it. It has not made any of these moves,
and the world after a move is outside this lab's evidence entirely.

Dotted arrows to "stays" are the other half of the claim: `api/` is the service's
own contract, and the orchestration services own answers the application owes
its callers — conversation scope, budget admission, turn commit, audit
attribution. Neither becomes movable by being wanted elsewhere.
