# Reference Architecture

High-level view of the lab's layers. Details and rationale in [architecture.md](../architecture.md).

```mermaid
flowchart TB
    Client["Client (Web / App)"]

    subgraph TrustBoundary["Trust boundary: only the gateway may set identity headers"]
        GW["Gateway<br/>strips/overrides any client-supplied<br/>X-Tenant-Id / X-Group-Ids,<br/>injects verified values"]
    end

    subgraph Backend["FastAPI Backend (reachable only via the gateway)"]
        direction TB
        APIL["API layer<br/>require_principal (401 + WWW-Authenticate) · validation · rate limit · correlation ID"]
        Orch["Orchestration layer (deterministic)<br/>conversation state (tenant-scoped) · prompt assembly · RAG/Agent routing"]
        subgraph Adapters["Adapter layer (cage for nondeterminism)"]
            direction LR
            LLMA["LLM adapter<br/>timeout · retry"]
            RetA["Retrieval adapter<br/>principal required, no default —<br/>builds the ACL filter, never an unfiltered query"]
        end
        APIL --> Orch --> Adapters
    end

    Client --> GW --> APIL
    LLMA --> AOAI["Azure OpenAI"]
    RetA --> Search[("Azure AI Search<br/>shared index, logical isolation —<br/>tenant_id + allowed_groups filter per query")]
    Orch --> State[("Conversation state store<br/>keyed by (tenant_id, conversation_id)")]

    Obs["Observability plane: Application Insights<br/>correlation ID · tenant_id · token usage · latency"]
    APIL -.-> Obs
    Orch -.-> Obs
    Adapters -.-> Obs
```

Solid arrows: runtime request flow. Dotted arrows: telemetry.

**Trust boundary (Day 15).** `X-Tenant-Id`/`X-Group-Ids` are trusted-gateway input, not
end-user-verifiable credentials: only the gateway may set them, and the backend must be unreachable
except through it. Sent straight to the backend, these headers are impersonation knobs, not
authentication. Isolation inside Azure AI Search is **logical, not physical** — every tenant's
chunks share one index, and the ACL filter derived from the request's `Principal` is the only thing
that separates them; there is no "access denied" signal, only documents that are filtered out and
therefore indistinguishable from documents that were never indexed. See
[api-conventions.md](../api-conventions.md#identity-and-tenancy-day-15) for the full contract.
