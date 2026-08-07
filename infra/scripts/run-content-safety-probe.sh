#!/usr/bin/env bash
# Ephemeral end-to-end run: create a Content Safety account, run the Day 21
# Prompt Shields probe against it, and delete + purge the account — always,
# even if create, endpoint/key retrieval, or the probe itself fails.
#
# Trap ownership is a correctness contract: this script arms its EXIT trap
# BEFORE calling create-content-safety.sh, so a create that fails halfway
# still gets torn down. create-content-safety.sh itself installs at most a
# recovery-HINT trap (it prints the recovery command, it does not delete
# anything) — the real delete/purge cleanup is owned solely here, so the two
# scripts never both clean up and clobber each other's exit status.
#
# The cleanup trap preserves the ORIGINAL exit status: a probe (or any other
# step) that fails must not become a "success" just because cleanup itself
# succeeded. The explicit end-of-run teardown runs BEFORE `trap - EXIT`
# disarms the trap, so a failure in that explicit teardown call is not
# masked — it re-triggers the same cleanup path and the run still ends
# non-zero.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID     - target subscription (never rely on default context)
#   AZ_RESOURCE_GROUP      - existing resource group
#   AZ_CONTENT_SAFETY_NAME - globally unique account name
#   EVIDENCE_OUT            - path the probe writes its evidence JSON to
# Optional env vars:
#   AZ_LOCATION               - defaults to japaneast
#   PROMPT_SHIELDS_CASES_FILE - defaults to tools/prompt_shields_cases.json
#                                (the canonical fixture) at the repo root
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_LOCATION:=japaneast}"
: "${AZ_CONTENT_SAFETY_NAME:?Set AZ_CONTENT_SAFETY_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CASES_FILE="${PROMPT_SHIELDS_CASES_FILE:-$REPO_ROOT/tools/prompt_shields_cases.json}"
EVIDENCE_OUT="${EVIDENCE_OUT:?Set EVIDENCE_OUT}"

# Validate inputs BEFORE any mutation.
[ -r "$CASES_FILE" ] || { echo "cases file not readable: $CASES_FILE" >&2; exit 1; }

cleanup() {
  status=$?
  "$SCRIPT_DIR/delete-content-safety.sh" || echo "cleanup: delete-content-safety.sh failed" >&2
  exit "$status"   # preserve the original failure status
}
trap cleanup EXIT   # armed BEFORE create can leave a resource behind

"$SCRIPT_DIR/create-content-safety.sh"

ENDPOINT=$(az cognitiveservices account show --name "$AZ_CONTENT_SAFETY_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP" --subscription "$AZ_SUBSCRIPTION_ID" \
  --query properties.endpoint -o tsv)
KEY=$(az cognitiveservices account keys list --name "$AZ_CONTENT_SAFETY_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP" --subscription "$AZ_SUBSCRIPTION_ID" \
  --query key1 -o tsv)

CONTENT_SAFETY_ENDPOINT="$ENDPOINT" CONTENT_SAFETY_KEY="$KEY" \
  uv run python -m tools.prompt_shields_probe --cases-file "$CASES_FILE" --evidence-out "$EVIDENCE_OUT"

# Explicit teardown; only disarm the trap after it succeeds.
"$SCRIPT_DIR/delete-content-safety.sh"
trap - EXIT
