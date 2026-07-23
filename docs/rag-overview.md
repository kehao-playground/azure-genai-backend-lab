# RAG Overview

Day 11 milestone (docs tier). This document records the RAG design decisions for Part 3 (Days 11–15) before any RAG code lands: the pipeline decomposition, the Azure service mapping, and the classic-vs-agentic choice.

## RAG as a backend retrieval pattern

RAG (Retrieval-Augmented Generation) lets the model answer over data it was never trained on: before calling the LLM, search a corpus, put the top matches into the prompt, and instruct the model to answer from that context ([Azure Architecture Center RAG guide](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-solution-design-and-evaluation-guide), checked 2026-07).

In backend terms it is a query-then-respond read path — the same shape as "look up the order in the database, then render the response", with the render step replaced by an LLM. The analogy breaks in one place: a database returns what it returns, while an LLM given reference context may still ignore it, distort it, or add to it. RAG downgrades hallucination from *unfalsifiable* to *checkable against retrieved sources*; closing the remaining gap is Day 15's grounding / no-answer work.

## Two pipelines, two lifecycles

See [diagrams/rag-two-pipelines.md](diagrams/rag-two-pipelines.md).

- **Indexing pipeline** (runs when data changes): chunk documents → enrich with metadata → embed → persist to the search index.
- **Query pipeline** (runs per request): retrieve (hybrid search + rerank) → augment (top-N chunks into the prompt) → generate.

They differ in everything that matters operationally:

| | Indexing pipeline | Query pipeline |
|---|---|---|
| Trigger | data updates (batch) | every request (online) |
| Failure blast radius | bad data poisons every later query | one request answers badly |
| Debugging surface | chunk & index contents | retrieval results & prompt |
| Cost driver | embedding calls + index storage (scales with data) | search + LLM calls (scales with traffic) |

This decomposition drives the Part 3 milestone order: Day 12 builds the indexing side (chunking, embeddings, index schema), Day 13 the retrieval side (search modes), Day 14 wires the query pipeline into the API.

## Why RAG and not fine-tuning

Both Microsoft and OpenAI frame this as two different problems, not two competing solutions ([Microsoft comparison](https://learn.microsoft.com/en-us/azure/developer/ai/augment-llm-rag-fine-tuning), [OpenAI accuracy guide](https://developers.openai.com/api/docs/guides/optimizing-llm-accuracy), checked 2026-07):

- **Knowledge problems** (facts the model lacks: missing, stale, or proprietary) → RAG. Content changes without retraining, retrieval can enforce per-user access control, and answers can cite sources.
- **Behavior problems** (format, tone, reasoning style inconsistency) → fine-tuning / prompt engineering. Fine-tuning wants hundreds-to-thousands of task examples and buys behavior, not facts ([fine-tuning considerations](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/fine-tuning-considerations), checked 2026-07).

This project's problem (answer over a document corpus that changes) is a knowledge problem, so Part 3 is RAG. The two compose — fine-tuning can teach a model to use retrieved context better — but that is out of scope for this series.

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

- FP1's correct behavior is an honest "no answer" — a contract decision, not a model behavior. It becomes the Day 15 no-answer policy and is already a named BDD scenario in the testing strategy.
- FP1–FP3 happen before the LLM sees the prompt: retrieval quality is the upstream bottleneck ([Microsoft RAG evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rag-evaluators.md), checked 2026-07). Observability (correlation ids, per-stage logging — Days 8–9 infrastructure) is a precondition for operating RAG, since the same paper's conclusion is that RAG validation is only feasible in operation.

## Azure mapping and the classic-vs-agentic choice

| Pipeline stage | Azure service |
|---|---|
| Embed (indexing & query) | Azure OpenAI embeddings — `text-embedding-3-small` planned; a dedicated `/openai/v1/embeddings` endpoint on the v1 surface, **not** part of the Responses API ([embeddings how-to](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/embeddings), checked 2026-07) |
| Index + retrieve | Azure AI Search — hybrid search (BM25 + vector, fused with Reciprocal Rank Fusion) with optional semantic ranker (L2 rerank of the top 50) ([information retrieval guide](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-information-retrieval), checked 2026-07) |
| Generate | Azure OpenAI Responses API (`chat-mini` deployment, unchanged since Day 5) |

Azure AI Search offers two RAG approaches as of 2026-07 ([RAG overview](https://learn.microsoft.com/en-us/azure/search/retrieval-augmented-generation-overview)): **agentic retrieval** (preview; LLM-planned parallel subqueries) and the **classic RAG pattern** (GA; hybrid search + semantic ranking). This project uses **classic RAG**:

- GA-only policy: readers must be able to reproduce the series months later.
- Every query-pipeline step stays explainable and independently debuggable — the point of a teaching repo.
- Microsoft's own guidance: single-search/single-index workloads fit standard RAG better; each agentic reasoning step adds latency and tokens (~2–3 s standard vs ~8–15 s agentic per their figures, [agentic RAG guide](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-agentic), checked 2026-07).

Agentic retrieval is revisited conceptually in Part 4 (retrieval as an agent tool).

## Cost constraints (checked 2026-07)

Azure AI Search dedicated tiers bill per Search Unit per hour **while the service exists**, independent of query volume ([pricing tiers](https://learn.microsoft.com/en-us/azure/search/search-sku-tier)). Under this repo's cost policy the service is therefore ephemeral: created for a test session, torn down the same day, with create/teardown scripts as first-class deliverables (Day 13). Known documentation conflict, to be settled empirically in Day 13: learn pages state semantic ranker runs on the free tier, while the pricing page states it is unavailable on the dedicated free tier.
