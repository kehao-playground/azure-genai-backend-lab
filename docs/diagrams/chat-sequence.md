# Chat Sequence

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI Backend
    participant Store as ConversationStore
    participant LLM as Azure OpenAI (store=false)

    Client->>API: POST /api/v1/chat {message, conversation_id?}
    alt conversation_id supplied
        API->>Store: get(conversation_id)
        Store-->>API: history (or 404 conversation_not_found)
    else omitted
        API->>API: issue new conversation_id
    end
    API->>LLM: Responses API: replay items (incl. encrypted reasoning) + new user input
    LLM-->>API: assistant reply
    API->>Store: append(transcript turn + replay items)
    Note over API,Store: turn-commit: both messages together,\nonly after success
    API-->>Client: ChatResponse {message, conversation_id, correlation_id}
```
