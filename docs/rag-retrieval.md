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

The service's own documentation is explicit that semantic ranking cannot "rerun the query over
the entire corpus": it reranks the top 50 of an existing result set ([semantic ranking
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

Both are bounded before a request is built. `top` must be between 1 and 1,000: "The default page
size is 50, while the maximum page size is 1,000. If you specify a value greater than 1,000 and
there are more than 1,000 results found in your index, only the first 1,000 results are returned"
([shape search results](https://learn.microsoft.com/en-us/azure/search/search-pagination-page-layout),
`ms.date` 2026-07-21, checked 2026-07). That last clause is the reason the ceiling is enforced
locally rather than left to the service: too large a `top` is not an error, it is a 200 answering
a different question than the one asked.

`vector_k` is required to be at least 1 and at most 2,147,483,647. The REST reference documents
the field as an `int32` — "Number of nearest neighbours to return as top hits" — and publishes no
limit on how many neighbours may be asked for ([Documents - Search
Post](https://learn.microsoft.com/en-us/rest/api/searchservice/documents/search-post), checked
2026-07). The declared type is still a bound, and it is the only one this repository may enforce:
a value outside `int32` is not the field being described, while a tighter ceiling would be a
number no source supports, and the next reader would cite it. That is why the two parameters are
bounded differently — `top`'s ceiling is published, `vector_k`'s is a consequence of its type.

Both are also required to *be* integers, which is not the same check as being in range. `bool` is
a subclass of `int` in Python, so `True` satisfies an `isinstance` test and every comparison
against a bound, then travels as the JSON literal `true` where the service documents a number. A
float clears the same comparisons and then means different things on each side: the fake slices a
list with it, the adapter puts a JSON float on the wire. Both clients apply one shared validator,
so neither can accept a call the other refuses — a fake that is more permissive than the service
turns a green suite into a production failure, and one that is stricter is a fake nobody can
develop against.

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
`k` is a small constant — the documentation describes the algorithm as performing best with `k`
at "a small value, such as 60". It is fixed inside the service, not a query parameter, and it is
"entirely separate from the `k` that controls the number of nearest neighbors" — the one this
repo sets as `DEFAULT_VECTOR_K`.

The exact form is worth measuring rather than assuming, because the scores this service returned
do not match the formula it documents. The scoring page gives the score as `1/(rank + k)`, "where
`rank` is the position of the document in the list", and its worked example counts positions
from 1. Running the four modes against this repo's sample corpus (25 chunks, 6 queries,
api-version `2026-04-01`) and recomputing every fused score from the keyword and vector rank
lists: all 150 hybrid rows agree, to the six decimal places the capture records, with

```
score = sum over lists of 1 / (60 + rank)     # rank counted from 0
```

while the documented 1-based form matches none of them. What the capture pins is the combination,
not either half alone — a constant of 60 over 0-based ranks is indistinguishable from 59 over
1-based ranks — and it pins that for the observed responses, not for an internal implementation
this repository has never seen.

Read that result with its scope attached. The agreement is to the six decimal places the
evidence records, not to full float precision. The input ranks were not read out of the service:
they come from separate keyword-only and vector-only calls issued with the same query text and
the same query vector, which is an inference about what the hybrid request fused, not a
subscore dump. And it is one region, one date, one API version, one static corpus, and one query
shape with two fused legs and default weights. That is observed behavior, not a contract Azure
has published.

The reconstruction is only possible at all because this corpus is no larger than `top` — 25
chunks against `top` 25 — so the separate keyword and vector runs return complete lists. On a
corpus larger than `top`, the inputs to the fusion are not fully observable from outside it.

The published figure cannot settle the question either way. `0.032786883413791656` is bit-for-bit
the IEEE-754 float32 value of `2/61`, which is what a document first in both lists scores under
the documented formula and what a document *second* in both lists scores under the 0-based form
reconstructed here. The page introduces it as the top result, but "top" there means top after fusion, and a
document second in both lists (`2/61 ≈ 0.0328`) outranks one that is first in only a single list
(`1/60 ≈ 0.0167`). Since the page never publishes the document's rank in each input list, both
readings remain open. Treat the example as illustrative of scale, not as evidence about the rank
convention.

A low RRF score is not a weak match; it is a different ruler. The service's own troubleshooting
guidance puts it plainly — a score of 0.03 can still indicate a strong match.

The practical consequence is that **no threshold survives a mode change**. A cutoff tuned against
`VECTOR` scores discards everything under `HYBRID`: a cosine floor of 0.333 sits above `2/60 ≈
0.0333`, which is the ceiling for the shape measured here — two equally weighted sources under
the 0-based reconstruction. Fuse more sources, or weight them, and the ceiling moves. A cutoff
drawn anywhere in the cosine range still excludes
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

## Where the REST vocabulary stops

Every request body in this document is built inside an adapter — the query body in
`services/azure_search.py`, the indexing, index-management and enumeration bodies in
`services/search_data_plane.py`. Nothing above them names a wire field. `models/search.py` knows
the four modes, the hit and the argument contract; the write-path orchestration in
`services/search_indexing.py` asks for "the chunks of this document, resuming after this key" and
never spells an OData filter.

The indexing write path is where that line is easiest to blur, because the request body is not
built per call: documents are grouped into batches first, against a document-count limit and a
byte ceiling, and the buffer that is measured has to be the buffer that travels. Both halves live
in the adapter. `plan_batches()` serializes the documents once, attaches the `@search.action` each
one needs, and yields batches; `post_batch()` sends a batch's buffer through as raw content. The
orchestration above passes what it is given straight back and reads only the keys — it says
`IndexingAction.UPSERT` or `IndexingAction.REMOVE` and never `upload`, `delete` or
`{"value": [...]}`. A batch is typed as a protocol carrying keys and nothing else, so a
replacement adapter is free to define its own, and the two functions are taken as a pair: a batch
means nothing to a transport that did not build it.

The line is drawn there so that replacing REST with the SDK, or one service with another, is a
change to one file rather than a search across the codebase for strings like `vectorQueries` or
`@search.action`. Whether the boundary actually holds is testable: the request bodies — query,
enumeration and indexing alike — are pinned byte for byte, so moving construction around cannot
quietly change what travels.

The adapters own their connection pool when they allocate it and never close one that was handed
in. Long-running tools open them in an `async with` block, which releases the pool at a point in
time the program chooses instead of whenever the object is collected.

## Access control is a query-time filter, not a separate check

`SearchClient.search()` takes a required `principal: Principal` argument with no default (Day 15).
There is no overload, keyword, or code path that issues an unfiltered query — "no authorization
context" is not a value this boundary can represent, so it cannot be reached by omission the way an
optional argument could be forgotten at a callsite.

`services/acl.py::build_acl_filter(principal)` is the single function that turns a `Principal` into
the OData `filter` clause `build_search_body()` sends on every request (query and vector alike):

```odata
tenant_id eq 'tenant-a' and (
  not allowed_groups/any()
  or allowed_groups/any(g: search.in(g, 'finance,support'))
)
```

Tenant match is unconditional; the group clause's public branch (`not allowed_groups/any()`) applies
to *every* principal regardless of its own groups, because an `allowed_groups: []` document is
tenant-wide readable by definition. When the principal itself carries no groups, the filter
simplifies to `tenant_id eq '...' and not allowed_groups/any()` — such a principal can only ever see
tenant-wide documents, never a group-scoped one. Escaping is a two-layer invariant, and only one
layer is handled by code. `escape_odata_literal()` (`'` doubled to `''`) covers the OData
string-literal grammar, and every literal embedded in the filter passes through it — the same
function indexing enumeration already uses, shared by `services/acl.py` and
`services/search_data_plane.py` rather than each carrying its own copy. But the group list travels
as a *comma-joined* second argument to `search.in`, whose value-list delimiter grammar (comma /
space) `escape_odata_literal()` does not touch (checked 2026-08,
[search-query-odata-search-in-function](https://learn.microsoft.com/en-us/azure/search/search-query-odata-search-in-function)).
That second layer is protected by the `Principal` identifier charset `[A-Za-z0-9_-]{1,64}`, which
forbids commas and whitespace — a deliberate dependency on the validation boundary, asserted by a
hostile-bypass unit test (`tests/unit/test_acl.py`) so that loosening the charset without revisiting
the delimiter contract fails loudly instead of silently corrupting the group list.

**`vectorFilterMode: preFilter` accompanies every vector query, explicitly, rather than relying on
whatever the index's creation-date-dependent default happens to be.** The GA alternative —
`postFilter` — runs the unfiltered HNSW traversal on each shard first, applies the filter to each
shard's *unfiltered local top-`k`*, then aggregates the survivors into the global top-`k` (checked
2026-08, [vector-search-filters](https://learn.microsoft.com/en-us/azure/search/vector-search-filters);
the preview `strictPostFilter` mode goes further and filters only after an unfiltered *global*
top-`k` is formed). Under `postFilter`, documents outside the caller's ACL consume local top-`k`
slots before the filter runs, so a tenant whose documents lose that per-shard neighbour race gets
fewer results, or none — false negatives, not the top-`k` of what it is actually allowed to see.
That reads as a relevance regression, not a bug, until someone traces it back to the filter mode —
and it worsens as the index accumulates documents belonging to *other* tenants, which is exactly
the shape a shared, multi-tenant index has by design. Pre-filtering applies the filter during each
shard's traversal, so the `k` neighbours returned are already scoped correctly. The trade-off is
performance, not correctness: Microsoft's own benchmarks show pre-filtering slower than
post-filtering as index size grows and filters become selective — this lab's corpus is far below
the scale where that matters, but the cost is real and unmeasured here.

`FakeSearchClient` does not parse OData — nothing in the fake ever builds a filter string. Instead it
enforces the policy via `is_document_visible(document, principal)`, a pure-Python predicate encoding
the same `tenant_id`/`allowed_groups` semantics that `build_acl_filter()` encodes in OData. Both
functions live in `services/acl.py`, which concentrates the review surface in one module — but they
remain two independent encodings of one policy: nothing mechanical prevents one from drifting if the
other changes. Their agreement is evidenced by the unit tests that exercise both against shared
cases and by the scoped live probes, which is bounded evidence, not a structural guarantee of
equivalence over all inputs. The fake applies visibility *before* scoring (matching the pre-filter
shape, never post-top-`k` discard), and still records `last_filter` — the OData expression the real
adapter would have sent for the same principal — purely as an assertion about the wire, not as what
was actually enforced in the test.

There is no "access denied" outcome anywhere in this pipeline. A document outside the caller's
tenant/group scope is excluded by the filter before it is ever scored, and it comes back
indistinguishable from a document that was never indexed at all — the same shape as FP1 (missing
content) in [rag-overview.md](rag-overview.md#failure-modes-design-inputs-not-afterthoughts). A
cross-tenant question therefore resolves as `status: "no_answer"`, exactly like an answer-absent
corpus, never as a distinct denial signal a client could branch on.

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
retried upsert has proved the key durable. Three contracts specific to a *live* search service
sit on top of that ordering and are new to this document.

**A per-`parent_id` critical section.** Two concurrent replacements of one document can each pass
their own gate and then delete each other's chunks. Every request returns 200, every page of
enumeration is correct, and nothing raises: the corruption is invisible to error handling, which
is what makes it worth a lock rather than a retry.

Cursor paging (`orderby chunk_id asc` with a strict `chunk_id gt '...'` filter) replaces `skip`,
whose results are not stable when the index changes underneath the walk. It removes displacement;
it does **not** provide snapshot isolation and is not what makes concurrency safe.

The lock's scope is one process. A deployment running multiple workers needs a durable lease, a
generation field on the document, or a compare-and-set on that generation.

The lock registry is reference-counted: an entry exists only while a job holds or awaits it, and
the last one out removes it. The naive `dict[str, Lock]` grows with the number of documents ever
indexed and never shrinks. The reclamation has to survive cancellation too — a job cancelled while
queued must drop its own reference without releasing a lock it never held and without removing an
entry someone else is still holding. That mistake is worse than the leak it fixes, because it puts
two jobs in the critical section and the damage surfaces nowhere near the lock.

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
2026-06-02, checked 2026-07). Documents carrying 1536-dimension vectors reach the byte limit
before the document count limit, so batching by count alone produces a 400 from code that looks
correct.

The batcher serializes each batch **once** and the transport sends that exact buffer — both sides
of that sentence are the same adapter, and what passes between them is the buffer itself rather
than the documents it was built from. Measuring with one serializer and transmitting with another
would mean the limit being guarded is not the limit that travels. A single document too large to
fit alone fails before anything is sent.
