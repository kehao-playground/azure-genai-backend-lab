# RAG Query Sequence

Day 14 milestone. `POST /api/v1/rag` — single-turn, `store=False`, no conversation
history and no token-budget ledger (that machinery is `/chat`-only, Day 7/Day 9).

```mermaid
sequenceDiagram
    participant Client
    participant API as API (rag.py)
    participant Retriever
    participant Embed as Embedding client
    participant Search as Azure AI Search
    participant RagService
    participant Chat as ChatService (rag_answer prompt)

    Client->>API: POST /api/v1/rag {question}
    API->>RagService: answer(question)
    RagService->>Retriever: retrieve(question)
    Retriever->>Embed: embed([question])
    Embed-->>Retriever: query vector
    Retriever->>Search: hybrid search (text + vector, top=RAG_TOP)
    Search-->>Retriever: SearchResult (hits, ranked)
    Retriever-->>RagService: SearchResult

    alt zero hits
        RagService-->>API: RagAnswer(status="no_answer", answer=None, usage=None)
        Note right of RagService: short-circuit — no LLM call
    else one or more hits
        RagService->>Chat: complete([{role: user, content: sources + question}])
        Note right of Chat: instructions = rag_answer.md (citation + refusal rules,<br/>sources marked non-instructions);<br/>sources travel as untrusted user-message data
        Chat-->>RagService: message, usage, incomplete_reason
        RagService-->>API: RagAnswer(status="answered", answer, hits, usage)
    end

    API-->>Client: RagResponse {answer, status, incomplete_reason, sources[], usage, correlation_id}
```

## Reading notes

- **No score threshold.** The zero-hits branch is the only structural no-answer
  gate. Day 13's live probe showed hybrid RRF scores cannot separate an
  answer-present corpus from an answer-absent one, so there is no cutoff to
  apply between "hits" and "answered" — grounding past that point is
  instructional (the prompt), not numeric.
- **Model refusal still returns `status: "answered"`.** If the LLM follows
  rule 3 of `rag_answer.md` and says the sources are insufficient, that is a
  successful generation from the pipeline's point of view — the honest gap is
  that a client cannot distinguish "grounded answer" from "polite refusal"
  by `status` alone; it has to read `answer` and the cited sources.
- **Sources are untrusted data, not instructions.** Retrieved chunks travel in
  the *user* message (`render_user_message`); the *instructions* live only in
  the prompt template, which explicitly tells the model to treat source text
  as non-instructions. This is mitigation, not immunity — a poisoned corpus
  entry crafted to look like an instruction is still on the threat model, not
  closed by this design.
- **No tenancy/authorization filter.** Retrieval searches the whole index;
  per-tenant or per-user scoping is deferred to Day 15.
- **`usage` and `incomplete_reason` still apply per call** even though there is
  no conversation and no budget ledger — `LLM_MAX_OUTPUT_TOKENS` caps this
  single call exactly as it caps a `/chat` turn.
