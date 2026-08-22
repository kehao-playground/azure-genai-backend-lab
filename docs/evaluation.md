# Evaluation (Day 28)

Nothing on this page supports a percentage, and that has to be said before
any number does appear: ten hand-picked questions and five repeated judge
calls per question cannot estimate a population, and reading either count
as one would repeat a mistake this series has already made and corrected
twice — a single retrieval probe treated as more than one observation
(Day 13), a hand-picked eight-case security probe almost written up as a
detection rate (Day 21). So the rule this page follows throughout: no rate
is derived from the numbers below. Five judge repeats that agree are
reported as *not observed to flip this time*, never as *stable*. A run
where they disagree is reported as *observed to flip*, never as *N%
unstable*. Neither result proves anything about the next run — only about
this one.

With that fixed, here is what Day 28 actually adds: `tools/eval_cases.json`
(ten questions over the real sample corpus, each carrying a set of
citation and content assertions) and `tools/eval_run.py` (a runner that
scores every question two different ways and gives only one of those ways
the power to fail a build).

## 1. This lab already had most of an eval suite

Three existing pieces each cover one layer, none of them under that name:

| Existing tool | Layer | What it already gets right | What it does not cover |
|---|---|---|---|
| `tools/compare_retrieval.py` (Day 13) | Retrieval, golden query set | Frozen queries, pre-registered chunk ids, one variable per experiment, checkpointed evidence | Needs a live Search service; not named as an eval |
| `tools/tenant_smoke.py` (Day 15) | Authorization, regression | PASS/FAIL/INCONCLUSIVE three-state result; never asserts against an empty result set; an authorized baseline gates the other probes | ACL only |
| `tests/bdd/features/` (behave, 8 features) | Contract, regression | HTTP status codes, the error envelope, the SSE vocabulary | Never looks at answer content |

The one layer genuinely missing was generation quality — whether an answer
actually says what it should, not whether the pipeline wired up correctly.
That is what `tools/eval_cases.json` and `tools/eval_run.py` add.

### 1.1 Overlap with existing behave coverage, stated plainly

`tests/bdd/features/rag_no_answer_policy.feature` already has a scenario
titled "Zero retrieval hits produce a no-answer response without calling
the LLM", and `tests/bdd/features/tenant_isolation.feature` already covers
cross-tenant and missing-group denial. Three of the ten eval cases —
`zero-hit-structural-no-answer`, `globex-oncall-ack-window-denied`, and
`acme-asks-globex-dispute-window` — assert the same behaviors those
scenarios already assert.

The overlap is real, and it is not where this dataset earns its keep. The
difference is the input: behave drives synthetic fixture documents: the
eval dataset drives the real sample corpus through real questions. That
buys one class of regression behave structurally cannot see — the corpus
itself breaking (a new document shipped without `allowed_groups`, a
confidential paragraph moved into a public document) — while behave keeps
covering the HTTP layer this dataset never touches (§2). Neither suite
replaces the other, and this dataset does not claim to be the primary
defense for the three behaviors it happens to share with behave.

## 2. The corpus problem: fake search starts out empty

`build_search_client` returns an empty `FakeSearchClient` in fake mode
(`src/azgenai_lab/services/azure_search.py:505-509`) — zero documents means
zero hits, which trips Day 14's structural no-answer short-circuit before
the model is ever called. Calling `RagService.answer()` against that empty
client would make all ten cases report `no_answer` and "pass" every
assertion they have no ability to check.

Day 17 hit the identical wall: `_seeded_fake_retriever`
(`src/azgenai_lab/services/agent_tools.py:210`) exists specifically to run
the real chunking pipeline over the real corpus and seed a `FakeSearchClient`
with the result, because — its own docstring's words — "a wiring demo over
zero documents would prove nothing." `tools/eval_run.py` reuses that exact
function (via `build_seeded_rag_service`) rather than writing a second
seeding path that could drift from the first.

The cost of reuse over a second production code path: the deterministic
layer exercises `RagService.answer()` directly, not the `/rag` HTTP
handler. Everything at or above that boundary — routing, request parsing,
the SSE surface — is behave's job, not this dataset's. Nothing under
`src/azgenai_lab/` changed to make this milestone possible; it works
entirely by reusing an existing seam.

