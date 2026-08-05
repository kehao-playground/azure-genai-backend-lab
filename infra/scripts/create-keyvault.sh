#!/usr/bin/env bash
# Create an ephemeral Azure Key Vault for the Day 20 secret-handling demo.
#
# The vault is created with Azure RBAC authorization (the az CLI default since
# 2.42): creating the vault grants NO data-plane access, not even to the
# creator. Reading or writing a secret requires an explicit role assignment
# such as "Key Vault Secrets Officer", which this script makes for the
# signed-in user so the demo can proceed. Role assignments can take a couple
# of minutes to propagate — a 403 right after creation usually means "wait",
# not "broken".
#
# Purge protection stays OFF on purpose: enabling it is irreversible and would
# keep the globally-unique vault name locked for the whole retention window
# after deletion. Ephemeral resources must stay deletable (see
# delete-keyvault.sh, which purges).
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID  - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP   - existing resource group
#   AZ_KEYVAULT_NAME    - globally unique vault name (3-24 chars, alphanumeric/hyphen)
# Optional env vars:
#   AZ_LOCATION          - defaults to japaneast
#   AZ_KV_RETENTION_DAYS - soft-delete retention, defaults to 7 (minimum allowed)
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_KEYVAULT_NAME:?Set AZ_KEYVAULT_NAME}"
AZ_LOCATION="${AZ_LOCATION:-japaneast}"
AZ_KV_RETENTION_DAYS="${AZ_KV_RETENTION_DAYS:-7}"

# A subscription that has never held a vault fails with
# MissingSubscriptionRegistration — the Microsoft.KeyVault resource provider
# is not registered by default on fresh subscriptions (hit live 2026-08).
if [ "$(az provider show --namespace Microsoft.KeyVault \
    --subscription "$AZ_SUBSCRIPTION_ID" --query registrationState -o tsv)" != "Registered" ]; then
  echo "Registering the Microsoft.KeyVault resource provider (one-time, may take a minute)"
  az provider register --namespace Microsoft.KeyVault --subscription "$AZ_SUBSCRIPTION_ID" --wait
fi

echo "Creating key vault '$AZ_KEYVAULT_NAME' in $AZ_LOCATION (RBAC authorization, ${AZ_KV_RETENTION_DAYS}-day soft delete)"
az keyvault create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_KEYVAULT_NAME" \
  --location "$AZ_LOCATION" \
  --retention-days "$AZ_KV_RETENTION_DAYS" \
  --sku standard

VAULT_ID=$(az keyvault show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_KEYVAULT_NAME" \
  --query id -o tsv)

# az ad signed-in-user reads from the tenant of the DEFAULT az context, which
# is shared mutable state. If the default context points at another tenant,
# this returns an object id that means nothing in the vault's tenant and the
# role assignment fails — log in to the vault's tenant first.
CALLER_OBJECT_ID=$(az ad signed-in-user show --query id -o tsv)

echo "Assigning 'Key Vault Secrets Officer' on the vault to the signed-in user"
az role assignment create \
  --assignee-object-id "$CALLER_OBJECT_ID" \
  --assignee-principal-type User \
  --role "Key Vault Secrets Officer" \
  --scope "$VAULT_ID" >/dev/null

echo "Vault: https://$AZ_KEYVAULT_NAME.vault.azure.net/"
echo
echo "This vault is ephemeral. Run delete-keyvault.sh when finished — it purges,"
echo "otherwise the soft-deleted vault reserves the name for $AZ_KV_RETENTION_DAYS days."
