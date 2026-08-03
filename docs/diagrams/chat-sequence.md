# Chat Sequence

Extended Day 15: every request now resolves a `Principal` before touching `ConversationStore`, and
every store/lock key is `(tenant_id, conversation_id)` rather than a bare `conversation_id`.
Extended Day 19: the same dependency resolves that `Principal` either from trusted gateway headers
or from a verified Microsoft Entra ID access token, chosen once at startup from `AUTH_MODE`. The
JWT internals are not repeated here — see the
[Entra authentication sequence](entra-auth-sequence.md) and
[entra-id-auth.md](../entra-id-auth.md).

```mermaid
sequenceDiagram
    participant Client
    participant Dep as require_principal
    participant API as FastAPI Backend
    participant Store as ConversationStore
    participant LLM as Azure OpenAI (store=false)

    Client->>Dep: POST /api/v1/chat {message, conversation_id?}<br/>headers mode: X-Tenant-Id, X-User-Id, X-Group-Ids?<br/>entra mode: Authorization: Bearer
    alt credential missing, malformed or unverifiable
        Dep-->>Client: 401 unauthorized (WWW-Authenticate: Bearer)
    else verified, but lacks the required scope/role (entra mode)
        Dep-->>Client: 403 insufficient_scope
    else accepted
        Dep-->>API: Principal(tenant_id, user_id, group_ids)
    end
    alt conversation_id supplied
        API->>Store: get(tenant_id, conversation_id)
        Store-->>API: history (or 404 conversation_not_found)
        Note right of Store: a conversation_id from another tenant is<br/>indistinguishable from an unknown one — 404,<br/>never 403, no mutation on this failed lookup
    else omitted
        API->>API: issue new conversation_id (scoped to tenant_id)
    end
    alt token ledger >= CONVERSATION_TOKEN_BUDGET
        API-->>Client: 429 token_budget_exceeded (no upstream call)
    end
    API->>LLM: Responses API: replay items (incl. encrypted reasoning) + new user input, max_output_tokens
    LLM-->>API: reply + status (completed | incomplete) + usage {input, output, reasoning, total}
    API->>Store: append(tenant_id, conversation_id, transcript turn + replay items + usage tokens)
    Note over API,Store: turn-commit: messages and token ledger together,<br/>only after success, keyed by (tenant_id, conversation_id)
    API-->>Client: ChatResponse {message, conversation_id, correlation_id, usage, status, incomplete_reason?}
```

`/api/v1/chat/stream` follows the same `require_principal` step before the stream opens: a rejected
credential is a pre-stream JSON 401 or 403 (the Day 6 two-stage error boundary), never an SSE
`error` event, and the tenant/user context stays set for the whole streamed response body.
