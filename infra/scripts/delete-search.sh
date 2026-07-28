#!/usr/bin/env bash
# Delete the ephemeral Azure AI Search service — and only that service.
#
# Deliberately targeted rather than folded into teardown.sh: that script
# deletes the whole resource group, which also holds the Azure OpenAI resource
# this series keeps. The Search service is the ephemeral thing, not the group.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID, AZ_RESOURCE_GROUP, AZ_SEARCH_NAME
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_SEARCH_NAME:?Set AZ_SEARCH_NAME}"

az search service delete \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_SEARCH_NAME"

echo "Deleted search service $AZ_SEARCH_NAME."
echo "A free-tier service cannot be upgraded in place; recreate with --sku basic if needed."
