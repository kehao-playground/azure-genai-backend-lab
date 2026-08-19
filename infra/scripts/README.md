# Scripts

| Script | Purpose | Status |
|---|---|---|
| `create-budget-alert.sh` | Subscription budget (US$20) with alerts — run first | working |
| `create-resource-group.sh` | Create the demo resource group | working |
| `create-openai.sh` | Azure OpenAI account + mini-model deployment (token-billed, may persist) | working |
| `delete-openai.sh` | Delete and purge the Azure OpenAI account | working |
| `create-search.sh` | Create an ephemeral Azure AI Search service (free tier by default, `AZ_SEARCH_SKU=basic` when needed) | working |
| `delete-search.sh` | Delete that service only — `teardown.sh` deletes the whole resource group, including the Azure OpenAI resource this series keeps | working |
| `teardown.sh` | Delete the demo resource group and everything in it | working |
| `create-acr.sh` | Ephemeral Azure Container Registry (Basic SKU by default; per-run unique name) with provider pre-check | working |
| `delete-acr.sh` | Delete that registry — synchronous, no soft-delete/purge step (unlike Key Vault / Content Safety) | working |
| `deploy-container-app.sh` | Deploy to Azure Container Apps in eleven stages: managed identity, three role assignments, Search key → Key Vault, `az acr build`, Log Analytics workspace, environment, app, two-stage readiness gate; `AZ_SEARCH_MODE=fake` (default `real`) skips every Search/Key Vault coupling for sessions that don't need either | working |
| `delete-container-app.sh` | Tear that down in seven steps — app, environment, workspace, **role assignments before the identity** (see below) | working |
| `update-container-app.sh` | Point an already-deployed app at a new image by digest and refuse success unless it is actually served: pre-mutation snapshot (verbatim, tag or digest), `az containerapp update --image`, fail-closed image and revision-state read-backs, `/health` exact-body smoke — no automatic rollback, only an advisory command printed on failure | working |
| `configure-apim.sh` | APIM (Consumption tier) fronting Azure OpenAI v1 with managed-identity auth | working |
| `delete-apim.sh` | Delete and purge the APIM instance + its role assignments | working |
| `create-keyvault.sh` | Ephemeral Key Vault (explicit RBAC flag; purge protection off by omission — the API rejects an explicit `false` — with read-back asserted; 7-day soft delete) + idempotent Secrets Officer role for the signed-in user | working |
| `delete-keyvault.sh` | Delete and purge that vault from any state (live / soft-deleted / absent), bounded waits, final absence assertion | working |
| `create-entra-app.sh` | Create the Day 19 API + client Entra ID app registrations, one client secret, and delegated admin consent | working |
| `assign-entra-app-role.sh` | Assign the API's application role to the client service principal (idempotent) | working |
| `delete-entra-app.sh` | Delete those two registrations — and only those two | working |
| `create-content-safety.sh` | Ephemeral Content Safety account (F0; conditional S0 fallback on allowlisted error code) with provider pre-check | working |
| `delete-content-safety.sh` | Delete and purge that account from any state (live / soft-deleted / absent), bounded waits, final absence assertion | working |
| `run-content-safety-probe.sh` | Orchestrate create → Prompt Shields probe → delete/purge, EXIT-trap cleanup armed before create | working |
| `create-github-oidc.sh` | Provision the two federated (secret-less) GitHub Actions identities (build: `AcrPush` on the registry; deploy: `Container Apps Contributor` on the app), the GitHub `production` environment (required reviewer + branch-restricted to `main`, read back and compared), repository variables, then arms `DEPLOY_ENABLED=true` last | working |
| `delete-github-oidc.sh` | Tear that down from the record file `create-github-oidc.sh` wrote: `DEPLOY_ENABLED=false` first, delete both federated credentials, a repo-scoped drain check that aborts (never cancels) on any non-terminal run, delete role assignments then app registrations, delete the GitHub environment and repository variables — plus a read-only `--verify-teardown` mode that only removes the record file once nothing it names is still found | working |

All scripts read configuration from environment variables, fail fast, and never hardcode subscription IDs or secrets.

Every script requires `AZ_SUBSCRIPTION_ID` and passes `--subscription` explicitly on each az call. The default az context is shared mutable state — an `az login` in another terminal can silently repoint it, which is exactly how you delete resources in the wrong subscription.

