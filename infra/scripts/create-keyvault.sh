#!/usr/bin/env bash
# Create an ephemeral Azure Key Vault for the Day 20 secret-handling demo.
#
# The security posture is explicit, not inherited: the script passes
# --enable-rbac-authorization true and reads the created vault's properties
# back, so a CLI version with different defaults cannot silently change the
# contract. Purge protection is the one property that CANNOT be passed
# explicitly as false — the API rejects it ("cannot be set to false...
# irreversible action", hit live 2026-08-06); it is either omitted (off) or
# true (forever). So its OFF posture is enforced the only way possible: the
# flag is never passed, and the read-back below fails the run if the created
# vault somehow reports purge protection enabled. Validated live with az CLI
# 2.88.0 (japaneast, 2026-08); the read-back assertions — not a version pin —
# are what keep other CLI versions safe.
#
# Under RBAC, creating a vault grants NO data-plane access, not even to the
# creator; the script assigns "Key Vault Secrets Officer" to the signed-in
# user, idempotently (an existing identical assignment is kept, not
# duplicated). Role assignments can take minutes to propagate: a 403 shortly
# after creation MAY be propagation. Verify principal/role/scope once, then
# retry with backoff to a deadline; past the deadline treat it as
# misconfiguration to diagnose, not something to keep waiting out.
#
# Purge protection stays OFF on purpose: enabling it is irreversible and would
# keep the globally-unique vault name locked for the whole retention window
# after deletion. Ephemeral resources must stay deletable (see
# delete-keyvault.sh, which purges).
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID  - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP   - existing resource group
#   AZ_KEYVAULT_NAME    - globally unique vault name; 3-24 chars, letters/
#                         digits/hyphens, starts with a letter, ends with a
#                         letter or digit, no consecutive hyphens
# Optional env vars:
#   AZ_LOCATION          - defaults to japaneast; delete-keyvault.sh defaults
#                          to the same value — override both or neither
#   AZ_KV_RETENTION_DAYS - soft-delete retention, integer 7-90, defaults to 7
#
# Privileges the caller needs (and they are distinct sets): provider
# registration on the subscription (Microsoft.KeyVault/register/action, first
# run only), vault creation on the resource group, and role-assignment write
# on the vault scope (Microsoft.Authorization/roleAssignments/write, e.g.
# Owner or User Access Administrator). The role is assigned to the SIGNED-IN
# USER (--assignee-principal-type User); running this as a service principal
# requires changing that step.
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_KEYVAULT_NAME:?Set AZ_KEYVAULT_NAME}"
AZ_LOCATION="${AZ_LOCATION:-japaneast}"
AZ_KV_RETENTION_DAYS="${AZ_KV_RETENTION_DAYS:-7}"

# --- local contract checks, before any Azure call --------------------------
if ! [[ "$AZ_KEYVAULT_NAME" =~ ^[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]$ ]] || [[ "$AZ_KEYVAULT_NAME" == *--* ]]; then
  echo "AZ_KEYVAULT_NAME '$AZ_KEYVAULT_NAME' is invalid: 3-24 chars, letters/digits/hyphens only, must start with a letter, end with a letter or digit, no consecutive hyphens." >&2
  exit 1
fi
if ! [[ "$AZ_KV_RETENTION_DAYS" =~ ^[0-9]+$ ]] || (( AZ_KV_RETENTION_DAYS < 7 || AZ_KV_RETENTION_DAYS > 90 )); then
  echo "AZ_KV_RETENTION_DAYS '$AZ_KV_RETENTION_DAYS' is invalid: integer between 7 and 90." >&2
  exit 1
fi

# --- tenant preflight, before any mutation ---------------------------------
# Resource commands pin --subscription, but `az ad signed-in-user show` reads
# the DEFAULT az context, which is shared mutable state. Detect the mismatch
# before creating anything rather than during role assignment (hit live
# 2026-08-05: the failure used to surface only after the vault existed).
TARGET_TENANT=$(az account show --subscription "$AZ_SUBSCRIPTION_ID" --query tenantId -o tsv)
ACTIVE_TENANT=$(az account show --query tenantId -o tsv)
if [ "$TARGET_TENANT" != "$ACTIVE_TENANT" ]; then
  echo "Active az context is in tenant $ACTIVE_TENANT but subscription $AZ_SUBSCRIPTION_ID lives in tenant $TARGET_TENANT." >&2
  echo "Fix with: az login --tenant $TARGET_TENANT" >&2
  echo "     or : az account set --subscription $AZ_SUBSCRIPTION_ID" >&2
  exit 1
fi
CALLER_OBJECT_ID=$(az ad signed-in-user show --query id -o tsv)

# --- provider registration BEFORE any Key Vault state query ----------------
# On a fresh subscription every keyvault query (list, list-deleted, show)
# fails with MissingSubscriptionRegistration — the Microsoft.KeyVault
# resource provider is not registered by default (hit live 2026-08). The
# registration check therefore has to run before the state checks below, or
# they die first and the pre-check is unreachable (review r05 F1: an earlier
# revision had exactly that ordering bug).
REG_STATE=$(az provider show --namespace Microsoft.KeyVault \
  --subscription "$AZ_SUBSCRIPTION_ID" --query registrationState -o tsv)
