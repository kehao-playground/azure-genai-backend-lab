# RAG: Two Pipelines

The indexing pipeline is the offline/asynchronous path (triggered by initial import, data changes, or a schedule); the query pipeline is the online path for each request that requires retrieval augmentation. See [../rag-overview.md](../rag-overview.md) for why the split matters.

This English diagram is the semantic companion to the article's published figure. The publication PNG is rendered from a localized (zh-TW) Mermaid source tracked in the planning repo alongside the article assets; changes here must be mirrored there.

```mermaid
flowchart LR
    subgraph indexing["Indexing pipeline (offline: initial import / data changes / schedule)"]
        A[Documents] --> B[Chunk] --> C[Enrich<br/>metadata] --> D[Embed]
    end
    E[("Search index<br/>(where the two pipelines meet)")]
    D --> E
    subgraph query["Query pipeline (online: every query that needs RAG)"]
        Q[User question] --> T[Text query<br/>BM25]
        Q --> QE[Embed query<br/>same embedding model] --> V[Vector query]
        T --> F[RRF fusion<br/>+ rerank]
        V --> F
        F --> G[Augment<br/>top-K chunks into prompt] --> L[Generate<br/>LLM answer]
    end
    E -. serves retrieval .-> F
```

Layout note: the Search index is deliberately a shared node *between* the subgraphs (not inside the indexing container) so the `Search index → RRF fusion` edge renders node-to-node; Mermaid clips cross-subgraph edges at container borders when both endpoints live inside `direction LR` subgraphs.
