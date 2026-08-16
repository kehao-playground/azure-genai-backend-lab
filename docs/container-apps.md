# Deploying to Azure Container Apps (Day 24)

Day 23 ended with an image that builds and boots. Day 24 puts that same
image on a real runtime without scattering a single key: the app
authenticates to Azure OpenAI with a managed identity, the Search admin
key lives in Key Vault and is resolved by the platform, and the registry
pull is identity-based too. The deployment is finished when a readiness
gate proves the whole chain — public FQDN → Entra token → app → managed
identity → Azure OpenAI — answers a real request, not when
`az containerapp create` exits 0.

Everything here is ephemeral. The environment, the app, the registry, the
identity, the Log Analytics workspace and the three role assignments are
created at the start of a deploy session and removed at the end of it, by
scripts that read their own work back before claiming it happened.

Microsoft Learn pages cited below were checked 2026-08-16 unless a
different date is given; each citation carries the page's own `ms.date`
where the page publishes one.

Companion documents: [docker.md](docker.md) (the image),
[managed-identity.md](managed-identity.md) (why the identity is
user-assigned, and what a role assignment actually grants),
[key-vault-config.md](key-vault-config.md) (the secret inventory and
rotation semantics), [entra-id-auth.md](entra-id-auth.md) (the caller-side
401/403 contract this deployment turns on).

---

## 1. Why Container Apps for this series

The series needs a container runtime that a solo engineer can stand up and
tear down in an afternoon, on a US$20/month ceiling, without operating a
cluster. Container Apps fits three of this project's standing constraints:

- **It runs the Day 23 image, built from an unchanged Dockerfile.** Same
  base, same non-root user, same `/health`, same `SAMPLE_DOCS_DIR`
  mechanism; the deployment adds no build step of its own. What changes is
  configuration — plus one new runtime dependency the keyless path pulls
  in (`aiohttp`, the async transport `azure.identity.aio` needs).
