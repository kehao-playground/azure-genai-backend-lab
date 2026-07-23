# RAG Overview

Day 11 milestone (docs tier). This document records the RAG design decisions for Part 3 (Days 11–15) before any RAG code lands: the pipeline decomposition, the Azure service mapping, and the classic-vs-agentic choice.

## RAG as a backend retrieval pattern

RAG (Retrieval-Augmented Generation) lets the model answer over data it was never trained on: before calling the LLM, search a corpus, put the top matches into the prompt, and instruct the model to answer from that context ([Azure Architecture Center RAG guide](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-solution-design-and-evaluation-guide), checked 2026-07).

In backend terms it is a query-then-respond read path — the same shape as "look up the order in the database, then render the response", with the render step replaced by an LLM. The analogy breaks in one place: a database returns what it returns, while an LLM given reference context may still ignore it, distort it, or add to it. RAG downgrades hallucination from *unfalsifiable* to *checkable against retrieved sources*; closing the remaining gap is Day 15's grounding / no-answer work.

## Two pipelines, two lifecycles

See [diagrams/rag-two-pipelines.md](diagrams/rag-two-pipelines.md).

- **Indexing pipeline** (offline/asynchronous; triggered by initial import, data changes, or a schedule): chunk documents → enrich with metadata → embed → persist to the search index.
- **Query pipeline** (online; runs for each request that requires retrieval augmentation): embed the query (hybrid search needs both a text and a vector query) → retrieve (hybrid search + rerank) → augment (top-K chunks into the prompt) → generate.

Query embedding is an online upstream call with its own latency, failure surface, and bill. This project uses application-owned vectorization (calling the v1 embeddings endpoint directly) rather than Azure AI Search integrated vectorization, so "retrieval broke" debugging must first distinguish an embedding-call failure from a search failure.

They differ in everything that matters operationally:

| | Indexing pipeline | Query pipeline |
|---|---|---|
| Trigger | initial import / data changes / schedule (offline batch) | every query that needs RAG (online) |
| Failure blast radius | bad data poisons every later query | one query answers badly |
| Debugging surface | chunk & index contents | query embedding, retrieval results & prompt |
| Cost driver | embedding calls + index storage (scales with data) | query embedding + search + LLM calls (scales with traffic) |

This decomposition drives the Part 3 milestone order: Day 12 builds the indexing side (chunking, embeddings, index schema), Day 13 the retrieval side (search modes), Day 14 wires the query pipeline into the API.

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

- FP1's correct behavior is an honest "no answer" — a contract decision, not a model behavior. It becomes the Day 15 no-answer policy. A feature file (`rag_no_answer_policy.feature`) reserves the topic, but its only scenario currently verifies the 501 placeholder; the executable no-answer contract scenario lands with Day 15.
- FP1–FP3 happen before the LLM sees the prompt: retrieval is the upstream bottleneck — if the correct context is absent from the prompt, the LLM has little chance of a satisfactory corpus-grounded answer ([Microsoft RAG evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rag-evaluators), checked 2026-07). Days 8–9 provide the observability *foundation* (correlation ids, prompt provenance, usage); RAG per-stage logging (retrieval mode/count, selected chunk ids and scores, stage latency, context-budget contribution, metadata redaction) does not exist yet and is part of the Day 13/14 DoD — the paper's conclusion is that RAG validation is only feasible in operation.

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

Billable Azure AI Search Dedicated tiers (Basic and above) bill per Search Unit per hour **while the service exists**, independent of query volume ([pricing tiers](https://learn.microsoft.com/en-us/azure/search/search-sku-tier)). Two exceptions: the Free tier is a $0, shared, limited service (one per subscription), and the Serverless Developer tier (consumption-based, available in Japan East) is preview with billing currently deferred — excluded from this repo's GA-only mainline. Under this repo's cost policy the service is ephemeral regardless of tier: created for a test session, torn down the same day, with create/teardown scripts as first-class deliverables (Day 13). Known documentation conflict, to be settled empirically in Day 13: learn pages state semantic ranker runs on the free tier, while the pricing page states it is unavailable on the dedicated free tier.
