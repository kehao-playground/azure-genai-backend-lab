#!/usr/bin/env bash
# Create the Azure OpenAI resource and a mini-model deployment.
# Standard/GlobalStandard deployments bill per token only (no idle cost),
# so this resource may persist between sessions.
# Required env vars:
#   AZ_RESOURCE_GROUP        - resource group name (e.g. rg-azgenai-lab)
#   AZ_LOCATION              - Azure region (e.g. japaneast)
#   AZ_OPENAI_NAME           - Azure OpenAI account name (e.g. aoai-azgenai-lab)
# Optional env vars:
#   AZ_OPENAI_DEPLOYMENT     - deployment name (default: chat-mini)
#   AZ_OPENAI_MODEL          - model name (default: gpt-5-mini)
#   AZ_OPENAI_MODEL_VERSION  - model version (default: 2025-08-07)
#   AZ_OPENAI_SKU            - deployment SKU (default: GlobalStandard)
#   AZ_OPENAI_CAPACITY       - capacity in K TPM (default: 50)
set -euo pipefail

: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP (e.g. rg-azgenai-lab)}"
: "${AZ_LOCATION:?Set AZ_LOCATION (e.g. japaneast)}"
: "${AZ_OPENAI_NAME:?Set AZ_OPENAI_NAME (e.g. aoai-azgenai-lab)}"
AZ_OPENAI_DEPLOYMENT="${AZ_OPENAI_DEPLOYMENT:-chat-mini}"
AZ_OPENAI_MODEL="${AZ_OPENAI_MODEL:-gpt-5-mini}"
AZ_OPENAI_MODEL_VERSION="${AZ_OPENAI_MODEL_VERSION:-2025-08-07}"
AZ_OPENAI_SKU="${AZ_OPENAI_SKU:-GlobalStandard}"
AZ_OPENAI_CAPACITY="${AZ_OPENAI_CAPACITY:-50}"

az cognitiveservices account create \
  --name "$AZ_OPENAI_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --location "$AZ_LOCATION" \
  --kind OpenAI \
  --sku S0 \
  --custom-domain "$AZ_OPENAI_NAME" \
  --output table

az cognitiveservices account deployment create \
  --name "$AZ_OPENAI_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --deployment-name "$AZ_OPENAI_DEPLOYMENT" \
  --model-name "$AZ_OPENAI_MODEL" \
  --model-version "$AZ_OPENAI_MODEL_VERSION" \
  --model-format OpenAI \
  --sku-name "$AZ_OPENAI_SKU" \
  --sku-capacity "$AZ_OPENAI_CAPACITY" \
  --output table

echo "Endpoint: $(az cognitiveservices account show \
  --name "$AZ_OPENAI_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
  --query properties.endpoint --output tsv)"
echo "Created deployment $AZ_OPENAI_DEPLOYMENT ($AZ_OPENAI_MODEL $AZ_OPENAI_MODEL_VERSION, $AZ_OPENAI_SKU ${AZ_OPENAI_CAPACITY}K TPM)."
echo "Pair with delete-openai.sh when this resource is no longer needed."