## 3. Two layers, and only one of them owns the exit code

| | `deterministic` | `judged` |
|---|---|---|
| Generates with | Fake LLM, corpus-seeded fake search | Real `chat-mini` generation, real `chat-mini` judging |
| Scores | Status, which documents were cited, cross-tenant leakage, prerequisite health | Whether the answer covers the expected facts and avoids the forbidden ones |
| Reproducible across repeats? | Yes | No — measuring that instability is the point |
| Affects the exit code? | Yes, and only this layer does | Never |
| Verdict authority | Code | A human, recorded against one run's evidence, not against the dataset (§7) |

This is a structural split, not a shared field: a case's assertions live
under two separate JSON keys, `deterministic` and `judged`, not one array
with a `"layer"` tag a typo could misclassify. Getting a string wrong
cannot silently promote a judged assertion into a gating one.

`tools/eval_run.py` maps outcomes to a process exit code:

| Exit code | `ExitCode` name | What produced it | Why it is its own code |
|---|---|---|---|
| `0` | `OK` | The deterministic gate ran and every case's deterministic verdict was `PASS` (judged-layer results, if any, do not affect this) | The only green result |
| `1` | `GATE_FAILED` | The deterministic gate ran and at least one case came back `FAIL` or `INCONCLUSIVE` | The only code that means "something is wrong with the thing under test" |
| `2` | `SETUP_FAILED` | Dataset validation failed, the corpus would not load, or `--judge` was passed without usable credentials — every one of these happens *before* any verdict exists | A run that never reached a verdict must not look the same as `0` |

`--judge` without credentials is a `SETUP_FAILED`, not a silent skip: a
layer that did not run and a layer that ran clean have to be visibly
different results, or a missing check reads as a passing one. The judged
layer, however it turns out, never changes which of these three codes the
process exits with — `gate_exit_code` only ever takes the deterministic
results as its argument; there is no parameter through which a judged
verdict could reach it.

## 4. Dataset format: `tools/eval_cases.json`

One JSON file, `{"version": 1, "corpus": "data/sample-docs", "cases": [...]}`,
following the shape this repo already uses for a hand-picked case set plus
its own runner (`tools/prompt_shields_cases.json`, Day 21). Each case:

```json
{
  "id": "acme-refund-window-standard",
  "question": "How many days does a customer have to return a standard purchase for a full refund?",
  "principal": { "tenant": "acme", "user": "eval-agent", "groups": [] },
  "protects": "Day 14 answered path: single-document must_cite/subset agreement",
  "requires": [],

  "deterministic": {
    "status": "answered",
    "status_note": null,
    "must_cite": ["returns-policy"],
    "citations_subset_of": ["returns-policy"],
    "subset_note": null,
    "must_not_cite": []
  },

  "judged": {
    "expected_facts": [{ "id": "fact_standard_window_30_days", "text": "..." }],
    "forbidden_facts": [{ "id": "fact_14_days_for_standard", "text": "..." }],
    "rubric": null
  },
  "judged_skip_reason": null
}
```

### 4.1 Field by field

| Field | Required | Rule |
|---|---|---|
| `id` | yes | Unique across the file; every report line, every `requires` entry, and every evidence record joins on it |
| `question` | yes | The literal question sent to the service |
| `principal` | yes | `tenant` / `user` / `groups`; every case carries the identity that asks it |
| `protects` | yes | Which day's decision this case guards. A case with no cited decision is a wish, not a regression test |
| `requires` | yes | Array, may be empty. Ids of prerequisite cases (§4.3) |
| `deterministic.status` | yes | `"answered"`, `"no_answer"`, or `null` (no assertion made) |
| `deterministic.status_note` | conditional | Required, non-empty, when `status` is `null` — forces a reason to be written down |
| `must_cite` | yes | Array of doc ids (may be empty). All must appear among the cited documents |
| `citations_subset_of` | yes | Array of doc ids, or `null` (no closed-set claim) |
| `subset_note` | conditional | Required, non-empty, when `citations_subset_of` is `null` |
| `must_not_cite` | yes | Array of doc ids (may be empty). None may appear |
| `judged` | yes | An object, or `null` |
| `judged_skip_reason` | conditional | Required, non-empty, when `judged` is `null` |
| `judged.expected_facts` | yes | Facts the answer must cover, each `{id, text}`; the judge reports only `id`s, never text (§6) |
| `judged.forbidden_facts` | yes | Facts the answer must not assert, same `{id, text}` shape |
| `judged.rubric` | yes | Free text, or `null` to fall back to the built-in rubric |

