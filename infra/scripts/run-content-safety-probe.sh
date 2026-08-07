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
# ONE exception to "the trap always tears down": create-content-safety.sh
# exits 3 when its pre-existence guard refuses because an account already
# exists under this name (live or soft-deleted). That guard runs before any
# mutation, so this run created nothing — skipping teardown then avoids
# purging an account it never created. Every other non-zero status still
# gets the normal delete+purge cleanup.
#
# That refusal is detected via a dedicated CREATE_REFUSED flag set ONLY at
# the create-content-safety.sh call site below, never by comparing the
# trap's bare `$status` to 3. `$status` in the trap is whatever the LAST
# failing command returned, and other commands in this script — notably
# delete-content-safety.sh's own internal `az ... delete` calls — can also
# exit 3 for unrelated reasons (Azure CLI's own resource-not-found status).
# If the explicit end-of-run teardown near the bottom of this script fails
# with exit 3, a bare `status -eq 3` check would misread that as "create
# refused, nothing to tear down" and skip the retry — even though create
# succeeded and an account this run made is still live.
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
#   AZ_CONTENT_SAFETY_SKU     - defaults to F0; forwarded to the probe so its
#                                evidence header can record what SKU was
#                                asked for (see the caveat at its own default
#                                below if create-content-safety.sh falls back
#                                to S0 internally)
#   PROMPT_SHIELDS_CASES_FILE - defaults to tools/prompt_shields_cases.json
#                                (the canonical fixture) at the repo root;
#                                a relative override is also resolved
#                                against the CALLER's cwd (see cwd note below)
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
# Mirrors create-content-safety.sh's own default so the probe's evidence
# header can record the SKU it asked for. Known gap: if that script falls
# back from F0 to S0 internally (its own retry, inside a child process),
# this value is not updated to match -- the evidence header can be stale
# in that one case, but it is never fabricated when the var is unset.
: "${AZ_CONTENT_SAFETY_SKU:=F0}"
CALLER_DIR="$PWD"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CASES_FILE="${PROMPT_SHIELDS_CASES_FILE:-$REPO_ROOT/tools/prompt_shields_cases.json}"
EVIDENCE_OUT="${EVIDENCE_OUT:?Set EVIDENCE_OUT}"
# Absolutize against the caller's cwd BEFORE the later `cd "$REPO_ROOT"` —
# otherwise a relative CASES_FILE or EVIDENCE_OUT would silently be
# reinterpreted against the repo root instead of where the caller actually
# meant it. The default CASES_FILE is already absolute (built from
# REPO_ROOT), so this is a no-op unless PROMPT_SHIELDS_CASES_FILE was
# overridden with a relative path.
if [[ "$CASES_FILE" != /* ]]; then
  CASES_FILE="$CALLER_DIR/$CASES_FILE"
fi
if [[ "$EVIDENCE_OUT" != /* ]]; then
  EVIDENCE_OUT="$CALLER_DIR/$EVIDENCE_OUT"
fi

# Validate inputs BEFORE any mutation.
[ -r "$CASES_FILE" ] || { echo "cases file not readable: $CASES_FILE" >&2; exit 1; }

# Set BEFORE the trap is armed (so the trap never reads an unset variable
# under `set -u` on any early-exit path) and flipped ONLY at the
# create-content-safety.sh call site below — see the block comment above for
# why this must not be inferred from a bare exit-status comparison.
CREATE_REFUSED=0

cleanup() {
  status=$?
  # CREATE_REFUSED is set only when create-content-safety.sh itself returned
  # exit 3 (its pre-existence guard refusing because an account already
  # existed under this name, live or soft-deleted). That guard runs before
  # any mutation, so this run created nothing — purging here would destroy
  # an account this run never owned, so skip teardown. See the matching
  # comment in create-content-safety.sh for the full contract. Every other
  # failure — including a `delete-content-safety.sh` exit 3 from its own
  # internal az call — still needs the normal delete+purge cleanup.
  if [ "$CREATE_REFUSED" = 1 ]; then
    echo "cleanup: create-content-safety.sh refused (account already existed) — skipping teardown, nothing was created." >&2
  else
    "$SCRIPT_DIR/delete-content-safety.sh" || echo "cleanup: delete-content-safety.sh failed" >&2
  fi
  exit "$status"   # preserve the original failure status
}
trap cleanup EXIT   # armed BEFORE create can leave a resource behind

# `$?` inside this `||` group is create-content-safety.sh's own exit status
# (the `if ! cmd; then` shape would give 0 instead, losing that status).
# CREATE_REFUSED is set here and ONLY here, so no later command's unrelated
# exit-3 can be mistaken for this refusal.
"$SCRIPT_DIR/create-content-safety.sh" \
  || { create_status=$?; [ "$create_status" -eq 3 ] && CREATE_REFUSED=1; exit "$create_status"; }

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
  AZ_LOCATION="$AZ_LOCATION" AZ_CONTENT_SAFETY_SKU="$AZ_CONTENT_SAFETY_SKU" \
  uv run python -m tools.prompt_shields_probe --cases-file "$CASES_FILE" --evidence-out "$EVIDENCE_OUT"

# Explicit teardown; only disarm the trap after it succeeds.
"$SCRIPT_DIR/delete-content-safety.sh"
trap - EXIT
