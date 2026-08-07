#!/usr/bin/env bash
# Delete AND purge the ephemeral Content Safety account, from whatever state
# it is in.
#
# States handled:
#   live               -> delete, wait for the soft-deleted proxy, purge, wait
#   soft-deleted only  -> purge directly
#   absent             -> succeed, reporting there was nothing to do
#
# Deviation from delete-keyvault.sh: AZ_RESOURCE_GROUP is REQUIRED in every
# state, not just when the account is live. The Cognitive Services
# soft-delete proxy resource ID embeds the ORIGINAL resource group
# (.../resourceGroups/$AZ_RESOURCE_GROUP/deletedAccounts/$AZ_CONTENT_SAFETY_NAME),
# unlike Key Vault's purge (name + location only) — so purge cannot be
# constructed from name + location alone even after the account itself is
# gone, and the state (live vs. soft-deleted-only) isn't known until after
# the resource group would already need to be in hand. Purging needs
# subscription-level Microsoft.CognitiveServices/locations/deletedAccounts/
# delete permission, distinct from resource-group-scoped account delete.
#
# The script ends by asserting the name is absent from BOTH the active and
# the soft-deleted account listings. It does not (and cannot) speak for other
# subscription-side state — the Microsoft.CognitiveServices provider
# registration created on first use intentionally remains.
#
# Every wait below is bounded; on deadline the script exits non-zero and
# reports the exact state it observed, rather than looping forever.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID     - target subscription (never rely on default context)
#   AZ_RESOURCE_GROUP      - resource group the account was created in
#   AZ_CONTENT_SAFETY_NAME - account name
# Optional env vars:
#   AZ_LOCATION             - defaults to japaneast, same default as
#                             create-content-safety.sh — override both or neither
#   AZ_CS_CREATE_ATTEMPTED  - set to 1 by run-content-safety-probe.sh before
#                             it issues the create. It means "a create was
#                             issued under this name and its outcome may be
#                             unknown", which makes a single reading of
#                             "in neither listing" NOT proof of absence: if
#                             Azure accepted the create but the CLI reported
#                             failure and the listings have not caught up,
#                             exiting 0 here abandons an account that
#                             materialises moments later with no teardown
#                             ever running. With the flag set, the "absent"
#                             branch takes a bounded stabilization wait
#                             first. Left unset (the default) when an
#                             operator runs this script standalone, so a name
#                             that really is gone still exits immediately.
#                             This is only safe because the orchestrator's
#                             account names are unique per run — waiting for
#                             a not-yet-visible account can never end up
#                             waiting onto somebody else's resource.
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID (default az context may point at the wrong subscription)}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_CONTENT_SAFETY_NAME:?Set AZ_CONTENT_SAFETY_NAME}"
AZ_LOCATION="${AZ_LOCATION:-japaneast}"
# Poll/retry knobs exist for the fake-CLI regression tests; the defaults are
# the production behavior (~2-minute deadlines, 10s purge-retry backoff).
AZ_CS_POLL_ATTEMPTS="${AZ_CS_POLL_ATTEMPTS:-24}"
AZ_CS_POLL_INTERVAL="${AZ_CS_POLL_INTERVAL:-5}"
AZ_CS_RETRY_INTERVAL="${AZ_CS_RETRY_INTERVAL:-10}"
AZ_CS_CREATE_ATTEMPTED="${AZ_CS_CREATE_ATTEMPTED:-0}"

# Each query propagates its exit status; call sites abort on query failure
# instead of reading a failed (empty) substitution as a count.
deleted_count() {
  az cognitiveservices account list-deleted --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_CONTENT_SAFETY_NAME'])" -o tsv
}
live_count() {
  az cognitiveservices account list --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_CONTENT_SAFETY_NAME'])" -o tsv
}
# A failed query aborts via set -e on the assignment itself: the pattern is
# always VAR=$(query) on its own line — never $(query) inside a condition,
# where a failure collapses to an empty string and gets compared as a count.
fail_query() {
  echo "Failed to query account state ($1) — see error above; state unknown, this step mutated nothing. Re-run to retry." >&2
  exit 1
}

# --- state: live? ----------------------------------------------------------
LIVE=$(live_count) || fail_query live_count
DELETED=0
if [ "$LIVE" = "0" ]; then
  DELETED=$(deleted_count) || fail_query deleted_count
fi

