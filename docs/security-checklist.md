# Security checklist (Day 21)

The operable form of [prompt-injection.md](prompt-injection.md): one line per control, each
pointing at the file that implements it or the section that explains it. Reasoning lives in
the threat-model document; this page is for ticking off. The Day 29 production checklist
([production-readiness-checklist.md](production-readiness-checklist.md)) references this
page rather than restating it.

Every reference below is to a file in this repository unless it names a document.

## Input boundary — untrusted text entering a prompt

- [ ] Retrieved sources are fenced with a **per-request random** marker, not a literal one — `services/rag.py` (`render_sources`, `_default_nonce`), [§3.1](prompt-injection.md#31-g1--the-fences-were-forgeable-fixed)
- [ ] The nonce is drawn once per request and threaded through sizing *and* final render — `services/rag.py` (`RagService.answer`, `_select_within_budget`)
- [ ] The nonce generator is injected, so tests pin a value instead of asserting on randomness — `services/rag.py` (`nonce_factory`)
- [ ] The answering prompt states that sources are data and that marker-looking text inside a source body is not a boundary — `prompts/rag_answer.md` (v3, rules 4–5)
- [ ] The agent prompt states that `search_docs` snippets are untrusted corpus text — `prompts/ops_agent.md` (v2, rule 4), [§3.2](prompt-injection.md#32-g2--the-agent-path-had-no-untrusted-data-rule-fixed-at-the-instruction-layer)
- [ ] Tool results are serialized as JSON so corpus text cannot escape the app's own envelope — `services/agent_tools.py` (`make_search_docs`)
- [ ] Instructions are versioned assets loaded at startup, never assembled from request data — `prompts/loader.py`, `prompts/*.md`
- [ ] No template engine and no variable interpolation in any prompt path (no SSTI surface) — `prompts/loader.py`
- [ ] Conversation history carries only `user`/`assistant` items; end-user text never enters a `system`-role message — [api-conventions.md](api-conventions.md#conversation-state)
- [ ] `/agent` task input and `/rag` question are byte-capped before they reach the model — `services/agent_framework.py` (`AGENT_MAX_TASK_BYTES = 4000`), `api/rag.py` (`RagRequest.question`, `max_length=2000`). `/chat` and `/chat/stream` declare only `min_length=1` — no upper bound on message length

## Tool least privilege

- [ ] Identity is closure-bound at bind time; no tool takes a tenant, group, store or budget parameter — `services/agent_tools.py` (`make_search_docs`, `make_get_conversation_usage`)
- [ ] Tools are bound per run, not per process — `services/agent_tools.py` (`bind_principal_tools`, `AgentToolDeps`)
- [ ] Every tool is read-only; no mutating tool exists in the toolset — `services/agent_tools.py`
- [ ] Adding, widening or making a tool mutating is a review trigger for this whole page — [§8](prompt-injection.md#8-honest-boundaries)
- [ ] Config disclosure is an explicit field allowlist; `Settings` is never serialized — `services/agent_tools.py` (`make_get_runtime_config`)
- [ ] Tool output is byte-bounded, and an oversized hit is dropped rather than mangled — `services/agent_tools.py` (`MAX_SEARCH_HITS`, `MAX_SNIPPET_CHARS`, `MAX_TOOL_RESULT_BYTES`, `_fit_within_budget`)
- [ ] No tool path reaches SQL, a shell, or `eval` — `services/agent_tools.py`

## Access control and confused-deputy

- [ ] `Principal` is required with no default on every call that reaches the index — `services/rag.py`, `services/retrieval.py`, `services/azure_search.py`
- [ ] The ACL filter is built server-side from the principal, never from model or caller input — `services/acl.py` (`build_acl_filter`)
- [ ] Document ACL metadata is a contract: a missing or wrong-typed field raises, never defaults to public — `services/acl.py` (`require_acl_metadata`)
- [ ] The fake search path enforces the same policy function as the real one — `services/acl.py` (`is_document_visible`), `services/azure_search.py` (`FakeSearchClient`)
- [ ] Not-found, cross-tenant and scope-mismatched lookups return one indistinguishable shape — `services/agent_tools.py` (`_USAGE_NOT_FOUND`), [api-conventions.md](api-conventions.md#trust-boundary-read-before-deploying-past-a-lab-environment)
- [ ] In headers mode, the gateway strips every client-supplied identity header — [api-conventions.md](api-conventions.md#trust-boundary-read-before-deploying-past-a-lab-environment)
- [ ] In Entra mode, `tid`/`oid`/`groups` come from a verified token, and the gateway still strips those headers — [entra-id-auth.md](entra-id-auth.md)

## Output boundary

- [ ] Citation markers outside the range of sources actually sent are stripped — `services/rag.py` (`_validate_citations`)
- [ ] The client contract for rendering answers is written down and handed over — [known gaps](#known-gaps-scope-decisions-not-oversights)
- [ ] Zero retrieved hits short-circuit to `no_answer` without calling the model — `services/rag.py` (`RagService.answer`)
- [ ] Error responses carry the standard envelope and leak no upstream detail to the caller — [api-conventions.md](api-conventions.md#error-envelope)

## Cost and unbounded consumption

- [ ] Per-call output cap applied on every request — `LLM_MAX_OUTPUT_TOKENS`, [cost-and-monitoring.md](cost-and-monitoring.md#the-two-guardrails)
- [ ] Per-conversation lifetime budget checked before inference — `CONVERSATION_TOKEN_BUDGET`, [cost-and-monitoring.md](cost-and-monitoring.md#the-two-guardrails)
- [ ] Assembled prompt is bounded server-side, rank-ordered, stop-at-first-overflow — `services/rag.py` (`MAX_PROMPT_BYTES`, `_select_within_budget`)
- [ ] Agent tool calls are bounded by sequential tool mode, a per-run admission counter, and the framework's own cap — `services/agent_framework.py` (`AdmissionState`)
- [ ] A subscription budget alert exists, and is understood as delayed notification, not a spending cap — `infra/scripts/create-budget-alert.sh`, [cost-and-monitoring.md](cost-and-monitoring.md)
- [ ] Every script that creates a probe or demo Azure resource pairs with a delete script — OpenAI, Search, Key Vault, Content Safety and the Entra app registration (`infra/scripts/create-*.sh` / `delete-*.sh`), plus `teardown.sh` for the whole resource group. Two named exceptions: `create-budget-alert.sh` has no delete counterpart because the subscription budget alert is deliberately persistent, not ephemeral; `deploy-container-app.sh` has no dedicated delete script and is removed only via `teardown.sh`

## Logging and redaction

- [ ] Question text and retrieved chunk content never reach a log line — `services/rag.py` (`_log_rag_stage`)
- [ ] Tool argument text is logged as a byte length only — `services/agent_framework.py` (`_args_bytes`)
- [ ] Group ids are never logged — `core/logging.py`, [api-conventions.md](api-conventions.md#logging)
- [ ] `user_id` is treated as pseudonymous personal data, not as an opaque tag — [api-conventions.md](api-conventions.md#logging)
- [ ] Agent Framework `Trace`-level logging and sensitive-data telemetry are never enabled — both log full chat message text ([Agent Safety](https://learn.microsoft.com/en-us/agent-framework/agents/safety), checked 2026-08); nothing in this repository turns either on
- [ ] Secrets are read from the environment as `SecretStr` and never committed or baked into an image — `core/config.py`, [key-vault-config.md](key-vault-config.md)

## External extensions (evaluated, not wired in)

- [ ] Prompt Shields is treated as a probabilistic layer on top of structural defenses, never as a replacement — [§5](prompt-injection.md#5-the-probabilistic-layer-azure-ai-content-safety-prompt-shields)
- [ ] The probe resource is ephemeral: created, used, deleted **and purged** in one session — `infra/scripts/run-content-safety-probe.sh`, `infra/scripts/delete-content-safety.sh`
- [ ] The probe's cleanup trap is armed before the first mutation — `infra/scripts/run-content-safety-probe.sh`
- [ ] A non-2xx probe response is never recorded as "no attack detected" — `tools/prompt_shields_probe.py` (`classify_response`)
- [ ] Defender for Cloud AI threat protection is a subscription-level billing decision; not enabled here — [§6](prompt-injection.md#6-one-level-up-defender-for-cloud)

## Known gaps (scope decisions, not oversights)

**G3 — exfiltration through rendered output is not handled by this backend.** The API returns
the model's text verbatim: no image stripping, no URL rewriting, no output encoding. The sink
is the renderer, so the mitigation is a contract the client must meet:

- [ ] The answer is treated as **untrusted text** and rendered as plain text by default.
- [ ] If Markdown is rendered, **raw HTML is disabled**.
- [ ] Remote image and URL fetches happen only via an **allowlist, a proxy, or an explicit user action**.
- [ ] Model output is **never handed to an interpreter** — DOM, shell, SQL or otherwise.

Two more, stated so they are not mistaken for coverage:

- **No jailbreak or injection detector runs in the request path** (T1). Prompt Shields is probed from outside; it is not a dependency of `/chat`, `/rag` or `/agent`.
- **Read-only tools are today's toolset, not an enforced invariant** (T4). Nothing in the code prevents a mutating tool from being added; the review trigger above is the only control.
- **A failed turn may incur billable upstream processing without entering the ledger** (T9) — [cost-and-monitoring.md](cost-and-monitoring.md#known-gaps-disclosed-not-hidden).
