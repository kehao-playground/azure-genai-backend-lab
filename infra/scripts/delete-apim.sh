#!/usr/bin/env bash
# Delete the APIM instance created by configure-apim.sh, including the role
# assignment its managed identity held on the Azure OpenAI account.
# Note: APIM soft-deletes by default; this script purges so the name is freed.
# Required env vars:
#   AZ_SUBSCRIPTION_ID   - target subscription (never rely on the default az context)
#   AZ_RESOURCE_GROUP    - resource group name (e.g. rg-azgenai-lab)
#   AZ_LOCATION          - Azure region of the instance (needed for purge)
#   AZ_APIM_NAME         - APIM instance name
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID (default az context may point at the wrong subscription)}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP (e.g. rg-azgenai-lab)}"
: "${AZ_LOCATION:?Set AZ_LOCATION (e.g. japaneast)}"
: "${AZ_APIM_NAME:?Set AZ_APIM_NAME}"

# Role assignments on other resources are not removed by deleting the service;
# drop any assignment held by this instance's identity first. Filter by
# principalId instead of --assignee: the assignee path does a graph lookup
# that fails for identities mid-deletion and leaves orphans (hit live 2026-07).
APIM_PRINCIPAL_ID="$(az apim show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_APIM_NAME" \
  --query identity.principalId --output tsv 2>/dev/null || true)"
if [ -n "$APIM_PRINCIPAL_ID" ]; then
  for ASSIGNMENT_ID in $(az role assignment list \
      --subscription "$AZ_SUBSCRIPTION_ID" \
      --all \
      --query "[?principalId=='$APIM_PRINCIPAL_ID'].id" --output tsv); do
    az role assignment delete --ids "$ASSIGNMENT_ID"
  done
fi

az apim delete \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_APIM_NAME" \
  --yes

az apim deletedservice purge \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --service-name "$AZ_APIM_NAME" \
  --location "$AZ_LOCATION"

echo "Deleted and purged APIM instance $AZ_APIM_NAME."
