# Production Readiness Checklist (Day 29)

Seven topics. Every line pairs a **portable requirement** — something you could
carry to another backend — with **this lab's answer**, which is one of:

- a **file** in this repository that implements it,
- **Not present**, or
- **Out of scope for a lab**, which is reserved for requirements that ask for an
  *organization* or a *real user population*. "We could have but didn't" is
  **Not present**, not out of scope. That distinction is the only thing keeping
  the third category from becoming a junk drawer.

## How the two layers are separated

A line is machine-checkable **if and only if a runnable command follows it**.
There is no `[verifiable]` tag, because a tag is a claim and a command is a
check. Every command below was run on 2026-08-24 against this tree; where a
line has no command, it has a question you have to answer yourself instead.

The split does not follow the topic. It follows whether the requirement is a
**property of the code** or a **decision made by a person**:

|  | command follows | question follows |
|---|---|---|
| **points at a file** | the container runs as a non-root user | error messages leak no upstream detail |
| **Not present** | there is no inbound rate limiting | there is no on-call runbook |
| **Out of scope** | (rare) | retention approved by legal |

"Not present" is often *checkable* — a command that shows the thing is absent
beats a sentence claiming it is. Two warnings about that, both learned here:

- **An absence proof needs a precise command.** `grep -i limiter` over `src/`
  returns 12 hits in this repository, every one of them either the constant
  `_DELIMITER` or the *upstream* `RateLimitError` being handled. None is an
  inbound control. A loose pattern proves the opposite of the truth.
- **A command that reads empty is not a command that read zero.** `az … -o tsv`
  returns an empty string on failure, and an unguarded comparison turns that
  into a finding. Compare against a value, and fail closed on empty.

---

## Security

Every control, with the file that implements it:
[security-checklist.md](security-checklist.md). This page does not restate them.

---

## Reliability

