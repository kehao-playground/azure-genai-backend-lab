# Reference Architecture

High-level view of the lab's layers. Details and rationale in [architecture.md](../architecture.md).

```mermaid
flowchart TB
    Client["Client (Web / App)"]

    subgraph TrustBoundary["Trust boundary: only the gateway may set identity headers"]
        GW["Gateway (AUTH_MODE=headers)<br/>strips/overrides any client-supplied<br/>X-Tenant-Id / X-User-Id / X-Group-Ids,<br/>injects verified values"]
    end

    subgraph Backend["FastAPI Backend (reachable only via the gateway)"]
        direction TB
        APIL["API layer<br/>require_principal (401 unauthorized / 403 insufficient_scope<br/>+ WWW-Authenticate) · validation · rate limit · correlation ID"]
        Orch["Orchestration services (deterministic, in services/)<br/>conversation · rag · agent_turn — parallel, not stacked<br/>conversation state (tenant-scoped) · prompt assembly · routing<br/>budget admission + ledger commit · audit attribution"]
        subgraph Adapters["Adapters (same package, cage for nondeterminism)"]
            direction LR
            LLMA["LLM adapter<br/>timeout · retry"]
            RetA["Retrieval adapter<br/>principal required, no default —<br/>builds the ACL filter, never an unfiltered query"]
            AgtA["Agent adapter (AgentService Protocol)<br/>runtime behind an app-owned contract"]
        end
        APIL --> Orch --> Adapters
    end

    Client --> GW --> APIL
    Client -->|"AUTH_MODE=entra: Authorization: Bearer<br/>(validated by the backend itself)"| APIL
    APIL -->|OIDC discovery / JWKS| Entra["Microsoft Entra ID"]
    LLMA --> AOAI["Azure OpenAI"]
    RetA --> Search[("Azure AI Search<br/>shared index, logical isolation —<br/>tenant_id + allowed_groups filter per query")]
    Orch --> State[("Conversation state store<br/>keyed by (tenant_id, conversation_id)<br/>system of record for content · token ledger")]
    AgtA --> Runtime["Agent runtime<br/>(Microsoft Agent Framework)"]

    Obs["Observability plane: Application Insights<br/>single assembly point (core/telemetry.py)<br/>correlation ID authoritative · trace id is baggage<br/>tenant_id · token usage · latency"]
    Aud["Audit plane: one terminal event per validated request<br/>ids · counts · outcome — never content<br/>closed schema, validated at the emission boundary"]
    APIL -.-> Obs
    Orch -.-> Obs
    Adapters -.-> Obs
    APIL -.-> Aud
    Orch -.-> Aud
```

Solid arrows: runtime request flow. Dotted arrows: telemetry and audit — two planes, not one. Telemetry answers *how did this behave*; audit answers *what happened, and to whom it is attributable*. Audit records ids, counts, and outcomes only: the content system of record is the conversation store. See [observability.md](../observability.md) and [audit-logging.md](../audit-logging.md).

**Trust boundary (Day 15, extended Day 19).** Two mutually exclusive identity sources sit behind
one dependency, chosen once at startup from `AUTH_MODE`.

In **headers mode**, `X-Tenant-Id`/`X-User-Id`/`X-Group-Ids` are trusted-gateway input, not
end-user-verifiable credentials: only the gateway may set them — it must strip or override all
three from any client-supplied request — and the backend must be unreachable except through it.
Sent straight to the backend, these headers are impersonation knobs, not authentication.

In **Entra mode** the backend validates a Microsoft Entra ID Bearer token itself, against the
signing keys it fetches from the configured tenant's OIDC metadata, and the `X-*` identity headers
are read by nothing. See [entra-id-auth.md](../entra-id-auth.md) and the
[authentication sequence](entra-auth-sequence.md).

Unchanged either way: isolation inside Azure AI Search is **logical, not physical** — every
tenant's chunks share one index, and the ACL filter derived from the request's `Principal` is the
only thing that separates them; there is no "access denied" signal, only documents that are
filtered out and therefore indistinguishable from documents that were never indexed. See
[api-conventions.md](../api-conventions.md#identity-and-tenancy) for the full contract.
