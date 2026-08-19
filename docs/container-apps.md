# Deploying to Azure Container Apps (Day 24)

Day 23 ended with an image that builds and boots. Day 24 puts that
packaging on a real runtime: ACR rebuilds the image from the unchanged
Dockerfile (§4), now carrying the managed-identity wiring and one new
runtime dependency, and no key value travels in the app definition. Two
paths are genuinely keyless — the app authenticates to Azure OpenAI with
a managed identity, and the registry pull is identity-based. The Search
data plane is still key-based: its admin key lives in Key Vault and the
platform resolves it into the container, where the app sends it as an
`api-key` header. The deployment is finished when a readiness
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

- **It runs the Day 23 packaging, from an unchanged Dockerfile.** Same
  base, same non-root user, same `/health`, same `SAMPLE_DOCS_DIR`
  mechanism; the deployment adds no build *stage* of its own. The image
  itself is rebuilt for deployment (§4's `az acr build`), because the
  keyless path pulls in one new runtime dependency (`aiohttp`, the async
  transport `azure.identity.aio` needs) and the managed-identity wiring
  in `src/`; so the bytes differ from Day 23's local image even though
  the build graph does not.
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
direction. User-assigned rather than system-assigned because of ordering:
a system-assigned principal does not exist until the app does, so its
roles can only be granted *after* the workload may already have started
once. A two-phase script (create app → read principal back → assign
roles → restart) is perfectly possible — the cost is that extra phase and
the window in which the app runs role-less. User-assigned lets every role
assignment exist before the first start, which is the constraint Day 20
already settled
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
deployment gets nothing for free — the app YAML's explicit `probes:` block
is what points the platform at the same endpoint the container already
answers.

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
created by the deploy script. **Every command in this runbook is run from
the repository root**, which is also the only directory the `uv run
python tools/…` invocations below resolve from.

```bash
export AZ_SUBSCRIPTION_ID=<subscription-guid>
export AZ_RESOURCE_GROUP=rg-azgenai-lab
export AZ_LOCATION=japaneast
export AZ_OPENAI_NAME=<openai-account>
export AZ_SEARCH_NAME=<search-name>
export AZ_KEYVAULT_NAME=<vault-name>
export ENTRA_TENANT_ID=<tenant-guid>

infra/scripts/create-resource-group.sh
infra/scripts/create-openai.sh           # skip if the standing account exists
infra/scripts/create-search.sh
infra/scripts/create-keyvault.sh
ENTRA_API_APP_NAME=azgenai-lab-api ENTRA_CLIENT_APP_NAME=azgenai-lab-client \
  infra/scripts/create-entra-app.sh      # prints both app ids and the secret, once
ENTRA_API_APP_ID=<api-app-id> ENTRA_CLIENT_APP_ID=<client-app-id> \
  infra/scripts/assign-entra-app-role.sh
infra/scripts/create-acr.sh              # prints the generated registry name
```

`export`, not a command-prefix assignment, for everything more than one
script needs: a `VAR=value ./script.sh` prefix sets the variable for *that
one command only*, and several of these scripts abort on a missing
required variable rather than defaulting. `AZ_LOCATION` in particular is
required by both `create-resource-group.sh` and `create-openai.sh`, and
`ENTRA_TENANT_ID` is required by `assign-entra-app-role.sh` as well as by
`create-entra-app.sh`.

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
AZURE_SEARCH_ENDPOINT=https://<search-name>.search.windows.net \
AZURE_SEARCH_ADMIN_KEY=<search-admin-key> \
AZURE_OPENAI_ENDPOINT=https://<openai-account>.openai.azure.com \
AZURE_OPENAI_API_KEY=<openai-key> \
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=embed-small \
  uv run python tools/index_corpus.py --recreate-index --tenant-id <tenant-guid>
```

This runs on the laptop, not in the container, so it takes the key path:
`AZURE_OPENAI_AUTH` defaults to `api_key`, and the embedding client raises
before the first chunk is embedded if `AZURE_OPENAI_API_KEY` is unset.
There is no managed identity to borrow here — that is the whole reason the
deployed app gets one and this tool does not. Read both values back from
Azure rather than retyping them:

```bash
az cognitiveservices account keys list --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_OPENAI_NAME" --query key1 -o tsv
az search admin-key show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --service-name "$AZ_SEARCH_NAME" \
  --query primaryKey -o tsv
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

On top of §8.1's exports (`AZ_SUBSCRIPTION_ID`, `AZ_RESOURCE_GROUP`,
`AZ_LOCATION`, `AZ_OPENAI_NAME`, `AZ_SEARCH_NAME`, `AZ_KEYVAULT_NAME`,
`ENTRA_TENANT_ID`), which this step still needs:

