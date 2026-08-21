# Observability

Day 27 makes one LLM request's lifecycle visible end to end: the HTTP request,
the stages inside it, the model call, and the HTTP call underneath that.

The interesting part is not the setup. It is what a one-line setup does **not**
cover, and this page is mostly about that.

## What the one-line setup actually gives you

`azure-monitor-opentelemetry` advertises one call, `configure_azure_monitor()`.
Against this codebase, here is what each of Day 27's five concerns looked like
after that call and before any of the work described below:

| Concern | State after one line |
|---|---|
| Request trace | Present — with a caveat that silently removes it, see [Composition](#composition) |
| Dependency calls | **Empty.** The distro bundles no httpx instrumentation, and every upstream call this service makes travels over httpx |
| Correlation id | An `operation_Id` exists, but it is not the id this API's contract has used since Day 5 |
| Model latency | HTTP-level only, and even that does not mean what it looks like — see [Streaming](#streaming) |
| Streaming trace | The server span does cover the stream; the ownership model underneath it does not match Day 22's, and assuming it does leads to wrong conclusions |

The distro's supported-instrumentation list is `azure_sdk`, `django`,
`fastapi`, `flask`, `psycopg2`, `requests`, `urllib`, `urllib3`. No httpx, and
nothing for the OpenAI SDK. `opentelemetry-instrumentation-openai-v2` does not
close that gap either: instrumenting the Responses API is an open request
there ([issue #3436](https://github.com/open-telemetry/opentelemetry-python-contrib/issues/3436),
whose linked PR landed response extractors rather than instrumentation), and
Day 5 pinned this whole series to the Responses API.

So the dependency half of every trace is ours to build.

## Composition

One function, `configure_telemetry(settings)` in `core/telemetry.py`, called
once per entrypoint: `create_app()` and `tools/index_corpus.py`. It is
reentrant, so a process that reaches both installs one provider.

**Telemetry is off unless `APPLICATIONINSIGHTS_CONNECTION_STRING` is set.** No
connection string means no provider, no patched client and no environment
mutation — the posture CI, local development and every `USE_FAKE_*` path run
under.

### The trap worth knowing about

The distro's automatic FastAPI instrumentation replaces `fastapi.FastAPI` in
the `fastapi` module namespace. `main.py` binds that name at import time and
`create_app()` uses the bound reference, so automatic instrumentation reaches
nothing here — and the symptom is not an error. It is **no server spans at
all**, forever, silently.

This codebase therefore disables the automatic path outright and instruments
the app instance:

```python
configure_azure_monitor(instrumentation_options={"fastapi": {"enabled": False}}, ...)
FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"], excluded_urls=...)
```

Two details in that call:

- `exclude_spans` drops the ASGI `http send`/`http receive` child spans. Without
  it the documented tree below is not the only correct answer.
- `excluded_urls` is matched with `re.search` against the **full URL**, so the
  pattern is anchored. A bare `health` would also swallow `/api/v1/healthz` and
  any request whose query string mentioned health.

`/health` is excluded because Container Apps runs startup, liveness and
readiness probes against it, liveness every 10 seconds
([container-apps.md](container-apps.md)). At 100% sampling those would dominate
the data.

One thing that is **not** load-bearing, stated because the opposite is a
reasonable guess: the call's position relative to middleware registration does
not matter. `instrument_app` does not use `add_middleware`; it wraps
`build_middleware_stack` so `OpenTelemetryMiddleware` ends up outside the whole
user stack whenever it is called.

## The span tree

```
/chat, /chat/stream
POST /api/v1/chat                    SERVER
└─ chat {deployment}                 CLIENT   ← ours
   └─ POST /openai/v1/responses      CLIENT   ← httpx

/rag
POST /api/v1/rag                     SERVER
├─ rag.retrieval                     INTERNAL
│  ├─ embeddings {deployment}        INTERNAL → POST /openai/v1/embeddings
│  └─ azure.search.query             INTERNAL → POST /indexes/…/docs/search
├─ rag.assemble_context              INTERNAL
└─ rag.generation                    INTERNAL
   └─ chat {deployment}              CLIENT   → POST /openai/v1/responses

/agent
POST /api/v1/agent                   SERVER
└─ invoke_agent {agent-uuid}         ← agent-framework
   ├─ chat {deployment}         × N  ← agent-framework
   │  └─ POST /openai/v1/responses   ← httpx (ours)
   └─ execute_tool {tool}       × M  ← agent-framework
```

**`embeddings` and `azure.search.query` are children of `rag.retrieval`, not
siblings of it.** "Embeddings took 300ms" means something different depending
on which of those is true.

**On `/agent`, `N` and `M` are not 1.** One tool round is already two model
calls. That the model-call count, not the tool count, is what grows is the Day
16/17 point about where an agent's cost actually goes.

### Two producers of the same span

`/agent` runs through agent-framework, which emits `invoke_agent`, `chat` and
`execute_tool` on its own as soon as a global tracer provider exists. `/chat`
and `/rag` do not go through the framework — they use the OpenAI SDK directly —
so their `chat` span is raised by this codebase.

The two look alike **by decision**: the framework names its span
`chat {deployment}` and so do we, and both carry `gen_ai.operation.name` and
`gen_ai.request.model`. A test holds that intersection so a framework upgrade
that renames either one turns red instead of quietly splitting the tree in two.

They are not identical, and the difference is worth knowing before you filter
on it. Measured against a live component (2026-08-21):

| Span source | `gen_ai.provider.name` |
|---|---|
| ours (`/chat`, `/rag`) | `azure.ai.openai` |
| framework's `chat` | `openai` |
| framework's `invoke_agent` | `microsoft.agent_framework` |

Read a trace and you will see `invoke_agent` suffixed with the agent's UUID
rather than a readable name. That is the framework's choice, not ours.

## Attributes

Semantic-convention keys, imported from `opentelemetry.semconv` rather than
spelled out as strings (they live under `_incubating`, so a rename should fail
at import instead of emitting a dead key):

`gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.response.finish_reasons`, `gen_ai.response.time_to_first_chunk`.

Plus two of our own, closed-valued:

| Key | Values | When |
|---|---|---|
| `azgenai.outcome` | `success` \| `rejected` \| `error` | always |
| `azgenai.error.code` | Day 22's closed error-code set | only when the outcome is not `success` |

`rejected` versus `error` is not decided here: it comes from
`upstream_outcome()` in `core/errors.py`, the same split the audit log uses. A
4xx is this caller's request being rejected; a 5xx is a failure that was not
their fault.

**Unknown means absent, never zero.** A failed or disconnected call has no
usage attributes rather than usage of 0 — Day 9 disclosed that such a call may
still have incurred billable processing upstream, and writing 0 would turn "we
do not know" into "it cost nothing".

### Content never reaches telemetry

Prompt text, completions, chunk content, tool arguments, group ids: none of it
becomes a span attribute or event. This is the same rule Day 15, 19, 21 and 22
built up, and it is enforced the same way — a recursive forbidden-attribute-name
test, not a write-time filter.

Two things that would break it are switched off explicitly, and they are **two
different switches**:

| Switch | Owner | Our setting |
|---|---|---|
| `ENABLE_SENSITIVE_DATA` | agent-framework | `enable_instrumentation(enable_sensitive_data=False)`, called programmatically |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | OpenTelemetry gen_ai | written as the string `"false"` |

Turning off one does not turn off the other. The framework's must be set
programmatically because its settings object is a module-level singleton built
at import, so changing the environment afterwards does nothing — and passing
`None` makes it re-read the environment, which hands the decision back to
whoever set it.

The OTel one is *set* rather than left unset on purpose: "unset" is an
assumption that an upstream default will not change, and this switch decides
whether conversation content is copied into a second store. That is a data
governance decision, not a convenience.

**Measured caveat (2026-08-21, live).** "Sensitive data off" does not mean "no
application text". The framework's `execute_tool` span carries
`gen_ai.tool.description` — the tool function's **full docstring, verbatim** —
with `enable_sensitive_data=False`. That switch governs prompts, completions
and tool arguments, not a tool's own self-description.

The name-based guard above does not catch it either: `gen_ai.tool.description`
contains none of the forbidden substrings. This is the concrete instance of
the limitation listed under [honest boundaries](#honest-boundaries) — the guard
checks attribute names, not values. Here the text is our own docstring rather
than user data, so the exposure is small; it would not be, for a tool whose
description carried internal detail.

One more, deliberately not left to the exception machinery: every span this
codebase opens passes `record_exception=False`. OpenTelemetry's default is
`True`, and it writes the exception's own **message** into a span event —
upstream detail, verbatim, past every rule above. Span status carries the
classification instead.

## Correlation

`X-Correlation-Id` remains the contract and the join key. Application Insights
has its own identifier — `operation_Id`, the W3C trace id — and it is not this
one: that identifier describes the tree, this one is what Day 5 published and
every error envelope since has carried.

**Join on `correlation_id`.** It is stamped on the server span and on every
application-owned span; spans raised by instrumentation (httpx, the framework)
rely on the trace tree instead.

Since Day 27 the inbound header is validated before it is trusted — exactly one
header, 1–128 bytes, ASCII VCHAR, no normalisation. Failing that is not an
error; the request proceeds with a generated id. See
[api-conventions.md](api-conventions.md#correlation-id) for the contract,
including the consequence that the id echoed back may not be the one sent.

Outbound calls carry W3C `traceparent` and **no** `X-Correlation-Id`: no
upstream knows what to do with our id, and forwarding it would copy
caller-controlled text somewhere else for nobody's benefit.

## Streaming

A streaming call's span cannot be a context manager. It opens before the
request goes out and stays open across the body iteration, which happens after
the function that started it has returned. It is owned instead, by an object
whose `aclose()` is idempotent because two paths legitimately reach it: the
terminal-event path, which knows usage and status, and the exit path, which
only knows the stream is over. First writer wins.

Three timings, and they are three different numbers:

| Measurement | What it is |
|---|---|
| `gen_ai.response.time_to_first_chunk` | Seconds from **request issuance** to the first chunk — the semantic convention's own definition, which is why the span starts before `responses.create` rather than when the adapter hands back a stream |
| The LLM span's duration | The whole generation, terminal event included |
| The httpx span's duration | **Not generation latency.** See below |

**The httpx span ends when response headers come back, not when the body is
consumed.** Its `with` block wraps `handle_async_request`, which returns once
the status and headers are available; the body is a stream read outside that
block.

One streamed `/chat/stream` call against gpt-5-mini, measured on a live
component (2026-08-21, japaneast) — three numbers from one request:

| Measurement | Value | What it covers |
|---|---|---|
| httpx span | **1.067 s** | request sent → response headers back |
| `gen_ai.response.time_to_first_chunk` | **2.410 s** | → the first content chunk |
| `chat chat-mini` span | **2.568 s** | the whole generation |

Headers came back at 1.07 s; the first chunk did not arrive until 2.41 s,
because the model spent the gap reasoning (that response reported 64 reasoning
tokens). Reading the httpx span as "how long the model took" would have
under-reported this request by more than half.

The ASGI server span, by contrast, ends only on the final body message
(`http.response.body` with `more_body` false), so it does span the whole SSE
response.

### Not the same shape as the audit log

Day 22's streaming audit has **two mutually exclusive owners** — a pre-stream
finalizer and a post-transfer observer. The span has **one** owner across the
whole life of the request. The two are not isomorphic, and reasoning about one
from the other will produce wrong answers about what is recorded when a client
disconnects.

### One measured detail

An async generator that has never been started does not run its `finally` on
`aclose()` — there is no suspended frame to throw `GeneratorExit` into. A
generator-only design therefore leaks the span in exactly the case where
nothing ever consumed the stream. The owner is wrapped in an async iterator
class instead, whose `aclose()` closes both.

## Sampling

Set explicitly to 1.0 in this lab. **The distro's own default is a rate-limited
sampler at 5 traces per second**, which is a reasonable production default and a
confusing one when you are following an article with a single request in flight
and cannot find it.

At real volume, turn it down. The lab keeps 100% because losing the one request
you are looking at defeats the exercise.

## What is not exported

**Logs and metrics are both off** (`OTEL_LOGS_EXPORTER=none`,
`OTEL_METRICS_EXPORTER=none`, live metrics and performance counters disabled).
Day 27 ships traces.

Log ownership is unchanged: stderr, then whatever the hosting log pipeline does
with it — Container Apps already collects container stdout/stderr into Log
Analytics. That keeps [audit-logging.md](audit-logging.md)'s statement true word
for word, and keeps the same lines from being ingested and billed twice.

The cost of that choice is real and worth stating: **clicking into a span in
Application Insights will not show you the log lines from that request.** You
have to cross to Log Analytics and join on `correlation_id` yourself.

A detail that bites: the distro reads `os.environ`, and this repo's settings go
through pydantic-settings, which populates `Settings` and never the process
environment. Putting these variables in `.env` alone does nothing — the exports
continue, no error is raised, and the only symptom is the bill. They are written
into `os.environ` by `configure_telemetry` before the distro is called, and a
conflicting pre-existing value is a startup failure rather than a silent
overwrite.

## Cost

Application Insights here is workspace-based, so it bills at Log Analytics
rates. From the Azure Retail Prices API for **japaneast, USD (checked
2026-08-21)**:

| Meter | Tier | Price |
|---|---|---|
| Analytics Logs Data Ingestion | from 0 GB | **0.00 USD / GB** |
| Analytics Logs Data Ingestion | from 5 GB | **3.34 USD / GB** |
| Analytics Logs Data Retention | — | 0.15 USD / GB / month |

The monthly free grant is visible in the meter itself as a pricing tier rather
than as a separate benefit: the first 5 GB are priced at zero. Note the grant
is per billing account, not per workspace, so a second workspace does not bring
a second 5 GB.

Prices are regional and change. The numbers above carry their region, currency
and check date for that reason — a bare figure in a document is a stale claim
waiting to happen.

Turning off logs, metrics, live metrics and performance counters, and excluding
`/health`, are all cost decisions as much as clarity ones: probe traffic every
10 seconds is the largest volume this app would otherwise emit.

Following Day 9's rule: **the authority on what was ingested is Cost
Management, not our own span count.**

## Running it locally

Starting the image on a non-Azure host logs **two ERROR lines with tracebacks**
during startup:

```
ERROR opentelemetry.resource.detector.azure.vm Failed to receive Azure VM metadata: timed out
```

Nothing is wrong. The distro enables the `azure_app_service,azure_vm` resource
detectors by default (`OTEL_EXPERIMENTAL_RESOURCE_DETECTORS`) and they probe
IMDS, which does not exist outside Azure. Telemetry works regardless — the line
to look for is `Transmission succeeded: Item received: N. Items accepted: N`.

## Honest boundaries

- The indexing path (`tools/index_corpus.py` → `embed_chunks`) does not go
  through the retriever's call site, so it produces httpx transport spans but
  **no semantic `embeddings` span**.
- `invoke_agent` and its children are the framework's spans. An upgrade can
  rename them; a test will catch it, but the tree in this document is not
  something this codebase fully controls.
- The one-server-span-per-request test proves that of **the shipped
  composition**, which disables automatic instrumentation. It does not prove
  what automatic and manual instrumentation do together — that combination is
  never run.
- Content-free telemetry is enforced by attribute **name**. A caller-controlled
  value copied into a well-named field would not be caught by it; that is why
  the correlation header is bounded before it is stamped anywhere.
