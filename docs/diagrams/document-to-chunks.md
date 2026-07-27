# One document, many search documents

Chunking changes the unit of storage: the index holds chunks, not documents.
`parent_id` is what still makes a document addressable as a set — which is what
the replacement strategy in [../rag-indexing.md](../rag-indexing.md) needs.

```mermaid
flowchart TD
    doc["returns-policy.md<br/>front matter: doc_id, title,<br/>doc_type, tenant_id, effective_date"]
    doc --> s1["Section: Refund window"]
    doc --> s2["Section: Exceptions<br/>(over budget)"]
    s1 --> c0["chunk_id: returns-policy-0000<br/>parent_id: returns-policy"]
    s2 --> c1["chunk_id: returns-policy-0001<br/>parent_id: returns-policy"]
    s2 --> c2["chunk_id: returns-policy-0002<br/>parent_id: returns-policy<br/>(overlaps 0001)"]
```

Every chunk carries the same `title`, `doc_type`, `tenant_id`, and `effective_date` copied
from the source document's front matter — the fan-out multiplies the number of search
documents, not the number of distinct metadata values. `c1` and `c2` come from the same
oversized section, split in two; their overlap is sentence-aligned and internal to that
section, never copied from `s1`'s chunk. See
[The overlap contract](../rag-indexing.md#the-overlap-contract).
