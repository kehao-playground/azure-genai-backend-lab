#!/usr/bin/env bash
# Delete the demo resource group and EVERYTHING in it.
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default az context)
#   AZ_RESOURCE_GROUP  - resource group name to delete
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID (default az context may point at the wrong subscription)}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"

echo "Subscription: $AZ_SUBSCRIPTION_ID"
echo "About to DELETE resource group '$AZ_RESOURCE_GROUP' and all resources in it."
read -r -p "Type the resource group name to confirm: " CONFIRM
if [[ "$CONFIRM" != "$AZ_RESOURCE_GROUP" ]]; then
  echo "Confirmation mismatch; aborting." >&2
  exit 1
fi

az group delete --subscription "$AZ_SUBSCRIPTION_ID" \
  --name "$AZ_RESOURCE_GROUP" --yes --no-wait
echo "Deletion of $AZ_RESOURCE_GROUP started (running async)."
