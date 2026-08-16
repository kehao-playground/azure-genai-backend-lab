#!/usr/bin/env bash
# Deploy the backend to Azure Container Apps (Day 24), keyless where Azure
# allows it and gated on readiness rather than on "the CLI returned 0".
#
# Eleven labelled stages, in an order that is a contract rather than a
# convenience: a role granted before the identity exists, an app created
# before its image is in the registry, or a readiness gate run before
# provisioning finished are all deploys that report a success they never
# earned. Every stage that reads state reads it back from Azure and fails
# closed on an empty `-o tsv` result -- Day 19 and Day 21 each lost a live
# session to an empty read treated as a value.
#
# What this creates is all ephemeral, but it is NOT all removed by one
# script, and the split matters: an operator who reads "teardown removes
# everything" and runs only delete-container-app.sh is left paying for what
# it never claimed to touch.
#
# Removed by delete-container-app.sh:
#   * a user-assigned managed identity (the app's identity in every direction)
#   * three role assignments on three existing resources (ACR, Key Vault, AOAI)
#   * a Log Analytics workspace, created explicitly under a per-run unique
#     name rather than left to auto-provision (see stage 6)
#   * a Container Apps environment and the app itself
#
# NOT removed by delete-container-app.sh -- each outlives it and is owned by
# the script that owns its host resource (see the full teardown order in
# docs/container-apps.md §8.5):
#   * one Key Vault secret (azure-search-admin-key) -- goes with the vault,
#     via delete-keyvault.sh
#   * one image tag in the existing registry -- goes with the registry, via
#     delete-acr.sh
#
# The identity is user-assigned rather than system-assigned because its role
# assignments must exist BEFORE the app first runs (docs/managed-identity.md
# §3): a system-assigned principal does not exist until the app does, which
# inverts the ordering this script depends on. Role assignments also outlive
# the identity they name -- deleting the identity leaves them behind as
# dangling principal ids -- which is why teardown order matters and why
# delete-container-app.sh removes assignments before the identity.
#
# Prerequisites, each created by its own script and none created here:
#   create-resource-group.sh, create-openai.sh, create-search.sh,
#   create-keyvault.sh, create-acr.sh, create-entra-app.sh
#
# Also required: the `containerapp` az extension. Recent CLI versions install
# it on first use, which in a non-interactive shell can either prompt or fail
# depending on `extension.use_dynamic_install`. Install it once up front
# (`az extension add --name containerapp`) rather than discovering the answer
# at stage 7, after the identity, its roles and the secret already exist.
# Stage 1 checks for it and says so loudly if it is absent, but does not
# refuse to continue: dynamic install genuinely works on many configurations,
# and turning a working setup into a hard abort would be a worse failure than
# the warning it replaces.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID   - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP    - existing resource group holding every resource below
#   AZ_ACR_NAME          - existing registry (from create-acr.sh, which prints it)
#   AZ_KEYVAULT_NAME     - existing key vault (create-keyvault.sh)
#   AZ_SEARCH_NAME       - existing search service (create-search.sh)
#   AZ_OPENAI_NAME       - existing Azure OpenAI account (create-openai.sh)
#   ENTRA_TENANT_ID      - tenant of the API app registration (create-entra-app.sh)
#   ENTRA_AUDIENCE       - the API application's client id, the `aud` the server validates
#   ENTRA_CLIENT_APP_ID  - the client application's id (create-entra-app.sh); stage 11's
#                          readiness gate uses it to acquire its own app-only token
#   ENTRA_CLIENT_SECRET  - that client application's secret; read only from the
#                          environment, never logged, never an argv (same discipline
#                          tools/entra_smoke.py already applies to it)
# Optional env vars:
#   AZ_LOCATION                       - defaults to japaneast
#   AZ_ACA_ENV_NAME                   - defaults to acaenv-azgenai-lab
#   AZ_ACA_APP_NAME                   - defaults to aca-azgenai-lab
#   AZ_MI_NAME                        - defaults to mi-azgenai-lab
#   IMAGE_TAG                         - defaults to day-24
#   ENTRA_REQUIRED_SCOPE              - defaults to access_as_user (create-entra-app.sh's default)
#   ENTRA_REQUIRED_APP_ROLE           - defaults to Api.Access (ditto)
#   AZURE_OPENAI_ENDPOINT             - defaults to the account's own read-back endpoint
#   AZURE_OPENAI_DEPLOYMENT_NAME      - defaults to chat-mini
#   AZURE_OPENAI_EMBEDDING_DEPLOYMENT - defaults to embed-small
#   AZURE_SEARCH_ENDPOINT             - defaults to https://$AZ_SEARCH_NAME.search.windows.net
#   GATE_DEADLINE_SECONDS             - readiness gate deadline, defaults to 1200
#   ACA_POLL_ATTEMPTS / ACA_POLL_INTERVAL - provisioning and health polling bounds
#   ROLE_POLL_ATTEMPTS                - role-assignment read-back attempts
#   AZ_LAW_NAME                       - Log Analytics workspace name; defaults to a
#                                        fresh per-run name (lawazgenai + 8 hex chars
#                                        from python3's secrets module), the same
#                                        per-run-unique discipline create-acr.sh uses.
#                                        Printed at the end so it can be handed to
#                                        delete-container-app.sh.
#
# Privileges the caller needs: resource write on the resource group, role
# assignment write (Microsoft.Authorization/roleAssignments/write, e.g. Owner
# or User Access Administrator) on the three scopes below, and Key Vault
# Secrets Officer on the vault (create-keyvault.sh grants the last one).
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_ACR_NAME:?Set AZ_ACR_NAME (create-acr.sh prints the generated name)}"
: "${AZ_KEYVAULT_NAME:?Set AZ_KEYVAULT_NAME}"
: "${AZ_SEARCH_NAME:?Set AZ_SEARCH_NAME}"
: "${AZ_OPENAI_NAME:?Set AZ_OPENAI_NAME}"
: "${ENTRA_TENANT_ID:?Set ENTRA_TENANT_ID (create-entra-app.sh prints it)}"
: "${ENTRA_AUDIENCE:?Set ENTRA_AUDIENCE (the API application id)}"
# No apostrophe in either message below: an unmatched `'` inside a
# `${VAR:?word}` word is significant to bash's parser even though the whole
# thing sits inside double quotes -- it has to be, so the word can itself
# contain a balanced nested quote -- and a single stray one does not just
# fail loudly. It silently merges the rest of the statement (and the next
# one) into one swallowed literal, so the *next* guard's `${VAR:?...}` never
# actually expands or checks anything. Confirmed by hand: the previous
# wording here paired an apostrophe in this line with one in the
# ENTRA_CLIENT_SECRET line below, and with both present the secret guard
# silently never ran, at all, in any mode -- not merely traced or untraced.
: "${ENTRA_CLIENT_APP_ID:?Set ENTRA_CLIENT_APP_ID (create-entra-app.sh prints it; the client id the readiness gate uses for its own credential)}"