### 4.2 `[]` versus `null`

Two values that both mean "empty" only look interchangeable until a
validator has to decide which fields may use which:

| Value | Meaning |
|---|---|
| `[]` | The assertion runs, over an empty set |
| `null` | The assertion is not made at all |

Only `citations_subset_of` may be `null`, because its `[]` already carries
a distinct, useful meaning of its own: *sources must be empty*
(`zero-hit-structural-no-answer` uses exactly that, in place of a prose
comment). `must_cite` and `must_not_cite` may only ever be arrays — for
them, `[]` and "not asserted" mean the same thing (no document is required,
none is forbidden), and giving the same meaning two spellings would only
create ambiguity where none is needed. The dataset loader (`load_cases`,
`tools/eval_run.py`) enforces this at parse time: a `null` `must_cite` or
`must_not_cite` is a dataset error, not a runtime default.

### 4.3 `requires`

A case can name prerequisite case ids. `tools/eval_run.py` propagates
purely off each prerequisite's *deterministic* verdict — the judged layer
never participates in this decision, or its non-repeatable noise could
leak into the gate. If any prerequisite is not `PASS` (`FAIL` or
`INCONCLUSIVE` both count), the dependent case's own `PASS` is downgraded
to `INCONCLUSIVE`; a dependent that already failed on its own merits is
left alone. `globex-oncall-ack-window-denied` requires
`globex-oncall-ack-window`: a denial case only means something once its
own authorized baseline is known to work.

A cycle, a self-reference, or a `requires` entry naming an unknown id is
rejected at load time. Evaluation may proceed in any topological order —
an acyclic graph guarantees one exists, not that it is unique — but the
console report and evidence records always list cases in dataset order,
so two runs are line-for-line comparable regardless of which valid
evaluation order either one happened to use internally.

### 4.4 What the validator checks beyond field shape

`tests/unit/test_eval_cases.py` and `load_cases` together also enforce,
against the real corpus under `data/sample-docs/`:

- every doc id in `must_cite`, `must_not_cite`, and `citations_subset_of`
  actually exists, and under the case's own tenant
- `must_cite` and `must_not_cite` never share a doc id
- when `citations_subset_of` is not `null`, `must_cite` is a subset of it,
  and it shares no doc id with `must_not_cite`
- `expected_facts` and `forbidden_facts` ids are unique within one case
  (the judge reports ids, not text — a duplicate id would make its
  response ambiguous)
- `principal.groups` is checked for identifier format only, never for
  membership in the corpus's `allowed_groups` — a syntactically valid
  group with no matching document is still a legitimate way to test "an
  unauthorized principal asks this"

## 5. Golden questions and a regression dataset are the same file, at two different times

The first time a question is written down, it describes what the system
*should* answer — a golden question. The moment something has actually
broken once, and that question is kept (with its `protects` field pointing
at the decision it now guards), the same file is a regression dataset.
There is no second file to maintain, and no migration step between the two
states: `tools/eval_cases.json` is both, depending only on whether a case
has already caught a real regression yet.

## 6. Running it

```
uv run python tools/eval_run.py                         # deterministic layer only, offline
uv run python tools/eval_run.py --judge                 # + judged layer, needs real chat-mini credentials
uv run python tools/eval_run.py --judge --repeats 5      # + N judge repeats per case (default is already 5)
uv run python tools/eval_run.py --calibrate --lab-root .  # retrieval-only calibration document, no generation
```

Without `--judge`, the process makes zero provider calls: it runs the fake
LLM over the corpus-seeded fake search, scores every case's deterministic
assertions, and prints one line per case (`VERDICT<TAB>case_id`, followed
by any failure detail) before exiting with the code from §3.

`--judge` runs two additional generation passes, on top of the always-run
deterministic pass:

