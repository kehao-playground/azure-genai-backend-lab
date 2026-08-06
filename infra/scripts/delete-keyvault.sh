#!/usr/bin/env bash
# Delete AND purge the ephemeral Key Vault, from whatever state it is in.
#
# States handled:
#   live               -> delete, wait for the soft-deleted proxy, purge, wait
#   soft-deleted only  -> purge directly (works even if the resource group is
#                         already gone: purge needs only name + location)
#   absent             -> succeed, reporting there was nothing to do
#
# The script ends by asserting the name is absent from BOTH the active and the
# soft-deleted vault listings. That assertion is exactly the teardown claim
# this script can make: no active or soft-deleted vault by this name remains
# in the subscription. It does not (and cannot) speak for other
# subscription-side state — the Microsoft.KeyVault provider registration
# created on first use intentionally remains, and role assignments scoped to
# the vault die with the vault.
#
# Purging is possible only because create-keyvault.sh leaves purge protection
# off. Every wait below is bounded; on deadline the script exits non-zero and
# reports the exact state it observed, rather than looping forever.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID - target subscription (never rely on the default az context)
#   AZ_KEYVAULT_NAME   - vault name
# Optional env vars:
#   AZ_RESOURCE_GROUP  - required only when the vault is still live
#   AZ_LOCATION        - defaults to japaneast, same default as
#                        create-keyvault.sh — override both or neither
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID (default az context may point at the wrong subscription)}"
: "${AZ_KEYVAULT_NAME:?Set AZ_KEYVAULT_NAME}"
AZ_LOCATION="${AZ_LOCATION:-japaneast}"
# Poll/retry knobs exist for the fake-CLI regression tests; the defaults are
# the production behavior (~2-minute deadlines, 10s purge-retry backoff).
AZ_KV_POLL_ATTEMPTS="${AZ_KV_POLL_ATTEMPTS:-24}"
AZ_KV_POLL_INTERVAL="${AZ_KV_POLL_INTERVAL:-5}"
AZ_KV_RETRY_INTERVAL="${AZ_KV_RETRY_INTERVAL:-10}"

# Each query propagates its exit status; call sites abort on query failure
# instead of reading a failed (empty) substitution as a count (r05 F1 class).
deleted_count() {
  az keyvault list-deleted --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_KEYVAULT_NAME'])" -o tsv
}
live_count() {
  az keyvault list --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_KEYVAULT_NAME'])" -o tsv
}
# A failed query aborts via set -e on the assignment itself: the pattern is
# always VAR=$(query) on its own line — never $(query) inside a condition,
# where a failure collapses to an empty string and gets compared as a count.
fail_query() {
  echo "Failed to query vault state ($1) — see error above; state unknown, this step mutated nothing. Re-run to retry." >&2
  exit 1
}

# --- state: live? ----------------------------------------------------------
LIVE=$(live_count) || fail_query live_count
if [ "$LIVE" != "0" ]; then
  : "${AZ_RESOURCE_GROUP:?Vault is live: set AZ_RESOURCE_GROUP to delete it}"
  echo "Deleting live vault '$AZ_KEYVAULT_NAME'"
  az keyvault delete \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_KEYVAULT_NAME"

  # Deletion is asynchronous: wait (bounded) until the soft-deleted proxy is
  # visible before purging, instead of racing it.
  found=0
  for _ in $(seq 1 "$AZ_KV_POLL_ATTEMPTS"); do
    DELETED=$(deleted_count) || fail_query deleted_count
    if [ "$DELETED" != "0" ]; then found=1; break; fi
    sleep "$AZ_KV_POLL_INTERVAL"
  done
  if [ "$found" != "1" ]; then
    echo "Deleted vault proxy for '$AZ_KEYVAULT_NAME' did not appear within the deadline." >&2
    echo "Re-run this script to retry from the current state." >&2
    exit 1
  fi
else
  DELETED=$(deleted_count) || fail_query deleted_count
  if [ "$DELETED" = "0" ]; then
    echo "Nothing to do: '$AZ_KEYVAULT_NAME' is neither live nor soft-deleted in this subscription."
    exit 0
  fi
fi

# --- state: soft-deleted -> purge, with bounded retries --------------------
echo "Purging soft-deleted vault '$AZ_KEYVAULT_NAME' ($AZ_LOCATION)"
purged=0
for attempt in 1 2 3; do
  if az keyvault purge \
      --subscription "$AZ_SUBSCRIPTION_ID" \
      --name "$AZ_KEYVAULT_NAME" \
      --location "$AZ_LOCATION"; then
    purged=1
    break
  fi
  echo "Purge attempt $attempt failed (transient conflicts happen right after delete); retrying in ${AZ_KV_RETRY_INTERVAL}s" >&2
  sleep "$AZ_KV_RETRY_INTERVAL"
done
if [ "$purged" != "1" ]; then
  echo "Purge failed after 3 attempts. Re-run this script to retry from the current state." >&2
  exit 1
fi

# --- final assertion: absent from both listings ----------------------------
for _ in $(seq 1 "$AZ_KV_POLL_ATTEMPTS"); do
  LIVE=$(live_count) || fail_query live_count
  DELETED=$(deleted_count) || fail_query deleted_count
  if [ "$LIVE" = "0" ] && [ "$DELETED" = "0" ]; then
    echo "Deleted and purged key vault $AZ_KEYVAULT_NAME."
    echo "Verified: no active or soft-deleted vault by this name remains in the subscription."
    exit 0
  fi
  sleep "$AZ_KV_POLL_INTERVAL"
done
echo "Purge was accepted but '$AZ_KEYVAULT_NAME' is still listed (live=$LIVE soft-deleted=$DELETED)." >&2
echo "Re-run this script to retry." >&2
exit 1