# Tracing suspended for this one guard, the same pattern stage 4 uses for the
# Search admin key: `${VAR:?msg}` traces as `+ : <value>` whenever the
# variable IS set -- the message is dead code on that path, so wording alone
# can never make this safe -- and an operator running with `bash -x` (or
# SHELLOPTS=xtrace inherited from a parent) would otherwise put the secret on
# stderr the moment this line runs. Restored immediately after, not assumed
# off.
XTRACE_RESTORE=false
case "$-" in
  *x*) XTRACE_RESTORE=true; set +x ;;
esac
: "${ENTRA_CLIENT_SECRET:?Set ENTRA_CLIENT_SECRET (the secret for that client application; never logged)}"
if [ "$XTRACE_RESTORE" = true ]; then
  set -x
fi

AZ_LOCATION="${AZ_LOCATION:-japaneast}"
AZ_ACA_ENV_NAME="${AZ_ACA_ENV_NAME:-acaenv-azgenai-lab}"
AZ_ACA_APP_NAME="${AZ_ACA_APP_NAME:-aca-azgenai-lab}"
AZ_MI_NAME="${AZ_MI_NAME:-mi-azgenai-lab}"
IMAGE_TAG="${IMAGE_TAG:-day-24}"

# Settings requires a delegated scope OR an application role in entra mode
# (core/config.py, _entra_fields_required_in_entra_mode) -- omit both and the
# container exits at import time with a validation error, which the readiness
# gate would then report as an unreachable app. Defaults match
# create-entra-app.sh's own defaults so the common path needs neither.
ENTRA_REQUIRED_SCOPE="${ENTRA_REQUIRED_SCOPE:-access_as_user}"
ENTRA_REQUIRED_APP_ROLE="${ENTRA_REQUIRED_APP_ROLE:-Api.Access}"
# Empty means "read it back from the account in stage 3" rather than "unset":
# the account's own reported endpoint beats a string assembled from a naming
# convention that Azure is free to change.
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}"
AZURE_OPENAI_DEPLOYMENT_NAME="${AZURE_OPENAI_DEPLOYMENT_NAME:-chat-mini}"
AZURE_OPENAI_EMBEDDING_DEPLOYMENT="${AZURE_OPENAI_EMBEDDING_DEPLOYMENT:-embed-small}"
AZURE_SEARCH_ENDPOINT="${AZURE_SEARCH_ENDPOINT:-https://${AZ_SEARCH_NAME}.search.windows.net}"

GATE_DEADLINE_SECONDS="${GATE_DEADLINE_SECONDS:-1200}"
ACA_POLL_ATTEMPTS="${ACA_POLL_ATTEMPTS:-60}"
ACA_POLL_INTERVAL="${ACA_POLL_INTERVAL:-10}"
ROLE_POLL_ATTEMPTS="${ROLE_POLL_ATTEMPTS:-6}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# --- helpers -----------------------------------------------------------------