# --- "not visible yet" is not "absent" -------------------------------------
# Only when this run issued a create whose outcome may be unknown (see
# AZ_CS_CREATE_ATTEMPTED in the header). Same bounded-poll idiom and the same
# fail_query discipline as every other wait here: each query assigns on its
# own line and aborts on failure, never reading a failed query as "still
# absent". On deadline the script falls through to the normal absent branch.
if [ "$LIVE" = "0" ] && [ "$DELETED" = "0" ] && [ "$AZ_CS_CREATE_ATTEMPTED" = "1" ]; then
  echo "'$AZ_CONTENT_SAFETY_NAME' is in neither listing, but this run issued a create under this name — waiting (bounded) in case the account is still materialising." >&2
  for _ in $(seq 1 "$AZ_CS_POLL_ATTEMPTS"); do
    sleep "$AZ_CS_POLL_INTERVAL"
    LIVE=$(live_count) || fail_query live_count
    if [ "$LIVE" != "0" ]; then break; fi
    DELETED=$(deleted_count) || fail_query deleted_count
    if [ "$DELETED" != "0" ]; then break; fi
  done
fi

if [ "$LIVE" != "0" ]; then
  echo "Deleting live Content Safety account '$AZ_CONTENT_SAFETY_NAME'"
  az cognitiveservices account delete \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_CONTENT_SAFETY_NAME"

  # Deletion is asynchronous: wait (bounded) until the soft-deleted proxy is
  # visible before purging, instead of racing it.
  found=0
  for _ in $(seq 1 "$AZ_CS_POLL_ATTEMPTS"); do
    DELETED=$(deleted_count) || fail_query deleted_count
    if [ "$DELETED" != "0" ]; then found=1; break; fi
    sleep "$AZ_CS_POLL_INTERVAL"
  done
  if [ "$found" != "1" ]; then
    echo "Deleted account proxy for '$AZ_CONTENT_SAFETY_NAME' did not appear within the deadline." >&2
    echo "Re-run this script to retry from the current state." >&2
    exit 1
  fi
elif [ "$DELETED" = "0" ]; then
  if [ "$AZ_CS_CREATE_ATTEMPTED" = "1" ]; then
    # Honest about what was and was not established: the wait ran and ended,
    # which is evidence of absence over that window, not proof for all time.
    echo "'$AZ_CONTENT_SAFETY_NAME' never appeared in either listing within the stabilization deadline; nothing to delete."
    echo "If it does materialise later, tear it down with:" >&2
    echo "  AZ_SUBSCRIPTION_ID=$AZ_SUBSCRIPTION_ID AZ_RESOURCE_GROUP=$AZ_RESOURCE_GROUP AZ_LOCATION=$AZ_LOCATION AZ_CONTENT_SAFETY_NAME=$AZ_CONTENT_SAFETY_NAME ./delete-content-safety.sh" >&2
  else
    echo "Nothing to do: '$AZ_CONTENT_SAFETY_NAME' is neither live nor soft-deleted in this subscription."
  fi
  exit 0
fi

# --- state: soft-deleted -> purge, with bounded retries --------------------
echo "Purging soft-deleted Content Safety account '$AZ_CONTENT_SAFETY_NAME' ($AZ_LOCATION)"
PURGE_ID="/subscriptions/$AZ_SUBSCRIPTION_ID/providers/Microsoft.CognitiveServices/locations/$AZ_LOCATION/resourceGroups/$AZ_RESOURCE_GROUP/deletedAccounts/$AZ_CONTENT_SAFETY_NAME"
purged=0
for attempt in 1 2 3; do
  if az resource delete \
      --ids "$PURGE_ID" \
      --subscription "$AZ_SUBSCRIPTION_ID"; then
    purged=1
    break
  fi
  echo "Purge attempt $attempt failed (transient conflicts happen right after delete); retrying in ${AZ_CS_RETRY_INTERVAL}s" >&2
  sleep "$AZ_CS_RETRY_INTERVAL"
done
if [ "$purged" != "1" ]; then
  echo "Purge failed after 3 attempts. Re-run this script to retry from the current state." >&2
  exit 1
fi

# --- final assertion: absent from both listings ----------------------------
for _ in $(seq 1 "$AZ_CS_POLL_ATTEMPTS"); do
  LIVE=$(live_count) || fail_query live_count
  DELETED=$(deleted_count) || fail_query deleted_count
  if [ "$LIVE" = "0" ] && [ "$DELETED" = "0" ]; then
    echo "Deleted and purged Content Safety account $AZ_CONTENT_SAFETY_NAME."
    echo "Verified: no active or soft-deleted account by this name remains in the subscription."
    exit 0
  fi
  sleep "$AZ_CS_POLL_INTERVAL"
done
echo "Purge was accepted but '$AZ_CONTENT_SAFETY_NAME' is still listed (live=$LIVE soft-deleted=$DELETED)." >&2
echo "Re-run this script to retry." >&2
exit 1