| Pass | Model | Calls per case | Produces | Used for |
|---|---|---|---|---|
| A — deterministic | Fake | 1 | status, cited sources | The gate, and the exit code |
| B — generation | Real `chat-mini` | 1 (only for judged-eligible cases) | The answer and sources the judge will review | Input to pass C |
| C — judging | Real `chat-mini` | `--repeats` (default 5) | A judge verdict per repeat | The judged-layer result and its stability record |

Passes A and B use the same retriever, principal, and question, so their
sources should be identical — retrieval carries no randomness here. If a
case's `sources_sha256` disagrees between the two passes, that case's
judged result is `INCONCLUSIVE(pass_a_pass_b_sources_sha256_mismatch)` and
judging is not attempted at all: the measuring instrument itself
disagreed with its own earlier reading, so nothing downstream of that can
be attributed to judge instability specifically.

`--calibrate` skips generation entirely and instead emits a JSON document
of retrieval-only observations (hits, ranks, chunk ids) for every case
against the currently seeded corpus — the same shape this dataset's
questions were originally calibrated against
(`reviews/evidence/day28/calibrate_probe.py`, kept in the private planning
repo since it names no production code). It exists so a reader can check
that a case's citation assertions still match what the corpus actually
returns, without spending a single generation call to find out.

## 7. The judge contract

The judge is the same `chat-mini` deployment used for generation, given a
fixed prompt (`JUDGE_PROMPT`, module constant in `tools/eval_run.py` —
deliberately outside `src/azgenai_lab/prompts/`, Day 8's production prompt
registry, since this milestone changes no production code) and a JSON
input built by `build_judge_input`:

```json
{
  "question": "...",
  "answer": "BEGIN UNTRUSTED ANSWER <nonce>\n...\nEND UNTRUSTED ANSWER <nonce>",
  "sources": [{"doc_id": "...", "heading_path": "...", "content": "BEGIN UNTRUSTED SOURCE <nonce> 1\n...\nEND UNTRUSTED SOURCE <nonce> 1"}],
  "expected_facts": [{"id": "fact_standard_window_30_days", "text": "..."}],
  "forbidden_facts": [{"id": "fact_14_days_for_standard", "text": "..."}]
}
```

### 7.1 The data boundary

`answer` and every `sources[].content` are untrusted data, not
instructions — both a generated answer and retrieved corpus text can
contain instruction-shaped wording, and the judge is precisely the thing
reading both. The only trusted instruction sources are `JUDGE_PROMPT`
itself and the dataset's own `expected_facts`/`forbidden_facts` schema
fields. Both untrusted fields are wrapped in a `BEGIN UNTRUSTED ... {nonce}`
/ `END UNTRUSTED ... {nonce}` fence, where the nonce is drawn once per call
(`secrets.token_hex(16)`) — the same per-request nonce discipline `/rag`
adopted after Day 21 found its own fixed-literal fence could be forged by
corpus text. The prompt tells the model to treat fenced content as data
regardless of what it says.

That prompt wording is **instruction-level mitigation, not a structural
guarantee** — the same honest limit Day 21 recorded for tool results.
Nothing stops a sufficiently adversarial model from ignoring the
instruction anyway. The actual structural defense is what comes next: a
judge response steered into inventing, dropping, or misclassifying a fact
id is rejected before it ever becomes a verdict, regardless of what the
prompt asked for.

### 7.2 Output shape and the four invariants

The judge replies with exactly one JSON object and nothing else:

```json
{
  "covered_fact_ids": ["fact_standard_window_30_days"],
  "missing_fact_ids": [],
  "violated_fact_ids": [],
  "unsupported_claims": [],
  "rationale": "..."
}
```

The model never returns a verdict field — an earlier design draft let it
report `"verdict": "pass"` alongside its own fact lists, and a response
that claimed `pass` while its lists said otherwise had no rule to resolve
the contradiction. `derive_judge_verdict` computes the verdict itself, from
the parsed lists only, after `parse_judge_response` has checked four
invariants against this case's known fact ids. Any violation raises
`JudgeParseError`, which the caller records as `ERROR(parse)`:

