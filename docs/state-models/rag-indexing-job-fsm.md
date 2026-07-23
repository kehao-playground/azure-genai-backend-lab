# RAG Indexing Job FSM

States follow the canonical indexing pipeline in [../rag-overview.md](../rag-overview.md): load → chunk → enrich → embed → persist. Enrichment is a distinct state because metadata failures are an independently observable failure boundary (bad metadata poisons filtering/ranking even when embeddings are fine).

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> LoadingDocuments
    LoadingDocuments --> Chunking
    Chunking --> Enriching
    Enriching --> Embedding
    Embedding --> Indexing
    Indexing --> Completed
    LoadingDocuments --> Failed
    Chunking --> Failed
    Enriching --> Failed
    Embedding --> Failed
    Indexing --> Failed
    Completed --> [*]
    Failed --> [*]
```