```bash
export AZ_ACR_NAME=<printed by create-acr.sh>
export ENTRA_AUDIENCE=<api-app-id>
export ENTRA_CLIENT_APP_ID=<client-app-id>
export ENTRA_CLIENT_SECRET=<client-secret>   # read from the environment only

infra/scripts/deploy-container-app.sh
```

`ENTRA_CLIENT_SECRET` is never passed as an argument and never printed;
the script even suspends shell tracing around the guard that reads it,
because `${VAR:?msg}` traces as `+ : <value>` when the variable *is* set.

**Stage 1 changes your machine's state.** It runs `az account set
--subscription "$AZ_SUBSCRIPTION_ID"`, which repoints the *default* `az`
context for every shell that reads it — including the terminal you are in
after this script exits. Every command in the script also passes
`--subscription` explicitly; the default is repointed anyway because
`az acr build` and the directory-object commands read it. The script
announces this on stdout for the same reason it is written down here: if
your CLI was pointed at a work subscription, it is not any more. Point it
back when the session ends.

Eleven labelled stages, in an order that is a contract rather than a
convenience — a role granted before the identity exists, an app created
before its image is in the registry, or a gate run before provisioning
finished are all deploys reporting a success they never earned:

| Stage | What it does |
|---|---|
| 1 | Preflight, and nothing here creates or bills anything: GUID-check `ENTRA_TENANT_ID` / `ENTRA_AUDIENCE`, warn if the `containerapp` extension is missing, register `Microsoft.App`, `Microsoft.ManagedIdentity`, `Microsoft.OperationalInsights` if needed, and **refuse if the app already exists** |
| 2 | Create the user-assigned identity; read back its resource id, principal id and client id |
| 3 | Assign the three roles, each **verified by read-back** rather than by the create call's exit code |
| 4 | Read the Search admin key and store it in Key Vault as `azure-search-admin-key` |
| 5 | `az acr build --platform linux/amd64` — the registry builds the image, no local Docker daemon and no push credentials |
| 6 | Create the Log Analytics workspace explicitly, under a per-run unique name |
| 7 | Create the Container Apps environment (CLI flags — `env create` has no `--yaml`) and poll it to `Succeeded` |
| 8 | Create the app from one generated YAML file, which names its own `properties.environmentId` |
| 9 | Poll `provisioningState` to `Succeeded`, then read back the ingress FQDN |
| 10 | Gate 1 (control plane): state the verdict on everything read back so far |
| 11 | Gate 2 (data plane): poll `/health` to 200, then run the authenticated readiness gate |

Stage 1 is where three otherwise-late failures were pulled back to. The
one that mattered most is the "app already exists" refusal: it used to sit
in stage 8, by which point stage 6 had already created a *second*,
freshly-named Log Analytics workspace. The run then aborted and printed a
teardown command naming the new workspace — while the environment stayed
wired to the old one, which survived, billing, under a random name the
operator no longer had. The other two are cheaper but the same shape: a
non-GUID `ENTRA_AUDIENCE` (the portal displays the Application ID URI as
`api://<guid>`, and pasting the whole thing is the obvious mistake) makes
`Settings()` raise at import so the container never binds a port — a
failure that would otherwise surface at stage 11, after every stage of
mutation. The extension check is deliberately a **warning, not a gate**:
dynamic install genuinely works on many configurations, so refusing to
continue would break working setups to prevent a message.

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

Two things about that upload, both learned on 2026-08-17 by doing it.

