#!/usr/bin/env bash
# Delete AND purge the ephemeral Key Vault.
# Vaults are soft-deleted; without a purge the globally-unique name stays
# reserved (and the vault restorable) for the retention window. Purging is
# possible only because create-keyvault.sh leaves purge protection off.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default az context)
#   AZ_RESOURCE_GROUP  - resource group name (e.g. rg-azgenai-lab)
#   AZ_LOCATION        - Azure region of the vault (e.g. japaneast)
#   AZ_KEYVAULT_NAME   - vault name
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID (default az context may point at the wrong subscription)}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP (e.g. rg-azgenai-lab)}"
: "${AZ_LOCATION:?Set AZ_LOCATION (e.g. japaneast)}"
: "${AZ_KEYVAULT_NAME:?Set AZ_KEYVAULT_NAME}"

az keyvault delete \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_KEYVAULT_NAME"

az keyvault purge \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --name "$AZ_KEYVAULT_NAME" \
  --location "$AZ_LOCATION"

echo "Deleted and purged key vault $AZ_KEYVAULT_NAME."
