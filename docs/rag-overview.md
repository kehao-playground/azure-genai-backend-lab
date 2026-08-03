# RAG Overview

Day 11 milestone (docs tier), originally written before any RAG code landed. This document records the RAG design decisions for Part 3 (Days 11–15): the pipeline decomposition, the Azure service mapping, and the classic-vs-agentic choice. Day 12 (indexing), Day 13 (retrieval), Day 14 (the query pipeline, `POST /api/v1/rag`), and Day 15 (tenant/group access control on that same pipeline) have since shipped against this design.

## RAG as a backend retrieval pattern

RAG (Retrieval-Augmented Generation) lets the model answer over data it was never trained on: before calling the LLM, search a corpus, put the top matches into the prompt, and instruct the model to answer from that context ([Azure Architecture Center RAG guide](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-solution-design-and-evaluation-guide), checked 2026-07).

In backend terms it is a query-then-respond read path — the same shape as "look up the order in the database, then render the response", with the render step replaced by an LLM. The analogy breaks in one place: a database returns what it returns, while an LLM given reference context may still ignore it, distort it, or add to it. RAG downgrades hallucination from *unfalsifiable* to *checkable against retrieved sources*; it does not close the gap. Day 14 implemented grounding as far as this pipeline can take it — see the honest gap noted below.

## Two pipelines, two lifecycles

See [diagrams/rag-two-pipelines.md](diagrams/rag-two-pipelines.md).

