#!/usr/bin/env bash
# Create the demo resource group.
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default az context)
#   AZ_RESOURCE_GROUP  - resource group name (e.g. rg-azgenai-lab)
#   AZ_LOCATION        - Azure region (e.g. eastasia)
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID (default az context may point at the wrong subscription)}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP (e.g. rg-azgenai-lab)}"
: "${AZ_LOCATION:?Set AZ_LOCATION (e.g. eastasia)}"

az group create --subscription "$AZ_SUBSCRIPTION_ID" \
  --name "$AZ_RESOURCE_GROUP" --location "$AZ_LOCATION" --output table
echo "Created resource group $AZ_RESOURCE_GROUP in $AZ_LOCATION."
echo "Remember: run teardown.sh when the test session ends."
