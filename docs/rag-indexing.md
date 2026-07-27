# RAG Indexing

Day 12 milestone (code-light tier). This document records the indexing-pipeline design from
[rag-overview.md](rag-overview.md): how a source document becomes chunks, how chunks become
vectors, and how vectors become an Azure AI Search index. The code lives in
`services/document_loader.py`, `services/chunking.py`, `services/embeddings.py`,
`services/indexing_results.py`, `models/rag.py`, and `models/search_index.py`. Day 13 wires
these contracts to a live search service; this milestone delivers the contracts and the pure
logic that does not need one.

## Chunking strategy

A chunk is a Markdown section, not a fixed-size slice. `chunk_markdown` in `services/chunking.py`
walks a document's headings, builds a `heading_path` for each one (the document title, then every
enclosing heading, joined by `" > "`), and treats each section's prose as one candidate chunk. Only
a section that does not fit `chunk_max_chars` is broken up further, and only then by the largest
available natural boundary: paragraph break first, sentence boundary second, and a hard character
cut as the last resort when no sentence boundary is close enough. The CJK sentence terminators
`。！？` are recognized alongside `.!?`, and the boundary check does not assume whitespace between
sentences — Chinese text has none, so a whitespace-based splitter would treat an entire paragraph
as one unbreakable word. That degradation order is exercised only in test fixtures; the shipped
corpus is English (see `services/document_loader.py`), and no CJK appears in this document either.

Section boundaries make citations meaningful. A chunk that always starts and ends on a Markdown
section boundary lets a citation point at "document, section" instead of "document, byte offset
N" — the same distinction Azure AI Search draws between fixed-size and variable-sized chunking
([chunk documents](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-chunk-documents),
checked 2026-07): "partition your data based [on] end-of-sentence punctuation marks,
end-of-line markers, or ... document structure" is called out as its own technique, distinct from
fixed-size chunking with overlap.

A heading with no prose of its own is dropped — including a grouping heading whose only content is
deeper subheadings. This is deliberate, not an oversight: no prose, no chunk, because a chunk with
nothing but a heading has nothing to search or cite. The heading is not lost, though. It survives
as a breadcrumb segment inside every descendant's `heading_path`, so "Refund window" (which has no
prose) is still findable through the `heading_path` of "Refund window > Standard purchases". The
implicit preamble section — text before the first heading — follows the same rule and is kept only
when it has something to say.

## The overlap contract

Overlap exists to carry a little context forward when a section is split into more than one
chunk. Four rules define it precisely (from the header of `services/chunking.py`):

1. **Overlap only occurs within one oversized section that has been split into multiple pieces.**
   Two adjacent sections that each fit the budget on their own never get an overlap between them —
   nothing links them but heading order.
2. **A section boundary resets overlap; content never repeats across sections.** A heading is
   already a semantic boundary, so copying content across it would only add noise, not context.
