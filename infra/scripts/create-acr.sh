#!/usr/bin/env bash
# Create an ephemeral Azure Container Registry for the Day 24 image push step.
#
# Like every Azure resource in this series, the registry is created at the
# start of a deploy session and destroyed at the end (see delete-acr.sh) — an
# orphaned registry is a recurring bill, not a one-time cost.
#
# Per-run unique default name: Day 21's cleanup deleted (and purged —
# irreversibly) a resource belonging to a CONCURRENT run because it
# identified the resource by a fixed name. AZ_ACR_NAME therefore defaults to
# a prefix plus an 8-hex CSPRNG suffix generated fresh on every invocation,
# never a fixed name. The resolved name is printed so the operator can export
# it for the later deploy step.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default context)
# Optional env vars:
#   AZ_RESOURCE_GROUP - existing resource group, defaults to rg-azgenai-lab
#   AZ_ACR_NAME       - globally unique registry name (alphanumeric only, 5-50
#                       chars); defaults to acrazgenai + 8 hex characters from
#                       python3's secrets module, freshly generated each run
#   AZ_ACR_SKU        - defaults to Basic
#
# Role assignment mode is pinned to rbac (see the `az acr create` call
# below). Microsoft has announced ABAC-enabled registries, on which the
# classic AcrPush/AcrPull/AcrDelete roles are not honored; `rbac` is the CLI
# default today, but that default is Microsoft's to change, not ours to
# assume. This series' CI federated identity is assigned the classic
# AcrPush role, so pinning `rbac` keeps that assignment meaningful. Migrating
# to ABAC (not done in this series) would require: AcrPush -> Container
# Registry Repository Writer; AcrPull -> Container Registry Repository
# Reader + Container Registry Repository Catalog Lister; and `az acr build`
# would need `--source-acr-auth-id [caller]` on an ABAC registry.
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
AZ_RESOURCE_GROUP="${AZ_RESOURCE_GROUP:-rg-azgenai-lab}"
AZ_ACR_SKU="${AZ_ACR_SKU:-Basic}"
if [ -z "${AZ_ACR_NAME:-}" ]; then
  AZ_ACR_NAME="acrazgenai$(python3 -c "import secrets; print(secrets.token_hex(4))")"
fi
echo "ACR name: $AZ_ACR_NAME"

# --- fail-closed read helper -------------------------------------------------
# An `az ... -o tsv` call that exits nonzero is already caught by `set -e` on
# the assignment. What that does NOT catch is a call that exits 0 but prints
# nothing — an empty read must never be silently treated as "unset" or
# "zero"; it has to abort explicitly (Day 19 and Day 21 both lost time to
# exactly this).
require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (az returned empty output); aborting before any mutation." >&2
    exit 1
  fi
}

# --- provider registration BEFORE any ACR state query -----------------------
REG_STATE=$(az provider show --namespace Microsoft.ContainerRegistry \
  --subscription "$AZ_SUBSCRIPTION_ID" --query registrationState -o tsv)
require_value "$REG_STATE" "the Microsoft.ContainerRegistry provider registration state"
if [ "$REG_STATE" != "Registered" ]; then
  echo "Registering the Microsoft.ContainerRegistry resource provider (one-time, may take a minute)"
  az provider register --namespace Microsoft.ContainerRegistry --subscription "$AZ_SUBSCRIPTION_ID" --wait
fi

# --- registry state check: create must know what already exists -----------
LIVE_COUNT=$(az acr list --subscription "$AZ_SUBSCRIPTION_ID" \
  --query "length([?name=='$AZ_ACR_NAME'])" -o tsv)
require_value "$LIVE_COUNT" "existing registry count"

if [ "$LIVE_COUNT" != "0" ]; then
  echo "Registry '$AZ_ACR_NAME' already exists in this subscription — skipping create (idempotent)."
else
  # From here on a failure can leave a real registry behind: print the exact
  # recovery command instead of making the operator reconstruct it.
  trap 'status=$?; if [ "$status" -ne 0 ]; then
    echo "" >&2
    echo "create-acr.sh failed (exit $status). If the registry was already created, clean up with:" >&2
    echo "  AZ_SUBSCRIPTION_ID=$AZ_SUBSCRIPTION_ID AZ_RESOURCE_GROUP=$AZ_RESOURCE_GROUP AZ_ACR_NAME=$AZ_ACR_NAME ./delete-acr.sh" >&2
  fi' EXIT

  echo "Creating container registry '$AZ_ACR_NAME' in resource group '$AZ_RESOURCE_GROUP' (SKU $AZ_ACR_SKU)"
  # --role-assignment-mode rbac is explicit, not the CLI default we happen
  # to inherit: see the header comment for why this can't be left implicit.
  az acr create \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_ACR_NAME" \
    --sku "$AZ_ACR_SKU" \
    --role-assignment-mode rbac >/dev/null
fi

echo
echo "This registry is ephemeral. Run delete-acr.sh when finished:"
echo "  AZ_SUBSCRIPTION_ID=$AZ_SUBSCRIPTION_ID AZ_RESOURCE_GROUP=$AZ_RESOURCE_GROUP AZ_ACR_NAME=$AZ_ACR_NAME ./delete-acr.sh"