| # | Invariant | What it blocks |
|---|---|---|
| 1 | `expected_facts` ids ⊆ `covered_fact_ids` ∪ `missing_fact_ids` | The judge silently dropping an expected fact — including replying with both lists empty |
| 2 | `covered_fact_ids` ∪ `missing_fact_ids` ⊆ `expected_facts` ids | A forbidden-fact id, an id from a different case, or free text smuggled into either list |
| 3 | `covered_fact_ids` ∩ `missing_fact_ids` = ∅ | The same fact claimed both covered and missing at once |
| 4 | `violated_fact_ids` ⊆ `forbidden_facts` ids | The judge inventing a forbidden fact, or naming an expected one here |

`unsupported_claims` is the one free-text field kept: it carries the
default rubric's catch-all — any factual claim the answer makes that is
not one of the case's declared facts and cannot be grounded in the
supplied sources. It cannot be checked against a known id set, so it is
only ever read as empty-or-not, never matched item by item.

Verdict derivation, once the four invariants hold: `fail` if
`missing_fact_ids`, `violated_fact_ids`, or `unsupported_claims` is
non-empty; `pass` only if all three are empty.

### 7.3 What one repeat can come back as

| Outcome | Recorded as | Judged-layer effect |
|---|---|---|
| Parsed cleanly | `"pass"` or `"fail"` | Counted toward the verdict |
| Malformed JSON, missing key, extra text, or an invariant violation | `ERROR(parse)` | That repeat counts as `INCONCLUSIVE`-worthy |
| Timeout / rate limit / upstream 5xx | `ERROR(upstream)` | Same |
| Content filter rejection | `ERROR(filtered)` | Same |

None of these ever changes the exit code — the judged layer, whatever it
finds, is reported and never gates.

### 7.4 One case, multiple repeats: how a verdict is derived

`derive_judged_result` takes the full, already-run set of repeats for one
case (never fewer than `--repeats` entries — a retry is recorded as its
own attempt, not swallowed) and applies two checks before it will report a
verdict at all:

1. **Every repeat must agree on `answer_sha256` and `sources_sha256`.**
   These are computed once, from pass B's output, before any repeat runs;
   if a later repeat's hash of what it actually reviewed differs (it
   should not — the same `answer`/`hits` are passed to every repeat), the
   case is `INCONCLUSIVE`: the repeats did not all review the same thing,
   so nothing about their disagreement can be attributed to judge
   instability.
2. **Every repeat's outcome must be `"pass"` or `"fail"`** — any
   `ERROR(...)` outcome anywhere in the set makes the case `INCONCLUSIVE`.

When both checks pass, the case's `verdict` is **repeat 1's outcome** —
not a majority, not any other aggregate across the `N` repeats. Computing
a majority (or any other threshold) across a hand-picked, five-repeat
sample would itself be exactly the kind of rate this page's opening
section rules out. The full, ordered sequence of every repeat's outcome is
kept regardless (`repeats` on `JudgedResult`), and that sequence — not a
derived number — is what the report's stability line shows.

### 7.5 `judged: null`, and the states around it

Three states describe why a case's judged `verdict` is absent, so an
absent verdict is never left to mean only one of them by inference:

- **`SKIPPED`** — the dataset itself declares `judged: null`. Zero calls
  are made; the case does not count toward the `N`-repeat statistics.
  `globex-oncall-ack-window-denied` and `zero-hit-structural-no-answer`
  are both `SKIPPED` in this dataset — the first because the assertion is
  about a document's *absence*, which content review cannot add anything
  to; the second because a structural `no_answer` never produces a
  generated answer to review at all.
- **`INCONCLUSIVE`** — a runtime condition made judging impossible or its
  result untrustworthy, even though the dataset declared `judged` for this
  case. Every reason this runner produces:
  - `no_answer_at_runtime` — pass A (or pass B) came back `no_answer` even
    though the dataset expected content to review
  - `pass_b_generation_error: <ExceptionClassName>` — pass B's real
    generation call raised an `UpstreamError`; only the exception's class
    name is recorded, never its message text, because an upstream error
    message is provider text this runner does not control (a real Azure
    429 reads "...exceeded call rate limit.", and this report's whole
    contract is that it never states a rate)
  - `pass_a_pass_b_sources_sha256_mismatch` — described above
  - `answer_sha256/sources_sha256 mismatch across repeats at attempt(s) [...]`
    — from `derive_judged_result`
  - `repeat error(s): attempt N=ERROR(...)` — from `derive_judged_result`,
    when one or more individual repeats errored
