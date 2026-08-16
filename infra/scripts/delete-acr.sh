#!/usr/bin/env bash
# Delete the ephemeral Azure Container Registry, if it exists.
#
# Unlike Key Vault, ACR deletion is synchronous and has no soft-delete/purge
# step: once `az acr delete` returns, the registry and every image in it are
# gone. This script still reads state back afterward to confirm rather than
# trusting the delete call's exit code alone — the same fail-closed
# discipline as create-acr.sh: an `-o tsv` read that returns empty output
# must abort, never be read as success.
#
# States handled:
#   present -> delete --yes, then read back to confirm gone
#   absent  -> succeed, reporting there was nothing to do
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default az context)
#   AZ_ACR_NAME         - registry name
# Optional env vars:
#   AZ_RESOURCE_GROUP   - defaults to rg-azgenai-lab, same default as
#                         create-acr.sh — only used when the registry exists
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID (default az context may point at the wrong subscription)}"
: "${AZ_ACR_NAME:?Set AZ_ACR_NAME}"
AZ_RESOURCE_GROUP="${AZ_RESOURCE_GROUP:-rg-azgenai-lab}"

require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (az returned empty output); state unknown, this step mutated nothing. Re-run to retry." >&2
    exit 1
  fi
}

registry_count() {
  az acr list --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_ACR_NAME'])" -o tsv
}

# --- state: present? ---------------------------------------------------------
COUNT=$(registry_count)
require_value "$COUNT" "existing registry count"
if [ "$COUNT" = "0" ]; then
  echo "Nothing to do: '$AZ_ACR_NAME' does not exist in this subscription."
  exit 0
fi

echo "Deleting container registry '$AZ_ACR_NAME'"
az acr delete \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACR_NAME" \
  --yes

# --- read-back: confirm gone, never trust the delete call's exit code alone -
AFTER=$(registry_count)
require_value "$AFTER" "post-delete registry count"
if [ "$AFTER" != "0" ]; then
  echo "Registry '$AZ_ACR_NAME' still listed after delete." >&2
  exit 1
fi

echo "Deleted container registry $AZ_ACR_NAME."
echo "Verified: no registry by this name remains in the subscription."