3. **`chunk_max_chars` bounds a chunk's final length, including the overlap tail and the heading
   prefix.** The budget check happens on `embedding_input`, not on `content` alone — see
   [Embedding input versus citation text](#embedding-input-versus-citation-text).
4. **Overlap is a target, aligned to sentence boundaries.** The tail carried forward is the
   longest sentence-complete suffix of the previous piece that fits within `chunk_overlap_chars`;
   only when no sentence boundary exists in that window does the splitter fall back to an exact
   character cut (`_sentence_aligned_tail` in `services/chunking.py`).

Rule 4 is a deliberate divergence from Azure AI Search's built-in Text Split skill, whose
`pageOverlapLength` parameter takes a fixed number of trailing characters regardless of where a
sentence ends ([chunk documents](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-chunk-documents),
checked 2026-07) — which routinely opens the next chunk mid-sentence. Since this splitter is
structural everywhere else (section boundaries, then paragraph, then sentence), its overlap is
structural too: a chunk should never open on a sentence fragment when a sentence boundary was
available to align to.

## Sizes are characters, not tokens

`chunk_max_chars` and `chunk_overlap_chars` are character counts, not token counts, and the
splitter never imports a tokenizer. This continues Day 9's series position — meter what the
provider reports, do not estimate — and it matches how Azure AI Search's own generally-available
splitter works: the Text Split skill's `maximumPageLength` and `pageOverlapLength` parameters are
measured in characters by default, and token-based chunking exists only in a preview API version
([chunk documents](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-chunk-documents),
checked 2026-07): "Token chunking is available in the latest preview version of
[Skillsets - Create or Update]... (REST API)." The GA surface counts characters.

The real safety margin is not the 2,000-character default — it is the embedding model's own input
ceiling. Two Microsoft Learn pages disagree on that ceiling's exact value:

- The chunk-documents page states: "the maximum length of input text for the Azure OpenAI
  text-embedding-3-small model is **8,191 tokens**"
  ([chunk documents](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-chunk-documents),
  `ms.date` 2026-06-08, checked 2026-07).
- The embeddings how-to page states: "The maximum input length for the current embedding models
  is **8,192 tokens**"
  ([embeddings how-to](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/embeddings),
  `ms.date` 2026-07-22, checked 2026-07).

This repo follows the embeddings how-to figure (8,192) as the owning document for embedding
request limits — it is the more recently dated of the two, and §[Embedding model and
batching](#embedding-model-and-batching) below derives a batch size directly from it. The
discrepancy is recorded here rather than silently resolved, the same way
[rag-overview.md](rag-overview.md) records the semantic-ranker free-tier conflict.

The chunk-documents page also states that "each token is around four characters of text for
common OpenAI models" — a ratio for English. It does not hold for Chinese, where one character is
commonly close to one token. `chunk_max_chars=2000` is comfortably under the 8,192-token ceiling
for prose in either writing system at the character densities this splitter produces, but the
project treats the character budget, not this ratio, as the enforced contract; the ratio is not
asserted anywhere in code.

## Embedding input versus citation text

Two fields carry text on a `Chunk` (`models/rag.py`) and they are not interchangeable:

- **`content`** is the source text a citation points at — what a reader is shown.
- **`embedding_input`** (a property: `heading_path + "\n\n" + content`) is what actually gets
  vectorized.

They differ on purpose. A chunk drawn from the middle of a long document still needs to carry
*which* document and *which* section it came from into the embedding space, or a query matching
that section's topic has no way to prefer it — the section's own prose alone might not mention the
document's subject at all. Folding the heading path into the vector's input solves that; folding it
into the displayed `content` would duplicate a heading the reader already sees rendered above the
citation.

`heading_path`'s first segment is always the document title (`"Returns Policy"` on its own, or
`"Returns Policy > Refund window > Standard purchases"` for a nested section) — enforced in
`Chunk.__post_init__`, which raises if `heading_path` does not begin with `title` as a complete
leading segment (a substring match would wave through `"Return"` against `"Returns Policy"`). This
rule exists because the embedding input is the only place the source document is named at all —
neither `content` nor any other embedded field repeats it.

One consequence follows from combining the two: the character budget that
`chunk_max_chars` enforces applies to `embedding_input`, not to `content` alone. A document with a
deep heading hierarchy has a shorter prose budget than one with a shallow one, because the
`heading_path` prefix and the `"\n\n"` join both count against the same ceiling
(`chunk_markdown`'s `budget = max_chars - len(heading_path) - len(_EMBEDDING_JOIN)`). Applying the
limit to `content` instead would let a deeply nested chunk's actual embedding request silently
exceed the ceiling.

## Metadata fields

Every field on `SourceDocument` (front matter, parsed by `services/document_loader.py`) is copied
onto every `Chunk` derived from it:

| Field | Source | Notes |
|---|---|---|
| `doc_id` / `parent_id` | front matter `doc_id` | Filename-as-identity: `doc_id` must equal the file's stem. Limited to 64 characters (see [Chunk ids and replacement](#chunk-ids-and-replacement)). |
| `title` | front matter `title` | Also the mandatory first segment of every chunk's `heading_path`. |
| `doc_type` | front matter `doc_type` | Free text; facetable in the index for scoping retrieval by document category. |
| `tenant_id` | front matter `tenant_id` | **Reserved for Day 15.** The field is populated and filterable now so that Day 15's multi-tenant filtering has something to filter on; no filtering logic exists yet. |
| `effective_date` | front matter `effective_date` (YAML date) | Filterable and sortable, so a stale document's chunks can be excluded from being treated as a current answer. |
| `heading_path` | derived during chunking | Always starts with `title`; see [Embedding input versus citation text](#embedding-input-versus-citation-text). |
| `content` | derived during chunking | The citation text; see above. |

Front-matter parsing is strict and closed-set (`_REQUIRED_FIELDS` in `document_loader.py`): all
five fields are required, no unknown field is tolerated, and `doc_id` must match the filename. This
mirrors how Day 8 validates prompt template front matter — fail at load time, not at index time.

## Embedding model and batching

Embeddings come from `text-embedding-3-small`, deployed as `embed-small` (matching the existing
`chat-mini` naming), called through `AzureOpenAIEmbeddingClient` in `services/embeddings.py` — the
same v1 GA surface as the chat adapter, but a separate `/embeddings` endpoint billed on its own
per-token rate, not part of the Responses API. `EMBEDDING_DIMENSIONS = 1536` is a module constant
in `models/search_index.py`, not a `Settings` field: the index schema, the embeddings request's
`dimensions` parameter, and the response-length check all read the same constant, so they cannot
silently disagree at runtime.

Three request limits govern batching, all documented on the same page as the token-ceiling figure
this repo follows
([embeddings how-to](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/embeddings),
checked 2026-07):

- At most **2,048** inputs per request ("the maximum array size is 2,048").
- At most **8,192** tokens per individual input ("the maximum input length for the current
  embedding models is 8,192 tokens").
- At most **300,000** tokens summed across every input in one request — a request over this
  aggregate can fail even when every individual input is legal ("Embedding requests return HTTP
  400 when the sum of input tokens exceeds 300,000, even if every individual input is under the
  per-input limit").

Limiting a batch to 2,048 inputs is not sufficient on its own: 2,048 individually-legal inputs can
still add up past the 300,000-token aggregate. Without a tokenizer, there is no way to measure that
sum directly, so `MAX_BATCH_INPUTS` in `services/embeddings.py` is derived from the two numeric
ceilings instead of the input count:

```
floor(300_000 / 8_192) = 36        36 * 8192 = 294,912 <= 300,000
                                    37 * 8192 = 303,104 >  300,000
```

36 is a provable upper bound — any smaller number would need its own justification, so none is
added. The cost of this approach is stated rather than hidden: the per-input 8,192-token ceiling is
still not verified ahead of time (a 2,000-character chunk could, at an unusually high token
density, exceed it), so a violation is *detected* by the upstream 400, not *prevented* locally. That
is the accepted cost of not depending on a tokenizer.

`FakeEmbeddingClient` produces deterministic vectors from a SHA-256 hash of the input text. These
vectors carry no semantics — two chunks about the same topic are no closer together than two
unrelated ones — and exist only to exercise batching, wiring, and the dimension contract.
Retrieval quality measured against the fake means nothing; that evaluation needs the real model
(Day 13). `build_embedding_client()` is the one composition point that chooses between the fake and
the real adapter; no handler branches on `use_fake_embeddings`.

## Index schema

`models/search_index.py` is the single source of truth for the index definition, exported to
`docs/search/index-schema.json` by `tools/export_index_schema.py` and drift-checked in CI the same
way `docs/openapi/openapi.yaml` is (see [docs/search/README.md](search/README.md)). One chunk is
one search document:

| Field | Type | Attributes | Rationale |
|---|---|---|---|
| `chunk_id` | `Edm.String` | key, filterable | One chunk is one search document; this is its identity. |
| `parent_id` | `Edm.String` | filterable | Points back to the source document; reindexing computes stale chunks from it. |
| `title` | `Edm.String` | searchable, filterable | Shown alongside a citation. |
| `heading_path` | `Edm.String` | searchable | The section-level granularity of a citation. |
| `content` | `Edm.String` | searchable, `en.microsoft` analyzer | The keyword (BM25) side of hybrid search. |
| `doc_type` | `Edm.String` | filterable, facetable | Scopes retrieval by document category. |
| `tenant_id` | `Edm.String` | filterable | Reserved for Day 15's multi-tenant filtering. |
| `effective_date` | `Edm.DateTimeOffset` | filterable, sortable | Keeps an expired document from being treated as a current answer. |
| `content_vector` | `Collection(Edm.Single)` | searchable, `stored: true`, `retrievable: false`, `dimensions: 1536` | The vector side of hybrid search; see [`stored` is the vector's only backup](#stored-is-the-vectors-only-backup). |

`vectorSearch` defines one `hnsw` algorithm (`metric: cosine`, matching Azure OpenAI embeddings)
and one profile that `content_vector` references.

Three service constraints shape this schema, all confirmed on the vector-index how-to page
([create a vector index](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-create-index),
`ms.date` 2026-01-14, checked 2026-07):

- **A vector field must be `searchable: true`, and must not be `filterable`, `facetable`, or
  `sortable`.** ("`filterable`, `facetable`, and `sortable` must be false.") Any filtering on the
  chunks — including Day 15's tenant isolation — has to ride on a parallel scalar field that exists
  from the first build; there is no way to filter *on* the vector itself.
- **`stored` can only be set at index creation and cannot be changed afterward.** ("The `stored`
  property is set during index creation on vector fields and is irreversible.")
- **Dimensions are fixed by the model, within a range.** `text-embedding-3-small` supports 1 to
  1536 dimensions via Matryoshka Representation Learning truncation ("`text-embedding-3-small`
  ranges from 1 to 1536"); this project takes the full width. Changing the dimension count later
  means a different embedding space — see [Four irreversible
  decisions](#four-irreversible-decisions).

## `stored` is the vector's only backup

`content_vector` is defined with `stored: true` and `retrievable: false` — the service's own
default combination, not a choice this repo had to justify from scratch. `stored: true` keeps a
retrievable-if-toggled copy of the source vector alongside the internal vector index that queries
actually use.

Setting `stored: false` instead would save up to half that field's disk. The cost is worse than
the saving: it is irreversible ("Setting the `stored: false` attribution is irreversible... If you
want retrievable vector content later, you must drop and rebuild the index"), and it silently
breaks partial updates — the storage-options page states it plainly: if a `merge` or
`mergeOrUpload` on an existing document does not include the vector field, "the vector data is
lost without an error or warning"
([eliminate optional vector instances](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-storage-options),
`ms.date` 2026-04-27, checked 2026-07). No indexing-result status code reports this failure mode —
from the service's point of view, nothing failed.

`stored: true` is also the only escape hatch from paying the embedding bill again on a schema
rebuild that does not touch the vector space itself (see [Four irreversible
decisions](#four-irreversible-decisions)): with the source vector retained, `retrievable` can be
flipped to `true` — a change the reindex how-to page lists as requiring no rebuild at all — the
existing vectors exported, and then loaded straight into the new index alongside the scalar
fields. `stored: false` forecloses that path permanently. The 50% storage saving that `stored:
false` offers is, in effect, the cost of giving up your only vector backup.

## Four irreversible decisions

Every decision below asks the same question: after this project ships, what does it cost to
change your mind?

| # | Decision | Cost of changing it |
|---|---|---|
| 1 | Chunk boundaries (chunking strategy or `chunk_max_chars`/`chunk_overlap_chars`) | Citations point at different text than what was originally linked; already-published source links may no longer match what a reader sees. |
| 2 | Metadata field set | Adding a field needs no rebuild — existing documents get a `null` until the next reindex backfills it (a rebuild-free change per the reindex how-to page). Changing a field's *attributes* (`searchable`/`filterable`/`sortable`/`facetable`, analyzer assignment, data type) needs a drop and full rebuild. |
| 3 | Embedding model or dimensions | A different model or a different dimension count is a different embedding space — old and new vectors are not comparable, so every chunk must be re-embedded. |
| 4 | Index schema field definitions (name, type, `searchable`/`filterable`/`sortable`/`facetable`, analyzer assignment) | Drop and rebuild the index, then reload every document into it. |

Row 4 carries a distinction worth stating precisely, because it is easy to overstate: **a schema
rebuild always requires reloading every document, but it requires re-embedding only when the
embedding model, the dimensions, the embedding input text, or the chunk boundaries have changed —
or when the existing vectors were not kept.** Because this schema keeps `stored: true`, the
existing vectors normally *are* kept, so a rebuild triggered purely by a scalar-field change (a
new analyzer assignment, say) can reload the scalar fields from source while carrying the already-
computed vectors across, rather than paying for embeddings a second time. Row 3 is the only row
where the vectors themselves are invalid and must be recomputed regardless of what was stored.

It is equally easy to overstate row 2 in the other direction. Not every schema change forces a
rebuild. The reindex how-to page lists changes that apply with no rebuild at all
([update or rebuild an index](https://learn.microsoft.com/en-us/azure/search/search-howto-reindex),
`ms.date` 2026-01-20, checked 2026-07): adding a new field, adding an index description, setting
`retrievable` on an existing field, updating `searchAnalyzer` on a field that already has an
`indexAnalyzer`, adding a new analyzer definition, and adding, updating, or deleting scoring
profiles, synonym maps, semantic configurations, or CORS settings. Row 2 in the table above refers
specifically to changing an *existing* field's core attributes or type — the case the same page
says needs "a full rebuild."

## Chunk ids and replacement

`make_chunk_id` in `models/rag.py` derives a chunk's key from its position:
`f"{parent_id}-{ordinal:04d}"`. Document keys are constrained by Azure AI Search's own naming
rules ([naming rules](https://learn.microsoft.com/en-us/rest/api/searchservice/naming-rules),
checked 2026-07): at most 1,024 characters, letters/digits/`-`/`_`/`=` only (anything else must be
URL-safe Base64 encoded), and the first character may not be `_`.

That rule constrains the *final* `chunk_id`, not the authored `doc_id` — a distinction validation
gets right in two stages:

1. **`doc_id` is capped at 64 characters** (`DOC_ID_MAX_LENGTH` in `models/rag.py`) and checked
   against the same character-set rule. A document key limit of 1,024 characters is generous for a
   computer-generated identifier, but `doc_id` is authored by a person in front matter; holding it
   to a human scale (64 characters) means a naming mistake surfaces immediately as an authoring
   error, not as a capacity problem discovered later.
2. **The derived `chunk_id` is validated again, in full**, inside `Chunk.__post_init__`
   (`validate_document_key`, shared with `parent_id`). This catches what stage 1 alone cannot: a
   `doc_id` that is itself a legal, maximum-length key (1,024 characters) would produce a `chunk_id`
   five characters longer once `-0000` is appended — an illegal key that stage-1 validation, run
   only against `doc_id`, would never see.

Validating both stages against the same rule set — rather than trusting front matter — means a bad
key is caught at load or chunk time, not the first time it is uploaded to a live service.

Chunk ids are position-based rather than content-hashed on purpose: a hash would be reproducible
and collision-resistant, but nothing in a portal view would tell a reader which document a chunk
came from. A position-based id like `returns-policy-0002` is self-describing at the cost of
stability — editing one line near the top of a document shifts every later chunk's ordinal, so
that document's *entire* previous chunk set becomes orphaned rather than partially valid. This is
why a document's chunks must always be replaced as a set, never patched chunk-by-chunk (see
[Failure handling](#failure-handling)). One consequence of the `{ordinal:04d}` format is worth
naming: past 10,000 chunks in a single document, the ordinal grows to five digits and the id
remains legal and unique, but chunk ids stop being lexicographically sortable in ordinal order —
not a concern for this project's sample corpus, but a real one for a much larger document.

## Failure handling

Reindexing a document is not a single atomic operation against Azure AI Search — a batch upload
can partially fail — so the replacement strategy has to define, precisely, what state is left
behind when something goes wrong partway through.

### The stage gate

`load → chunk → embed` must all succeed before anything touches the index. An embedding failure
(see [The embedding 400 is not an indexing 400](#the-embedding-400-is-not-an-indexing-400)) aborts
the whole document before any mutation is attempted: the index is left exactly as it was, rather
than half updated.

### Upload-then-delete-stale, gated

Replacing a document's chunks does not delete the old ones first. Deleting first and re-uploading
afterward would be unsafe: if the delete succeeds and the upload then hits a partial failure (a
207 with some documents rejected, a 429, a 503), the document is left with *fewer* chunks than it
should have — content missing from the index, invisibly, until the next successful run. Instead:

1. Compute the new chunk set and its `chunk_id`s.
2. **Upload** (upsert semantics) every new chunk.
3. **Gate**: proceed to step 4 only if every uploaded chunk's individual result was a success —
   evaluated by `may_delete_stale()` in `services/indexing_results.py`.
4. Look up the existing `chunk_id`s indexed under this document's `parent_id`, and delete whichever
   of them are **not** in the new set.

The gate in step 3 is what makes this ordering safe, and it is enforced strictly:
`may_delete_stale()` returns `True` only when every expected key came back exactly once, nothing
unexpected came back, and every one of them succeeded. Any permanent failure, any exhausted retry,
a duplicate key in the response, or a missing key all block the gate outright — the function does
not attempt to deduplicate or partially credit a retry's later success, because doing so could let
a retried success paper over an earlier, unresolved failure for the same key. When the gate does
not open, no old chunk is deleted, and the document is reported as needing a re-run.

### Per-document status codes

An indexing batch response answers `200` when every document in it succeeded and `207` when at
least one did not — and a `207` is still a *successful* HTTP response
([update or rebuild an index](https://learn.microsoft.com/en-us/azure/search/search-howto-reindex),
checked 2026-07): "Status code 200 is returned for a successful response, meaning that all items
have been stored durably... Status code 207 is returned when at least one item wasn't successfully
indexed." A caller that only checks for an exception or a 2xx status will treat a `207` — a partial
failure — as if it were a complete success. `classify()` in `services/indexing_results.py` exists
because every `207` must be read document by document, using each entry's own `status` and
`statusCode`:

| `statusCode` | Disposition | Handling |
|---|---|---|
| 200 / 201 | Succeeded | — |
| 400 | Permanent | Not retried; malformed document, logged with its `chunk_id` and `errorMessage`. |
| 404 | Permanent | Merge target not found; this project only uploads, so this indicates a logic error if it ever occurs. |
| 409 | Retryable | Concurrent write to the same document; serialize or back off. |
| 422 | Retryable | Index temporarily unavailable (`allowIndexDowntime`). |
| 429 | Retryable, and alerted separately | Usually a storage-capacity signal, not a rate-limiting one — worth its own alert. |
| 503 | Retryable | Service overloaded; back off before retrying. |

`classify()` also treats a contradictory result — `status: false` paired with a success status
code — as a failure: the service's own `status` boolean is authoritative, and the safe reading of
a contradiction is the pessimistic one. Any status code not in this table is classified `Permanent`
on the same reasoning: retrying an unrecognized failure forever is worse than stopping and
surfacing it.

Indexing batches are themselves bounded: at most 1,000 documents or about 16 MB per batch,
whichever limit is reached first
([service limits](https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity),
`ms.date` 2026-06-02, checked 2026-07): "Supported maximum payload limit is 16 MB for indexing...
Supported maximum 1,000 documents per batch of index uploads, merges, or deletes." This project's
sample corpus never approaches either limit; the constraint matters for a corpus large enough to
need multiple batches per document set.

### Idempotent re-runs and the non-atomic window

Because upload is an upsert and delete-stale operates on a set difference, the entire replacement
sequence can be re-run safely after a failure: re-running step 2 re-uploads the same new chunks
without duplicating them, and step 4 recomputes which stale chunks remain. A failed run leaves the
index queryable throughout — never with a gap — because the old chunks are only ever removed after
the gate confirms the new ones are all present.

The residual risk this strategy accepts is duplication, not loss: between step 2 completing and
step 4 completing, a query can retrieve both the new chunk for a section and the stale chunk it is
replacing. **This window is scoped to the single-index, no-alias strategy used in this repo** — it
is not an inherent limitation of Azure AI Search. An index alias pointed at a generation-suffixed
index (build the new index fully, then repoint the alias) would let readers see either the fully
old or fully new state and nothing in between; that is the production escape from this window, and
it is out of scope here (see the non-goals in the design record). Within this repo's strategy, the
choice being made is deliberate: a document that briefly shows duplicate content is recoverable by
a re-run, while a document that briefly shows *no* content is not an acceptable failure mode.

## The embedding 400 is not an indexing 400

Two very different failures both eventually surface as "something went wrong with a chunk," and
they must not be read through the same table. They live in different modules, at different points
in the pipeline, and with different response shapes:

| | Embedding request rejection | Search indexing failure |
|---|---|---|
| Timing | Embed stage, **before** any mutation to the index | **After** chunks have been uploaded to Search |
| Response shape | One request-level error object for the whole batch — no per-input index or status | One `status` / `statusCode` entry per document, in the batch response's `value` array |
| Can it name the offending chunk? | No — a 400 on a 36-input batch cannot say which input caused it | Yes — each document's own entry names it |
| Owning module | `services/embeddings.py` (`EmbeddingRejectedError`) | `services/indexing_results.py` (`classify()`) |
| Retryable? | No — `EmbeddingRejectedError.retryable = False`; a 400 means resending the same request cannot succeed | Depends on `statusCode` (see the table in [Failure handling](#failure-handling)) |

When an embeddings request is rejected, `embed_chunks()` in `services/embeddings.py` logs every
`chunk_id` that was in the failed batch alongside the upstream `request_id`, then raises
`EmbeddingRejectedError` and abandons the whole document before the stage gate ever lets it reach
Search. Bisecting the batch to find the one offending input is deliberately not implemented — with
at most 36 chunk ids in the log (see [Embedding model and
batching](#embedding-model-and-batching)), a person can find it by inspection, and automating that
search is treated as separate, out-of-scope work.

An earlier review round rejected routing the embedding 400 through `services/indexing_results.py`'s
per-document classification table. That table exists to interpret a shape the embeddings API never
returns — a per-document `status`/`statusCode` array — so forcing a request-level rejection through
it would either fabricate a document-level status that was never reported, or silently discard the
one thing the embeddings failure actually carries: the full list of chunk ids that were in the
batch. Keeping the two failures in separate modules, with separate vocabularies, is what keeps a
reader — or an on-call engineer — able to tell which surface a given failure came from.
