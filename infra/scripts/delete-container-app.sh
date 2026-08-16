#!/usr/bin/env bash
# Tear down what deploy-container-app.sh creates, in an order that undoes the
# orphan-assignment trap the Day 24 design review found: deleting a managed
# identity does NOT clean up its role assignments. Azure leaves them behind,
# each one displaying "Identity not found" in the portal, with nothing in the
# UI pointing back at the ACR/Key Vault/Azure OpenAI resources still carrying
# them. So this script deletes the three role assignments FIRST, reads them
# back to prove they are gone, and only then deletes the identity that named
# them. Deleting the identity before that read-back succeeds would turn a
# recoverable state (an identity that still exists to be re-queried) into a
# permanent one (orphaned assignments with no principal left to look up).
#
# Seven steps, in order:
#   1. delete the Container App, if it exists -- read back to confirm gone
#   2. delete the Container Apps environment, if it exists -- read back to
#      confirm gone
#   3. delete the Log Analytics workspace, if a name was given -- Azure does
#      NOT delete this on its own when the environment above is deleted (a
#      separate Microsoft.OperationalInsights/workspaces resource, its own
#      lifecycle); a missing name knob is skipped with a warning, the same
#      not-silent discipline step 5 uses for the ACR/Key Vault/AOAI scopes
#   4. read back the managed identity's principal id -- absent identity means
#      nothing identity-related is left to do, so this exits 0 here
#   5. delete the role assignment at each scope this identity was granted
#      (ACR, Key Vault, Azure OpenAI); a scope whose name knob is unset is
#      skipped with a warning, never silently
#   6. read back ALL role assignments still held by that principal id --
#      fail-closed: a non-empty result, or a read that fails outright, aborts
#      BEFORE step 7
#   7. delete the managed identity -- read back to confirm gone
#
# The identity ordering (now steps 4-7) is unchanged from the original
# six-step contract -- step 3 is an independent addition ahead of it, not a
# reordering of it.
#
# What this does NOT delete: the resources deploy-container-app.sh only
# reads and mutates in place -- the ACR, Key Vault (and the Search-key secret
# inside it), Azure OpenAI account, Search service, and the Entra app
# registration all outlive this script. Their own create/delete scripts own
# them (delete-acr.sh, delete-keyvault.sh, delete-search.sh, delete-openai.sh,
# delete-entra-app.sh); nothing here touches them beyond reading their
# resource ids to build a role-assignment scope.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP  - resource group holding the app, environment and identity
# Optional env vars:
#   AZ_ACA_APP_NAME  - defaults to aca-azgenai-lab (deploy-container-app.sh's default)
#   AZ_ACA_ENV_NAME  - defaults to acaenv-azgenai-lab (ditto)
#   AZ_MI_NAME       - defaults to mi-azgenai-lab (ditto)
#   AZ_LAW_NAME      - Log Analytics workspace name deploy-container-app.sh printed;
#                      unset skips the workspace delete (warned, not silent) --
#                      there is no sensible default, since deploy generates a
#                      fresh per-run name each time
#   AZ_ACR_NAME      - registry the identity was granted AcrPull on; unset skips
#                      that scope's role-assignment delete (warned, not silent)
#   AZ_KEYVAULT_NAME - vault the identity was granted Key Vault Secrets User on;
#                      unset skips that scope the same way
#   AZ_OPENAI_NAME   - Azure OpenAI account the identity was granted Cognitive
#                      Services OpenAI User on; unset skips that scope the same way
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
AZ_ACA_APP_NAME="${AZ_ACA_APP_NAME:-aca-azgenai-lab}"
AZ_ACA_ENV_NAME="${AZ_ACA_ENV_NAME:-acaenv-azgenai-lab}"
AZ_MI_NAME="${AZ_MI_NAME:-mi-azgenai-lab}"

# An `az ... -o tsv` call that exits nonzero is already caught by `set -e` on
# the assignment. What that does NOT catch is a call that exits 0 and prints
# nothing. An empty read is a failed read: it must abort, never be treated as
# "absent", "zero" or "not yet" -- the same discipline create-acr.sh and
# delete-acr.sh use.
require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (az returned empty output); aborting." >&2
    exit 1
  fi
}

# === step 1: delete the app ==================================================
echo "== step 1: delete the app =="
app_count() {
  az containerapp list \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --query "length([?name=='$AZ_ACA_APP_NAME'])" -o tsv
}
APP_COUNT=$(app_count)
require_value "$APP_COUNT" "the existing app count"
if [ "$APP_COUNT" = "0" ]; then
  echo "  '$AZ_ACA_APP_NAME' does not exist -- nothing to do"
