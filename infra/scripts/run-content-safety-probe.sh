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
#   EVIDENCE_OUT            - path the probe writes its evidence JSON to; if
#                              relative, it is resolved against the CALLER's
#                              cwd (see cwd note below), not the repo root
# Optional env vars:
#   AZ_LOCATION               - defaults to japaneast
#   PROMPT_SHIELDS_CASES_FILE - defaults to tools/prompt_shields_cases.json
#                                (the canonical fixture) at the repo root
#
# cwd note: `tools/` is not an installed package, so `uv run python -m
# tools.prompt_shields_probe` only resolves when uv's own cwd is the repo
# root. The documented invocation is `cd infra/scripts && ./run-content-
# safety-probe.sh`, which is NOT the repo root, so this script cd's to
# REPO_ROOT itself right before invoking the probe — after every
# cwd-relative input (EVIDENCE_OUT) has already been made absolute against
# the ORIGINAL caller cwd, so a relative path still lands where the caller
# expects, not wherever the script happened to cd into.
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_LOCATION:=japaneast}"
: "${AZ_CONTENT_SAFETY_NAME:?Set AZ_CONTENT_SAFETY_NAME}"
CALLER_DIR="$PWD"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CASES_FILE="${PROMPT_SHIELDS_CASES_FILE:-$REPO_ROOT/tools/prompt_shields_cases.json}"
EVIDENCE_OUT="${EVIDENCE_OUT:?Set EVIDENCE_OUT}"
# Absolutize against the caller's cwd BEFORE the later `cd "$REPO_ROOT"` —
# otherwise a relative EVIDENCE_OUT would silently be reinterpreted against
# the repo root instead of where the caller actually meant it.
if [[ "$EVIDENCE_OUT" != /* ]]; then
  EVIDENCE_OUT="$CALLER_DIR/$EVIDENCE_OUT"
fi

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
  --query properties.endpoint -o tsv) \
  || { echo "Failed to read back the account endpoint (see error above)." >&2; exit 1; }
KEY=$(az cognitiveservices account keys list --name "$AZ_CONTENT_SAFETY_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP" --subscription "$AZ_SUBSCRIPTION_ID" \
  --query key1 -o tsv) \
  || { echo "Failed to read back the account key (see error above)." >&2; exit 1; }

# cd to the repo root so `uv run python -m tools.prompt_shields_probe`
# resolves regardless of the caller's cwd (see cwd note above). Every path
# used from here on (CASES_FILE, EVIDENCE_OUT) is already absolute.
cd "$REPO_ROOT"
CONTENT_SAFETY_ENDPOINT="$ENDPOINT" CONTENT_SAFETY_KEY="$KEY" \
  uv run python -m tools.prompt_shields_probe --cases-file "$CASES_FILE" --evidence-out "$EVIDENCE_OUT"

# Explicit teardown; only disarm the trap after it succeeds.
"$SCRIPT_DIR/delete-content-safety.sh"
trap - EXIT
