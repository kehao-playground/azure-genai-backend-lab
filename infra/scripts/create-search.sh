#!/usr/bin/env bash
# Create an ephemeral Azure AI Search service.
# Free tier by default: one per subscription, 50 MB, 3 indexes, shared
# infrastructure, and it may be deleted after long inactivity. Pass
# --sku basic when the free tier cannot answer the question being tested.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP  - existing resource group
#   AZ_SEARCH_NAME     - globally unique service name
# Optional:
#   AZ_LOCATION        - defaults to japaneast
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_SEARCH_NAME:?Set AZ_SEARCH_NAME}"
LOCATION="${AZ_LOCATION:-japaneast}"
SKU="free"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sku) SKU="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo "Creating $SKU search service '$AZ_SEARCH_NAME' in $LOCATION"
az search service create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_SEARCH_NAME" \
  --location "$LOCATION" \
  --sku "$SKU"

echo "Service properties (record these in the evidence file):"
az search service show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_SEARCH_NAME" \
  --query "{sku:sku.name, location:location, semanticSearch:semanticSearch}" -o json

echo "Admin key (export as AZURE_SEARCH_ADMIN_KEY):"
az search admin-key show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --service-name "$AZ_SEARCH_NAME" \
  --query primaryKey -o tsv

echo "Endpoint: https://$AZ_SEARCH_NAME.search.windows.net"
echo
echo "This service is ephemeral. Run delete-search.sh when finished."