else
  az containerapp delete \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_ACA_APP_NAME" \
    --yes >/dev/null
  # Never trust the delete call's exit code alone -- the same fail-closed
  # confirm-gone discipline delete-acr.sh uses.
  AFTER_APP_COUNT=$(app_count)
  require_value "$AFTER_APP_COUNT" "post-delete app count"
  if [ "$AFTER_APP_COUNT" != "0" ]; then
    echo "App '$AZ_ACA_APP_NAME' still listed after delete." >&2
    exit 1
  fi
  echo "  deleted '$AZ_ACA_APP_NAME'"
fi

# === step 2: delete the environment ==========================================
echo "== step 2: delete the environment =="
env_count() {
  az containerapp env list \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --query "length([?name=='$AZ_ACA_ENV_NAME'])" -o tsv
}
ENV_COUNT=$(env_count)
require_value "$ENV_COUNT" "the existing environment count"
if [ "$ENV_COUNT" = "0" ]; then
  echo "  '$AZ_ACA_ENV_NAME' does not exist -- nothing to do"
else
  az containerapp env delete \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_ACA_ENV_NAME" \
    --yes >/dev/null
  AFTER_ENV_COUNT=$(env_count)
  require_value "$AFTER_ENV_COUNT" "post-delete environment count"
  if [ "$AFTER_ENV_COUNT" != "0" ]; then
    echo "Environment '$AZ_ACA_ENV_NAME' still listed after delete." >&2
    exit 1
  fi
  echo "  deleted '$AZ_ACA_ENV_NAME'"
fi

# === step 3: delete the Log Analytics workspace ==============================
echo "== step 3: delete the Log Analytics workspace =="
# deploy-container-app.sh creates this workspace explicitly (rather than
# letting `az containerapp env create` auto-provision one) precisely so this
# step can find it by name -- an auto-provisioned workspace has no name this
# script would ever know to look for, and Azure does not delete it when the
# environment above is deleted.
if [ -n "${AZ_LAW_NAME:-}" ]; then
  law_count() {
    az monitor log-analytics workspace list \
      --subscription "$AZ_SUBSCRIPTION_ID" \
      --resource-group "$AZ_RESOURCE_GROUP" \
      --query "length([?name=='$AZ_LAW_NAME'])" -o tsv
  }
  LAW_COUNT=$(law_count)
  require_value "$LAW_COUNT" "the existing Log Analytics workspace count"
  if [ "$LAW_COUNT" = "0" ]; then
    echo "  '$AZ_LAW_NAME' does not exist -- nothing to do"
  else
    # --force releases the name immediately instead of leaving it reserved
    # for 14 days under Azure's default recoverable delete: this script only
    # ever names a fresh per-run workspace, so there is nothing to recover
    # and nothing gained by leaving a reservation behind.
    az monitor log-analytics workspace delete \
      --subscription "$AZ_SUBSCRIPTION_ID" \
      --resource-group "$AZ_RESOURCE_GROUP" \
      --workspace-name "$AZ_LAW_NAME" \
      --force --yes >/dev/null
    AFTER_LAW_COUNT=$(law_count)
    require_value "$AFTER_LAW_COUNT" "post-delete Log Analytics workspace count"
    if [ "$AFTER_LAW_COUNT" != "0" ]; then
      echo "Log Analytics workspace '$AZ_LAW_NAME' still listed after delete." >&2
      exit 1
    fi
    echo "  deleted '$AZ_LAW_NAME'"
  fi
else
  echo "  WARNING: AZ_LAW_NAME not set -- skipping the Log Analytics workspace delete" >&2
  echo "  (deploy-container-app.sh prints the generated name; an orphaned workspace bills for ingestion and retention until removed by hand)" >&2
fi

# === step 4: read back the identity's principal id ===========================
echo "== step 4: read back the identity's principal id =="
MI_COUNT=$(az identity list \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --query "length([?name=='$AZ_MI_NAME'])" -o tsv)
require_value "$MI_COUNT" "the existing managed identity count"
if [ "$MI_COUNT" = "0" ]; then
  echo "  '$AZ_MI_NAME' does not exist -- nothing identity-related left to do"
  exit 0
fi

