#!/usr/bin/env bash
# Create an ephemeral Azure AI Search service.
# Free tier by default: one per subscription, 50 MB, 3 indexes, shared
# infrastructure, and it may be deleted after long inactivity. Set
# AZ_SEARCH_SKU=basic when the free tier cannot answer the question being tested.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP  - existing resource group
#   AZ_SEARCH_NAME     - globally unique service name
# Optional env vars:
#   AZ_LOCATION        - defaults to japaneast
#   AZ_SEARCH_SKU      - defaults to free
#   AZ_SEARCH_SEMANTIC - semantic ranker plan: disabled|free|standard.
#                         Unset means the flag is not sent at all.
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_SEARCH_NAME:?Set AZ_SEARCH_NAME}"
AZ_LOCATION="${AZ_LOCATION:-japaneast}"
AZ_SEARCH_SKU="${AZ_SEARCH_SKU:-free}"

# Optional: semantic ranker plan. Unset means the flag is not sent at all,
# so existing behaviour is byte-identical. az 2.89.1 allows
# disabled|free|standard; hybrid_semantic queries need free or standard.
AZ_SEARCH_SEMANTIC="${AZ_SEARCH_SEMANTIC:-}"

semantic_args=()
if [ -n "$AZ_SEARCH_SEMANTIC" ]; then
  semantic_args=(--semantic-search "$AZ_SEARCH_SEMANTIC")
fi

echo "Creating $AZ_SEARCH_SKU search service '$AZ_SEARCH_NAME' in $AZ_LOCATION"
# The "${arr[@]+"${arr[@]}"}" expansion below is deliberately verbose: a
# plain "${semantic_args[@]}" on an empty array is an "unbound variable"
# error under `set -u` on bash < 4.4 (e.g. macOS's stock /bin/bash 3.2),
# which this script hits on its default, AZ_SEARCH_SEMANTIC-unset path when
# invoked via its own shebang. Do not simplify this back.
az search service create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_SEARCH_NAME" \
  --location "$AZ_LOCATION" \
  --sku "$AZ_SEARCH_SKU" \
  "${semantic_args[@]+"${semantic_args[@]}"}"

echo "Service properties (record these in the evidence file):"
az search service show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_SEARCH_NAME" \
  --query "{sku:sku.name, location:location, semanticSearch:semanticSearch}" -o json

echo "Admin key (export as AZURE_SEARCH_ADMIN_KEY; for immediate use only—never paste into evidence files, screenshots, or committed text):"
az search admin-key show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --service-name "$AZ_SEARCH_NAME" \
  --query primaryKey -o tsv

echo "Endpoint: https://$AZ_SEARCH_NAME.search.windows.net"
echo
echo "This service is ephemeral. Run delete-search.sh when finished."