- **Indexing pipeline** (offline/asynchronous; triggered by initial import, data changes, or a schedule): chunk documents → enrich with metadata → embed → persist to the search index.
- **Query pipeline** (online; runs for each request that requires retrieval augmentation): embed the query (hybrid search needs both a text and a vector query) → retrieve (hybrid search + rerank) → augment (top-K chunks into the prompt) → generate. Implemented Day 14 as `POST /api/v1/rag`; see [diagrams/rag-query-sequence.md](diagrams/rag-query-sequence.md) and the [`/rag` contract](api-conventions.md#rag-retrieval-augmented-generation) for the shipped shape.

Query embedding is an online upstream call with its own latency, failure surface, and bill. This project uses application-owned vectorization (calling the v1 embeddings endpoint directly) rather than Azure AI Search integrated vectorization, so "retrieval broke" debugging must first distinguish an embedding-call failure from a search failure.

They differ in everything that matters operationally:

| | Indexing pipeline | Query pipeline |
|---|---|---|
| Trigger | initial import / data changes / schedule (offline batch) | every query that needs RAG (online) |
| Failure blast radius | bad data poisons every later query | one query answers badly |
| Debugging surface | chunk & index contents | query embedding, retrieval results & prompt |
| Cost driver | embedding calls + index storage (scales with data) | query embedding + search + LLM calls (scales with traffic) |

This decomposition drove the Part 3 milestone order: [Day 12](rag-indexing.md) built the indexing side (chunking, embeddings, index schema), [Day 13](rag-retrieval.md) the retrieval side (search modes), Day 14 wired the query pipeline into `POST /api/v1/rag`, and Day 15 threaded a `Principal` through every stage of that same pipeline so retrieval never runs without an authorization scope — see [Principal flow through the pipeline](#principal-flow-through-the-pipeline) below.

## Principal flow through the pipeline

Day 15 fixed a gap the query pipeline had carried since Day 14: retrieval was unscoped by tenant or
user, so any caller's question could surface any tenant's chunks. The fix threads a typed
`Principal` — `tenant_id` plus deduplicated `group_ids`, resolved once per request by the
`require_principal` FastAPI dependency from trusted gateway headers — through every layer that
touches the search index, with no layer able to opt out:

```
RagService.answer(question, principal)
  -> Retriever.retrieve(question, principal)
       -> SearchClient.search(..., principal=principal)   # required, no default
```

`principal` has no default anywhere on this call chain. A default would make "no authorization
context" a spelling a caller could reach by omission, invisibly, at any of the three call sites; a
required keyword argument makes an unscoped query a type error instead of a runtime data leak.
`AzureSearchClient.search()` is the one place that turns a `Principal` into the wire-level OData
`filter` — see [access control is a query-time filter, not a separate
check](rag-retrieval.md#access-control-is-a-query-time-filter-not-a-separate-check) — and
`FakeSearchClient` enforces the identical policy in Python via `is_document_visible()`, so a test
that passes against the fake cannot be relying on an authorization check the real adapter omits.

A cross-tenant or wrong-group question resolves exactly like FP1 below: zero visible hits, `status:
"no_answer"`, no distinguishable "you asked but were denied" signal. That is a deliberate corollary
of shared-index, query-time isolation (see [the trust-boundary
note](api-conventions.md#trust-boundary-read-before-deploying-past-a-lab-environment)): the index itself does not know the
difference between "nothing matched" and "something matched but you may not see it", and this
pipeline does not manufacture that distinction after the fact.

### Two Day 15 debts, stated as honest boundaries, not silent gaps

- **Source fencing is a marker, not a sandbox.** Every retrieved chunk is wrapped in
  `BEGIN UNTRUSTED SOURCE n` / `END UNTRUSTED SOURCE n` before it reaches the prompt
  (`render_sources()`), on top of the template-level instruction that source text is data, not
  commands. That raises the bar against a corpus entry phrased as an instruction; it does not remove
  the entry from the model's context window, and a sufficiently well-crafted poisoned chunk is still
  on the threat model exactly as [Failure modes](#failure-modes-design-inputs-not-afterthoughts) FP4
  describes it — "not extracted" has a mirror failure, "extracted as if it were an instruction",
  that fencing mitigates rather than closes.
- **Citation validation is syntactic, not evidentiary.** `_validate_citations()` strips a `[n]`
  marker whose number falls outside `1..included_hit_count` — the range of sources actually sent to
  the model, after budget-driven dropping — and logs only the invalid numbers, never the answer
  text. What survives validation is a citation that *points at a source that was really in context*;
  it is not proof the cited sentence is actually supported by that source's content. A model can
  still cite source `[2]` for a claim that source does not substantiate, and this pipeline has no
  stage that would catch it. This is the same class of honest gap as FP1's model-refusal case below:
  the check that exists is real and worth having, and it stops at exactly the boundary stated here.

## Why RAG and not fine-tuning

Both Microsoft and OpenAI frame this as two different problems, not two competing solutions ([Microsoft comparison](https://learn.microsoft.com/en-us/azure/developer/ai/augment-llm-rag-fine-tuning), [OpenAI accuracy guide](https://developers.openai.com/api/docs/guides/optimizing-llm-accuracy), checked 2026-07):

- **Knowledge problems** (facts the model lacks: missing, stale, or proprietary) → RAG. Content changes without retraining, retrieval can enforce per-user access control, and answers can cite sources.
- **Behavior problems** (format, tone, reasoning style inconsistency) → fine-tuning / prompt engineering. Fine-tuning wants hundreds-to-thousands of task examples and is strongest at behavior shaping; Microsoft also documents domain/topic specialization on stable proprietary data as a fine-tuning fit ([fine-tuning considerations](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/fine-tuning-considerations), checked 2026-07).

Knowledge-vs-behavior is the first diagnostic axis, not a universal capability boundary. This project's problem is a *changing* knowledge problem — the corpus changes without retraining, retrieval can enforce query-time access control, and answers can cite sources — so Part 3 is RAG. The two compose — fine-tuning can teach a model to use retrieved context better — but that is out of scope for this series.

## Failure modes (design inputs, not afterthoughts)

The canonical taxonomy is the Seven Failure Points ([Barnett et al., CAIN 2024](https://arxiv.org/abs/2401.05856)); each point lands on a specific pipeline stage:

| # | Failure | Pipeline stage |
|---|---|---|
| FP1 | Missing content (answer not in corpus) | indexing (corpus) |
| FP2 | Missed top-ranked (in index, not in top-K) | query: retrieve |
| FP3 | Not in context (retrieved, lost in consolidation) | query: augment |
| FP4 | Not extracted (in context, model missed it) | query: generate |
| FP5 | Wrong format | query: generate |
| FP6 | Incorrect specificity | query: generate |
| FP7 | Incomplete answer | query: generate |

Design consequences adopted here:

- FP1's correct behavior is an honest "no answer" — a contract decision, not a model behavior. Day 14 implements it structurally, not as a model instruction: zero retrieval hits short-circuit to `status: "no_answer"` before the LLM is called, because Day 13's live probe showed hybrid RRF scores cannot separate an answer-present corpus from an answer-absent one — there is no threshold to gate on past that point. The remaining gap is honest, not closed: a model-level refusal ("the sources don't cover this") still returns `status: "answered"`, because from the pipeline's point of view a refusal is a successful generation. A client has to read `answer` and `sources`, not just `status`, to tell the two apart.
- FP1–FP3 happen before the LLM sees the prompt: retrieval is the upstream bottleneck — if the correct context is absent from the prompt, the LLM has little chance of a satisfactory corpus-grounded answer ([Microsoft RAG evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rag-evaluators), checked 2026-07). Days 8–9 provide the observability *foundation* (correlation ids, prompt provenance, usage); Day 13 added the search adapter's per-call line (mode, candidate window, returned chunk ids/scores) and Day 14 closed the remaining gap with an augmentation-stage line — see "Observability" below — the paper's conclusion is that RAG validation is only feasible in operation.

### Observability

Every RAG request's three stages log one line each, all joinable on `correlation_id`: `search` (Day 13, mode/candidate window/returned chunk ids and scores), `rag stage=assemble_context` (Day 14, hit count, chunk ids, per-source content character lengths, total assembled context characters, question character length), and `llm call`/`llm usage` (Day 8/9, prompt identity and token counts). `correlation_id` is stamped on every log record by a `LogRecord` factory installed in `configure_logging()` (`core/logging.py`) that reads the same `ContextVar` the correlation-id middleware populates per request, so call sites do not need to read or pass it manually to be joinable. Redaction rule, held consistently across the RAG stage line: question text and chunk content are never logged, only counts, ids, and character lengths — the same why-not-what discipline Day 9 applied to token usage.

## Azure mapping and the classic-vs-agentic choice

| Pipeline stage | Azure service |
|---|---|
| Embed (indexing & query) | Azure OpenAI embeddings — `text-embedding-3-small` planned (third-generation choices are `3-small`/`3-large`; `ada-002` remains listed); a dedicated `/openai/v1/embeddings` endpoint on the v1 surface, **not** part of the Responses API, priced separately by embedding-model tokens ([embeddings how-to](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/embeddings), [pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/), checked 2026-07) |
| Index + retrieve | Azure AI Search — hybrid search (BM25 + vector, fused with Reciprocal Rank Fusion) with optional semantic ranker (L2 rerank of the top 50) ([information retrieval guide](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-information-retrieval), checked 2026-07) |
| Generate | Azure OpenAI Responses API (`chat-mini` deployment, unchanged since Day 5) |

Azure AI Search offers two RAG approaches as of 2026-07 ([RAG overview](https://learn.microsoft.com/en-us/azure/search/retrieval-augmented-generation-overview)): **agentic retrieval** (LLM-planned parallel subqueries) and the **classic RAG pattern** (GA; hybrid search + semantic ranking).

Agentic retrieval's lifecycle is granular, not a blanket preview ([overview](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview), [migration guide](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-how-to-migrate), checked 2026-07): knowledge bases, knowledge retrieval, and several knowledge-source types are GA in REST API `2026-04-01`, while LLM query planning (active at low/medium reasoning effort), answer synthesis, multi-turn messages, and full portal access remain preview.

This project uses **classic RAG**:

- The capabilities this series would actually want from agentic retrieval — LLM-planned subqueries — remain preview; GA-only policy means readers must be able to reproduce the series months later, and the GA minimal/extractive surface alone gives up agentic's main selling point.
- Every query-pipeline step stays explainable and independently debuggable — the point of a teaching repo.
- Each added LLM reasoning step adds latency and tokens. Note: the widely quoted "~2–3 s standard vs ~8–15 s agentic" figures describe an *application-level* agentic RAG example (an agent making 3–5 retrieval tool calls) from the Architecture Center — not a benchmark of the Azure AI Search agentic retrieval service ([agentic RAG pattern](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-agentic), checked 2026-07).

Application-level agentic RAG (retrieval as an agent tool — a different thing from AI Search agentic retrieval) is revisited conceptually in Part 4.

## Cost constraints (checked 2026-07)

Billable Azure AI Search Dedicated tiers (Basic and above) bill per Search Unit per hour **while the service exists**, independent of query volume ([pricing tiers](https://learn.microsoft.com/en-us/azure/search/search-sku-tier)). Two exceptions: the Free tier is a $0, shared, limited service (one per subscription), and the Serverless Developer tier (consumption-based, available in Japan East) is preview with billing currently deferred — excluded from this repo's GA-only mainline. Under this repo's cost policy the service is ephemeral regardless of tier: created for a test session, torn down the same day, with create/teardown scripts as first-class deliverables (Day 13). Documentation conflict, **still unresolved**, probed once on 2026-07-29: three pages said semantic ranker is usable on the Free tier — the tier page ("runs on the Free tier"), the semantic overview (free "subject to service limits for the free tier"), and the billing page, whose plans table marks the free *plan* "Available on all pricing tiers" — while the pricing page said the feature "is not available in the Dedicated Free tier". Measured: a Free-tier service in japaneast provisions with `semanticSearch: free`, and a `queryType=semantic` query against it returns HTTP 200 with `@search.rerankerScore` on every result. The documentation conflict itself remains **unresolved**: the pricing page says the feature is unavailable in Dedicated Free, which is a statement about availability, not a description of Standard-plan billing, so it is not reconciled away by the plans table. What the measurement establishes is narrower and specific — semantic reranking ran on this Free-tier service, in japaneast, on 2026-07-29, at api-version `2026-04-01`. Two documented limits keep that from meaning "semantic ranking is free": the free plan covers the first 1,000 semantic-ranker requests per month, past which "semantic ranker requests return a billing error"; and because the standard plan needs Basic or higher, a Free-tier service has no upgrade path without moving off Free. Both limits come from the documentation — this session did not exhaust the allowance and so observed neither.
