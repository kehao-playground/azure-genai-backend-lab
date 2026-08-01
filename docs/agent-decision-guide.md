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
  hard-wired `retrieval → generation` pipeline. The flow graph is fixed before the request
  arrives.
- **Model-owned control flow (an agent)** — the model's output decides the next action: which
  tool to call, with which arguments, whether to call another one, when to stop. The program
  provides tools and limits; the *execution path* is chosen at inference time, per request.

That reframing turns "should we use an agent?" into a question with a testable answer: **can you
draw the flow as a fixed graph at request time?** If yes, write code that walks the graph. If the
next step genuinely depends on what the model finds along the way — tool choice driven by
intermediate results, iteration count unknowable up front — only then does model-owned control
flow buy anything.

Microsoft's own framework documentation says the quiet part out loud: *"If you can write a
function to handle the task, do that instead of using an AI agent"*
([Agent Framework overview](https://learn.microsoft.com/en-us/agent-framework/overview/agent-framework-overview),
checked 2026-08). The Azure Architecture Center puts the same rule on a complexity spectrum —
direct model call → single agent with tools → multiagent orchestration — with the instruction to
*"use the lowest level of complexity that reliably meets your requirements"*
([AI agent orchestration patterns](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns),
checked 2026-08).

## Worked example: why `/rag` is a pipeline, not an agent

The RAG endpoint is the closest thing this repo has to an "agentic" workload, and it was
deliberately built as a code-owned pipeline. That choice bought guarantees that an agentic
variant (the model decides whether and when to search) cannot express:

| Guarantee (shipped) | Why it needs code-owned flow |
|---|---|
| Structural no-answer (Day 14): zero retrieved hits → `no_answer`, **zero** LLM calls | Only holds if retrieval *always* runs and the LLM call is conditional in code. An agent may skip retrieval or answer from parametric memory; "did it search?" becomes a per-request inference outcome. |
| ACL enforcement (Day 15): every search carries a server-built `preFilter`; `principal` has no default | The filter is attached at a boundary code always passes through. With model-chosen tool calls, "every path is filtered" becomes a property you test statistically instead of prove structurally. |
| Prompt budget (Day 14): rank-ordered source selection, stop-at-first-overflow, `sources` = exactly what the model saw | Requires the composer to own prompt assembly. An agent accumulating tool results owns its own context growth. |
| Cost shape (Day 9): one turn ≈ one model call, budget checked **before** inference | An agent loop is 1 + N calls per turn, N chosen by the model at runtime — the ledger can still count it, but can no longer predict it. |

None of these are arguments against agents. They are the list of things to *re-derive* when a
loop replaces the pipeline — which is exactly why the pipeline stays the default until a
requirement shows up that a fixed graph cannot express.

## Signals, both directions

You likely need an agent when, for a single endpoint:

- The set of tools to use — and their order — depends on intermediate results, not just on the
  request. (A fixed `if` over request fields is still a graph. Write the `if`.)
- The iteration count is genuinely unknowable up front (investigate → act → re-check loops).
- The task is open-ended enough that enumerating flows would mean re-implementing planning in
  `if` statements — and you have evidence of that, because you tried.

You do not need an agent when:

- The flow is a fixed sequence or DAG per request type. That is a function (or a workflow
  engine); the Agent Framework itself ships graph-based *workflows* as the non-agentic option
  for exactly this case.
- "The model should decide" is aspirational rather than observed — no concrete request exists
  that the fixed graph mishandles.
- The motivation is architectural fashion. An agent that always calls its one tool once is a
  pipeline paying loop overhead: same result, more latency, more tokens, wider failure surface.

## What an agent costs (the honest ledger)

Three multipliers arrive together the moment control flow becomes model-owned:

1. **Latency and tokens.** Every decision point is a model call, and each call re-sends the
   grown context. A 4-step loop is ≥5 sequential inference rounds before the first user-visible
   token of the final answer.
2. **A wider failure surface.** Code-owned flow confines nondeterminism to *content*; an agent
   extends it to the *execution path*: wrong tool, wrong arguments, non-termination. Iteration
   limits are the standing mitigation (the Architecture Center calls them out for single agents
   with tools, checked 2026-08) — and prompt injection escalates from "wrong words in a reply"
   to "attacker-influenced tool invocation" (Day 21 territory).
3. **A guardrail re-audit.** Day 5's timeout becomes per-step vs. per-task. Day 6's SSE contract
   already reserved room for additive tool events (clients must ignore unknown event names —
   that clause was written for this part of the series). Day 9's budget must count every loop
   call, not one call per turn. Day 15's rule that `principal` reaches every search unchanged
   must survive tools calling tools.

## Where an agent sits in this architecture (Days 17–18 preview)

When an agent does land in this repo, it lands as a service like every other adapter, not as a
new architecture:

- Behind a Protocol, selected at the composition point — handlers never know whether a pipeline
  or an agent produced the answer (same rule as fake/real adapters since Day 2).
- The HTTP contract stays ours: error envelope, correlation id, SSE vocabulary (tool events
  additive). Framework types stop at the adapter boundary, exactly as Responses API typed events
  did on Day 6.
- Tools are least-privilege functions this backend owns — thin, validated wrappers over code
  that already exists — never raw reach into infrastructure.
- Framework choice is pinned from the 2026-07 spike: **Microsoft Agent Framework**
  (`agent-framework-core==1.13.0` + `agent-framework-openai==1.12.0`, minimal set rather than
  the meta package), running on the same Responses API base with `store=False`, with
  `usage_details` feeding Day 9's metering. Details land with the Day 17 implementation.

## Checklist

Before an endpoint becomes an agent, write down:

1. The concrete request the fixed graph cannot handle (if you cannot produce one, stop here).
2. The tool list, each tool's blast radius, and why each is least-privilege.
3. The iteration limit and what the client sees when it trips.
4. Per-turn cost bounds: worst-case model calls × context growth, and how the Day 9 ledger
   counts them.
5. Which existing guarantees (structural no-answer, ACL threading, budget-before-inference)
   weaken, and whether that trade is being made knowingly.

A proposal that survives all five deserves an agent. Most don't — and that outcome is the guide
working, not the guide failing.
