#!/usr/bin/env bash
# Create an ephemeral Application Insights component for the Day 27 tracing
# session, and put its connection string where the app can reach it.
#
# Two things this script is deliberate about.
#
# 1. The Log Analytics workspace is never created implicitly. A workspace-based
#    component needs one, and `az monitor app-insights component create` will
#    happily conjure a default if none is named -- a separate
#    Microsoft.OperationalInsights/workspaces resource with its own lifecycle
#    and its own monthly bill, under a name this session never learned and so
#    could never tear down. Day 24 shipped exactly that mistake once already.
#    Either name an existing workspace (AZ_LAW_NAME) or let this script create
#    one under a per-run unique name and record that it owns it.
#
# 2. Ownership of the workspace is recorded, not inferred. deploy-container-app.sh
#    creates a workspace of its own for the Container Apps environment and
#    delete-container-app.sh deletes it by name. If this script also believed it
#    owned that workspace, whichever teardown ran second would abort fail-closed
#    on a resource that was already gone -- and Day 24 recorded what an aborted
#    teardown leaves behind. The record file carries law_owned=true|false and
#    delete-app-insights.sh touches the workspace only when it reads true.
#
# The connection string contains an instrumentation key. It is written straight
# to Key Vault and never to the record file: that file gets `cat`ed into
# terminals, can be caught by `set -x`, and is one .gitignore mistake from a
# commit. Shell tracing is suspended across the block that handles the value.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID    - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP     - existing resource group
#   AZ_APPINSIGHTS_NAME   - component name, unique within the resource group
#   AZ_KEYVAULT_NAME      - existing vault (create-keyvault.sh) to hold the
#                           connection string; the caller needs "Key Vault
#                           Secrets Officer" on it
# Optional env vars:
#   AZ_LOCATION           - defaults to japaneast
#   AZ_LAW_NAME           - existing Log Analytics workspace to reuse. Unset
#                           means this script creates one (lawappi + 8 hex
#                           chars) and takes ownership of deleting it.
#   AZ_RECORD_FILE        - where to write the teardown record; defaults to
#                           ./.app-insights-record.env
#
# Teardown: delete-app-insights.sh, reading the same record file.
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?set AZ_RESOURCE_GROUP}"
: "${AZ_APPINSIGHTS_NAME:?set AZ_APPINSIGHTS_NAME}"
: "${AZ_KEYVAULT_NAME:?set AZ_KEYVAULT_NAME}"
AZ_LOCATION="${AZ_LOCATION:-japaneast}"
AZ_RECORD_FILE="${AZ_RECORD_FILE:-./.app-insights-record.env}"
SECRET_NAME="applicationinsights-connection-string"

require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (az returned empty output); aborting." >&2
    exit 1
  fi
}

# --- provider registration before any query against these namespaces --------
# Same failure mode create-keyvault.sh documents: on a subscription that has
# never used them, every list/show against the namespace fails with
# MissingSubscriptionRegistration, so the state queries below would die before
# a check placed after them could run.
for NAMESPACE in Microsoft.OperationalInsights Microsoft.Insights; do
  REG_STATE=$(az provider show --subscription "$AZ_SUBSCRIPTION_ID" \
    --namespace "$NAMESPACE" --query registrationState -o tsv)
  require_value "$REG_STATE" "the $NAMESPACE provider registration state"
  if [ "$REG_STATE" != "Registered" ]; then
    echo "== registering $NAMESPACE (first run on this subscription) =="
    az provider register --subscription "$AZ_SUBSCRIPTION_ID" \
      --namespace "$NAMESPACE" --wait
  fi
done