## Key Vault (Day 20)

A minimal round trip (each variable's contract is in the script headers;
`AZ_LOCATION` defaults to `japaneast` in **both** scripts — override both or
neither, because purge needs the same location the vault was created in):

```bash
export AZ_SUBSCRIPTION_ID=... AZ_RESOURCE_GROUP=rg-azgenai-lab AZ_KEYVAULT_NAME=kv-example-d20

./create-keyvault.sh                                   # validates inputs + tenant BEFORE mutating
az keyvault secret set --subscription "$AZ_SUBSCRIPTION_ID" \
  --vault-name "$AZ_KEYVAULT_NAME" --name demo --value example
az keyvault secret show --subscription "$AZ_SUBSCRIPTION_ID" \
  --vault-name "$AZ_KEYVAULT_NAME" --name demo --query value -o tsv
./delete-keyvault.sh                                   # delete -> wait -> purge -> assert absent
```

The caller's privileges are three **distinct** sets, and missing ones fail at
different stages: `Microsoft.KeyVault/register/action` on the subscription
(provider registration, first vault ever only), vault create/delete on the
resource group, and `Microsoft.Authorization/roleAssignments/write` on the
vault scope (e.g. Owner or User Access Administrator) for the Secrets Officer
grant. Reading/writing secrets afterwards needs the **data-plane** role the
script assigns — under RBAC not even the vault's creator has data access
without it. The role is granted to the signed-in **user**
(`--assignee-principal-type User`); running the script as a service principal
requires changing that step.

Recovery is state-based, not history-based: `create-keyvault.sh` refuses to
run over an existing live or soft-deleted vault and prints the exact
`delete-keyvault.sh` command if it fails midway; `delete-keyvault.sh` can be
re-run from any state — it handles live, soft-deleted (including when the
resource group is already gone: purge needs only name + location), and absent,
with bounded waits, and exits by asserting the name is gone from both the
active and soft-deleted listings. That assertion is the exact teardown claim
the scripts make — subscription-level provider registration intentionally
remains.

## Entra ID (Day 19)

Directory objects have no `--subscription` equivalent: every `az ad` call goes to whichever tenant the active account belongs to. So the three Entra scripts take `ENTRA_TENANT_ID` explicitly and compare it against `az account show --query tenantId -o tsv` **before the first mutation**, refusing to run against the wrong tenant rather than discovering it afterwards.

The provisioning is split into two steps on purpose. The live smoke needs two different tenant states, and one run cannot hold both: a token that carries no application role, and the same client after the role is assigned.

```bash
export ENTRA_TENANT_ID=... ENTRA_API_APP_NAME=azgenai-lab-api ENTRA_CLIENT_APP_NAME=azgenai-lab-client

# 1. provision everything EXCEPT the application-role assignment
./create-entra-app.sh --defer-app-role-assignment
#    -> prints the app ids, the server env block, and the client secret once

# 2. run the server with the printed env block plus the fake adapters, and
#    keep its log where the smoke tool can read it
AUTH_MODE=entra ENTRA_TENANT_ID=... ENTRA_AUDIENCE=... ENTRA_REQUIRED_SCOPE=access_as_user \
  ENTRA_REQUIRED_APP_ROLE=Api.Access USE_FAKE_LLM=true USE_FAKE_SEARCH=true USE_FAKE_EMBEDDINGS=true \
  uv run uvicorn azgenai_lab.main:app 2>&1 | tee server.log

# 3. negative smoke: a valid-audience token with no role must get 403
ENTRA_CLIENT_SECRET=... uv run python tools/entra_smoke.py --phase no-role \
  --tenant-id "$ENTRA_TENANT_ID" --api-app-id <api-app-id> --client-id <client-app-id>

# 4. assign the role (idempotent — safe to re-run)
ENTRA_API_APP_ID=<api-app-id> ENTRA_CLIENT_APP_ID=<client-app-id> ./assign-entra-app-role.sh

# 5. full smoke: delegated 200, app-only 200, ID token 401, non-JWT 401, log join
ENTRA_CLIENT_SECRET=... uv run python tools/entra_smoke.py --phase full \
  --tenant-id "$ENTRA_TENANT_ID" --api-app-id <api-app-id> --client-id <client-app-id> \
  --server-log server.log --evidence-out evidence.txt

# 6. teardown — deletes both registrations, their service principals, the
#    secret, the permission grant and the role assignment
ENTRA_API_APP_ID=<api-app-id> ENTRA_CLIENT_APP_ID=<client-app-id> ./delete-entra-app.sh
```

