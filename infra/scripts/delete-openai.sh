#!/usr/bin/env bash
# Delete AND purge the Azure OpenAI resource.
# Cognitive Services accounts are soft-deleted; without a purge the name
# stays reserved and quota stays allocated for 48 hours.
# Required env vars:
#   AZ_RESOURCE_GROUP  - resource group name (e.g. rg-azgenai-lab)
#   AZ_LOCATION        - Azure region of the account (e.g. japaneast)
#   AZ_OPENAI_NAME     - Azure OpenAI account name (e.g. aoai-azgenai-lab)
set -euo pipefail

: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP (e.g. rg-azgenai-lab)}"
: "${AZ_LOCATION:?Set AZ_LOCATION (e.g. japaneast)}"
: "${AZ_OPENAI_NAME:?Set AZ_OPENAI_NAME (e.g. aoai-azgenai-lab)}"

az cognitiveservices account delete \
  --name "$AZ_OPENAI_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP"

az cognitiveservices account purge \
  --name "$AZ_OPENAI_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --location "$AZ_LOCATION"

echo "Deleted and purged Azure OpenAI account $AZ_OPENAI_NAME."
