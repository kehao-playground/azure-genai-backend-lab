# Agent Decision Guide

Day 16 opens Part 4 (agents) with a deliberately unglamorous artifact: a decision guide for
*whether* an endpoint should be an agent at all. Everything this backend shipped through Day 15 —
chat, streaming, conversation state, prompt templates, token budgets, RAG with ACL — was built
**without** an agent, and none of it was a compromise. That track record is the baseline any
agent proposal has to beat.

## The axis that matters: who owns control flow

"Agent" is not a capability tier above "chat API". The difference is ownership of control flow:

- **Code-owned control flow** — application code decides every model call and what happens with
  its output. The model produces *content*; the program decides *what happens next*. Every
  endpoint in this repo works this way: `/chat` is one model call per turn, `/rag` is a
  hard-wired `retrieval → generation` pipeline.
- **Model-owned control flow (an agent)** — the model's output decides the next action: which
  tool to call, with which arguments, whether to call another one, when to stop. The program
  provides tools and limits; the *action selection* happens at inference time, per request.

That reframing turns "should we use an agent?" into a question with a testable answer: **at
every step, can a program rule decide which edge to take — without asking the model to select
an action?** If yes, write code that walks the graph. If the next action genuinely depends on
what the model produced along the way — tool choice driven by intermediate results, stopping
point unknowable up front — only then does model-owned control flow buy anything.

One tempting shortcut does **not** work: "can you draw the flow as a fixed graph at request
time?" is not the criterion. An agent loop is also a fixed graph — a cyclic one whose candidate
tools, `model → tool → model` edges, and iteration limit are all drawable before any request
arrives. What is dynamic is not the topology; it is who selects the edge at runtime. The fixed
graph remains a useful heuristic in one direction only: a graph with no "model selects the next
edge" node is, by construction, fully expressible as code. The decision procedure is drawn in
[`docs/diagrams/agent-decision-flow.md`](diagrams/agent-decision-flow.md) — a graph in which,
fittingly, every edge is rule-decided.

Microsoft's own framework documentation says the quiet part out loud: *"If you can write a
function to handle the task, do that instead of using an AI agent"*
([Agent Framework overview](https://learn.microsoft.com/en-us/agent-framework/overview/agent-framework-overview),
checked 2026-08). The Azure Architecture Center puts the same rule on a complexity spectrum —
direct model call → single agent with tools → multiagent orchestration — with the instruction to
*"use the lowest level of complexity that reliably meets your requirements"*
([AI agent orchestration patterns](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns),
checked 2026-08). Note that both endorse function-first; neither endorses any graph-drawability
test — that framing is this guide's own, and it is a heuristic, not the criterion.

## Worked example: why `/rag` is a pipeline, not an agent

The RAG endpoint is the closest thing this repo has to an "agentic" workload, and it was
deliberately built as a code-owned pipeline. That choice bought guarantees whose *proofs live in
the pipeline structure*. An agentic variant (the model decides whether and when to search) keeps
some of them at the tool boundary — but the end-to-end proofs degrade into properties you
maintain with evals, traces, and runtime policy:

| Guarantee (shipped) | What happens under model-owned flow |
|---|---|
| Structural no-answer (Day 14): zero retrieved hits → `no_answer`, **zero** LLM calls | Only holds if retrieval *always* runs and the LLM call is conditional in code. An agent may skip retrieval or answer from parametric memory; "did it search?" becomes a per-request inference outcome. This is an end-to-end property — it degrades. |
| ACL enforcement (Day 15): every search carries a server-built `preFilter`; `principal` has no default | Two layers. *Per-call*: if the agent's search tool is a fail-closed wrapper over `SearchClient.search(..., principal=...)`, the filter on every actual search remains a structural, type-level guarantee — model-chosen invocation does not weaken it. *End-to-end*: "every answer searched first and grounded only in filtered results" is what loses its structural proof and needs evals/traces/policy. |
| Prompt budget (Day 14): rank-ordered source selection, stop-at-first-overflow, `sources` = exactly what the model saw | Requires the composer to own prompt assembly. Deterministic budget enforcement can be re-established at tool-result/composer boundaries, but the pipeline's "sources = model's exact evidence" claim is an end-to-end property. |
| Cost shape (Day 9): one turn ≈ one model call, budget checked **before** inference | An agent turn is `N + 1` model calls, where N = the number of sequential tool-result rounds the model chooses at runtime. Iteration and context caps still yield a worst-case bound; the *actual* count becomes a per-request variable. |

None of these are arguments against agents. They are the list of things to *re-derive* when a
loop replaces the pipeline — which is exactly why the pipeline stays the default until a
requirement shows up that program rules cannot route.

## Signals, both directions

You likely need an agent when, for a single endpoint:

- The set of tools to use — and their order — depends on intermediate results, not just on the
  request. (A fixed `if` over request fields is still program-owned edge selection. Write the
  `if`.)
- The number of act-and-check rounds is genuinely unknowable up front (investigate → act →
  re-check loops).
- Enumerating the routing rules would mean re-implementing planning in `if` statements — and you
  have evidence of that, because you tried.

You do not need an agent when:

- The flow is a fixed sequence or DAG per request type. That is a function (or a workflow
  engine); the Agent Framework itself ships graph-based *workflows* as the non-agentic option
  for exactly this case.
- "The model should decide" is aspirational rather than observed — no concrete request exists
  whose next step program rules cannot decide.
- The motivation is architectural fashion. An agent that always calls its one tool once is a
  pipeline paying loop overhead: same result, more latency, more tokens, wider failure surface.

## What an agent costs (the honest ledger)

Three multipliers arrive together the moment control flow becomes model-owned:

1. **Latency and tokens.** The unit of agent cost is the *round*: the model emits tool calls,
   the program executes them, results go back to the model. A single model response may emit
   several parallel tool calls — they share one model round, though each tool has its own
   latency, cost, and failure mode. With N defined as the number of *sequential* tool-result
   rounds, a turn is `N + 1` model calls (the last one produces the final answer), and every
   round re-sends the grown context. Under that explicit assumption, a 4-round loop is ≥5
   sequential inference rounds before the first user-visible token of the final answer.
2. **A wider blast radius for model-output nondeterminism.** Per this series' glossary,
   nondeterminism has many sources — external services, retrieval ordering, time, concurrency,
   mutable state — and code-owned flow eliminates none of them. What code-owned flow does
   confine is the *model-output* source: its blast radius is the content of a reply. An agent
   extends that same source to the *execution path*: wrong tool, wrong arguments,
   non-termination. Iteration limits are the standing mitigation (the Architecture Center calls
   them out for single agents with tools, checked 2026-08) — and prompt injection escalates from
   "wrong words in a reply" to "attacker-influenced tool invocation" (Day 21 territory).
3. **A guardrail audit — retain / extend / redesign, not a blanket rewrite.** Contracts built on
   "one turn ≈ one inference, one path, one terminal event" need **redesign**: Day 5's timeout
   (per-step vs. per-task), Day 9's budget checkpoint (before every round vs. once per turn).
   Contracts whose property survives but must be *proven* across the tool boundary need
   **extend**: Day 15's rule that `principal` reaches every search (including tools calling
   tools), Day 8's prompt provenance. Contracts not bound to a single call are **retained**:
   Day 6's SSE terminal-event ownership already anticipates additive event types. Producing
   this per-contract audit is Day 18 work.