# === step 1: Log Analytics workspace =========================================
echo "== step 1: Log Analytics workspace =="
if [ -n "${AZ_LAW_NAME:-}" ]; then
  LAW_OWNED=false
  echo "  reusing workspace: $AZ_LAW_NAME (this script will not delete it)"
  LAW_COUNT=$(az monitor log-analytics workspace list \
    --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$AZ_RESOURCE_GROUP" \
    --query "length([?name=='$AZ_LAW_NAME'])" -o tsv)
  require_value "$LAW_COUNT" "the existing Log Analytics workspace count"
  if [ "$LAW_COUNT" = "0" ]; then
    echo "AZ_LAW_NAME=$AZ_LAW_NAME does not exist in $AZ_RESOURCE_GROUP." >&2
    echo "Create it first, or unset AZ_LAW_NAME to have this script make one." >&2
    exit 1
  fi
else
  LAW_OWNED=true
  # Per-run unique, for the reason Day 21 learned the hard way: a teardown that
  # identifies resources by a shared fixed name can delete a concurrent run's.
  AZ_LAW_NAME="lawappi$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
  echo "  creating workspace: $AZ_LAW_NAME"
  az monitor log-analytics workspace create \
    --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$AZ_RESOURCE_GROUP" \
    --workspace-name "$AZ_LAW_NAME" --location "$AZ_LOCATION" -o none
fi

LAW_ID=$(az monitor log-analytics workspace show \
  --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$AZ_RESOURCE_GROUP" \
  --workspace-name "$AZ_LAW_NAME" --query id -o tsv)
require_value "$LAW_ID" "the Log Analytics workspace id"

# === step 2: Application Insights component ==================================
echo "== step 2: Application Insights component =="
az monitor app-insights component create \
  --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$AZ_RESOURCE_GROUP" \
  --app "$AZ_APPINSIGHTS_NAME" --location "$AZ_LOCATION" \
  --application-type web --workspace "$LAW_ID" -o none

# Read back rather than trust the create: the workspace binding is the whole
# point of naming one, and a component that silently landed on a different
# workspace would send this session's telemetry somewhere teardown never looks.
BOUND_WORKSPACE=$(az monitor app-insights component show \
  --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$AZ_RESOURCE_GROUP" \
  --app "$AZ_APPINSIGHTS_NAME" --query workspaceResourceId -o tsv)
require_value "$BOUND_WORKSPACE" "the component's bound workspace id"
if [ "$BOUND_WORKSPACE" != "$LAW_ID" ]; then
  echo "Component is bound to $BOUND_WORKSPACE, expected $LAW_ID; aborting." >&2
  exit 1
fi

# === step 3: connection string -> Key Vault ==================================
echo "== step 3: connection string -> Key Vault =="
# Tracing prints an assignment with its substituted value, so `bash -x` (or an
# inherited SHELLOPTS=xtrace) would put the instrumentation key in stderr.
# Suspended across this block and restored after, rather than assumed off --
# the same treatment deploy-container-app.sh gives the Search admin key.
XTRACE_RESTORE=false
case "$-" in
  *x*) XTRACE_RESTORE=true; set +x ;;
esac

CONNECTION_STRING=$(az monitor app-insights component show \
  --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$AZ_RESOURCE_GROUP" \
  --app "$AZ_APPINSIGHTS_NAME" --query connectionString -o tsv)
require_value "$CONNECTION_STRING" "the Application Insights connection string"

# --value puts the value in this process's argv, visible to `ps` for the
# lifetime of the call. Accepted for a single-operator ephemeral session on the
# same terms deploy-container-app.sh accepts it for the Search key; what is not
# accepted is the value on stdout, so only the secret's id is read back.
SECRET_URI=$(az keyvault secret set \
  --subscription "$AZ_SUBSCRIPTION_ID" --vault-name "$AZ_KEYVAULT_NAME" \
  --name "$SECRET_NAME" --value "$CONNECTION_STRING" \
  --query id -o tsv)
unset CONNECTION_STRING
require_value "$SECRET_URI" "the stored secret's id"

if [ "$XTRACE_RESTORE" = true ]; then set -x; fi

# === step 4: record file =====================================================
# Names and one flag. No secret value: this file is read in terminals and one
# .gitignore mistake from a commit.
cat >"$AZ_RECORD_FILE" <<RECORD
AZ_SUBSCRIPTION_ID=$AZ_SUBSCRIPTION_ID
AZ_RESOURCE_GROUP=$AZ_RESOURCE_GROUP
AZ_APPINSIGHTS_NAME=$AZ_APPINSIGHTS_NAME
AZ_KEYVAULT_NAME=$AZ_KEYVAULT_NAME
AZ_LAW_NAME=$AZ_LAW_NAME
law_owned=$LAW_OWNED
RECORD

echo
echo "Application Insights ready."
echo "  component:      $AZ_APPINSIGHTS_NAME"
echo "  workspace:      $AZ_LAW_NAME (owned by this script: $LAW_OWNED)"
echo "  secret:         $SECRET_NAME in $AZ_KEYVAULT_NAME"
echo "  record file:    $AZ_RECORD_FILE"
echo
echo "Deploy with the telemetry secret wired in:"
echo "  AZ_APPINSIGHTS_SECRET_URI=$SECRET_URI ./infra/scripts/deploy-container-app.sh"
echo
echo "Tear down with: AZ_RECORD_FILE=$AZ_RECORD_FILE ./infra/scripts/delete-app-insights.sh"
