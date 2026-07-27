# One document, many search documents

Chunking changes the unit of storage: the index holds chunks, not documents.
`parent_id` is what still makes a document addressable as a set — which is what
the replacement strategy in [../rag-indexing.md](../rag-indexing.md) needs.

```mermaid
flowchart TD
    doc["returns-policy.md<br/>front matter: doc_id, title,<br/>doc_type, tenant_id, effective_date"]
    doc --> pre["Preamble prose<br/>(before the first heading)"]
    doc --> grp["Section: Refund window<br/>(grouping heading only —<br/>no prose of its own)"]
    grp -.->|"no prose &#8594; no chunk"| dropped(["dropped"])
    grp --> s1["Section: Refund window &gt;<br/>Standard purchases"]
    grp --> s2["Section: Refund window &gt;<br/>Promotional purchases"]
    doc --> s3["Section: Exceptions<br/>(over budget &mdash; split in three)"]
    doc --> s4["Section: How to start a return"]
    pre --> c0["chunk_id: returns-policy-0000<br/>parent_id: returns-policy<br/>heading_path: Returns Policy"]
    s1 --> c1["chunk_id: returns-policy-0001<br/>parent_id: returns-policy"]
    s2 --> c2["chunk_id: returns-policy-0002<br/>parent_id: returns-policy"]
    s3 --> c3["chunk_id: returns-policy-0003<br/>parent_id: returns-policy"]
    s3 --> c4["chunk_id: returns-policy-0004<br/>parent_id: returns-policy<br/>(overlaps 0003)"]
    s3 --> c5["chunk_id: returns-policy-0005<br/>parent_id: returns-policy<br/>(overlaps 0004)"]
    s4 --> c6["chunk_id: returns-policy-0006<br/>parent_id: returns-policy"]

    classDef dropped fill:none,stroke:#999,stroke-dasharray: 4 4,color:#999;
    class dropped dropped;
```

Every chunk carries the same `title`, `doc_type`, `tenant_id`, and `effective_date` copied
from the source document's front matter — the fan-out multiplies the number of search
documents, not the number of distinct metadata values.

"Refund window" is a grouping heading with no prose of its own — it only introduces two
deeper subsections — so it produces **no chunk at all**; this is the rule
[the chunking strategy](../rag-indexing.md#chunking-strategy) defends at length, using this
same heading as its example. Its two children, "Standard purchases" and "Promotional
purchases", each carry their own prose and each become one chunk (`0001`, `0002`). The
implicit preamble — the prose before the first heading — follows the same no-prose-no-chunk
rule, but here it has content, so it becomes `0000`, whose `heading_path` is the bare document
title rather than a nested path. "Exceptions" overflows `chunk_max_chars` and splits into
**three** chunks (`0003`–`0005`); each split boundary's overlap is sentence-aligned and carried
forward only from the piece immediately before it within that same section, never from an
unrelated section. See [The overlap contract](../rag-indexing.md#the-overlap-contract).
