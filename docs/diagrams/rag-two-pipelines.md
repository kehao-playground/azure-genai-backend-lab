# RAG: Two Pipelines

The indexing pipeline is the offline/asynchronous path (triggered by initial import, data changes, or a schedule); the query pipeline is the online path for each request that requires retrieval augmentation. See [../rag-overview.md](../rag-overview.md) for why the split matters.

This English diagram is the semantic companion to the article's published figure. The publication PNG is rendered from a localized (zh-TW) Mermaid source tracked in the planning repo alongside the article assets; changes here must be mirrored there.

```mermaid
flowchart TB
    subgraph indexing["Indexing pipeline (offline: initial import / data changes / schedule)"]
        direction LR
        A[Documents] --> B[Chunk] --> C[Enrich<br/>metadata] --> D[Embed] --> E[(Search index)]
    end
    subgraph query["Query pipeline (online: every query that needs RAG)"]
        direction LR
        Q[User question] --> T[Text query<br/>BM25]
        Q --> QE[Embed query<br/>same embedding model] --> V[Vector query]
        T --> F[RRF fusion<br/>+ rerank]
        V --> F
        F --> G[Augment<br/>top-K chunks into prompt] --> L[Generate<br/>LLM answer]
    end
    E -.serves retrieval.-> F
```