# Read BEFORE anything identity-related is deleted: once the identity is
# gone, so is the only handle Azure gives us for finding its role
# assignments. This is the read the ordering exists to protect.
MI_PRINCIPAL_ID=$(az identity show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_MI_NAME" \
  --query principalId -o tsv)
require_value "$MI_PRINCIPAL_ID" "the managed identity principal id"

# === step 5: delete role assignments ==========================================
echo "== step 5: delete role assignments =="
# Scopes are read from the same name knobs deploy-container-app.sh grants
# roles against. --assignee-object-id, not --assignee: the plain form
# resolves the principal through Microsoft Graph, which the operator running
# this script may have no permission or network access to query -- the same
# reason the deploy script's role_assign helper avoids it.
delete_assignment_at() {
  local label="$1" scope_id="$2"
  az role assignment delete \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --assignee-object-id "$MI_PRINCIPAL_ID" \
    --scope "$scope_id" >/dev/null
  echo "  deleted the $label role assignment"
}

# A missing scope knob is skipped, not treated as an error: this script may
# be run against a partial or hand-built deployment. It is never SILENT
# about it though -- the warning goes to stderr, and step 6's read-back is
# by assignee alone (no --scope) precisely so a skipped scope's leftover
# assignment does not slip past this teardown unnoticed.
SKIPPED_SCOPES=""

if [ -n "${AZ_ACR_NAME:-}" ]; then
  ACR_ID=$(az acr show --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_ACR_NAME" --query id -o tsv)
  require_value "$ACR_ID" "the container registry resource id"
  delete_assignment_at "ACR ($AZ_ACR_NAME)" "$ACR_ID"
else
  echo "  WARNING: AZ_ACR_NAME not set -- skipping the ACR role-assignment delete" >&2
  SKIPPED_SCOPES="$SKIPPED_SCOPES ACR"
fi

if [ -n "${AZ_KEYVAULT_NAME:-}" ]; then
  KV_ID=$(az keyvault show --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_KEYVAULT_NAME" --query id -o tsv)
  require_value "$KV_ID" "the key vault resource id"
  delete_assignment_at "Key Vault ($AZ_KEYVAULT_NAME)" "$KV_ID"
else
  echo "  WARNING: AZ_KEYVAULT_NAME not set -- skipping the Key Vault role-assignment delete" >&2
  SKIPPED_SCOPES="$SKIPPED_SCOPES KeyVault"
fi

if [ -n "${AZ_OPENAI_NAME:-}" ]; then
  AOAI_ID=$(az cognitiveservices account show --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_OPENAI_NAME" --query id -o tsv)
  require_value "$AOAI_ID" "the Azure OpenAI account resource id"
  delete_assignment_at "Azure OpenAI ($AZ_OPENAI_NAME)" "$AOAI_ID"
else
  echo "  WARNING: AZ_OPENAI_NAME not set -- skipping the Azure OpenAI role-assignment delete" >&2
  SKIPPED_SCOPES="$SKIPPED_SCOPES AzureOpenAI"
fi

if [ -n "$SKIPPED_SCOPES" ]; then
  echo "  skipped scopes (name knob not set):$SKIPPED_SCOPES"
fi

# === step 6: read back -- the identity must hold zero role assignments ======
echo "== step 6: read back -- confirm zero role assignments remain =="
# Fail-closed, and deliberately by assignee alone (no --scope, no --role):
# this is the check that catches a scope this run skipped in step 5, or one
# a future deploy grants that this script does not yet know about. An empty
# read here is a failed query, not "zero found" -- `length([])` always
# prints a number when the call actually worked.
REMAINING=$(az role assignment list \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --assignee-object-id "$MI_PRINCIPAL_ID" \
  --fill-principal-name false \
  --query "length([])" -o tsv)
require_value "$REMAINING" "the role-assignment read-back"
if [ "$REMAINING" != "0" ]; then
  echo "$REMAINING role assignment(s) still held by '$AZ_MI_NAME' after step 5; not deleting the identity." >&2
  echo "Deleting it now would orphan them permanently -- the principal id would be gone." >&2
  exit 1
fi
echo "  confirmed: zero role assignments remain"

# === step 7: delete the identity =============================================
echo "== step 7: delete the identity =="
az identity delete \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_MI_NAME" >/dev/null
AFTER_MI_COUNT=$(az identity list \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --query "length([?name=='$AZ_MI_NAME'])" -o tsv)
require_value "$AFTER_MI_COUNT" "post-delete managed identity count"
if [ "$AFTER_MI_COUNT" != "0" ]; then
  echo "Managed identity '$AZ_MI_NAME' still listed after delete." >&2
  exit 1
fi
echo "  deleted '$AZ_MI_NAME'"

echo
echo "Torn down: app, environment, Log Analytics workspace, role assignments and identity all gone."
