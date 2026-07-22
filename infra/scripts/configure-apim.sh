#!/usr/bin/env bash
# Create an Azure API Management instance (Consumption tier ONLY — cost policy)
# fronting the Azure OpenAI v1 endpoint with managed-identity backend auth.
# Consumption bills per call (first 1M calls/month free, checked 2026-07), but
# the instance is still ephemeral-by-default: pair with delete-apim.sh.
# Required env vars:
#   AZ_SUBSCRIPTION_ID   - target subscription (never rely on the default az context)
#   AZ_RESOURCE_GROUP    - resource group name (e.g. rg-azgenai-lab)
#   AZ_LOCATION          - Azure region (e.g. japaneast)
#   AZ_OPENAI_NAME       - existing Azure OpenAI account to front (e.g. aoai-azgenai-lab)
#   AZ_APIM_NAME         - APIM instance name, globally unique DNS label (e.g. apim-azgenai-lab)
#   AZ_APIM_PUBLISHER_EMAIL - required by APIM at creation time
# Optional env vars:
#   AZ_APIM_API_PATH     - API path segment on the gateway (default: genai)
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID (default az context may point at the wrong subscription)}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP (e.g. rg-azgenai-lab)}"
: "${AZ_LOCATION:?Set AZ_LOCATION (e.g. japaneast)}"
: "${AZ_OPENAI_NAME:?Set AZ_OPENAI_NAME (existing Azure OpenAI account)}"
: "${AZ_APIM_NAME:?Set AZ_APIM_NAME (globally unique, becomes <name>.azure-api.net)}"
: "${AZ_APIM_PUBLISHER_EMAIL:?Set AZ_APIM_PUBLISHER_EMAIL}"
AZ_APIM_API_PATH="${AZ_APIM_API_PATH:-genai}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_FILE="$SCRIPT_DIR/../apim/genai-api-policy.xml"
[ -f "$POLICY_FILE" ] || { echo "Policy file not found: $POLICY_FILE" >&2; exit 1; }

APIM_ID="/subscriptions/$AZ_SUBSCRIPTION_ID/resourceGroups/$AZ_RESOURCE_GROUP/providers/Microsoft.ApiManagement/service/$AZ_APIM_NAME"
APIM_API_VERSION="2024-05-01"

# 1. APIM instance, Consumption tier, system-assigned managed identity.
#    Consumption activates in minutes; dedicated tiers take 30-45.
az apim create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_APIM_NAME" \
  --location "$AZ_LOCATION" \
  --sku-name Consumption \
  --publisher-name "azure-genai-backend-lab" \
  --publisher-email "$AZ_APIM_PUBLISHER_EMAIL" \
  --enable-managed-identity \
  --output table

# 2. Let the gateway's identity call the model: Cognitive Services OpenAI User
#    on the Azure OpenAI account. No key is read or stored anywhere.
APIM_PRINCIPAL_ID="$(az apim show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_APIM_NAME" \
  --query identity.principalId --output tsv)"
AOAI_ID="$(az cognitiveservices account show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_OPENAI_NAME" \
  --query id --output tsv)"
az role assignment create \
  --assignee-object-id "$APIM_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" \
  --scope "$AOAI_ID" \
  --output table

# 3. Passthrough API for the v1 endpoint: gateway path /$AZ_APIM_API_PATH/* maps
#    onto <aoai>/openai/v1/*.
#    --subscription-required true is NOT the CLI default: without it the API is
#    created with subscriptionRequired=false and the gateway is an open proxy
#    to the model (verified live 2026-07 — a keyless curl got HTTP 200).
AOAI_ENDPOINT="$(az cognitiveservices account show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_OPENAI_NAME" \
  --query properties.endpoint --output tsv)"
az apim api create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --service-name "$AZ_APIM_NAME" \
  --api-id genai-v1 \
  --path "$AZ_APIM_API_PATH" \
  --display-name "GenAI v1 passthrough" \
  --service-url "${AOAI_ENDPOINT%/}/openai/v1" \
  --protocols https \
  --subscription-required true \
  --output table
az apim api operation create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --service-name "$AZ_APIM_NAME" \
  --api-id genai-v1 \
  --operation-id post-all \
  --display-name "POST passthrough" \
  --method POST \
  --url-template "/*" \
  --output table

# 4. API-scope policy: per-subscription rate limit + managed-identity backend
#    auth + client-credential stripping. az has no policy command; use az rest.
POLICY_JSON="$(python3 - "$POLICY_FILE" <<'EOF'
import json, sys
xml = open(sys.argv[1]).read()
print(json.dumps({"properties": {"format": "rawxml", "value": xml}}))
EOF
)"
az rest --method put \
  --url "https://management.azure.com$APIM_ID/apis/genai-v1/policies/policy?api-version=$APIM_API_VERSION" \
  --body "$POLICY_JSON" \
  --output none
echo "Applied API policy from $POLICY_FILE"

# 5. One API-scoped subscription = one client credential. More clients, more
#    subscriptions — the per-subscription rate-limit then bounds each client.
az rest --method put \
  --url "https://management.azure.com$APIM_ID/subscriptions/demo-client?api-version=$APIM_API_VERSION" \
  --body '{"properties": {"scope": "/apis/genai-v1", "displayName": "demo-client"}}' \
  --output none
DEMO_KEY="$(az rest --method post \
  --url "https://management.azure.com$APIM_ID/subscriptions/demo-client/listSecrets?api-version=$APIM_API_VERSION" \
  --query primaryKey --output tsv)"

GATEWAY_URL="$(az apim show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_APIM_NAME" \
  --query gatewayUrl --output tsv)"
echo
echo "Gateway base URL: $GATEWAY_URL/$AZ_APIM_API_PATH"
echo "Demo subscription key (send as Ocp-Apim-Subscription-Key): $DEMO_KEY"
echo "Smoke test:"
echo "  curl -sS $GATEWAY_URL/$AZ_APIM_API_PATH/responses \\"
echo "    -H \"Ocp-Apim-Subscription-Key: \$DEMO_KEY\" -H 'Content-Type: application/json' \\"
echo "    -d '{\"model\": \"chat-mini\", \"input\": \"Reply with exactly: pong\"}'"
echo "Pair with delete-apim.sh when this instance is no longer needed."
