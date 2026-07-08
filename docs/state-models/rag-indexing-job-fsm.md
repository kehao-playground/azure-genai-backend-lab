# RAG Indexing Job FSM

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> LoadingDocuments
    LoadingDocuments --> Chunking
    Chunking --> Embedding
    Embedding --> Indexing
    Indexing --> Completed
    LoadingDocuments --> Failed
    Chunking --> Failed
    Embedding --> Failed
    Indexing --> Failed
    Completed --> [*]
    Failed --> [*]
```
