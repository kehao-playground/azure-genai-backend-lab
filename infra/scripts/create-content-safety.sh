#!/usr/bin/env bash
# Create an ephemeral Azure AI Content Safety account for the Day 21 Prompt
# Shields probe. Key-based auth only (no role assignment): the orchestrator
# reads the account key back and hands it to tools/prompt_shields_probe.py.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID     - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP      - existing resource group
#   AZ_CONTENT_SAFETY_NAME - globally unique account name (used as the custom
#                             subdomain too)
# Optional env vars:
#   AZ_LOCATION            - defaults to japaneast; delete-content-safety.sh
#                             defaults to the same value — override both or
#                             neither
#   AZ_CONTENT_SAFETY_SKU  - defaults to F0 (free tier, one per subscription)
#
# SKU fallback — empty allowlist is the safe default: F0 is one per
# subscription, so a second F0 create in the same subscription fails.
# CONTENT_SAFETY_SKU_FALLBACK_CODES (comma-separated) lists the
# machine-readable error `code` values that are safe to auto-retry with
# --sku S0. It starts EMPTY on purpose: until a live run (Task 8) observes a
# stable code that unambiguously means "SKU/quota, not something else", any
# create failure aborts rather than silently falling back and masking a real
# problem. A generic code (e.g. InvalidResourceName can mean a real name
# problem) must never be added here just because it showed up once. Until a
# code is deliberately allowlisted, rerun explicitly with
# AZ_CONTENT_SAFETY_SKU=S0.
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_CONTENT_SAFETY_NAME:?Set AZ_CONTENT_SAFETY_NAME}"
AZ_LOCATION="${AZ_LOCATION:-japaneast}"
AZ_CONTENT_SAFETY_SKU="${AZ_CONTENT_SAFETY_SKU:-F0}"
CONTENT_SAFETY_SKU_FALLBACK_CODES="${CONTENT_SAFETY_SKU_FALLBACK_CODES:-}"

# A failed query aborts explicitly instead of being misread as a benign
# value: the pattern is always VAR=$(query) || fail_query "..." on its own
# line — never $(query) inside a condition, where a failure collapses to an
# empty string and gets compared as though the query had succeeded (this
# repo has been bitten by exactly that shape three times, Day 20 review).
fail_query() {
  echo "Failed to query $1 (see error above); aborting without creating a Content Safety account." >&2
  exit 1
}

# --- provider registration BEFORE any Content Safety call ------------------
REG_STATE=$(az provider show --namespace Microsoft.CognitiveServices \
  --subscription "$AZ_SUBSCRIPTION_ID" --query registrationState -o tsv) \
  || fail_query "the Microsoft.CognitiveServices provider registration state"
if [ "$REG_STATE" != "Registered" ]; then
  echo "Registering the Microsoft.CognitiveServices resource provider (one-time, may take a minute)"
  az provider register --namespace Microsoft.CognitiveServices --subscription "$AZ_SUBSCRIPTION_ID" --wait
fi