**`--file` is passed as an absolute path, and that is load-bearing.**
`az acr build --help` describes it as "the relative path of the the docker
file to the source code root folder". The CLI does not implement that: with
an explicit `--file` the path is resolved against the *caller's working
directory*, and only the default is relative to the source location. Its own
source says so, directly above the existence check (azure-cli 2.89.0,
`command_modules/acr/build.py`):

```python
# NOTE: If docker_file_path is not specified, the default is Dockerfile in source_location.
# Otherwise, it's based on current working directory.
```

A relative `docker/Dockerfile` therefore works only when the caller's
working directory happens to be the repo root — the script's own location
guarantees nothing about that — and on the 2026-08-17 run it was not
(stage 5 died with `Unable to find 'docker/Dockerfile'`). The fix is an
absolute path, which is robust from any working directory and is safe
even though it points outside the context:
`_archive_utils.py` tars the source location and then *separately* opens the
Dockerfile and adds it to the archive under a generated name.

**`.dockerignore` prunes much less here than it does locally.** This
repo's context uploaded as **41.081 MiB** (2026-08-17). Day 23's final
same-tree cold measurement of a local build was **671.22kB** (the 1.12MB
sometimes quoted was the pre-fix reading; [docker.md](docker.md) records
both). The two numbers come from different trees on different days, so
read them as orders of magnitude, not a diff — the point is that the
remote path uploads ~60× more than the local one prunes to. Most of
the difference is a bug: in `_archive_utils.py`, `IgnoreRule.__init__`
strips a rule's trailing slash *only* inside the `if rule.startswith('!')`
branch. So an exclusion written `site/` compiles to the regex `^site/$`
while tar members are named `site` and `site/package.json` — it can never
match — whereas the exception `!site/` compiles to `^site$` and does. That
asymmetry is what makes it a bug rather than a convention. Every
directory rule in this repo's `.dockerignore` is written with a trailing
slash, so on this path none of them apply; `.venv` and `.git` are excluded
only by az's own hardcoded lists. Rules without a trailing slash (`site`,
`**/*.pyc`) work normally.

The remaining difference is not a bug: local BuildKit prunes the context to
the paths a `COPY` actually references, while `az acr build` tars the whole
context first and prunes only by `.dockerignore`.