Notes:

- The API service principal keeps `appRoleAssignmentRequired=false`. That is what makes step 3 a test of *this API*: Entra will issue a valid-audience token with no `roles` claim, and the 403 comes from the server refusing a credential it authenticated — not from the token endpoint refusing to mint one.
- `create-entra-app.sh` prints each application id **the moment it is created**, and on any abort it prints the exact `delete-entra-app.sh` command for whatever exists so far. A registration that was never created is passed as `none`, which the teardown script skips — so a half-finished run is still fully reversible without reasoning about which half exists.
- The client secret expires after **7 days**, matching the ephemeral posture. It is printed to the terminal exactly once and never written to a file. `ENTRA_CLIENT_SECRET` is read from the environment by the smoke tool, never passed as a command-line argument (`ps` shows another user the whole argv of a running process).
- `--evidence-out` is safe to commit: it carries PASS/FAIL, check names, redacted details and sorted claim *key names* — no tenant id, no app ids, no `oid`, no token, no secret.
- The server runs with the fake adapters, so the smoke exercises Entra verification only and costs nothing in model tokens.

## Content Safety (Day 21)

The Prompt Shields probe (see [prompt-injection.md §5](../../docs/prompt-injection.md#5-the-probabilistic-layer-azure-ai-content-safety-prompt-shields)) needs an ephemeral, key-authenticated Content Safety account — no role assignment, the orchestrator reads the account key back directly:

```bash
export AZ_SUBSCRIPTION_ID=... AZ_RESOURCE_GROUP=rg-azgenai-lab AZ_CONTENT_SAFETY_NAME=cs-example-d21
export EVIDENCE_OUT=evidence.txt

./run-content-safety-probe.sh   # create -> probe -> delete + purge, always
```

`AZ_CONTENT_SAFETY_NAME` is a **name prefix** for the orchestrator, not the account name. Each run appends an unpredictable 4-byte (8 hex character) suffix read from `/dev/urandom` — `cs-example-d21` becomes something like `cs-example-d21-a3f9c1d0` — and exports the resolved name back under the same variable, so the two child scripts still receive a final name and their own interface does not change. The name each run creates is printed as the run starts.

That is a correctness property, not tidiness. The pre-existence guard in `create-content-safety.sh` and the `create` call are separate operations: a name that was free at guard time can be taken in between, and the create then fails with a generic exit 1 rather than the guard's exit 3 — so the orchestrator's `CREATE_REFUSED` stays 0, its EXIT trap runs `delete-content-safety.sh`, and that script finds the account **by name** and purges it. Purge is irreversible. At a 4-byte (32-bit) random suffix, it is overwhelmingly likely that only this run invented the resolved name, so cleanup can almost always only target a resource this run created: two runs sharing a prefix would have to collide with probability on the order of 2⁻³², which is not realistically reachable at this project's run volume — the TOCTOU class is not narrowed to a point, but it is not something a run of this project will hit in practice. It is also what makes the stabilization wait below safe — waiting for a not-yet-visible account can never end up waiting onto somebody else's resource, short of that same astronomically unlikely collision.

`Microsoft.CognitiveServices/accounts` names are 2–64 characters, alphanumerics and hyphens, starting and ending with an alphanumeric ([Azure resource naming rules](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules), checked 2026-08). The suffix costs 9 characters ("-" + 8 hex), so the prefix must be **at most 55** and match the same character rule. A longer or malformed prefix aborts before anything is queried or created — it is never silently truncated, since a truncated prefix could land on a name the run does not own.

Each script's required/optional env vars (defaults in the script headers):

| Var | Required by | Notes |
|---|---|---|
| `AZ_SUBSCRIPTION_ID` | all three | never rely on the default az context |
| `AZ_RESOURCE_GROUP` | all three | must already exist |
| `AZ_CONTENT_SAFETY_NAME` | all three | **`run-content-safety-probe.sh`: a name prefix** (≤ 55 chars) that the run turns into a unique account name; the two child scripts take it as the final, globally unique name — also used as the custom subdomain |
| `EVIDENCE_OUT` | `run-content-safety-probe.sh` only | path the probe writes its evidence JSON to |
| `AZ_LOCATION` | optional, all three | defaults to `japaneast` in every script — override all or none, since purge needs the same location the account was created in |
| `AZ_CONTENT_SAFETY_SKU` | optional, `create-content-safety.sh` | defaults to `F0` (free tier, one per subscription) |
| `CONTENT_SAFETY_SKU_FALLBACK_CODES` | optional, `create-content-safety.sh` | comma-separated machine-readable error `code` values safe to auto-retry with `--sku S0`. **Ships empty on purpose**: until a live run observes a stable code that unambiguously means "SKU/quota, not something else," any create failure aborts instead of silently falling back and masking a real problem — rerun explicitly with `AZ_CONTENT_SAFETY_SKU=S0` instead |
| `PROMPT_SHIELDS_CASES_FILE` | optional, `run-content-safety-probe.sh` | defaults to `tools/prompt_shields_cases.json` (the canonical 8-case fixture) at the repo root |
| `AZ_CS_CREATE_ATTEMPTED` | optional, `delete-content-safety.sh` | set to `1` by the orchestrator before it issues the create; makes the "absent" branch take a bounded stabilization wait instead of concluding absence from one reading. Leave it unset when running the delete script standalone |

`create-content-safety.sh` registers the `Microsoft.CognitiveServices` provider before its first call if needed, then creates the account. On any failure past that point it prints the exact `delete-content-safety.sh` recovery command — it is a recovery hint only, it does not delete anything itself; the real delete/purge cleanup is owned solely by `run-content-safety-probe.sh`'s EXIT trap, armed *before* create runs, so a half-created account is still torn down and a failing probe still ends the run non-zero.

`delete-content-safety.sh` deletes and purges from any state (live / soft-deleted / absent), with bounded waits, and exits by asserting the name is gone from both the active and soft-deleted listings. Purging matters here for the same reason it does for Key Vault: Cognitive Services accounts (Content Safety included) soft-delete and **block re-creation of the same name for 48 hours**. A per-run name means a skipped purge no longer blocks the *next* run by name — but it leaves a soft-deleted account holding a name and a resource until something purges it, and those accumulate silently. Run the delete script for anything left behind.

One reading of "in neither listing" is not proof of absence, so the absent branch is conditional. If Azure accepted the create but the CLI reported failure and the listings have not caught up, exiting 0 there would abandon an account that materialises moments later with no teardown ever running. When `AZ_CS_CREATE_ATTEMPTED=1` — set by the orchestrator *before* it issues the create, precisely because the case it covers is a create whose outcome never came back — the script re-reads both listings on the same bounded-poll schedule as its other waits before concluding there is nothing to delete, and every query still aborts on failure rather than being read as "still absent". Standalone runs leave the variable unset and keep the immediate fast path.

## Container Apps (Day 24)

The full deploy session — which scripts to run, in what order, with which
variables — is documented in
[docs/container-apps.md § The deploy session, end to end](../../docs/container-apps.md#8-the-deploy-session-end-to-end).
Three things about these two scripts belong here.

**`create-acr.sh` generates its own name.** `AZ_ACR_NAME` defaults to a
prefix plus an 8-hex CSPRNG suffix, freshly generated each run, and the
resolved name is printed so it can be exported for the deploy step. Same
discipline as the Content Safety orchestrator above, for the same reason:
Day 21's cleanup irreversibly purged a *concurrent* run's resource because
it identified that resource by a fixed name. `deploy-container-app.sh`
generates its Log Analytics workspace name the same way and prints it —
hand that value to `delete-container-app.sh`, because there is no sensible
default to fall back on and an unnamed workspace keeps billing.

**The deploy script creates the Log Analytics workspace on purpose.**
`az containerapp env create` will auto-provision one if you don't, and
`env delete` does not remove it — it is a separate
`Microsoft.OperationalInsights/workspaces` resource with its own
lifecycle. Auto-provisioning it would leave a recurring charge behind that
this teardown could never find, because it was never told the name Azure
chose.

**Teardown order is the contract, not a convenience.** Azure does *not*
delete a managed identity's role assignments when the identity is deleted;
it leaves them behind on the ACR, Key Vault and Azure OpenAI resources,
each reading "Identity not found" and none of them visible unless you go
looking ([managed identity best practices § Maintenance](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/managed-identity-best-practice-recommendations#maintenance),
checked 2026-08-16). So `delete-container-app.sh` reads the principal id
*first* — it is the only handle for finding those assignments, and it dies
with the identity — deletes each assignment, proves zero remain with a
read-back by assignee alone, and only then deletes the identity. A
non-empty read-back aborts before the identity is touched: an identity
that still exists can be re-queried, an orphaned assignment cannot be
traced back to anything.

Both scripts follow the same fail-closed rule as everything else here: an
`az ... -o tsv` read that returns empty output aborts, and is never read
as "absent", "zero" or "not yet".

## CI/CD (Day 25)

The full pipeline design — the workflow shape, why two identities, subject
binding, the layered controls, digest-not-tag, repository variables versus
secrets, `DEPLOY_ENABLED` — is documented in
[docs/ci-cd.md](../../docs/ci-cd.md). What belongs here is the runbook
order: which script runs when, relative to the Container Apps scripts
above.

```bash
# 1. Bring up the deploy target first -- create-github-oidc.sh's role
#    assignments are scoped to these two resources and refuse to proceed
#    if either does not yet exist.
infra/scripts/create-acr.sh                # prints AZ_ACR_NAME
infra/scripts/deploy-container-app.sh       # prints AZ_ACA_APP_NAME's app id

# 2. Provision the two federated identities, the GitHub environment, and
#    arm the pipeline. Run once per session, after step 1.
export OIDC_RECORD_FILE=oidc-record.env
export GITHUB_REPO=<owner>/<repo>
export AZ_TENANT_ID=<tenant-guid> AZ_SUBSCRIPTION_ID=<subscription-guid>
export AZ_RESOURCE_GROUP=rg-azgenai-lab
export AZ_ACR_NAME=<printed by create-acr.sh>
export AZ_ACA_APP_NAME=<printed by deploy-container-app.sh>
infra/scripts/create-github-oidc.sh
#    -> prints both client ids and confirms DEPLOY_ENABLED=true

# 3. Push to main. ci.yml's `image` job builds and pushes by tag, then
#    reads the digest back; `deploy` waits for the required reviewer's
#    approval, then runs:
infra/scripts/update-container-app.sh --image <acr>.azurecr.io/azgenai-lab@sha256:...
#    -- this is the same script the pipeline itself invokes; it is not a
#    separate manual step, just the one worth knowing how to run by hand
#    for a manual re-point between pipeline runs.

# 4. Tear down, in the order docs/container-apps.md's own runbook uses:
#    the CI/CD identities first, while the ACR and app they hold
#    assignments on still exist.
OIDC_RECORD_FILE=oidc-record.env infra/scripts/delete-github-oidc.sh
# ... then the Container Apps teardown in container-apps.md §8.5
```

`create-github-oidc.sh` writes every identifier `delete-github-oidc.sh`
needs into `OIDC_RECORD_FILE`, appended the moment each object exists —
unlike the Entra and Content Safety scripts above, which recover by
printing ids and teardown commands rather than by writing a file, this
script provisions six-plus objects across two systems (Entra app
registrations, federated credentials, role assignments, a GitHub
environment, repository variables) and needs a durable list rather than a
terminal scrollback to reverse any of it. It refuses to overwrite an
existing record file, for the same reason: that file is the only list of
what a previous run created.

`AZ_SEARCH_MODE=fake` (default `real`) on `deploy-container-app.sh` is
worth calling out here specifically because it changes what step 1 above
needs: with `AZ_SEARCH_MODE=fake`, no Key Vault coupling happens and no
`AZ_KEYVAULT_NAME` is required, but `create-github-oidc.sh` in step 2 still
only ever grants the deploy identity `Container Apps Contributor` on the
app — the Search/Key Vault mode does not change which roles the CI/CD
identities hold.