# --- account state check: create must know what already exists ------------
# Mirrors create-keyvault.sh's own precedent (this repo's guard for exactly
# this shape of risk) and delete-content-safety.sh's query shape, so the two
# Content Safety scripts stay consistent. The exposure here is worse than Key
# Vault's: the orchestrator's EXIT trap deletes AND purges unconditionally on
# every exit path, so a name collision with an existing account (mistyped or
# copy-pasted AZ_CONTENT_SAFETY_NAME) would mean this script adopts someone
# else's account and the trap purges it.
#
# Exit code 3 is a deliberate, narrow signal shared with the orchestrator
# (run-content-safety-probe.sh): "refused before touching anything — an
# account already exists under this name, and nothing belonging to this run
# was created." The orchestrator's cleanup trap special-cases exactly this
# status to skip teardown, so it never purges an account it did not create.
# Every OTHER failure in this script (query failures, create failures) keeps
# exit 1, because those still need the orchestrator's normal teardown (e.g. a
# create that fails after partially creating the account). Do not reuse exit
# 3 for any new failure path unless it is equally true that this run created
# nothing.
if ! LIVE_COUNT=$(az cognitiveservices account list --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_CONTENT_SAFETY_NAME'])" -o tsv); then
  fail_query "live Content Safety accounts in the subscription"
fi
if [ "$LIVE_COUNT" != "0" ]; then
  echo "Content Safety account '$AZ_CONTENT_SAFETY_NAME' already exists live in this subscription. This script only creates from scratch:" >&2
  echo "reuse the existing account as-is, or run delete-content-safety.sh first." >&2
  exit 3
fi
if ! DELETED_COUNT=$(az cognitiveservices account list-deleted --subscription "$AZ_SUBSCRIPTION_ID" \
    --query "length([?name=='$AZ_CONTENT_SAFETY_NAME'])" -o tsv); then
  fail_query "soft-deleted Content Safety accounts"
fi
if [ "$DELETED_COUNT" != "0" ]; then
  echo "The name '$AZ_CONTENT_SAFETY_NAME' is held by a soft-deleted Content Safety account (soft delete reserves the name until purge)." >&2
  echo "Run delete-content-safety.sh (it purges from this state too), or pick another name." >&2
  exit 3
fi

# From here on a failure can leave a real account behind: print the exact
# recovery command instead of making the operator reconstruct it. This is a
# recovery HINT only — it does not run delete/purge itself. The real
# delete/purge cleanup is owned solely by the orchestrator
# (run-content-safety-probe.sh), so two scripts never both clean up and
# clobber each other's exit status.
trap 'status=$?; if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "create-content-safety.sh failed (exit $status). If the account was already created, clean up with:" >&2
  echo "  AZ_SUBSCRIPTION_ID=$AZ_SUBSCRIPTION_ID AZ_RESOURCE_GROUP=$AZ_RESOURCE_GROUP AZ_LOCATION=$AZ_LOCATION AZ_CONTENT_SAFETY_NAME=$AZ_CONTENT_SAFETY_NAME ./delete-content-safety.sh" >&2
fi' EXIT

create_account() {
  local sku="$1"
  az cognitiveservices account create \
    --name "$AZ_CONTENT_SAFETY_NAME" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --kind ContentSafety \
    --sku "$sku" \
    --location "$AZ_LOCATION" \
    --custom-domain "$AZ_CONTENT_SAFETY_NAME" \
    --yes \
    --subscription "$AZ_SUBSCRIPTION_ID"
}

# code_is_allowlisted CODE — matches against the comma-separated allowlist,
# which is empty by default (see header). A code that fails to parse (see
# below) is never allowlisted either.
code_is_allowlisted() {
  local code="$1" entry
  local IFS=','
  for entry in $CONTENT_SAFETY_SKU_FALLBACK_CODES; do
    [ "$entry" = "$code" ] && return 0
  done
  return 1
}

echo "Creating Content Safety account '$AZ_CONTENT_SAFETY_NAME' ($AZ_CONTENT_SAFETY_SKU) in $AZ_LOCATION"
# `if VAR=$(cmd 2>&1 >/dev/null); then ... else ... fi` is NOT the
# `$(query)`-inside-a-condition footgun this repo has been bitten by
# before: there the command's own exit status is discarded because only its
# stdout is compared as a string. Here the `if` tests the exit status of the
# assignment itself (== cmd's exit status), and the redirection order
# captures ONLY stderr into ERR_BODY (stdout is separately dropped) so a
# machine-readable error body can be parsed without losing the pass/fail
# signal.
if ERR_BODY=$(create_account "$AZ_CONTENT_SAFETY_SKU" 2>&1 >/dev/null); then
  :
else
  ERR_CODE=$(printf '%s' "$ERR_BODY" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
    print(body.get("error", {}).get("code", ""))
except Exception:
    print("")
')
  if [ -n "$ERR_CODE" ] && code_is_allowlisted "$ERR_CODE"; then
    echo "Create failed with allowlisted code '$ERR_CODE' — retrying once with --sku S0" >&2
    create_account S0 >/dev/null
    AZ_CONTENT_SAFETY_SKU=S0
  else
    echo "Content Safety account create failed${ERR_CODE:+ (code: $ERR_CODE)}." >&2
    printf '%s\n' "$ERR_BODY" >&2
    exit 1
  fi
fi

echo "Account: https://$AZ_CONTENT_SAFETY_NAME.cognitiveservices.azure.com/  (kind=ContentSafety, sku=$AZ_CONTENT_SAFETY_SKU)"
echo
echo "This account is ephemeral. Run delete-content-safety.sh when finished — it purges,"
echo "otherwise the soft-deleted account reserves the name."
