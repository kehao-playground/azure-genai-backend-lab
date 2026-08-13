# Running the API in Docker

The image is a two-stage build: a builder stage assembles a virtualenv with
uv, and the runtime stage copies only that virtualenv plus the bundled
sample corpus — no uv, no `src/`, no build graph. It runs as a non-root
user, ships a container-local health check, and bounds both request drain
and application shutdown on the way down. Zero configuration starts the API
in fake mode — no Azure resources, no keys, no outbound network.

## Quick start

```bash
docker build -f docker/Dockerfile -t azgenai-lab .
docker run --rm --name azgenai-lab -p 127.0.0.1:8000:8000 azgenai-lab
```

`-p 127.0.0.1:8000:8000` binds the published port to localhost only; a bare
`-p 8000:8000` publishes to every address the host answers on, which is
further than a local quick start needs to reach. `--rm` removes the
container once it stops, so running this command again under the same
name does not collide with a leftover exited container.

Open <http://127.0.0.1:8000/health> and <http://127.0.0.1:8000/docs>.

Stop it with a grace period larger than the drain budget (see
[Graceful shutdown](#graceful-shutdown)):

```bash
docker stop -t 30 azgenai-lab
```

## What is in the image

- **Builder stage**: `python:3.13-slim` plus uv, copied from
  `ghcr.io/astral-sh/uv:0.11` — a floating minor tag, not a pin; it
  resolved to `uv 0.11.33` on this machine on 2026-08-12 and will pick up
  later 0.11.x patches without this file changing. `uv sync --frozen
  --no-dev --no-editable` installs the dependencies and the project itself —
  including `azgenai_lab/prompts/*.md` — into `/app/.venv`.
  `UV_PYTHON_DOWNLOADS=0` keeps the venv on the interpreter the runtime
  stage ships.
- **Runtime stage**: `python:3.13-slim` plus `/app/.venv` and the sample
  corpus described below — no uv, no source tree, no build graph. The
  prompt loader fails fast at startup, so a container that serves `/health`
  has proven the packaged prompts are present.
- **Non-root**: the process runs as the system user `app`. The virtualenv
  is root-owned and read-only to it — the runtime user cannot rewrite its
  own code, which is a feature, not a limitation.
- **Sample corpus**: `/app/data/sample-docs`, copied in as repository data
  rather than wheel content, with `SAMPLE_DOCS_DIR` pointing at it. Fake
  search is the default and seeds the agent's index from that corpus at
  startup. The loader (`load_documents`) takes its base directory as a
  required argument — no default — because the one candidate default, the
  checkout-relative path computed from the loader module's own location,
  does not survive a non-editable install. Any non-Docker deployment that
  installs this package non-editable and leaves fake search on must set
  `SAMPLE_DOCS_DIR` (or otherwise supply the corpus path) itself; this
  image is one way to satisfy that, not the only one. A deployment that
  turns fake search off does not need the corpus at all.
- Measured on this machine (Docker 29.4.0, 2026-08-12) — a data point, not a
  promise: single-stage baseline 321MB → multi-stage 202MB. No before/after
  figure is given for the build context: the two builds' contexts were
  measured on differently-dirty working trees, so the comparison would not
  mean anything. What the measurement did find is in the next bullet.
- Two builds, not one, establish what actually rides into the context: this
  Dockerfile's own cold build transfers 1.12MB (reproduced identically on
  two independent cold builders) — its `COPY` instructions name only
  `pyproject.toml`, `uv.lock`, `README.md`, `src/`, and `data/sample-docs`,
  never `.`. A throwaway single-instruction Dockerfile (`COPY . /ctx`),
  built against the identical directory and `.dockerignore` on its own
  throwaway builder, transferred **43.60MB**, and a `du -sh` run inside
  that build found `.mypy_cache` alone accounted for **40.3M** of it — so
  the cache genuinely is present, sync-eligible content, not something
  `.dockerignore` was already excluding. Put side by side, the two numbers
  show BuildKit's local-source sync is instruction-aware: it only walks and
  transfers the specific paths a `COPY`/`ADD` instruction actually
  references, so `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, and
  everything else outside this Dockerfile's five referenced paths never
  enters the 1.12MB build, cold or not. That is what this Dockerfile's own
  instructions do today; it was not checked against the classic builder
  (`DOCKER_BUILDKIT=0`), and it stops being true the moment any instruction
  reads `COPY . .`. `.dockerignore` below excludes those three cache
  directories anyway, on the same defense-in-depth basis as its two `.env`
  lines: not because today's build needs it, but so a future `COPY . .`
  does not silently ship them. Bytecode under `src/` is a different story:
  Docker's
  `.dockerignore` matching uses Go's `filepath.Match` rules, and only the
  `**` wildcard matches any number of directories, including zero
  ([Docker Build docs, build context](https://docs.docker.com/build/concepts/context/#dockerignore-files),
  checked 2026-08-12) — so a pattern like `__pycache__/` without a leading
  `**/` matches only that literal path relative to the build context root,
  not nested occurrences under `src/`. Until this was fixed to
  `**/__pycache__/`, 444,079 bytes (`wc -c`, not `du`'s block-rounded
  output) of local bytecode rode into the builder stage and invalidated the
  `COPY src/ src/` layer every time someone ran the tests. Measured cold on
  a throwaway builder, on one working tree that had been used for local
  test runs, before and after that one-line-pair change: 1.12MB →
  671.22kB. (Only that pair is a valid comparison; the single-stage
  baseline's context was measured on a pristine checkout with no bytecode
  at all, so it is not comparable to either.)

## Environment variables

Settings come from the environment (plus `.env` in local development; the
image never bakes one in — `.dockerignore` blocks it as well). Every
variable has a demo-oriented default and the fake switches default to on,
so an empty environment runs entirely offline. Secrets are injected at
runtime only, never at build time; on Azure prefer Key Vault references
([key-vault-config.md](key-vault-config.md)) and Container Apps secrets
(Day 24).

**Read before deploying past a lab environment**: `AUTH_MODE`'s default,
`headers`, is a development mode — it trusts `X-Tenant-Id`/`X-User-Id`/
`X-Group-Ids` as-is, so anyone who can reach this container directly can
declare any tenant, user, or group it accepts
([api-conventions.md § Trust boundary](api-conventions.md#trust-boundary-read-before-deploying-past-a-lab-environment)).
A deployment reachable from outside a trusted network must either set
`AUTH_MODE=entra` or sit behind a gateway that strips or overrides those
three headers and cannot itself be bypassed by a direct connection to the
container.

| Variable | Default | Needed when |
|---|---|---|
| `USE_FAKE_LLM` | `true` | Set `false` to call Azure OpenAI for chat. |
| `USE_FAKE_EMBEDDINGS` | `true` | Set `false` to call the embeddings deployment. |
| `USE_FAKE_SEARCH` | `true` | Set `false` to call Azure AI Search. |
| `AZURE_OPENAI_ENDPOINT` | — | `USE_FAKE_LLM=false` or `USE_FAKE_EMBEDDINGS=false`. |
| `AZURE_OPENAI_API_KEY` | — | `USE_FAKE_LLM=false` or `USE_FAKE_EMBEDDINGS=false`. |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | — | `USE_FAKE_LLM=false`. |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | — | `USE_FAKE_EMBEDDINGS=false`. |
| `AZURE_SEARCH_ENDPOINT` | — | `USE_FAKE_SEARCH=false`. |
| `AZURE_SEARCH_ADMIN_KEY` | — | `USE_FAKE_SEARCH=false`. |
| `AUTH_MODE` | `headers` | `entra` switches caller auth to Entra ID JWTs (Day 19). |
| `ENTRA_TENANT_ID` | — | `AUTH_MODE=entra`. |
| `ENTRA_AUDIENCE` | — | `AUTH_MODE=entra`. |
| `ENTRA_REQUIRED_SCOPE` | — | `AUTH_MODE=entra`: this or `ENTRA_REQUIRED_APP_ROLE`. |
| `ENTRA_REQUIRED_APP_ROLE` | — | `AUTH_MODE=entra`: this or `ENTRA_REQUIRED_SCOPE`. |
| `LOG_LEVEL` | `INFO` | Tuning log volume. |
| `APP_NAME` | `azure-genai-backend-lab` | Cosmetic (health/root payloads). |
| `APP_ENV` | `local` | Environment label. |
| `LLM_TIMEOUT_SECONDS` | `30.0` | Per-attempt upstream timeout. |
| `LLM_MAX_RETRIES` | `2` | Upstream retry policy. |
| `LLM_MAX_OUTPUT_TOKENS` | `1000` | Per-call output cap (Day 9). |
| `CONVERSATION_TOKEN_BUDGET` | `50000` | Per-conversation lifetime token budget (Day 9). |
| `CHUNK_MAX_CHARS` | `2000` | Offline indexing (`tools/index_corpus.py`) and the fake-agent corpus seeded at startup when `USE_FAKE_SEARCH=true`. |
| `CHUNK_OVERLAP_CHARS` | `500` | Same chunking paths; must stay below half of `CHUNK_MAX_CHARS` (startup validation). |
| `RAG_TOP` | `5` | Retrieval hits handed to generation (Day 14). |
| `AGENT_MAX_ITERATIONS` | `5` | Agent loop guardrail (Day 17). |
| `AGENT_MAX_TOOL_CALLS` | `10` | Agent loop guardrail (Day 17). |
| `SHUTDOWN_CLEANUP_BUDGET_SECONDS` | `8.0` | Total shutdown-time budget for the four lifespan closers (see [Graceful shutdown](#graceful-shutdown)). `8.0` is also the maximum: it is what the 30s platform grace leaves after the 20s request drain and a 2s overhead margin, so this knob only goes down. |
| `SAMPLE_DOCS_DIR` | unset (falls back to the checkout-relative path, which only resolves inside an editable install) | The bundled corpus the fake agent index is seeded from. The image sets it to `/app/data/sample-docs` because a non-editable install (this image's) cannot rely on that fallback. |

The authoritative list is `src/azgenai_lab/core/config.py`; if this table
and the code disagree, the code wins.

## Health check

The Dockerfile ships a `HEALTHCHECK` that probes `/health` with a Python
one-liner — the slim base has no curl, and installing one just for a probe
would be backwards. Parameters: every 30s, 3s timeout, 5s start period,
3 retries.

The first probe does not wait for the regular 30s `--interval`. While a
container is still in its `--start-period` and its health status is
`Starting`, moby's health-check runner uses a shorter, separate cadence —
`--start-interval`, which defaults to 5s and was not set explicitly here —
and only falls back to `--interval` once the start period has elapsed
(`defaultStartInterval = 5 * time.Second` and the status/elapsed-time
branch in `getInterval()`, in
[`daemon/health.go`](https://github.com/moby/moby/blob/master/daemon/health.go),
checked 2026-08-12). With a 5s `--start-period`, that first ≈5s probe is
also the only one that can land inside it. Measured on this machine
(Docker 29.4.0, 2026-08-12): the health-check log recorded a single probe
at ≈5.1s after container start — close to that default 5s start interval,
not "matching the start period" in any causal sense, since the two happen
to share the same number here only because `--start-period=5s` was chosen.
What was **not** observed is the
`starting` state itself: several `docker exec` calls landed between
container start and the first `docker inspect` read, which already showed
`healthy` — the ≈5.1s figure comes from subtracting `StartedAt` from the
one health-check log entry's timestamp, not from watching the transition
happen.

The probe command runs inside the container, as the
image's active user — confirmed against Docker's own reference
implementation rather than its prose docs: moby's health-check runner sets
the probe's exec user from the container's configured `USER`
(`execConfig.User = cntr.Config.User`, same file). docs.docker.com's
Dockerfile reference does not state this outside its general `USER`
section, and that section does not itself name `HEALTHCHECK`.

Scope this honestly: the instruction serves **local `docker run` / Compose
observability only**. Azure Container Apps runs its own startup, liveness
and readiness probes (HTTP/TCP only; `exec` probes are not supported), and
its default TCP probes are added by the portal only under specific
conditions — ingress enabled, main app container, non-GPU workload
profile; sidecars never get them
([Azure Container Apps health probes](https://learn.microsoft.com/en-us/azure/container-apps/health-probes),
ms.date 2025-11-06, checked 2026-08-12). A CLI/IaC deployment must
configure probes explicitly; Day 24 points them at this same `/health`.
Kubernetes does not execute a Docker image's `HEALTHCHECK` either: a
containerd maintainer confirmed containerd does not implement it, and that
"Kubernetes does not use Dockerfile's `HEALTHCHECK` but uses its own
equivalents (probes)"
([containerd/containerd discussion #7657](https://github.com/containerd/containerd/discussions/7657),
comment by @AkihiroSuda, checked 2026-08-12) — Kubernetes' own
liveness/readiness/startup probe documentation doesn't mention
`HEALTHCHECK` at all, so this maintainer statement is the closest
available authority. Kubernetes does not discover `/health` on its own
either: a deployment of this image onto Kubernetes would need its own pod
spec configuring liveness/readiness probes against this same `/health`
endpoint, the same way Day 24's Container Apps deployment does.

## Graceful shutdown

The `CMD` is exec-form, so uvicorn runs as PID 1 and receives SIGTERM
directly (shell-form would strand the signal in `/bin/sh`). On SIGTERM,
uvicorn stops accepting connections and waits for in-flight requests —
including SSE streams — up to `--timeout-graceful-shutdown 20` seconds,
then cancels whatever remains.

Those 20 seconds bound **request drain only**. Application shutdown (the
lifespan chain that closes the four app-wide clients — the principal
resolver, and the conversation, RAG and agent services) runs after that
timeout, inside whatever grace the runtime grants: `docker stop` defaults
to 10 seconds before SIGKILL, so use `docker stop -t 30`; Azure Container
Apps sends SIGKILL when the termination grace expires — 30 seconds by
default
([Application lifecycle management in Azure Container Apps § Shutdown](https://learn.microsoft.com/en-us/azure/container-apps/application-lifecycle-management#shutdown),
checked 2026-08-12). That default is tunable: the ARM template exposes
`template.terminationGracePeriodSeconds` (non-negative integer; nil means
the 30s default —
[Microsoft.App/containerApps template reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/2025-07-01/containerapps),
checked 2026-08). This series keeps the default as its design point, and
Day 24's IaC pins `terminationGracePeriodSeconds: 30` explicitly rather
than relying on today's default staying put.

Application shutdown is itself bounded: `SHUTDOWN_CLEANUP_BUDGET_SECONDS`
(default `8.0`) is one deadline shared across all four closers, not four
independent per-closer timeouts, so a closer that hangs cannot strand the
rest indefinitely the way it could before Day 23 review A1. A closer that
times out is logged (`shutdown cleanup timed out closer=...`) and the loop
moves on to the next one; every closer still runs regardless of what an
earlier one did — including when the cleanup task is cancelled from
outside, in which case the cancellation is re-raised once the remaining
closers have had their turn rather than stranding them (Day 23 review F2).
The guarantee is that every closer is *attempted*, not that every closer
finishes: a caller that keeps calling `cancel()` can interrupt them one
after another. A single cancellation interrupts at most one further closer.
This only bounds *cooperative* delay — a closer still doing real (e.g.
shielded) work after being cancelled takes however long that work takes, so
the 8s default is a target, not a hard ceiling on wall time.

That default is also the configurable maximum, because the two shutdown
phases are consecutive and share one grace period:
`grace 30 − drain 20 − overhead margin 2 = 8`. The bound is computed from
those three named terms in `core/config.py`, and a unit test parses this
image's `--timeout-graceful-shutdown` back out of the Dockerfile so the
drain term cannot drift away from the flag it mirrors. Raising the cleanup
budget therefore means lowering the drain first — a visible trade, not an
env-var override. (Before Day 23 review F1 the cap was a standalone `30`,
which accepted 20 + 30 = 50 nominal seconds against a 30-second ceiling.)
The 2-second margin and the 8-second budget are both unmeasured
hypotheses — Day 24 must measure real-Azure teardown latency or shorten the
terms (see the Honest boundary paragraph below).

Measured on this machine (Docker 29.4.0, 2026-08-12): an idle container
(fake mode, zero env vars) stops in 0.669s; with a deliberately held SSE
stream — real (non-fake) LLM adapter code pointed at
`tools/slow_stream_mock.py`, a local stand-in for the Responses API, with
`LLM_TIMEOUT_SECONDS=60` so the 20s drain cutoff is what gets exercised
and not the SDK's own per-attempt timeout — drain is cut off after ~20s
(the `Cancel 1 running task(s), timeout graceful shutdown exceeded` log
line) and the container exits with code 0 in 20.819s total
(`docker stop -t 30`). The audit line for the drained turn records
`duration_ms: 26862.34`, comfortably inside that 60s ceiling. (An earlier
run against the same mock, without raising the timeout, produced a
similar-looking `duration_ms: 28139.00` under the SDK's 30s default — only
1.9s of margin, too close to rule out that run having hit the SDK's own
timeout instead of the drain cutoff. Raising `LLM_TIMEOUT_SECONDS` to 60s
removes that ambiguity without changing the drain number itself, which
this rerun reproduces.) The held-stream setup is replayable:
`tools/slow_stream_mock.py`.

Honest boundary: `tools/slow_stream_mock.py` is a small, cooperative local
server, not genuine Azure infrastructure — what these numbers isolate is
uvicorn's own drain cutoff, not how long the lifespan chain's `aclose()`
on the real httpx client would take against an actual Azure connection's
teardown latency, which was not measured here. Azure Container Apps' 30
second grace is the platform default, not a fixed ceiling —
`template.terminationGracePeriodSeconds` can raise or lower it (see
[Graceful shutdown](#graceful-shutdown) above). The budget arithmetic in
this document holds because this series pins 30 as its design point (Day
24's IaC sets it explicitly), not because the platform forces 30 on every
deployment.

A turn cancelled by drain emits an audit event with
`error_code: "client_disconnect"` and `committed: false`; an operator
watching a rolling restart may see a burst of these. That code names the
*usual* cause, not a proven one — see
[audit-logging.md](audit-logging.md#commit-truth-versus-delivery-truth).

## Production hardening beyond this lab

- **Pin by digest, both images the build pulls** — the runtime base
  (`python:3.13-slim@sha256:...`) and the uv image this Dockerfile copies
  the binary from (`ghcr.io/astral-sh/uv:0.11@sha256:...`), currently a
  floating minor tag. `image:tag@sha256:...` keeps the readable tag in the
  Dockerfile for a human to see what it roughly is, while the digest is
  what Docker actually resolves and pulls — readability and immutability
  are not a trade-off here. The lab keeps both unpinned for readability and
  says so instead of pretending otherwise.
- **Registry**: push to Azure Container Registry once a real runtime needs
  to pull the image (Day 24).
- **Smaller bases** (distroless-style) drop the shell and package manager
  this image still carries. The lab keeps them for debuggability — a
  deliberate trade, not an oversight.
- **Image scanning** (Docker Scout, Microsoft Defender for Cloud) belongs
  in CI once images are published; the Day 23 gate proves the build, boots
  the image, and checks the declared `HEALTHCHECK` itself reports
  `healthy` — it does not scan the image for vulnerabilities.
