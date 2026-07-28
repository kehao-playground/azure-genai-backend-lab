# RAG retrieval

Day 12 built the indexing side. This document covers the query side — how a question becomes a
request, what comes back, and which failures belong to which stage — together with the write-path
work that lands alongside it in this milestone: replacing a document's chunks safely, and the
batching that replacement rides on.

## Retrieval is three stages that fail separately

| Stage | Decides | Controlled by | Cannot fix |
|---|---|---|---|
| Candidate generation | whether the answer is in the running at all | `vector_k`, BM25 text recall | nothing downstream recovers a candidate that was never retrieved |
| Fusion | how two candidate lists become one | Reciprocal Rank Fusion (hybrid only) | single-mode queries have no fusion stage |
| Ranking | the order within the list | semantic ranker (L2) | recall — it only sees what is already on the list |

The service's own documentation is explicit that semantic ranking "can't rerun the query over the
entire corpus": it reranks the top 50 of an existing result set ([semantic ranking
overview](https://learn.microsoft.com/en-us/azure/search/semantic-search-overview), `ms.date`
2026-04-24, checked 2026-07). So "should I turn on the semantic ranker?" is answerable only after
establishing whether the answer is in the candidate set. If it is not, the setting to change is
`vector_k` or the text query, not the ranker.

## Four modes

| Mode | `search` | `vectorQueries` | `queryType` |
|---|---|---|---|
| `KEYWORD` | yes | absent | absent |
| `VECTOR` | absent | yes | absent |
| `HYBRID` | yes | yes | absent |
| `HYBRID_SEMANTIC` | yes | yes | `semantic` + `semanticConfiguration` |

`query_text` is required in every mode. In `VECTOR` it is retained for logging and comparison but
deliberately not sent as `search` — sending it would silently make the query hybrid and quietly
invalidate any comparison.

`query_vector` is optional because keyword search must not be forced to buy an embedding it never
uses. That is a cost decision and a measurement one: a keyword latency figure that includes an
embedding round trip is not a keyword latency figure.

## `k`, `top` and 50 are three different numbers

- `vectorQueries[].k` — how many nearest neighbours the vector side offers.
- `top` — how many results come back overall.
- The semantic ranker's 50 — how many of the *merged* results it reranks ([semantic ranking
  overview](https://learn.microsoft.com/en-us/azure/search/semantic-search-overview), checked
  2026-07).

`k = 50` is the default because a vector side offering fewer than 50 starves the ranker. It does
not mean the semantic candidate count *is* 50, and it does not control the keyword side. The RRF
formula also contains a constant called `k`, unrelated to either; this codebase names its
parameter `vector_k` so the two cannot be confused in a log line six months from now.

## Two scores, and only one of them has a rubric

`SearchHit` carries `score` and `reranker_score` separately and never normalizes them.

| Mode | `score` | `reranker_score` |
|---|---|---|
| `KEYWORD` | BM25 | `None` |
| `VECTOR` | cosine-derived | `None` |
| `HYBRID` | RRF | `None` |
| `HYBRID_SEMANTIC` | RRF | 0.0–4.0 |

RRF scores are much smaller than similarity scores by construction. The service's own
documentation ([create a hybrid
query](https://learn.microsoft.com/en-us/azure/search/hybrid-search-how-to-query), [hybrid search
scoring](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking)) runs the same
query both ways and shows one hotel document scoring `0.8399121` under pure vector search and
`0.032786883413791656` under hybrid — the same document, two scales, roughly twenty-six times
apart (`0.8399121 / 0.032786883413791656 ≈ 25.6`). That follows from the algorithm: a cosine
score occupies 0.333–1.00, while each query fused by RRF contributes at most about `1/k`, where
`k` is a small constant the service documents as 60. The published hybrid figure is exact
corroboration of that constant rather than just documentation wording: for a document ranked
first in both the vector and keyword lists, RRF contributes `1/(k + 1)` from each list, and
`0.032786883413791656` is bit-for-bit the IEEE-754 float32 value of `2/61`. A low RRF score is
not a weak match; it is a different ruler. The service's own troubleshooting guidance puts it
plainly — a score of 0.03 can still indicate a strong match.

