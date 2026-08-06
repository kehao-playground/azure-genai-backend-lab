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
| `deploy-container-app.sh` | Deploy to Azure Container Apps | placeholder (Day 24) |
| `configure-apim.sh` | APIM (Consumption tier) fronting Azure OpenAI v1 with managed-identity auth | working |
| `delete-apim.sh` | Delete and purge the APIM instance + its role assignments | working |
| `create-keyvault.sh` | Ephemeral Key Vault (explicit RBAC flag; purge protection off by omission — the API rejects an explicit `false` — with read-back asserted; 7-day soft delete) + idempotent Secrets Officer role for the signed-in user | working |
| `delete-keyvault.sh` | Delete and purge that vault from any state (live / soft-deleted / absent), bounded waits, final absence assertion | working |
| `create-entra-app.sh` | Create the Day 19 API + client Entra ID app registrations, one client secret, and delegated admin consent | working |
| `assign-entra-app-role.sh` | Assign the API's application role to the client service principal (idempotent) | working |
| `delete-entra-app.sh` | Delete those two registrations — and only those two | working |

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