## Where an agent sits in this architecture (Days 17–18 preview)

When an agent does land in this repo, it lands as a service behind an app-owned Protocol, not as
a new architecture:

- **`AgentService` behind a Protocol, assembled at the composition point.** The series plan
  schedules a *separate* agent endpoint for Day 18 — its handler knows it is running the agent
  use case. What the Protocol hides is the Agent Framework and provider *types*, exactly as
  fake/real adapters have hidden vendor types since Day 2 — not the fact that an agent exists.
- **Existing conventions carry over; schemas evolve additively.** The error envelope and
  correlation-id conventions remain. The agent endpoint's request/response schema, tool events,
  and the iteration-limit terminal outcome are *additive* transport/operational contract
  evolution in Day 18 — the room Day 6 reserved when it required clients to ignore unknown SSE
  event names. Framework types stop at the adapter boundary, as Responses API typed events did
  on Day 6.
- Tools are least-privilege functions this backend owns — thin, validated wrappers over code
  that already exists — never raw reach into infrastructure.
- Framework choice is pinned from the 2026-07 spike: **Microsoft Agent Framework**
  (`agent-framework-core==1.13.0` + `agent-framework-openai==1.12.0`). The meta package
  `agent-framework` resolves to `agent-framework-core[all]`, whose metadata lists exactly **30**
  optional `agent-framework-*` integration packages (PyPI metadata, checked 2026-08) — of which
  this series needs one, `agent-framework-openai`. Source-verifiable capabilities: the client
  runs on the same Responses API this repo has used since Day 5, `store=False` is explicitly
  supported, and loop usage is aggregated. The spike's live-run details (japaneast, 2026-07-31)
  are author-observed and not independently replayable — no script or redacted capture was
  retained. `usage_details` field names differ from Day 9's `TokenUsage`
  (`input_token_count` vs. `input_tokens`, etc.), so metering reconnects only through adapter
  field mapping and validation, with failed/disconnect paths and ledger commit semantics
  re-derived in the adapter. Details land with the Day 17 implementation.

## Checklist

Before an endpoint becomes an agent, write down:

1. The concrete request whose next step program rules cannot decide (if you cannot produce one,
   stop here).
2. The tool list, each tool's blast radius, and why each is least-privilege.
3. The iteration limit and what the client sees when it trips.
4. Per-turn cost bounds: worst-case sequential rounds under the iteration/context caps ×
   context growth, and how the Day 9 ledger counts them.
5. Which guarantees need redesign and which need proof across the tool boundary (structural
   no-answer, end-to-end grounding, budget-before-inference), and whether that trade is being
   made knowingly.

A proposal that survives all five deserves an agent. Most don't — and that outcome is the guide
working, not the guide failing.