if [ "$REG_STATE" != "Registered" ]; then
  echo "Registering the Microsoft.KeyVault resource provider (one-time, may take a minute)"
  az provider register --namespace Microsoft.KeyVault --subscription "$AZ_SUBSCRIPTION_ID" --wait
fi

# --- vault state check: create must know what already exists ---------------
# Every count query checks its own exit status: a failed query must abort as
# a query failure, never be read as a count (an empty substitution compares
# unequal to "0" and would misreport the failure as a name collision —
# review r05 F1's second half).
if ! LIVE_COUNT=$(az keyvault list --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_KEYVAULT_NAME'])" -o tsv); then
  echo "Failed to query live vaults in the subscription (see error above); aborting before any mutation." >&2
  exit 1
fi
if [ "$LIVE_COUNT" != "0" ]; then
  echo "Vault '$AZ_KEYVAULT_NAME' already exists live in this subscription. This script only creates from scratch:" >&2
  echo "reuse the existing vault as-is, or run delete-keyvault.sh first." >&2
  exit 1
fi
if ! DELETED_COUNT=$(az keyvault list-deleted --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_KEYVAULT_NAME'])" -o tsv); then
  echo "Failed to query soft-deleted vaults (see error above); aborting before any mutation." >&2
  exit 1
fi
if [ "$DELETED_COUNT" != "0" ]; then
  echo "The name '$AZ_KEYVAULT_NAME' is held by a soft-deleted vault (soft delete reserves the name until purge)." >&2
  echo "Run delete-keyvault.sh (it purges from this state too), or pick another name." >&2
  exit 1
fi

# From here on a failure can leave a real vault behind: print the exact
# recovery command instead of making the operator reconstruct it.
trap 'status=$?; if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "create-keyvault.sh failed (exit $status). If the vault was already created, clean up with:" >&2
  echo "  AZ_SUBSCRIPTION_ID=$AZ_SUBSCRIPTION_ID AZ_RESOURCE_GROUP=$AZ_RESOURCE_GROUP AZ_LOCATION=$AZ_LOCATION AZ_KEYVAULT_NAME=$AZ_KEYVAULT_NAME ./delete-keyvault.sh" >&2
fi' EXIT

echo "Creating key vault '$AZ_KEYVAULT_NAME' in $AZ_LOCATION (RBAC authorization on, purge protection off, ${AZ_KV_RETENTION_DAYS}-day soft delete)"
az keyvault create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_KEYVAULT_NAME" \
  --location "$AZ_LOCATION" \
  --enable-rbac-authorization true \
  --retention-days "$AZ_KV_RETENTION_DAYS" \
  --sku standard >/dev/null

# --- read the security posture back; never trust defaults ------------------
# One property per call on purpose: az array projections with -o tsv emit one
# value per line, which is exactly the parsing bug Day 19 shipped and fixed.
VAULT_ID=$(az keyvault show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_KEYVAULT_NAME" --query id -o tsv)
RB_RBAC=$(az keyvault show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_KEYVAULT_NAME" \
  --query "properties.enableRbacAuthorization" -o tsv)
RB_PURGE=$(az keyvault show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_KEYVAULT_NAME" \
  --query "properties.enablePurgeProtection" -o tsv)
RB_RETENTION=$(az keyvault show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_KEYVAULT_NAME" \
  --query "properties.softDeleteRetentionInDays" -o tsv)
RB_LOCATION=$(az keyvault show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_KEYVAULT_NAME" \
  --query "location" -o tsv)
if [ "$RB_RBAC" != "true" ] || [ "$RB_PURGE" = "true" ] \
   || [ "$RB_RETENTION" != "$AZ_KV_RETENTION_DAYS" ] || [ "$RB_LOCATION" != "$AZ_LOCATION" ]; then
  echo "Created vault does not match the requested posture:" >&2
  echo "  enableRbacAuthorization=$RB_RBAC (want true)" >&2
  echo "  enablePurgeProtection=${RB_PURGE:-<unset>} (must not be true)" >&2
  echo "  softDeleteRetentionInDays=$RB_RETENTION (want $AZ_KV_RETENTION_DAYS)" >&2
  echo "  location=$RB_LOCATION (want $AZ_LOCATION)" >&2
  exit 1
fi

# --- data-plane role for the signed-in user, idempotently ------------------
if [ "$(az role assignment list --assignee "$CALLER_OBJECT_ID" \
    --role "Key Vault Secrets Officer" --scope "$VAULT_ID" --query "length([])" -o tsv)" = "0" ]; then
  echo "Assigning 'Key Vault Secrets Officer' on the vault to the signed-in user"
  az role assignment create \
    --assignee-object-id "$CALLER_OBJECT_ID" \
    --assignee-principal-type User \
    --role "Key Vault Secrets Officer" \
    --scope "$VAULT_ID" >/dev/null
else
  echo "'Key Vault Secrets Officer' already assigned on the vault to the signed-in user — keeping it"
fi

echo "Vault: https://$AZ_KEYVAULT_NAME.vault.azure.net/  (RBAC on, purge protection off, retention ${AZ_KV_RETENTION_DAYS}d)"
echo
echo "This vault is ephemeral. Run delete-keyvault.sh when finished — it purges,"
echo "otherwise the soft-deleted vault reserves the name for $AZ_KV_RETENTION_DAYS days."
