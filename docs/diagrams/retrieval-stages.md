# Retrieval Stages

Companion diagram to [../rag-retrieval.md](../rag-retrieval.md): the same
three stages — candidate generation, fusion, ranking — drawn as one flow,
with each of the four modes marked by where it stops.

```mermaid
flowchart TD
    CORPUS[("Full corpus<br/>(everything indexed)")]
    CORPUS --> BM25["BM25 text recall<br/>(candidate generation)"]
    CORPUS --> VEC["Vector top-k<br/>(vectorQueries[].k,<br/>candidate generation)"]

    Q[Query] --> BM25
    Q --> QE["Embed query"] --> VEC

    BM25 -->|"KEYWORD stops here"| KTOP(["top returned"])
    VEC -->|"VECTOR stops here"| VTOP(["top returned"])

    BM25 --> RRF["RRF fusion"]
    VEC --> RRF

    RRF -->|"HYBRID stops here"| HTOP(["top returned"])

    RRF --> RERANK["Semantic rerank<br/>(reranks top 50 of<br/>the merged set)"]
    RERANK -->|"HYBRID_SEMANTIC stops here"| STOP(["top returned"])
    RERANK -.->|"cannot rerun the query<br/>over the corpus"| blocked(["corpus stays out of reach"])

    classDef blocked fill:none,stroke:#999,stroke-dasharray: 4 4,color:#999;
    class blocked blocked;
```

The dead-end edge out of the reranker is deliberate: the semantic ranker
"can't rerun the query over the entire corpus" — it only reorders whatever
candidate generation already produced. If the right chunk was never recalled
by BM25 or the vector query, no amount of reranking finds it; the fix is
`vector_k` or the text query, not `HYBRID_SEMANTIC`. See [Retrieval is three
stages that fail separately](../rag-retrieval.md#retrieval-is-three-stages-that-fail-separately).

`vector_k`, `top`, and the reranker's fixed 50 are three different numbers
that happen to share the same neighbourhood in this diagram: `vector_k`
bounds what the vector side offers into fusion, `top` bounds what any mode
finally returns, and 50 is a constant on the reranker's own input, independent
of both. See [`k`, `top` and 50 are three different
numbers](../rag-retrieval.md#k-top-and-50-are-three-different-numbers).
