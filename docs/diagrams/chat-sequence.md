# Chat Sequence

Extended Day 15: every request now resolves a `Principal` before touching `ConversationStore`, and
every store/lock key is `(tenant_id, conversation_id)` rather than a bare `conversation_id`.

```mermaid
sequenceDiagram
    participant Client
    participant Dep as require_principal
    participant API as FastAPI Backend
    participant Store as ConversationStore
    participant LLM as Azure OpenAI (store=false)

    Client->>Dep: POST /api/v1/chat {message, conversation_id?}<br/>X-Tenant-Id, X-Group-Ids?
    alt missing/malformed identity headers
        Dep-->>Client: 401 unauthorized (WWW-Authenticate: Bearer)
    else valid headers
        Dep-->>API: Principal(tenant_id, group_ids)
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

`/api/v1/chat/stream` follows the same `require_principal` step before the stream opens: a missing
or malformed principal is a pre-stream JSON 401 (the Day 6 two-stage error boundary), never an SSE
`error` event, and the tenant context stays set for the whole streamed response body.