The practical consequence is that **no threshold survives a mode change**. A cutoff tuned against
`VECTOR` scores discards everything under `HYBRID`: a cosine floor of 0.333 sits above even the
two-query RRF ceiling of roughly 0.0328, so a cutoff drawn anywhere in the cosine range excludes
every hybrid result. It also fails silently: the query still returns, the results are still
ranked, there is simply nothing above the line. Only `reranker_score` has a published rubric to
threshold against.

`reranker_score` is the only one with a published meaning: 4.0 answers the question completely,
3.0 is relevant but incomplete, 2.0 partially addresses it, 1.0 answers a small part, 0.0 is
irrelevant ([semantic ranking
overview](https://learn.microsoft.com/en-us/azure/search/semantic-search-overview), `ms.date`
2026-04-24, checked 2026-07). Two consequences follow. First, **0.0 is a verdict, not a missing
value** — parsing it with `payload.get(...) or None` inverts the signal. Second, the distribution
shifts with infrastructure conditions and model updates — the service's own documentation gives
this as its reason for advising against granular thresholds ([semantic ranking
overview](https://learn.microsoft.com/en-us/azure/search/semantic-search-overview), checked
2026-07) — so thresholds built on it must stay coarse.

## Authentication

The lab uses an admin key: creating an index, uploading and deleting documents all require
management capability, and a key keeps an ephemeral session cheap to configure. Production should
use Microsoft Entra ID with role-based access control, assigning roles split by read and write
responsibility. The key is a `SecretStr`, is never logged, and never appears in a client-facing
message.

## Replacing a document's chunks

Replacement follows the upload → gate → enumerate → delete ordering and the fail-closed gate
documented in [Upload-then-delete-stale, gated](rag-indexing.md#upload-then-delete-stale-gated),
including the cross-attempt rule that lets a retryable failure settle as a success once the
retried upsert has proved the key durable. Two contracts specific to a *live* search service sit
on top of that ordering and are new to this document.

**A per-`parent_id` critical section.** Two concurrent replacements of one document can each pass
their own gate and then delete each other's chunks. Every request returns 200, every page of
enumeration is correct, and nothing raises: the corruption is invisible to error handling, which
is what makes it worth a lock rather than a retry.

Cursor paging (`orderby chunk_id asc` with a strict `chunk_id gt '...'` filter) replaces `skip`,
whose results are not stable when the index changes underneath the walk. It removes displacement;
it does **not** provide snapshot isolation and is not what makes concurrency safe.

The lock's scope is one process. A deployment running multiple workers needs a durable lease, a
generation field on the document, or a compare-and-set on that generation.

Deletion has its own terminal state for the same reasons as upload. A failed deletion is the safe
side — stale content survives rather than new content vanishing — but the replacement is reported
as incomplete, with the unresolved stale ids named. Safe is not the same as done.

## Batching

This is search-indexing batching — the request sent to Azure AI Search's upload/merge/delete
endpoint — not the 36-input embedding batching in [Embedding model and
batching](rag-indexing.md#embedding-model-and-batching), which bounds a different request to a
different endpoint. Two ceilings apply at once: at most 1,000 documents per indexing request, and
a 16 MB payload limit for the request as a whole ([service
limits](https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity), `ms.date`
2026-06-02, checked 2026-07). Documents carrying 1536-dimension vectors reach the byte limit well
before the document count limit, so batching by count alone produces a 400 from code that looks
correct.

The batcher serializes each batch **once** and the transport sends that exact buffer. Measuring
with one serializer and transmitting with another would mean the limit being guarded is not the
limit that travels. A single document too large to fit alone fails before anything is sent.