Stage 6 exists because of a bill nobody would go looking for. `az
containerapp env create` auto-generates a Log Analytics workspace when
none is given — the CLI reference's own first example is titled "Create an
environment with an auto-generated Log Analytics workspace", and
`--logs-destination` defaults to `log-analytics`
([az containerapp env create](https://learn.microsoft.com/en-us/cli/azure/containerapp/env#az-containerapp-env-create),
checked 2026-08, ms.date 2026-08-04). That workspace is a separate
`Microsoft.OperationalInsights/workspaces` resource with its own
lifecycle; nothing in the environment delete documents cascading to it,
and this repo has not tested the auto-provision path — the script never
takes it. It creates the workspace explicitly, under a per-run unique
name it prints, so the teardown *owns* that lifecycle instead of having
to reverse-engineer which workspace an omitted argument produced.
Guessing at names would be the Day 21 mistake all over again.

Stage 8 binds the app to that environment **in the YAML**, not on the
command line. `az containerapp create --help` states that with `--yaml`,
"all other parameters will be ignored" — so `--environment` on that call
cannot be what carries the binding, and a YAML without
`properties.environmentId` is a stage 8 failure that arrives after the
identity, its three roles, the secret, the image, the workspace and the
environment all exist and all bill. Stage 7 therefore reads the
environment's resource id back (fail-closed, like every other read here)
and stage 8 writes it into the file. `--environment` is passed anyway:
harmless if ignored, correct if honoured, and the binding does not depend
on which is true.

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

Still from the repository root, still with §8.1's exports in the shell:

```bash
AZ_ACA_APP_NAME=aca-azgenai-lab AZ_ACA_ENV_NAME=acaenv-azgenai-lab \
AZ_MI_NAME=mi-azgenai-lab AZ_LAW_NAME=<printed by the deploy script> \
  infra/scripts/delete-container-app.sh

infra/scripts/delete-acr.sh
infra/scripts/delete-keyvault.sh
infra/scripts/delete-search.sh
ENTRA_API_APP_ID=<api-app-id> ENTRA_CLIENT_APP_ID=<client-app-id> \
  infra/scripts/delete-entra-app.sh
```

The first command is the one the deploy script prints for you, with the
run's real values filled in — including the generated `AZ_LAW_NAME`, which
has no default to fall back on — both on success and on any failure that
already mutated something. It names every scope knob
`delete-container-app.sh` needs, `AZ_ACR_NAME` and `AZ_OPENAI_NAME`
included, because an omitted knob does not fail loudly: that scope's
role-assignment delete is skipped with a warning, and step 6's fail-closed
read-back then aborts the run *before* the identity is deleted, leaving
behind exactly the orphaned assignments §9 exists to prevent.

## 9. Teardown ordering is a contract, not a convenience

`delete-container-app.sh` runs seven steps, and the order is the whole
design:

1. Delete the Container App — read back to confirm gone.
2. Delete the Container Apps environment — read back to confirm gone.
3. Delete the Log Analytics workspace, if a name was given.
4. Read back the managed identity's **principal id**.
5. Delete the role assignment at each scope (ACR, Key Vault, Azure OpenAI).
6. Read back **all** role assignments still held by that principal, at
   every scope (`--all`) — fail closed.
7. Only then delete the identity — read back to confirm gone.

What it does **not** delete, because each belongs to a resource that
outlives the deployment: the `azure-search-admin-key` secret (goes with
the vault, via `delete-keyvault.sh`) and the image tag in the registry
(goes with the registry, via `delete-acr.sh`). §8.5 is the full order.

**Azure does not delete a managed identity's role assignments when the
identity is deleted.** They are left behind, each one displaying "Identity
not found", scattered across the ACR, Key Vault and Azure OpenAI
resources, with nothing in the portal pointing back at them
([Managed identity best practice recommendations § Maintenance](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/managed-identity-best-practice-recommendations#maintenance)).
Nobody notices, because nothing breaks.

The principal id dies with the identity, and with it the ability to read
that id back *from the identity*. The same Maintenance page's remedy for
already-orphaned assignments is enumeration: list assignments whose
principal resolves to `ObjectType: Unknown` and remove them. That works,
but it is a sweep, not a lookup — an Unknown-type assignment no longer
says *which* deleted identity it belonged to. So step 4 reads the id
*before* anything identity-related is deleted, and step 6 refuses to
continue if a single assignment remains: deleting the identity at that
point converts a precisely attributable state (an identity that can still
be queried by id) into one that can only be cleaned up by scanning each
scope for Unknowns.

Step 6 deliberately queries **by assignee alone** — no `--scope`, no
`--role` — and with `--all`. That is what catches a scope this run skipped
because its name knob was unset, or one a future deploy grants that this
script does not know about yet. A missing name knob in step 5 is skipped
with a warning to stderr, never silently.

`--all` is load-bearing, not tidiness. `az role assignment list` documents
its own default as subscription scope only ("To view assignments scoped by
resource or group, use `--all`"), and all three assignments here are at
*resource* scope — the ACR, the vault, the Azure OpenAI account. Without
it the query returns `0` no matter what is actually assigned, and the
entire step is vacuous: run the teardown with `AZ_ACR_NAME` unset, step 5
warns and skips, a scope-blind step 6 reports zero remain, step 7 deletes
the identity, and the `AcrPull` assignment is orphaned permanently. No
behavioural test can catch a flag that only ever narrows what a query
sees, which is why the regression that pins it asserts on the command's
arguments.

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
contract for that measurement. **It has now been run** — once, on
2026-08-17, in japaneast; the results are in
[§11.1](#111-the-measured-result-2026-08-17) and every number there carries
that date.

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
| Request drain ended | `lifespan shutdown started` (uvicorn enters lifespan shutdown once the drain finishes — `--timeout-graceful-shutdown` bounds that drain, it does not schedule it; an idle app drains at once, see §11.1) |
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

### 11.1 The measured result (2026-08-17)

One run, japaneast, triggered by `az containerapp revision deactivate`,
with the revision and replica recorded before the trigger. All three
markers present and bound to that replica, so the run is valid under the
rules above. Logs were read from the Log Analytics workspace rather than a
log stream — after deactivation the replica is gone, and the workspace
still has its logs.

The app's markers, on the app's own clock:

```
02:09:06,351  lifespan shutdown started
02:09:06,351  shutdown cleanup started budget_seconds=8.0
02:09:06,352  shutdown cleanup finished elapsed_seconds=0.001
```

The platform's events for the same revision:

```
02:09:06.2106597Z  KEDAScalersStopped    stopping the watch for this revision
02:09:07.1582116Z  ContainerTerminated   reason 'ManuallyStopped'
```

Against the three terms:

- **Cleanup elapsed: 0.001s against the 8.0s budget.** Precisely
  verifiable, single-source. The budget is nowhere near binding.
- **Total termination: on the order of two seconds against a 30-second
  grace.** Coarsely verifiable — the deactivate call returned at 02:09:05Z
  and the platform reported termination at 02:09:07.16Z. Cross-source, so
  it is a window, not a duration.
- **The 2-second overhead margin: still not verified**, exactly as this
  contract predicted. Verifying it would require subtracting across two
  clocks. It remains a conservative allocation.

The Day 23 *attempted*-vs-*completed* ruling therefore stands unchanged:
nothing was interrupted, and no debt is recorded against it.

**What this run does not measure.** The app was idle — no in-flight
requests, no open upstream streams, no held conversation locks. This is the
floor of the cleanup path, not its behaviour under load; a shutdown during
active streaming could look very different, and nothing here speaks to it.
Day 23's local held-stream measurement is the closest available datum.

**A correction to how the drain was described.** The marker table
previously said uvicorn "enters lifespan shutdown only after
`--timeout-graceful-shutdown` completes", which reads as a fixed 20-second
wait. This run shows otherwise, within the clock discipline's limits: the
KEDA scaler-stop events for the revision are logged at 02:09:06.21 (they
are the platform's earliest teardown trace here, not an identified
SIGTERM-delivery timestamp) and the app's `lifespan shutdown started` at
02:09:06.351 — two different clocks, so no duration can be computed, but
both land in the same second, which rules out a 20-second wait on an idle
app. The flag is a **ceiling** on the drain, not a
delay that is spent. Day 23's own local measurement is consistent — with a
deliberately held SSE stream the drain *was* cut off at ~20s, because there
the ceiling was reached.

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

- **This page has now been deployed from, once, on 2026-08-17.** The
  scripts ran end to end in japaneast and the app served authenticated
  traffic. Three things this page previously listed as pending are
  answered: the shutdown timeline is in §11.1; `az acr build` uploaded
  41.081 MiB of build context, for the reasons in §8.3; and the readiness
  gate passed on its first authenticated attempt. That last one is **not**
  a propagation measurement — the role assignments already existed from an
  earlier attempt that day, so this session consumed no propagation wait
  and Day 20's 14m44s remains the only measured figure.
- **Three defects in these scripts were found by deploying, not by
  reviewing them.** `az acr build --file` is resolved against the caller's
  working directory rather than the source location (the CLI's help says
  the opposite; its source says this); an optional field omitted from the
  app YAML is transmitted as an explicit JSON `null`, which the API rejects
  for a non-nullable boolean; and a backtick inside a YAML *comment* in an
  unquoted heredoc ran as a command substitution. Each had passed every
  prior review of the same lines.
- **Single-observation discipline applies to everything measured in that
  session.** Day 13's rule stands: one probe does not adjudicate a
  documentation conflict, and a number measured once is pinned to its
  date, region and API version rather than generalized. Day 20's
  14-minute-44-second role propagation is exactly that kind of number —
  a counterexample to "up to 5 minutes", not a new bound — and the reason
  the readiness gate's default deadline is as long as the next bullet says
  it is.
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