# An `az ... -o tsv` call that exits nonzero is already caught by `set -e` on
# the assignment. What that does NOT catch is a call that exits 0 and prints
# nothing. An empty read is a failed read: it must abort, never be treated as
# "absent", "zero" or "not yet".
require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (az returned empty output); aborting." >&2
    exit 1
  fi
}

# Loop bounds are validated as integers before any loop runs. `for _ in $(seq
# 1 "$KNOB")` silently runs ZERO iterations when the knob is malformed, which
# turns a bounded wait into no wait at all and reports the resulting failure
# as if the deadline had been reached.
require_count() {
  local name="$1" value="$2"
  if ! [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "$name must be a positive integer; got '$value'." >&2
    exit 1
  fi
}
require_seconds() {
  local name="$1" value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$name must be a non-negative integer number of seconds; got '$value'." >&2
    exit 1
  fi
}
# core/config.py parses ENTRA_TENANT_ID and ENTRA_AUDIENCE with UUID(), so a
# non-GUID makes Settings() raise at import time and the container exits
# before it ever binds a port. The trap is not hypothetical: the portal
# displays the API application's Application ID URI as `api://<guid>`, and
# pasting that whole string into ENTRA_AUDIENCE is the obvious thing to do.
# Caught here, that is a one-line message; caught by the container, it is a
# boot loop discovered at stage 11, after all eleven stages of mutation.
require_guid() {
  local name="$1" value="$2"
  if ! [[ "$value" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "$name must be a bare GUID; got '$value'." >&2
    echo "The portal shows the Application ID URI as api://<guid> -- pass only the guid." >&2
    exit 1
  fi
}
require_count ACA_POLL_ATTEMPTS "$ACA_POLL_ATTEMPTS"
require_count ROLE_POLL_ATTEMPTS "$ROLE_POLL_ATTEMPTS"
require_seconds ACA_POLL_INTERVAL "$ACA_POLL_INTERVAL"
require_count GATE_DEADLINE_SECONDS "$GATE_DEADLINE_SECONDS"

APP_YAML=""
MUTATED=false
# Defaulted (not overwritten) here so the trap below can always reference it
# under `set -u`, even if a failure happens before stage 6 assigns its own
# value -- and without clobbering a value the operator already exported.
AZ_LAW_NAME="${AZ_LAW_NAME:-}"
on_exit() {
  local status=$?
  if [ -n "$APP_YAML" ]; then
    rm -f "$APP_YAML"
  fi
  if [ "$status" -ne 0 ] && [ "$MUTATED" = true ]; then
    echo "" >&2
    echo "deploy-container-app.sh failed (exit $status) after creating Azure resources." >&2
    echo "Tear down whatever exists with:" >&2
    # Every scope knob the teardown needs is printed, including AZ_ACR_NAME and
    # AZ_OPENAI_NAME: delete-container-app.sh SKIPS a role-assignment scope whose
    # knob is unset (warned, not silently), and its step 6 read-back is
    # fail-closed by assignee alone -- so a command missing either one aborts
    # before deleting the identity and leaves exactly the orphaned assignments
    # this teardown exists to prevent.
    echo "  AZ_SUBSCRIPTION_ID=$AZ_SUBSCRIPTION_ID AZ_RESOURCE_GROUP=$AZ_RESOURCE_GROUP \\" >&2
    echo "    AZ_ACA_APP_NAME=$AZ_ACA_APP_NAME AZ_ACA_ENV_NAME=$AZ_ACA_ENV_NAME \\" >&2
    echo "    AZ_MI_NAME=$AZ_MI_NAME AZ_KEYVAULT_NAME=$AZ_KEYVAULT_NAME \\" >&2
    echo "    AZ_ACR_NAME=$AZ_ACR_NAME AZ_OPENAI_NAME=$AZ_OPENAI_NAME \\" >&2
    echo "    AZ_LAW_NAME=$AZ_LAW_NAME ./delete-container-app.sh" >&2
  fi
}
trap on_exit EXIT

# === stage 1: preflight ======================================================
echo "== stage 1: preflight =="
# Everything in this stage is a check. Nothing below it creates, writes or
# bills anything -- that starts at stage 2 -- which is the whole reason the
# checks that would otherwise surface at stage 8 or stage 11 were pulled up
# to here.
require_guid ENTRA_TENANT_ID "$ENTRA_TENANT_ID"
require_guid ENTRA_AUDIENCE "$ENTRA_AUDIENCE"

# A check, not a gate. `az containerapp` lives in an extension; recent CLI
# versions install it on first use, so its absence here is not proof the
# deploy will fail -- refusing to continue would abort runs that dynamic
# install would have handled fine. What is NOT acceptable is finding out at
# stage 7, with the identity, its roles and the secret already created, from
# an error message that says nothing about extensions. So: say it, now, with
# the fix in it, and continue.
if ! az extension show --name containerapp >/dev/null 2>&1; then
  echo "  WARNING: the 'containerapp' az extension is not installed." >&2
  echo "  Recent CLI versions install it on first use, but in a non-interactive shell that can prompt or fail" >&2
  echo "  depending on extension.use_dynamic_install. Install it up front with: az extension add --name containerapp" >&2
fi

# Every command below also passes --subscription explicitly; this additionally
# repoints the DEFAULT az context, which `az acr build` and the directory-object
# commands read. It is a side effect on shared mutable state, so it is
# announced rather than done quietly.
az account set --subscription "$AZ_SUBSCRIPTION_ID"
echo "  default az context now points at $AZ_SUBSCRIPTION_ID"

# Microsoft.App and Microsoft.ManagedIdentity are the two providers this script
# creates resources under. Microsoft.OperationalInsights is here for the Log
# Analytics workspace this script creates explicitly in stage 6 -- without it
# that stage fails after the identity and its roles already exist.
for NAMESPACE in Microsoft.App Microsoft.ManagedIdentity Microsoft.OperationalInsights; do
  REG_STATE=$(az provider show --namespace "$NAMESPACE" \
    --subscription "$AZ_SUBSCRIPTION_ID" --query registrationState -o tsv)
  require_value "$REG_STATE" "the $NAMESPACE provider registration state"
  if [ "$REG_STATE" != "Registered" ]; then
    echo "  registering $NAMESPACE (one-time, may take a minute)"
    az provider register --namespace "$NAMESPACE" --subscription "$AZ_SUBSCRIPTION_ID" --wait
  fi
done

# This script only creates an app from scratch, and that refusal belongs
# HERE rather than at stage 8, where it used to live. Stage 6 creates a
# fresh, randomly-named Log Analytics workspace on every run; a rerun
# against an existing deployment would therefore create workspace B, leave
# the reused environment still wired to workspace A, abort at stage 8, and
# print a teardown command naming B -- so A survives, billing, under a
# random name the operator no longer has. Checked before stage 2, the whole
# sequence is a no-op, and the doomed run also skips stage 5's `az acr
# build`. It needs the containerapp extension and the provider registration
# above, which is why it is the last thing in this stage rather than the
# first.
APP_COUNT=$(az containerapp list \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --query "length([?name=='$AZ_ACA_APP_NAME'])" -o tsv)
require_value "$APP_COUNT" "the existing app count"
if [ "$APP_COUNT" != "0" ]; then
  echo "App '$AZ_ACA_APP_NAME' already exists. This script only creates from scratch:" >&2
  echo "run delete-container-app.sh first, or deploy under a different AZ_ACA_APP_NAME." >&2
  exit 1
fi
echo "  preflight passed; nothing has been created yet"

# === stage 2: user-assigned managed identity =================================
echo "== stage 2: user-assigned managed identity =="
MI_COUNT=$(az identity list \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --query "length([?name=='$AZ_MI_NAME'])" -o tsv)
require_value "$MI_COUNT" "the existing managed identity count"
if [ "$MI_COUNT" = "0" ]; then
  MUTATED=true
  echo "  creating '$AZ_MI_NAME' in $AZ_LOCATION"
  az identity create \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_MI_NAME" \
    --location "$AZ_LOCATION" >/dev/null
else
  echo "  '$AZ_MI_NAME' already exists — reusing it"
fi

# One property per call: an az array projection with -o tsv emits one value per
# LINE, which is exactly the parsing bug Day 19 shipped and fixed.
identity_field() {
  az identity show \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_MI_NAME" \
    --query "$1" -o tsv
}
MI_ID=$(identity_field id)
require_value "$MI_ID" "the managed identity resource id"
MI_PRINCIPAL_ID=$(identity_field principalId)
require_value "$MI_PRINCIPAL_ID" "the managed identity principal id"
MI_CLIENT_ID=$(identity_field clientId)
require_value "$MI_CLIENT_ID" "the managed identity client id"
echo "  identity ready (client id $MI_CLIENT_ID)"

# === stage 3: role assignments ===============================================
echo "== stage 3: role assignments =="
ACR_ID=$(az acr show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_ACR_NAME" --query id -o tsv)
require_value "$ACR_ID" "the container registry resource id"
KV_ID=$(az keyvault show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_KEYVAULT_NAME" --query id -o tsv)
require_value "$KV_ID" "the key vault resource id"
AOAI_ID=$(az cognitiveservices account show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_OPENAI_NAME" --query id -o tsv)
require_value "$AOAI_ID" "the Azure OpenAI account resource id"
if [ -z "$AZURE_OPENAI_ENDPOINT" ]; then
  AZURE_OPENAI_ENDPOINT=$(az cognitiveservices account show --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_OPENAI_NAME" \
    --query properties.endpoint -o tsv)
  require_value "$AZURE_OPENAI_ENDPOINT" "the Azure OpenAI account endpoint"
fi

# --assignee-object-id + --assignee-principal-type, not --assignee: the plain
# form makes the CLI resolve the principal through Microsoft Graph, and a
# just-created identity is not there yet.
#
# `create` is run and then VERIFIED rather than trusted. Its exit status alone
# cannot decide anything here: an identical existing assignment is reported as
# an error by some CLI versions and as success by others, and a directory
# replication lag right after stage 2 produces a transient PrincipalNotFound
# that a retry clears. So the read-back is the verdict, and it is bounded --
# an empty read-back aborts immediately (a failed read, not "not yet"), while
# a valid "0" is retried up to ROLE_POLL_ATTEMPTS times.
assign_role() {
  local role="$1" scope="$2" attempt count
  for ((attempt = 1; attempt <= ROLE_POLL_ATTEMPTS; attempt++)); do
    if ! az role assignment create \
        --subscription "$AZ_SUBSCRIPTION_ID" \
        --assignee-object-id "$MI_PRINCIPAL_ID" \
        --assignee-principal-type ServicePrincipal \
        --role "$role" \
        --scope "$scope" >/dev/null; then
      echo "  '$role' create reported an error on attempt $attempt; the read-back below decides." >&2
    fi
    # --assignee-object-id, not --assignee, on the read-back too: the plain
    # form resolves the principal through Microsoft Graph, so a deploy could
    # fail its verification step because the operator cannot query Graph --
    # nothing to do with whether the role is actually assigned. Same reason
    # --fill-principal-name is off: a display name nobody reads here is not
    # worth a second Graph call that can fail on its own.
    count=$(az role assignment list \
      --subscription "$AZ_SUBSCRIPTION_ID" \
      --assignee-object-id "$MI_PRINCIPAL_ID" \
      --fill-principal-name false \
      --role "$role" \
      --scope "$scope" \
      --query "length([])" -o tsv)
    require_value "$count" "the '$role' assignment read-back"
    if [ "$count" != "0" ]; then
      echo "  $role: assigned"
      return 0
    fi
    if ((attempt < ROLE_POLL_ATTEMPTS)); then
      sleep "$ACA_POLL_INTERVAL"
    fi
  done
  echo "Role '$role' is still not assigned to the identity on $scope after $ROLE_POLL_ATTEMPTS attempts." >&2
  echo "Without it the app cannot pull the image, read the secret, or call the model." >&2
  exit 1
}
MUTATED=true
assign_role "AcrPull" "$ACR_ID"
assign_role "Key Vault Secrets User" "$KV_ID"
assign_role "Cognitive Services OpenAI User" "$AOAI_ID"

# === stage 4: Search admin key -> Key Vault ==================================
echo "== stage 4: Search admin key -> Key Vault =="
# Shell tracing prints an assignment together with its substituted value, so
# `bash -x` (or SHELLOPTS=xtrace inherited from a parent) would put the admin
# key in stderr. Tracing is suspended across this block and restored after it,
# rather than assumed off.
XTRACE_RESTORE=false
case "$-" in
  *x*) XTRACE_RESTORE=true; set +x ;;
esac

SEARCH_KEY=$(az search admin-key show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --service-name "$AZ_SEARCH_NAME" \
  --query primaryKey -o tsv)
require_value "$SEARCH_KEY" "the Search admin key"

# --value puts the key in this process's argv, where `ps` can show it to
# another user on the same machine for the lifetime of the call. That is a
# real and deliberately accepted exposure for a single-operator ephemeral lab
# session; the alternative (--file) trades it for the key at rest on disk.
# What is NOT accepted anywhere is the key on stdout: only the secret's id is
# read back, never its value.
SECRET_ID=$(az keyvault secret set \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --vault-name "$AZ_KEYVAULT_NAME" \
  --name azure-search-admin-key \
  --value "$SEARCH_KEY" \
  --query id -o tsv)
unset SEARCH_KEY
require_value "$SECRET_ID" "the stored secret's id"

if [ "$XTRACE_RESTORE" = true ]; then
  set -x
fi

# Versionless on purpose: Container Apps refreshes a versionless Key Vault
# reference on its own schedule, so rotating the Search key does not require
# rewriting this app's definition (docs/key-vault-config.md).
KV_SECRET_URI="https://${AZ_KEYVAULT_NAME}.vault.azure.net/secrets/azure-search-admin-key"
echo "  stored, referenced versionless at $KV_SECRET_URI"

# === stage 5: image ==========================================================
echo "== stage 5: build the image in ACR =="
# Built by the registry, not locally: no local Docker daemon, no push
# credentials, and the platform is pinned because Container Apps runs
# linux/amd64 while this repo is developed on arm64 Macs.
#
# The build context is the repo root and .dockerignore prunes it; the
# Dockerfile is under docker/, so --file is not optional.
az acr build \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --registry "$AZ_ACR_NAME" \
  --platform linux/amd64 \
  --image "azgenai-lab:${IMAGE_TAG}" \
  --file docker/Dockerfile \
  "$REPO_ROOT"

# === stage 6: Log Analytics workspace ========================================
echo "== stage 6: Log Analytics workspace =="
# `az containerapp env create` auto-provisions a Log Analytics workspace when
# none is given -- convenient, but that workspace is a separate
# Microsoft.OperationalInsights/workspaces resource with its own lifecycle,
# and deleting the Container Apps environment does NOT delete it. An
# auto-provisioned workspace is therefore a silent recurring bill this
# teardown could never find, because it was never told the workspace's name.
# Creating it explicitly, under a name this script controls and prints, is
# what lets delete-container-app.sh delete it afterward.
#
# Per-run unique default name, the same discipline create-acr.sh uses: a
# fixed name risks colliding with -- and a concurrent run's teardown
# deleting -- another run's workspace (Day 21's lesson).
AZ_LAW_NAME="${AZ_LAW_NAME:-lawazgenai$(python3 -c "import secrets; print(secrets.token_hex(4))")}"
echo "  workspace name: $AZ_LAW_NAME"

LAW_COUNT=$(az monitor log-analytics workspace list \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --query "length([?name=='$AZ_LAW_NAME'])" -o tsv)
require_value "$LAW_COUNT" "the existing Log Analytics workspace count"
if [ "$LAW_COUNT" = "0" ]; then
  MUTATED=true
  az monitor log-analytics workspace create \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --workspace-name "$AZ_LAW_NAME" \
    --location "$AZ_LOCATION" >/dev/null
else
  echo "  '$AZ_LAW_NAME' already exists — reusing it"
fi

LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --workspace-name "$AZ_LAW_NAME" \
  --query customerId -o tsv)
require_value "$LAW_CUSTOMER_ID" "the Log Analytics workspace id"
echo "  workspace ready"

# === stage 7: Container Apps environment =====================================
echo "== stage 7: Container Apps environment =="
# CLI flags, not YAML: `az containerapp env create` has no --yaml. Only the app
# itself (stage 8) is defined declaratively.
ENV_COUNT=$(az containerapp env list \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --query "length([?name=='$AZ_ACA_ENV_NAME'])" -o tsv)
require_value "$ENV_COUNT" "the existing environment count"
if [ "$ENV_COUNT" = "0" ]; then
  echo "  creating environment '$AZ_ACA_ENV_NAME' in $AZ_LOCATION"
  # Shell tracing prints an assignment together with its substituted value, so
  # `bash -x` would put the workspace's shared key in stderr -- the same
  # reason stage 4 suspends tracing around the Search admin key. Restored
  # immediately after the one call that needs the key, not assumed off.
  XTRACE_RESTORE=false
  case "$-" in
    *x*) XTRACE_RESTORE=true; set +x ;;
  esac
  LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --workspace-name "$AZ_LAW_NAME" \
    --query primarySharedKey -o tsv)
  require_value "$LAW_SHARED_KEY" "the Log Analytics workspace shared key"
  az containerapp env create \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_ACA_ENV_NAME" \
    --location "$AZ_LOCATION" \
    --logs-destination log-analytics \
    --logs-workspace-id "$LAW_CUSTOMER_ID" \
    --logs-workspace-key "$LAW_SHARED_KEY" >/dev/null
  unset LAW_SHARED_KEY
  if [ "$XTRACE_RESTORE" = true ]; then
    set -x
  fi
else
  echo "  environment '$AZ_ACA_ENV_NAME' already exists — reusing it"
fi

ENV_STATE=""
for ((ATTEMPT = 1; ATTEMPT <= ACA_POLL_ATTEMPTS; ATTEMPT++)); do
  ENV_STATE=$(az containerapp env show \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_ACA_ENV_NAME" \
    --query properties.provisioningState -o tsv)
  require_value "$ENV_STATE" "the environment provisioning state"
  if [ "$ENV_STATE" = "Succeeded" ]; then
    break
  fi
  # Terminal states are terminal: polling a Failed or Canceled environment
  # for the rest of the budget cannot change the answer, it just spends ten
  # minutes of a paid live session restating what Azure already called
  # final. Same early break stage 9 uses on the app.
  if [ "$ENV_STATE" = "Failed" ] || [ "$ENV_STATE" = "Canceled" ]; then
    break
  fi
  if ((ATTEMPT < ACA_POLL_ATTEMPTS)); then
    sleep "$ACA_POLL_INTERVAL"
  fi
done
if [ "$ENV_STATE" != "Succeeded" ]; then
  echo "Environment '$AZ_ACA_ENV_NAME' is $ENV_STATE after $ACA_POLL_ATTEMPTS attempts." >&2
  exit 1
fi

# Read the environment's resource id for the YAML below. `az containerapp
# create --help` is explicit that with --yaml "all other parameters will be
# ignored", so --environment on that call cannot be relied on to bind the
# app to this environment -- the YAML has to say so itself, via
# properties.environmentId. This read is redundant if the flag happens to
# win, and it saves the session if it does not: without it, a --yaml that
# names no environment fails at stage 8, i.e. after the identity, its three
# roles, the secret, the image, the workspace and the environment all exist.
ENV_ID=$(az containerapp env show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACA_ENV_NAME" \
  --query id -o tsv)
require_value "$ENV_ID" "the environment resource id"
echo "  environment provisioned"

# === stage 8: the app, from one YAML =========================================
echo "== stage 8: create the app =="
# The "app already exists" refusal is stage 1's, deliberately: by the time
# this stage runs, a rerun would already have created a second Log Analytics
# workspace that nothing would ever tear down.

APP_YAML="$(mktemp)"
# The whole app definition in one file, so what is deployed can be read in one
# place. Note the top-level `identity:` block: it is what ATTACHES the
# user-assigned identity. The `identity:` fields under registries[] and
# secrets[] are references to an already-attached identity, so without the top
# block the image pull and the Key Vault reference both fail with an identity
# that "does not exist" on an app that names it twice.
cat >"$APP_YAML" <<YAML
identity:
  type: UserAssigned
  userAssignedIdentities:
    "${MI_ID}": {}
properties:
  # The binding to the environment, stated in the file rather than left to
  # the --environment flag on the create call below: `--yaml` documents that
  # "all other parameters will be ignored".
  environmentId: ${ENV_ID}
  configuration:
    activeRevisionsMode: single
    ingress:
      external: true
      targetPort: 8000
      transport: auto
    registries:
      - server: ${AZ_ACR_NAME}.azurecr.io
        identity: ${MI_ID}
    secrets:
      - name: search-admin-key
        keyVaultUrl: ${KV_SECRET_URI}
        identity: ${MI_ID}
  template:
    terminationGracePeriodSeconds: 30
    containers:
      - image: ${AZ_ACR_NAME}.azurecr.io/azgenai-lab:${IMAGE_TAG}
        name: azgenai-lab
        env:
          - name: AZURE_OPENAI_AUTH
            value: "entra"
          - name: AZURE_CLIENT_ID
            value: "${MI_CLIENT_ID}"
          - name: AUTH_MODE
            value: "entra"
          - name: ENTRA_TENANT_ID
            value: "${ENTRA_TENANT_ID}"
          - name: ENTRA_AUDIENCE
            value: "${ENTRA_AUDIENCE}"
          - name: ENTRA_REQUIRED_SCOPE
            value: "${ENTRA_REQUIRED_SCOPE}"
          - name: ENTRA_REQUIRED_APP_ROLE
            value: "${ENTRA_REQUIRED_APP_ROLE}"
          - name: USE_FAKE_LLM
            value: "false"
          - name: USE_FAKE_SEARCH
            value: "false"
          - name: USE_FAKE_EMBEDDINGS
            value: "false"
          - name: SAMPLE_DOCS_DIR
            value: "/app/data/sample-docs"
          - name: AZURE_OPENAI_ENDPOINT
            value: "${AZURE_OPENAI_ENDPOINT}"
          - name: AZURE_OPENAI_DEPLOYMENT_NAME
            value: "${AZURE_OPENAI_DEPLOYMENT_NAME}"
          - name: AZURE_OPENAI_EMBEDDING_DEPLOYMENT
            value: "${AZURE_OPENAI_EMBEDDING_DEPLOYMENT}"
          - name: AZURE_SEARCH_ENDPOINT
            value: "${AZURE_SEARCH_ENDPOINT}"
          - name: AZURE_SEARCH_ADMIN_KEY
            secretRef: search-admin-key
        probes:
          - type: Startup
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 2
            periodSeconds: 3
          - type: Liveness
            httpGet: { path: /health, port: 8000 }
            periodSeconds: 10
          - type: Readiness
            httpGet: { path: /health, port: 8000 }
            periodSeconds: 10
    scale:
      minReplicas: 1
      maxReplicas: 1
YAML

# --environment is kept alongside --yaml even though the CLI documents that
# it ignores it: harmless if ignored, correct if honoured, and the binding
# does not depend on which of the two is true -- properties.environmentId in
# the file above is what actually decides.
az containerapp create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACA_APP_NAME" \
  --environment "$AZ_ACA_ENV_NAME" \
  --yaml "$APP_YAML" >/dev/null

# === stage 9: provisioning read-back =========================================
echo "== stage 9: wait for provisioning =="
APP_STATE=""
for ((ATTEMPT = 1; ATTEMPT <= ACA_POLL_ATTEMPTS; ATTEMPT++)); do
  APP_STATE=$(az containerapp show \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_ACA_APP_NAME" \
    --query properties.provisioningState -o tsv)
  require_value "$APP_STATE" "the app provisioning state"
  if [ "$APP_STATE" = "Succeeded" ]; then
    break
  fi
  if [ "$APP_STATE" = "Failed" ] || [ "$APP_STATE" = "Canceled" ]; then
    break
  fi
  if ((ATTEMPT < ACA_POLL_ATTEMPTS)); then
    sleep "$ACA_POLL_INTERVAL"
  fi
done
if [ "$APP_STATE" != "Succeeded" ]; then
  echo "App '$AZ_ACA_APP_NAME' provisioningState is $APP_STATE after $ACA_POLL_ATTEMPTS attempts." >&2
  exit 1
fi

FQDN=$(az containerapp show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACA_APP_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv)
require_value "$FQDN" "the app ingress FQDN"
BASE_URL="https://${FQDN}"
echo "  provisioned at $BASE_URL"

# === stage 10: gate 1, control plane ==========================================
echo "== stage 10: gate 1 (control plane) =="
# No new calls: this stage is the verdict on the read-backs above, stated
# rather than implied. Azure agrees the identity, its three roles, the secret,
# the image, the workspace, the environment and the app all exist. None of
# that is evidence that the container serves a request -- that is stage 11's
# job, and the distinction is the point of having two gates.
echo "  identity, three role assignments, secret, image, workspace, environment and app all read back"

# === stage 11: gate 2, data plane ============================================
echo "== stage 11: gate 2 (data plane) =="
HEALTH_OK=false
HTTP_CODE=""
for ((ATTEMPT = 1; ATTEMPT <= ACA_POLL_ATTEMPTS; ATTEMPT++)); do
  # The status is compared explicitly rather than left to curl's -f: without
  # -L a 3xx is a zero exit, and a redirect away from /health is not health.
  if HTTP_CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "${BASE_URL}/health"); then
    if [ "$HTTP_CODE" = "200" ]; then
      HEALTH_OK=true
      break
    fi
  fi
  if ((ATTEMPT < ACA_POLL_ATTEMPTS)); then
    sleep "$ACA_POLL_INTERVAL"
  fi
done
if [ "$HEALTH_OK" != true ]; then
  echo "/health never returned 200 after $ACA_POLL_ATTEMPTS attempts (last status: ${HTTP_CODE:-none})." >&2
  echo "Inspect the container's own account of why with:" >&2
  echo "  az containerapp logs show --name $AZ_ACA_APP_NAME --resource-group $AZ_RESOURCE_GROUP --tail 100" >&2
  exit 1
fi
echo "  /health returned 200"

# /health proves the process is up; it proves nothing about Entra verification,
# the managed identity's model access, or the Search index -- all of which are
# exactly what changed in this deployment. The gate exercises the real endpoints
# with a real token, and its non-zero exit fails this script.
#
# The gate needs its own credential to call an app that runs with AUTH_MODE=entra:
# a request with no bearer token at all is a 401 this API decides at parse time,
# never a symptom of role-assignment propagation, so it would never turn into a
# 200 no matter how long the gate waited. --tenant-id/--client-id/--api-app-id
# feed the same client-credentials acquisition tools/entra_smoke.py's --phase
# no-role/full already use; ENTRA_CLIENT_SECRET is read from this process's own
# environment by the tool itself (never a flag, never traced, never logged).
echo "  running the authenticated readiness gate (deadline ${GATE_DEADLINE_SECONDS}s)"
(cd "$REPO_ROOT" && uv run python tools/entra_smoke.py \
  --gate \
  --base-url "$BASE_URL" \
  --deadline-seconds "$GATE_DEADLINE_SECONDS" \
  --tenant-id "$ENTRA_TENANT_ID" \
  --client-id "$ENTRA_CLIENT_APP_ID" \
  --api-app-id "$ENTRA_AUDIENCE")

cat <<SUMMARY

Deployed and gated.
  app:           $AZ_ACA_APP_NAME
  url:           $BASE_URL
  identity:      $AZ_MI_NAME (client id $MI_CLIENT_ID)
  image:         ${AZ_ACR_NAME}.azurecr.io/azgenai-lab:${IMAGE_TAG}
  log workspace: $AZ_LAW_NAME

This deployment is ephemeral. Tear it down in the same session:
  AZ_SUBSCRIPTION_ID=$AZ_SUBSCRIPTION_ID AZ_RESOURCE_GROUP=$AZ_RESOURCE_GROUP \\
    AZ_ACA_APP_NAME=$AZ_ACA_APP_NAME AZ_ACA_ENV_NAME=$AZ_ACA_ENV_NAME \\
    AZ_MI_NAME=$AZ_MI_NAME AZ_KEYVAULT_NAME=$AZ_KEYVAULT_NAME \\
    AZ_ACR_NAME=$AZ_ACR_NAME AZ_OPENAI_NAME=$AZ_OPENAI_NAME \\
    AZ_LAW_NAME=$AZ_LAW_NAME ./delete-container-app.sh
SUMMARY