- **`JUDGED`** — a real verdict was reached, and `reason` is `None`.

The console report (`render_report`) prints exactly three labeled lines
per case, always, so a case the judged layer never reached and one it
reached but could not judge are both visible — never a blank line standing
in for either:

```
acme-unanswerable-contact
  deterministic: PASS
  judged:        INCONCLUSIVE(no_answer_at_runtime)
  stability:     NOT MEASURED
```

`stability` is `NOT MEASURED` whenever no repeats ran at all (`SKIPPED`,
or any `INCONCLUSIVE` reached before judging started); it is the literal,
comma-joined sequence of every repeat's outcome whenever at least one
repeat did run — including an `INCONCLUSIVE` case reached *during*
`derive_judged_result`, so a reader can see exactly which attempt
disagreed, not just that something did.

## 8. Human feedback: the authority is a person, bound to one run

The dataset never carries a human's verdict on a specific answer. Only
case-level expectations (`expected_facts`, `forbidden_facts`, `rubric`)
live in `tools/eval_cases.json` — those describe what a good answer to this
question looks like, independent of any one run.

A human's read of an actual answer belongs in that run's evidence record
(`evidence_document`), tied together by hash: `run_id`, `answer_sha256`,
`sources_sha256`, `lab_commit`, and `dataset_sha256`. Putting a human
verdict in the dataset itself would conflate two different judgments that
must stay separable — "is this expectation reasonable" versus "did this
specific run's answer meet it" — and only the second one is falsifiable
against a specific, hash-identified answer. The judged layer's automated
verdict is not a substitute authority; it is a second, repeated,
machine-produced opinion that a human's read can agree or disagree with,
on the same evidence.

## 9. What this does not measure

1. **Retrieval quality.** `FakeSearchClient` scores lexically in every
   mode, and its own docstring says exactly that: "Retrieval quality
   observed here means nothing"
   (`src/azgenai_lab/services/azure_search.py:411-436`). The judged layer
   scores generation over whatever `FakeSearchClient` happened to return —
   never whether that return set was a *good* one. Measuring real recall
   needs a live Search service (currently torn down) and belongs to
   `tools/compare_retrieval.py`, not here.
2. **A judge that is not the model under test.** The judge is the same
   `chat-mini` deployment doing the generation, with no second model as a
   cross-check. Known self-review bias applies, and this milestone has no
   second deployment to measure "how much does the choice of judge model
   change the verdict."
3. **The HTTP layer.** The deterministic layer calls `RagService.answer()`
   directly (§2); routing, request parsing, and the SSE surface are
   behave's job, not this dataset's.
4. **Zero-hit behavior against a real index.** The zero-hit case's empty
   result set comes from lexical, in-memory matching against a made-up
   phrase — it shows the structural no-answer path is reachable, not that
   a live Azure AI Search index would return zero hits on the same query.
5. **The whole corpus.** Two documents — `streaming-sse` and
   `token-budget` — have no dedicated case. Calibration could not produce
   a clean citation assertion for either (§4 of the design record); this
   is recorded as a gap, not padded with a forced question.
6. **Anything behave does not already cover more cheaply.** Three cases
   overlap existing behave scenarios (§1.1); this dataset's value there is
   the real corpus behind them, not new contract coverage.
7. **A judge prompt with production governance.** `JUDGE_PROMPT` lives as a
   module constant in `tools/eval_run.py`, not under `src/azgenai_lab/prompts/`
   — so it has no Day 8 loader fail-fast behavior and no versioned release
   process, only a version int and a SHA-256 recorded into evidence by
   hand.

## 10. Extensions, not verified here

Two directions this series' plan names as places evaluation could grow.
Neither has been exercised by this milestone — they are listed as what
exists to reach for, not as behavior this repo has observed:

- **Azure AI Foundry evaluation** — a managed evaluation service with
  built-in groundedness/relevance graders, dataset management, and a UI
  for reviewing runs.
- **A GitHub Actions eval pipeline** — wiring `tools/eval_run.py` into the
  CI workflow this repo already has (Day 25), gating merges on the
  deterministic exit code rather than running it by hand.

Both are plausible next steps; this page makes no claim about how either
behaves, because this series has not run either one.