- [ ] **State held in one process is not lost when the service scales out.**
      This lab: **Not present.** `InMemoryConversationStore` keeps five `dict`s
      in process memory (`services/conversation_store.py`), and the only thing
      standing between that and lost conversations is `maxReplicas: 1` in
      `infra/scripts/deploy-container-app.sh` — a pin taken for *measurement
      attribution* ([container-apps.md §5](container-apps.md#5-scaling)), not as
      a state design. Raising `maxReplicas` **may expose** 404s and lost
      conversations from the per-process store. How Container Apps routes
      requests, and whether session affinity would mask it, has not been
      measured here.
      *You have to answer: which component owns conversation state when there
      are two replicas, and who decided that?*

- [ ] **Startup, liveness and readiness probes are configured, and the platform
      actually runs them** — an image `HEALTHCHECK` alone does not survive the
      move to a scheduler. This lab: the app YAML's `probes:` block,
      [container-apps.md §6](container-apps.md#6-probes).
      ```bash
      uv run pytest tests/unit/test_container_app_script_lifecycle.py::test_generated_yaml_carries_the_whole_app_contract
      ```
      That pins the `probes:` block in the YAML the deploy script generates. To
      check a *running* app instead — which is a different claim, and the one
      that matters once something has drifted — read it back:
      `az containerapp show … --query "properties.template.containers[0].probes"`.

- [ ] **Shutdown has an explicit budget, and drain and cleanup do not silently
      eat the same seconds.** This lab: `core/config.py` derives the cleanup
      bound from the platform grace minus the drain window minus a margin, and a
      test pins the arithmetic *and* the Dockerfile flag it mirrors.
      ```bash
      uv run pytest \
        tests/unit/test_config.py::test_cleanup_budget_bound_is_what_the_drain_leaves_of_the_platform_grace \
        tests/unit/test_config.py::test_request_drain_constant_matches_the_dockerfile_cmd
      ```

- [ ] **A configuration mistake fails at startup, not on the first request that
      needs it.** This lab: `prompts/loader.py` loads every prompt at
      composition time and raises; `core/telemetry.py` validates all three
      controlled environment variables before writing any of them.
      ```bash
      uv run pytest tests/unit/test_prompt_loader.py
      ```
      This is also the fault deliberately injected during the Day 29 rollback
      session — see [Rollback](#rollback).

- [ ] **Calls to a dependency have a timeout and a retry ceiling, and you can
      state what each one means.** This lab: `llm_timeout_seconds` (30.0) and
      `llm_max_retries` (2) in `core/config.py`. The timeout is **per attempt**,
      so one logical call can reach the provider three times; what that costs is
      an open question recorded in
      [cost-and-monitoring.md](cost-and-monitoring.md#known-gaps-disclosed-not-hidden).
      ```bash
      uv run python -c "from azgenai_lab.core.config import Settings; s=Settings(_env_file=None); print(s.llm_timeout_seconds, s.llm_max_retries)"
      ```

- [ ] **A failing dependency produces an explicit failure, not a quiet wrong
      answer.** This lab: zero retrieved hits short-circuit to `no_answer`
      without calling the model (`services/rag.py`); an unavailable index
      surfaces as `search_unavailable` rather than an empty-context answer.
      ```bash
      uv run behave
      ```

- [ ] **Error responses carry a stable envelope and leak no upstream detail.**
      This lab: [api-conventions.md](api-conventions.md#error-envelope).
      *You have to answer: has anyone read the actual bodies your service
      returns on its five most common failures, this quarter?* The BDD suite
      pins the shape; it does not prove nothing sensitive ever reaches a message.

- [ ] **There is a load test, or at least a documented basis for the capacity
      ceiling.** This lab: **Not present.**
      ```bash
      ls tests/            # unit, integration, bdd -- no load or soak suite
      ```

---

## Observability

- [ ] **Every request carries one identifier that joins across layers, and the
      inbound value is bounded before it is used.** This lab:
      `core/telemetry.py` (`accept_correlation_id`),
      [observability.md](observability.md#correlation). The correlation id, not
      the trace id, is the authority.
      ```bash
      uv run pytest tests/unit/test_correlation_validation.py
      ```

- [ ] **Traces cover outbound dependencies, not just your own handlers.**
      This lab: each of the six httpx clients is instrumented individually,
      because the distro bundles no httpx instrumentation
      ([observability.md](observability.md#composition)).
      ```bash
      uv run pytest tests/unit/test_telemetry_config.py::test_traces_only_options_are_passed
      ```

- [ ] **Transport spans are not read as generation latency.** This lab: an httpx
      span ends at response headers, so for a streamed reply it covers a
      fraction of the time the model spent; `TracingChatService` owns a semantic
      span instead ([observability.md](observability.md#streaming)).
      *You have to answer: which span on your dashboard is the one somebody will
      quote in a latency conversation, and does it end where they think?*

- [ ] **Telemetry carries no content, enforced by structure rather than
      discipline.** This lab: forbidden **attribute names** are asserted
      recursively. The honest gap is stated where it lives: the framework's
      `execute_tool` span carries the tool's docstring, which a name check
      cannot catch.
      ```bash
      uv run pytest tests/unit/test_telemetry_spans.py::test_no_forbidden_attribute_names_anywhere
      ```

- [ ] **High-volume traffic you generate yourself does not drown the signal.**
      This lab: `/health` is excluded by an anchored regex — anchored so that
      `/api/v1/healthz` is not swallowed with it.
      ```bash
      uv run pytest \
        tests/unit/test_telemetry_wiring.py::test_health_produces_no_server_span \
        tests/unit/test_telemetry_wiring.py::test_non_health_url_containing_health_still_produces_a_span
      ```

- [ ] **Sampling is a decision, not a default.** This lab: pinned to 1.0, with
      the distro's own default (a rate-limited sampler) written down next to it
      ([observability.md](observability.md#sampling)). 1.0 is a lab decision;
      at real volume it is the wrong one.
      ```bash
      uv run pytest tests/unit/test_telemetry_config.py::test_traces_only_options_are_passed
      ```

- [ ] **Something pages a human.** This lab: **Not present.** There is a
      subscription budget alert — a delayed cost notification — and no
      application alert rule of any kind.
      ```bash
      grep -rlE "az monitor (metrics|scheduled-query) alert" infra/scripts/ | wc -l   # 0
      ```
      This deployment has traces, an audit trail and a dashboard's worth of
      data, and **nothing that would wake anyone up.**
      *You have to answer: what is the first signal a human receives, and how
      does it reach them at 03:00?*

---

## Cost

- [ ] **Every call has an output ceiling.** This lab: `LLM_MAX_OUTPUT_TOKENS`
      (default 1000), [cost-and-monitoring.md](cost-and-monitoring.md#the-two-guardrails).
      ```bash
      uv run pytest tests/unit/test_token_budget.py
      ```

- [ ] **A budget is checked *before* inference, not after the bill.** This lab:
      `CONVERSATION_TOKEN_BUDGET` (default 50000) rejects with `429` before the
      call ([cost-and-monitoring.md](cost-and-monitoring.md#the-two-guardrails)).
      ```bash
      uv run behave
      ```

- [ ] **The accounting authority is the invoice, not your own counters.**
      This lab: stated in [cost-and-monitoring.md](cost-and-monitoring.md),
      and the ledger is documented as a guardrail rather than a bill.
      *You have to answer: when your number and Cost Management disagree, which
      one does your team act on — and has that ever been tested?*

- [ ] **The subscription-level backstop exists and is understood not to be a
      cap.** This lab: `infra/scripts/create-budget-alert.sh`. Crossing a budget
      threshold notifies; it does not stop resources, and cost data lags.
      *You have to answer: who receives that alert, and what do they do about
      it that the alert itself does not do?*

- [ ] **Gaps between what you meter and what you are billed are written down.**
      This lab: two are. A failed turn may incur billable processing and never
      enter the ledger; and because the client-side timeout is per attempt, a
      **successful** turn can sit on top of up to two abandoned attempts that
      the ledger, the `llm usage` line and the audit event all miss
      ([cost-and-monitoring.md](cost-and-monitoring.md#known-gaps-disclosed-not-hidden)).

- [ ] **You can list which meters a deployment turns on, and how much of each it
      uses.** This lab: **partly.** The meter list is
      [container-apps.md §12](container-apps.md#12-cost-shape); the *quantity*
      on the two dominant ones was not. The app YAML pins replica count and
      says nothing about CPU or memory, so the platform default decides the
      largest term in the bill.
      ```bash
      az containerapp show -g "$AZ_RESOURCE_GROUP" -n "$AZ_ACA_APP_NAME" \
        --query "properties.template.containers[0].resources" -o json
      ```
      Read back on 2026-08-24: `cpu: 0.5`, `memory: 1Gi`. Nothing in this
      repository had recorded that; it took a deployment to find out.

- [ ] **Nothing bills while nobody is looking.** This lab: **partly.** The
      one-replica pin bills from provisioning until teardown on purpose, and an
      orphaned Log Analytics workspace is the known way to leave money running —
      which is why teardown deletes it as its own step and reads back.
      *You have to answer: what in your subscription was created by a person who
      has since stopped thinking about it?*

- [ ] **Per-user quota.** This lab: **Not present**, deliberately. Day 19 put a
      verified identity on every protected request, so it became possible and
      was still not done; a per-identity counter needs its own durability,
      reset and cross-instance-consistency story.

---

## Data governance

- [ ] **The system of record for content is named.** This lab: the conversation
      store, explicitly not the log
      ([audit-logging.md](audit-logging.md#never-log)).

- [ ] **Sensitive fields cannot reach a log, enforced by absence rather than by
      a write-time filter.** This lab: audit events have no content-bearing
      field, and a recursive test walks the schema to prove it.
      ```bash
      uv run pytest \
        tests/unit/test_audit_schema.py::test_no_content_bearing_field_anywhere \
        tests/unit/test_audit_rag.py::test_rag_never_logs_question_or_chunk_content
      ```
      A filter you can forget to apply is a different control from a field that
      does not exist.

- [ ] **You keep ownership of conversation state rather than leaving it with the
      provider.** This lab: the Responses API is called with `store=False`.
      ```bash
      grep -rnE 'store=False|"store": False' src/azgenai_lab/services/
      ```

- [ ] **Identifiers in logs are classified.** This lab: `user_id` is documented
      as pseudonymous personal data — a directory object id in Entra mode —
      rather than an inert tag ([api-conventions.md](api-conventions.md#logging)).

- [ ] **A retention period exists, and someone with authority approved it.**
      This lab: **Not present.** `audit-logging.md` states plainly that the
      document makes no retention promise: the trail is JSON lines on stderr,
      and retention is whatever the hosting log pipeline does.
      ```bash
      grep -rniE "retention" src/azgenai_lab/core/config.py | wc -l   # 0
      ```
      *You have to answer: what is your retention period, who approved it, and
      on what date?* No amount of code will answer that one — which is the
      clearest example on this page of a requirement whose subject is a person,
      not a program.

- [ ] **You can say which region data lands in.** This lab: japaneast, chosen
      and recorded, but there is no data-residency statement.
      *You have to answer: does any obligation you are under name a region, and
      does anything stop a future deployment from choosing another?*

- [ ] **A deletion request has an execution path.** This lab: **Not present.**
      There is no delete endpoint and no persistent datastore to delete from.
      ```bash
      grep -rn "def delete" src/azgenai_lab/api/ | wc -l                    # 0
      grep -rniE "sqlalchemy|asyncpg|psycopg|pymongo|redis" src/ | wc -l    # 0
      ```

- [ ] **The model provider's data-processing terms were reviewed.** This lab:
      **Not present.** `store=False` is a technical decision; it is not a review
      of terms, and the two are easy to mistake for each other.

---

## Incident response

- [ ] **There is an on-call rotation and an escalation path.** This lab:
      **Out of scope for a lab** — the requirement asks for an organization, and
      there is no operations team here. This is the only out-of-scope line on
      this page.

- [ ] **An audit trail answers who did what, when.** This lab: one terminal
      event per classified request ([audit-logging.md](audit-logging.md)), with
      its limits stated in the same document: not durable, not tamper-evident,
      a process crash between outcome and flush loses the event.
      ```bash
      uv run pytest tests/unit/test_audit_emitter.py
      ```

- [ ] **You can follow one request from the edge to the upstream call.**
      This lab: `correlation_id` joins the stage lines, the `llm usage` line and
      the audit event; Day 27 carries it onto spans.
      ```bash
      uv run pytest tests/unit/test_telemetry_rag.py::test_rag_spans_carry_the_correlation_id
      ```

- [ ] **There is a runbook for an incident** — as opposed to a runbook for a
      deployment. This lab: **Not present.**
      [container-apps.md §8](container-apps.md#8-the-deploy-session-end-to-end)
      is a deploy runbook and [ci-cd.md §9](ci-cd.md#9-teardown-runbook-its-ordering-contract-and-no-admission-lock)
      is a teardown runbook. Neither tells you what to do at 03:00.

- [ ] **Your recovery procedure has been executed, and there is a record.**
      This lab: rollback has ([Rollback](#rollback)). An incident drill has not,
      and the rollback session is not one — nobody was paged, nothing was
      diagnosed under uncertainty.

- [ ] **A leaked secret can be revoked, and someone has done it.** This lab:
      **partly.** [key-vault-config.md §4](key-vault-config.md#4-rotation-what-key-vault-does-and-does-not-solve)
      documents what rotation actually costs — key regeneration invalidates
      immediately, a versionless reference picks up a new value within about 30
      minutes, and only env-var references restart the revision. None of it has
      been rehearsed.
      *You have to answer: how long between "the key is public" and "the key is
      dead", and how do you know?*

- [ ] **Your recovery procedure cannot be interrupted by something unrelated to
      the system being recovered.** This lab: **Not present** as a control, and
      the Day 29 session is the counterexample. Teardown order is a contract —
      role assignments before the identity that holds them, or they become
      orphans attributable to nothing
      ([container-apps.md §9](container-apps.md#9-teardown-ordering-is-a-contract-not-a-convenience)) —
      and the teardown has now been cut short three times, by three unrelated
      causes: a server-side delete that returned before it finished (Day 24), a
      client-side long-poll timeout (Day 25), and a wall-clock limit imposed by
      the terminal the operator happened to run it in (Day 29). Different layer
      each time; identical damage. What covers it is that the script is
      idempotent and the operator re-runs it.
      *You have to answer: if your recovery procedure stops halfway, is the
      system in a worse state than if it had never started?*

---

## Rollback

- [ ] **Deployments address an immutable reference, not a moving tag.**
      This lab: **partly.** The CI `deploy` job addresses a digest
      ([ci-cd.md §5](ci-cd.md#5-digest-not-tag)); the first deployment of a
      session, through `infra/scripts/deploy-container-app.sh`, writes a tag.
      ```bash
      uv run pytest tests/unit/test_update_container_app_script_lifecycle.py::test_image_reaches_az_unmodified_including_digest_form
      ```

- [ ] **The previous version is recorded before you overwrite it.** This lab:
      `infra/scripts/update-container-app.sh` snapshots the prior template image
      in step 1 — and warns, when that snapshot is a tag, that a tag is not a
      version.
      ```bash
      uv run pytest tests/unit/test_update_container_app_script_lifecycle.py::test_snapshot_that_is_a_digest_gets_no_tag_warning
      ```

- [ ] **The readiness check is a failure detector, not a success oracle.**
      This lab: the revision poll aborts on the enum's named failure states,
      keeps waiting through `Processing`, and treats every other value as *not
      evidence of failure* — because values outside the CLI extension's enum
      keep showing up (`RunningAtMaxScale` and `Activating`, four observations
      across Day 25 and Day 29).
      ```bash
      uv run pytest tests/unit/test_update_container_app_script_lifecycle.py -k "revision"
      ```

- [ ] **Automated deployment refuses to ship a stale commit.** This lab: the
      freshness guard fails when `github.sha` is no longer main's HEAD, and
      fails closed when the query about itself fails.
      ```bash
      uv run pytest tests/unit/test_check_freshness_script.py
      ```

- [ ] **You have actually rolled back, and timed it.** This lab: yes, twice, on
      2026-08-24 in japaneast. Control-plane convergence was **20 s** and
      **21 s** — two single observations on one date, not a trend and not an
      SLO. The measurement is T0 (command issued) to T1 (a new revision appears
      and is not in a known failure state), one clock, and it says nothing about
      when the data plane became usable.

- [ ] **You know what your health check does when the new version cannot
      start.** This lab: **now we do, and the answer is the uncomfortable one.**

  This was [ci-cd.md §11](ci-cd.md#11-what-the-live-session-settled-and-what-is-still-open)'s
  open question. Two failure modes were injected on 2026-08-24, one at a time:
  an image reference whose digest does not exist, and an image that pulls
  cleanly and then dies during startup (a required prompt file removed, so
  `load_prompt` raises at composition time).

  **Both times, all three of the deploy script's checks passed and the script
  exited 0.** The template read-back passed, because it reports what was
  requested. `runningState` read `Activating`, which is not a known failure
  state. And `/health` returned the byte-exact expected body — because under
  single revision mode the previous revision keeps serving while the new one
  cannot start.

  That is not an inference. Container Apps console logs carry `RevisionName_s`,
  and the external probe appears against the **old** revision each time, while
  the new one logged either nothing at all (image-pull failure) or a crash loop:

  ```text
  01:45:03.66Z  aca-…--0000002   "GET /health HTTP/1.1" 200 OK      <- previous revision
  01:45:04.71Z  aca-…--0000003   PromptTemplateError: … default_chat.md
  ```

  The control plane does say so, in a field nothing here was reading:
  `latestRevisionName` was the broken revision, and
  **`latestReadyRevisionName` was still the old one**. Traffic weight is not
  the tell — it read 100 on the broken revision both times.

  **The gate now reads that field.** `update-container-app.sh` step 3b polls
  `latestReadyRevisionName` until it matches the revision the update produced,
  and fails when it never does — a poll rather than a single read, because on
  a healthy deploy the field legitimately trails before catching up. Re-verified
  against real Azure the same day: both failures now exit non-zero, **and a
  healthy deployment still exits 0**.
      ```bash
      uv run pytest tests/unit/test_update_container_app_script_lifecycle.py -k "ready or never_becomes"
      ```

  Scope: one session, one date, one region, single revision mode, one
  observation per scenario. It establishes that the false pass happened and
  that these three cases now behave correctly — not that every case does.

      *You have to answer: does your deployment gate distinguish "the new
      version is serving" from "something is serving"?* Note which side of
      that line the four checks fall on: three of them answer "something is
      serving" and only one answers the question you actually have.

- [ ] **The rollback instructions are available at the moment you need them.**
      This lab: `update-container-app.sh` prints them on **both** paths. It
      used to print them only on failure — and the two failures above exited 0,
      so an operator who needed to roll back was handed nothing. A recovery
      instruction that only appears on the branch that happens not to fire is
      not a recovery instruction.
      ```bash
      uv run pytest tests/unit/test_update_container_app_script_lifecycle.py::test_rollback_command_is_printed_on_the_success_path_too
      ```
      One limit worth stating: the command rolls back to the **pre-mutation
      snapshot**, which is the state before this update — not necessarily a
      state that worked. Those coincide in normal use and come apart during
      consecutive incidents.
      *You have to answer: does your rollback target the last version, or the
      last version that worked, and do you know which?*

---

## What this page does not do

It does not score anything. There is no percentage, no maturity level and no
count of ticked boxes, because the lines are not equally weighted and a number
would invite exactly the comparison the split above exists to prevent: a
requirement with a command behind it and a requirement with a question behind
it are not the same kind of claim, and averaging them destroys the only
information this page carries.
