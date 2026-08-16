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
| `deploy-container-app.sh` | Deploy to Azure Container Apps | placeholder (Day 24) |
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