- **It has a documented shutdown contract.** SIGTERM, then SIGKILL when
  the termination grace expires, with the grace period settable per app
  ([Application lifecycle management](https://learn.microsoft.com/en-us/azure/container-apps/application-lifecycle-management),
  ms.date 2025-11-07). Day 23 derived this app's shutdown budget from a
  30-second grace and left the arithmetic untested; a platform that
  actually implements that contract is what makes it testable (§9).
- **It bills by usage and can scale to zero.** "You aren't billed usage
  charges if your container app scales to zero"
  ([Scaling in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/scale-app),
  ms.date 2026-05-19). This deployment deliberately does *not* scale to
  zero — see §5 — but the option is what makes a lab-sized deployment
  affordable outside a measurement window.

What this deployment is not: it is not Bicep (Day 26), it is not a CI/CD
pipeline (Day 25), and it does not put the app behind APIM. The unit of
reproducibility here is a shell script an operator runs by hand.

## 2. Topology: one identity, three roles

A single **user-assigned** managed identity is the app's identity in every
direction. User-assigned rather than system-assigned because its role
assignments must exist *before* the app first runs, and a system-assigned
principal does not exist until the app does — the ordering constraint
Day 20 already settled
([managed-identity.md §4](managed-identity.md#4-the-day-24-identity-plan-this-is-the-deployment-note)).

| Consumer | Role | Scope | Who resolves it |
|---|---|---|---|
| Container Apps pulling the image | `AcrPull` | the container registry | the platform (`registries[].identity`) |
| The Search admin key | `Key Vault Secrets User` | the key vault | the platform (Key Vault reference) |
| The app calling Azure OpenAI | `Cognitive Services OpenAI User` | the Azure OpenAI account | the app (`ManagedIdentityCredential`, `services/azure_openai_auth.py`) |

Only the third one runs inside the container. That is why
`identitySettings` keeps its default rather than taking Day 20's
lifecycle-`None` option: the app code genuinely needs to mint tokens, so
the identity cannot be withheld from the container.

The app's YAML attaches the identity with a **top-level `identity` block**:

```yaml
identity:
  type: UserAssigned
  userAssignedIdentities:
    "<managed-identity-resource-id>": {}
```

The `identity:` fields under `registries[]` and `secrets[]` are
*references* to an already-attached identity, not attachments. Omit the
top-level block and both the image pull and the Key Vault reference fail
against an identity the app names twice
([Container Apps ARM/YAML spec](https://learn.microsoft.com/en-us/azure/container-apps/azure-resource-manager-api-spec),
ms.date 2025-04-09).

## 3. Ingress

`infra/scripts/deploy-container-app.sh` configures external HTTP ingress
on `targetPort: 8000` with `transport: auto`.

- **External** exposes the app through the environment's inbound IP; with
  a public inbound IP it receives traffic from the public internet
  ([Ingress in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview),
  ms.date 2025-05-02).
- **TLS is terminated at the ingress point**, on HTTPS endpoints that
  "always use TLS 1.2 or 1.3"; HTTP on port 80 is redirected to HTTPS on
  443 by default. The container itself keeps serving plain HTTP on 8000 —
  it never sees a certificate.
- The app is reachable at an **automatically assigned FQDN** derived from
  the environment's DNS suffix. The deploy script reads that FQDN back
  from Azure (`properties.configuration.ingress.fqdn`) and every
  subsequent step uses the value Azure reported, never a string assembled
  from a naming convention.
- `transport: auto` is the platform default and "detects HTTP/1 or
  HTTP/2"
  ([Configure ingress](https://learn.microsoft.com/en-us/azure/container-apps/ingress-how-to),
  ms.date 2025-11-07).

One boundary worth knowing before you point a streaming client at this:
HTTP ingress documents a **request timeout of 240 seconds**
(ingress-overview, same page and date). `/api/v1/chat/stream` holds a
single long-lived response, so a stream that outlives that window is cut
by the platform, not by the app — a constraint this lab has not measured
and does not work around.

## 4. Revisions

The app runs in **single revision mode** (`activeRevisionsMode: single`),
which is also the platform default. Revisions are "a snapshot of each
version of your container app" and are **immutable**: you do not edit a
revision, you create another one
([Update and deploy changes](https://learn.microsoft.com/en-us/azure/container-apps/revisions),
ms.date 2025-10-27).

Two consequences matter here.

**Zero-downtime swap.** In single revision mode "the existing active
revision isn't deactivated until the new revision is ready", and with
ingress enabled it keeps receiving 100% of traffic until then. Ready means
provisioned, scaled to match the previous replica count, and *all replicas
passed their startup and readiness probes* — which is exactly why §6
configures those probes explicitly instead of leaving the platform to
guess.

**Deactivation is what produces a shutdown.** Containers shut down on
scale-in, on app deletion, and on revision deactivation
(application-lifecycle-management, ms.date 2025-11-07). In single revision
mode, deploying a new revision automatically deactivates the old one — so
a deliberate revision change is the trigger the shutdown measurement in §9
uses.

Which changes create a revision is a property of *where* the change lands:
changes under `properties.template` are revision-scope and create a new
revision; changes under `properties.configuration` (secret values, ingress
settings, registry credentials) are application-scope and do not
(revisions page, same date). Changing the Key Vault secret's *value* is
therefore not a revision-creating event — see §7 for what does happen.

## 5. Scaling

The deployed app pins `minReplicas: 1` and `maxReplicas: 1`.

That is a measurement decision, not a recommendation. The shutdown
timeline in §9 has to be attributed to one known replica; a platform free
to add or remove replicas underneath the measurement would let a
"last log line" come from a different replica than the one that received
the SIGTERM. One replica, fixed, removes that ambiguity.

The cost shape of the alternative is worth stating because it is the usual
reason to reach for Container Apps at all. The platform default is
`minReplicas: 0` with `maxReplicas: 10`, and with an HTTP scale rule an
app that receives no traffic scales to zero and incurs no usage charges;
replicas that stay in memory without processing "might be billed at a
lower *idle* rate" (scale-app, ms.date 2026-05-19). Inactive revisions are
not charged either (revisions page, ms.date 2025-10-27).

Pinned at one replica, this deployment does **not** get any of that: it
bills for a running replica from the moment it provisions until teardown.
That is the trade this session makes on purpose, and it is one more reason
the session ends with `delete-container-app.sh`.

A caveat the platform documentation flags and this app does not hit, but a
reader might: if you disable ingress and set neither `minReplicas` nor a
custom scale rule, the app scales to zero **and has no way to start back
up**.

## 6. Probes

All three probes are configured explicitly, all HTTP `GET /health` on port
8000:

| Probe | Settings |
|---|---|
| Startup | `initialDelaySeconds: 2`, `periodSeconds: 3` |
| Liveness | `periodSeconds: 10` |
| Readiness | `periodSeconds: 10` |

Day 23 handed this over deliberately: the image's own `HEALTHCHECK`
instruction serves local `docker run`/Compose only. Container Apps runs
its own probes, does not support `exec` probes, and adds default TCP
probes only under a specific set of portal conditions
([docker.md § Health check](docker.md#health-check)). A CLI or IaC
deployment gets nothing for free — these four lines of YAML are what point
the platform at the same endpoint the container already answers.

`/health` is unauthenticated by design (it is not under `/api/v1` and the
Entra dependency does not apply to it), which is what lets a probe and the
first stage of the readiness gate use it at all.

## 7. Environment variables and secrets

Configuration arrives in two distinct shapes, and the split is the point.

**Plain environment variables** carry everything that is not a secret:

| Variable | Value in this deployment |
|---|---|
| `AZURE_OPENAI_AUTH` | `entra` — mint a bearer token instead of reading a key |
| `AZURE_CLIENT_ID` | the user-assigned identity's client id (a public GUID, not a secret) |
| `AUTH_MODE` | `entra` — caller identity comes from a verified token (Day 19) |
| `ENTRA_TENANT_ID`, `ENTRA_AUDIENCE` | the API app registration's tenant and application id |
| `ENTRA_REQUIRED_SCOPE`, `ENTRA_REQUIRED_APP_ROLE` | `access_as_user` / `Api.Access` by default |
| `USE_FAKE_LLM`, `USE_FAKE_SEARCH`, `USE_FAKE_EMBEDDINGS` | all `false` — real services on all three seams |
| `SAMPLE_DOCS_DIR` | `/app/data/sample-docs` (the Day 23 fix, unchanged) |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | read back from the account, or overridden per run |
| `AZURE_SEARCH_ENDPOINT` | the Search service's endpoint |

`AZURE_OPENAI_API_KEY` is absent, and that is the headline: in `entra`
mode `resolve_aoai_auth()` builds an explicit
`ManagedIdentityCredential(client_id=...)` and hands the openai SDK the
token provider *callable*, so there is no key in the app definition, in
the environment, or in the image. Startup fails fast if `AZURE_CLIENT_ID`
is missing in that mode.

**One secret**, and it is not in the app definition either. The Search
admin key is written to Key Vault by the deploy script and referenced:

```yaml
secrets:
  - name: search-admin-key
    keyVaultUrl: https://<vault>.vault.azure.net/secrets/azure-search-admin-key
    identity: <managed-identity-resource-id>
```

and consumed by name:

```yaml
- name: AZURE_SEARCH_ADMIN_KEY
  secretRef: search-admin-key
```

Three details, all from
[Manage secrets in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets)
(ms.date 2026-03-31):

1. The Container Apps secret name (lowercase) and the environment variable
   name are independent; `secretRef` is the join between them.
2. The secret URI is **versionless on purpose**. A versionless reference
   retrieves the latest version within 30 minutes, so rotating the Search
   key does not require rewriting the app definition. Pinning a version
   opts out of that entirely.
3. Active revisions are automatically restarted **specifically when the
   secret is referenced in an environment variable** — which is this app's
   consumption shape. Rotation is therefore also an availability event
   here; the restart is the feature, not a surprise
   ([key-vault-config.md §4](key-vault-config.md) walks through the whole
   rotation ceremony and why a managed identity deletes most of it).

The key still exists, so the ceremony still exists. Day 20's conclusion
stands: the vault is where the *remaining* secrets go, and this deployment
has exactly one left because Search RBAC was never measured.

## 8. The deploy session, end to end

Prerequisites the scripts do not create: an `az login` against the right
tenant, and the Container Apps CLI extension. Install the extension **up
front** rather than letting it install on first use — in a
non-interactive shell that can prompt or fail depending on
`extension.use_dynamic_install`, and stage 7 is a bad place to find out,
because by then the identity, its roles and the secret already exist:

```bash
az extension add --name containerapp
```

### 8.1 Bring up the supporting resources

Each of these is an existing script with its own contract; none of them is
created by the deploy script.

```bash
export AZ_SUBSCRIPTION_ID=<subscription-guid>
export AZ_RESOURCE_GROUP=rg-azgenai-lab

./create-resource-group.sh
./create-openai.sh                       # skip if the standing account exists
AZ_SEARCH_NAME=<search-name> ./create-search.sh
AZ_KEYVAULT_NAME=<vault-name> ./create-keyvault.sh
ENTRA_TENANT_ID=<tenant-guid> ENTRA_API_APP_NAME=azgenai-lab-api \
  ENTRA_CLIENT_APP_NAME=azgenai-lab-client ./create-entra-app.sh
ENTRA_API_APP_ID=<api-app-id> ENTRA_CLIENT_APP_ID=<client-app-id> \
  ./assign-entra-app-role.sh
./create-acr.sh                          # prints the generated registry name
```

`create-acr.sh` and the Log Analytics workspace both use **per-run unique
names** (a prefix plus a CSPRNG suffix). That is not tidiness: Day 21's
cleanup deleted — and irreversibly purged — a *concurrent* run's resource
because it identified the resource by a fixed name. A name only this run
could have invented is what makes a later `delete` safe.

### 8.2 Index the corpus

`/api/v1/rag` returns answers only for documents indexed under the
caller's tenant. Under `AUTH_MODE=entra` the caller's tenant is the
token's `tid` claim — a GUID — while the sample corpus ships under the
tenants `acme`, `globex` and `opsdemo`. Index it without saying so and
every RAG question comes back `no_answer`, which reads exactly like a
broken deployment.

```bash
USE_FAKE_SEARCH=false USE_FAKE_EMBEDDINGS=false \
AZURE_SEARCH_ENDPOINT=... AZURE_SEARCH_ADMIN_KEY=... \
AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_EMBEDDING_DEPLOYMENT=embed-small \
  uv run python tools/index_corpus.py --recreate-index --tenant-id <tenant-guid>
```

Read the consequence before using the flag: `--tenant-id` **collapses
every source document onto that one tenant** for the run. Every chunk's
`tenant_id`, every parent and chunk key, and every ACL decision derive
from it, so `acme` and `globex` stop being separate tenants in that index.
That is fine for a single-tenant live smoke and wrong as a general
practice — Day 15's whole multi-tenant story depends on those tenants
being distinct. Omitting the flag is the default and changes nothing.

One further ACL note for the smoke: the app-only (client-credentials)
tokens Day 19's live smoke observed carried `roles` and no `groups` claim
at all, so such a caller sees only documents whose `allowed_groups` is
empty. The sample corpus has several of those, including the one the
default `--rag-question` matches.

### 8.3 Deploy

```bash
export AZ_ACR_NAME=<printed by create-acr.sh>
export AZ_KEYVAULT_NAME=<vault-name> AZ_SEARCH_NAME=<search-name>
export AZ_OPENAI_NAME=<openai-account>
export ENTRA_TENANT_ID=<tenant-guid> ENTRA_AUDIENCE=<api-app-id>
export ENTRA_CLIENT_APP_ID=<client-app-id>
export ENTRA_CLIENT_SECRET=<client-secret>   # read from the environment only

./deploy-container-app.sh
```

`ENTRA_CLIENT_SECRET` is never passed as an argument and never printed;
the script even suspends shell tracing around the guard that reads it,
because `${VAR:?msg}` traces as `+ : <value>` when the variable *is* set.

Eleven labelled stages, in an order that is a contract rather than a
convenience — a role granted before the identity exists, an app created
before its image is in the registry, or a gate run before provisioning
finished are all deploys reporting a success they never earned:

| Stage | What it does |
|---|---|
| 1 | Preflight: register `Microsoft.App`, `Microsoft.ManagedIdentity`, `Microsoft.OperationalInsights` if needed |
| 2 | Create the user-assigned identity; read back its resource id, principal id and client id |
| 3 | Assign the three roles, each **verified by read-back** rather than by the create call's exit code |
| 4 | Read the Search admin key and store it in Key Vault as `azure-search-admin-key` |
| 5 | `az acr build --platform linux/amd64` — the registry builds the image, no local Docker daemon and no push credentials |
| 6 | Create the Log Analytics workspace explicitly, under a per-run unique name |
| 7 | Create the Container Apps environment (CLI flags — `env create` has no `--yaml`) and poll it to `Succeeded` |
| 8 | Create the app from one generated YAML file |
| 9 | Poll `provisioningState` to `Succeeded`, then read back the ingress FQDN |
| 10 | Gate 1 (control plane): state the verdict on everything read back so far |
| 11 | Gate 2 (data plane): poll `/health` to 200, then run the authenticated readiness gate |

Stage 3 is worth a second look. `az role assignment create` cannot decide
anything on its own here: an identical existing assignment is reported as
an error by some CLI versions and as success by others, and a directory
replication lag right after stage 2 produces a transient
`PrincipalNotFound` that a retry clears. So the **read-back is the
verdict**, bounded by `ROLE_POLL_ATTEMPTS`, and an *empty* read aborts
immediately — an empty `-o tsv` result is a failed read, never "not yet".
Every stage in this script that reads state applies the same rule; Day 19
and Day 21 each lost a live session to an empty read treated as a value.

Stage 5 builds in the cloud. `az acr build` uploads the local build
context and builds there, and ACR Tasks default to Linux/AMD64
([ACR Tasks overview](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tasks-overview));
`--platform linux/amd64` is passed explicitly anyway, because this repo is
developed on arm64 Macs and the default is not something to bet a deploy
on.

Stage 6 exists because of a bill nobody would find. `az containerapp env
create` will auto-provision a Log Analytics workspace when none is given,
and deleting the environment does **not** delete that workspace — it is a
separate `Microsoft.OperationalInsights/workspaces` resource with its own
lifecycle. An auto-provisioned workspace is a silent recurring charge the
teardown could never remove, because it was never told the name Azure
chose. Creating it explicitly, under a per-run unique name the script
prints, is what makes the teardown deterministic. Guessing the name would
be the Day 21 mistake all over again.

### 8.4 Smoke

The deploy script's own gate calls `/api/v1/chat` only. Exercising RAG and
the agent endpoint is a separate, deliberate step:

```bash
ENTRA_CLIENT_SECRET=... uv run python tools/entra_smoke.py --gate \
  --base-url https://<app-fqdn> \
  --tenant-id <tenant-guid> --client-id <client-app-id> --api-app-id <api-app-id> \
  --check-rag --check-agent
```

`--check-rag` asserts a 200 with a **non-empty `sources` array** — a
`no_answer` with zero sources fails it, which is the point: it is a check
on retrieval actually reaching the caller's tenant, not on the endpoint
being reachable. `--check-agent` asserts a 200 from `/api/v1/agent`. Both
require `--gate`, and both are skipped (reported as "not evaluated", not
silently dropped) if the gate itself never reached 200.

### 8.5 Tear down, in this order

```bash
AZ_ACA_APP_NAME=aca-azgenai-lab AZ_ACA_ENV_NAME=acaenv-azgenai-lab \
AZ_MI_NAME=mi-azgenai-lab AZ_LAW_NAME=<printed by the deploy script> \
AZ_ACR_NAME=... AZ_KEYVAULT_NAME=... AZ_OPENAI_NAME=... \
  ./delete-container-app.sh

AZ_ACR_NAME=... ./delete-acr.sh
AZ_KEYVAULT_NAME=... ./delete-keyvault.sh
AZ_SEARCH_NAME=... ./delete-search.sh
ENTRA_API_APP_ID=... ENTRA_CLIENT_APP_ID=... ./delete-entra-app.sh
```

The deploy script prints this command with the run's real values filled
in, both on success and on any failure that already mutated something.

## 9. Teardown ordering is a contract, not a convenience

`delete-container-app.sh` runs seven steps, and the order is the whole
design:

1. Delete the Container App — read back to confirm gone.
2. Delete the Container Apps environment — read back to confirm gone.
3. Delete the Log Analytics workspace, if a name was given.
4. Read back the managed identity's **principal id**.
5. Delete the role assignment at each scope (ACR, Key Vault, Azure OpenAI).
6. Read back **all** role assignments still held by that principal — fail
   closed.
7. Only then delete the identity — read back to confirm gone.

**Azure does not delete a managed identity's role assignments when the
identity is deleted.** They are left behind, each one displaying "Identity
not found", scattered across the ACR, Key Vault and Azure OpenAI
resources, with nothing in the portal pointing back at them
([Managed identity best practice recommendations § Maintenance](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/managed-identity-best-practice-recommendations#maintenance)).
Nobody notices, because nothing breaks.

The principal id is the only handle Azure gives you for finding those
assignments, and it dies with the identity. So step 4 reads it *before*
anything identity-related is deleted, and step 6 refuses to continue if a
single assignment remains: deleting the identity at that point converts a
recoverable state (an identity that can still be re-queried) into a
permanent one.

Step 6 deliberately queries **by assignee alone** — no `--scope`, no
`--role`. That is what catches a scope this run skipped because its name
knob was unset, or one a future deploy grants that this script does not
know about yet. A missing name knob in step 5 is skipped with a warning to
stderr, never silently.

## 10. Trust boundary

This deployment runs `AUTH_MODE=entra` on **public ingress**. That pairing
is deliberate: Day 23 wrote down that `AUTH_MODE=headers` — the default —
trusts `X-Tenant-Id` / `X-User-Id` / `X-Group-Ids` exactly as the caller
sends them, so anyone who can reach the container can declare any tenant,
user, or group
([docker.md § Environment variables](docker.md#environment-variables),
[api-conventions.md § Trust boundary](api-conventions.md#trust-boundary-read-before-deploying-past-a-lab-environment)).
It is a demo default, not a safe one, and putting it on a public FQDN
would be the single worst thing this milestone could ship.

The rule, stated plainly: **`headers` mode never leaves the laptop.**
Anything reachable from outside a trusted network either verifies Entra
tokens itself or sits behind a gateway that strips those three headers and
cannot be bypassed by connecting to the container directly.

What `entra` mode buys is a `Principal` assembled entirely from verified
claims — tenant from `tid`, user from `oid`, groups from `groups` — and
the 401/403 contract Day 19 pinned. What it does not buy is authorization
inside the tenant beyond that: every caller holding a valid token with the
required scope or app role reaches the same endpoints.

## 11. The shutdown measurement contract

Day 23 shipped a shutdown budget derived from three named terms —
`grace 30 − drain 20 − overhead margin 2 = 8` — and admitted that the
2-second margin and the 8-second budget were both **unmeasured
hypotheses**, to be measured or shortened on Day 24. This section is the
contract for that measurement. **It has not been run yet. No number in
this document is a measurement, and the values below are recorded here
after the deploy session, or not at all.**

**Trigger.** A revision change, in single revision mode, deactivates the
old revision, and revision deactivation is one of the three documented
shutdown paths (application-lifecycle-management, ms.date 2025-11-07).
Before triggering it, the old revision and replica identity are recorded,
so every later timestamp can be bound to *that* replica rather than to
whatever replaced it.

**Markers, and where each one comes from.** `main.py` emits three INFO
lines on the way down:

| Point in the timeline | Source |
|---|---|
| SIGTERM received | the platform's system log / revision event — the app does **not** own the signal handler, uvicorn does |
| Request drain ended | `lifespan shutdown started` (uvicorn enters lifespan shutdown only after `--timeout-graceful-shutdown` completes) |
| Cleanup began | `shutdown cleanup started budget_seconds=…` |
| Cleanup ended | `shutdown cleanup finished elapsed_seconds=…` (the app's own `time.monotonic` delta) |
| Process ended | the last line of the container's console log |

**Clock discipline.** The console and system logs are different sources
with different clocks. Cross-source timestamps are used only for coarse
alignment; **no segment duration is computed by subtracting two
wall-clock timestamps across sources**. Segment lengths come from the
app's own monotonic elapsed value, which is why that value is in the log
line at all.

**What invalidates a measurement.** A missing marker invalidates that
run — the measurement is discarded and repeated, or explicitly downgraded
to "platform-observable termination window only" and reported as such.
Partial data does not get assembled into a conclusion. Evidence not bound
to the captured old-replica identity is likewise discarded, because the
"last log line" may belong to the replacement replica.

**What the measurement can and cannot verify:**

- *Precisely verifiable*: cleanup elapsed ≤ 8 seconds, from the app's own
  monotonic clock, single-source.
- *Coarsely verifiable*: total termination falls inside the 30-second
  grace, from the platform-observable window.
- *Not verified*: the 2-second overhead margin. It stays a conservative
  allocation, not a measured quantity — the cross-clock rule above is
  precisely what rules out verifying a 2-second term by subtraction.

**An honest boundary about the markers themselves.** The lifespan's
`finally` block is unconditional, so it also runs when startup fails
*before* `yield` — for example when the Entra resolver cannot be built. In
that path all three markers still appear, in the same order, and
`lifespan shutdown started` is not a drain-end marker at all, because
there was never a drain. An operator reading a log must not read this
sequence as proof of a graceful shutdown; corroborate it with the
platform's own revision event, or with the app having served a request at
all.

The related Day 23 decision — whether cleanup should promise *attempted*
or *completed* closers — is settled by this measurement, and the standing
default is to keep *attempted*: the closers are best-effort resource
releases, and a child-task refactor buys complexity rather than
correctness. If the measurement shows closers being interrupted in
practice, that ruling flips and is recorded as debt.

## 12. Cost shape

No prices are quoted here, because none were sourced for this document.
What is stated is which meters this deployment turns on, so nothing bills
by surprise:

- **Container Apps** — usage charges for a running replica. Pinned at
  `minReplicas: 1`, this deployment bills from provisioning until
  teardown; it is scale-to-zero that avoids usage charges (scale-app,
  ms.date 2026-05-19), and this session opts out of it deliberately (§5).
- **Container Registry** — a Basic-tier registry, standing, for as long as
  it exists.
- **Log Analytics** — ingestion and retention, and the reason §8's stage 6
  exists at all: an orphaned workspace keeps billing.
- **Azure OpenAI** — tokens, per the series' standing account.
- **Azure AI Search** — free tier by default; a Basic fallback bills by
  the hour.
- **Key Vault** — per-operation, the one meter this project has actually
  priced ([key-vault-config.md §5](key-vault-config.md#5-cost), Retail
  Prices API, checked 2026-08).

The authority on what any of this cost is the invoice and Azure Cost
Management, not this page — the same rule Day 9 set for tokens. The
subscription's budget alert is a delayed notification, not a cap.

## 13. Honest boundaries

- **Nothing on this page has been deployed yet.** It documents scripts and
  code as they exist on this branch, verified by reading them. Every
  observation from the live session — the shutdown timeline, whether a
  failed Key Vault reference is visible in the app's provisioning state,
  what `az acr build` actually uploads as build context — lands afterwards,
  dated, or does not land.
- **Single-observation discipline applies to everything measured in that
  session.** Day 13's rule stands: one probe does not adjudicate a
  documentation conflict, and a number measured once is pinned to its
  date, region and API version rather than generalized. Day 20's
  14-minute-44-second role propagation is exactly that kind of number —
  a counterexample to "up to 5 minutes", not a new bound (§8's gate
  deadline is sized from it; see below).
- **The readiness gate's retry policy is asymmetric on purpose.** Only
  connection errors, 429 and 5xx are retried, on a 5 s → 10 s → 20 s →
  30 s-cap backoff against a 1200-second default deadline. That shape is
  the shape of role-assignment propagation: Azure OpenAI rejects the app's
  managed-identity token, the app maps the rejection to an upstream error,
  and the caller sees a 5xx that waiting genuinely fixes. A 401 or 403
  *with a valid token attached* is terminal, because the app decides those
  before anything touches Azure OpenAI — it means the audience, scope or
  app-role configuration is wrong, and waiting twenty minutes only
  re-learns what the first attempt already showed. A 200 whose body
  carries an `error` envelope is treated as terminal too.
- **The gate proves usability, not correctness.** A 200 from `/api/v1/chat`
  proves the chain is wired; it says nothing about answer quality, and
  `--check-rag`'s non-empty `sources` assertion says nothing about whether
  the retrieved sources are the right ones.
- **Search still uses an admin key.** Keyless Search was asserted from
  documentation and never measured, and current Microsoft pages disagree
  on whether the free tier supports RBAC at all
  ([managed-identity.md §6](managed-identity.md#6-honest-boundaries)).
  This deployment therefore has one real secret, which is what gives the
  Key Vault reference in §7 a genuine consumer instead of a demonstration.
- **Nothing here is production IaC.** These are shell scripts an operator
  runs by hand, with per-run unique names and no state file. Bicep is
  Day 26; an automated pipeline is Day 25.
