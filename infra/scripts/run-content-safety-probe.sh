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
# ACCOUNT NAME OWNERSHIP: AZ_CONTENT_SAFETY_NAME is a name PREFIX here, not
# the account name. This script appends an unpredictable per-run suffix
# (6 hex characters from /dev/urandom) and exports the resolved name back
# under the same variable, so both child scripts — whose own interface is
# unchanged, they still take AZ_CONTENT_SAFETY_NAME as the final name — act
# on the same resolved name.
#
# This is a correctness property, not cosmetics. The pre-existence guard in
# create-content-safety.sh and the create itself are separate operations: a
# name that was free at guard time can be taken by something else before
# create runs, and that create then fails with a generic exit 1 — not the
# guard's exit 3 — so CREATE_REFUSED stays 0 and the cleanup trap below
# delete+PURGEs whatever now holds that name. Purge is irreversible. Because
# only this run could have invented the resolved name, cleanup can only ever
# target a resource this run created: the collision class disappears rather
# than being narrowed. It is also what makes the stabilization wait in
# delete-content-safety.sh safe — a bounded wait for a not-yet-visible
# account cannot possibly wait onto somebody else's resource.
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID     - target subscription (never rely on default context)
#   AZ_RESOURCE_GROUP      - existing resource group
#   AZ_CONTENT_SAFETY_NAME - account name PREFIX (see above); must be 1-57
#                             characters of alphanumerics and hyphens,
#                             starting and ending with an alphanumeric, so
#                             prefix + "-" + 6 hex fits the 64-character
#                             Microsoft.CognitiveServices/accounts limit
#                             (checked 2026-08)
#   EVIDENCE_OUT            - path the probe writes its evidence JSON to; if
#                              relative, it is resolved against the CALLER's
#                              cwd (see cwd note below), not the repo root
# Optional env vars:
#   AZ_LOCATION               - defaults to japaneast
#   AZ_CONTENT_SAFETY_SKU     - defaults to F0; the SKU requested when
#                                creating the account (see its own default
#                                below). The probe's evidence header does NOT
#                                record this requested value -- after create
#                                returns, this script reads back the
#                                account's ACTUAL SKU via `account show` and
#                                forwards that instead, so a fallback to S0
#                                inside create-content-safety.sh's own child
#                                process is reflected accurately
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
# The SKU requested when creating the account -- mirrors create-content-
# safety.sh's own default. Not what gets forwarded to the probe: that value
# is read back from the account itself (ACTUAL_SKU below), specifically so a
# create-time fallback to S0 inside create-content-safety.sh's own child
# process is never missed.
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

# --- resolve the per-run account name (before anything is queried) ---------
# Microsoft.CognitiveServices/accounts: 2-64 characters, alphanumerics and
# hyphens, start and end with an alphanumeric (Azure resource naming rules,
# checked 2026-08). The suffix costs 7 characters ("-" + 6 hex), so the
# prefix must be at most 57. Too long is a hard failure, never a silent
# truncation: a truncated prefix could collide with a name that is not ours,
# which is exactly the ownership property this whole mechanism exists to
# guarantee.
CS_NAME_PREFIX="$AZ_CONTENT_SAFETY_NAME"
CS_MAX_NAME_LENGTH=64
CS_SUFFIX_LENGTH=6
CS_MAX_PREFIX_LENGTH=$((CS_MAX_NAME_LENGTH - CS_SUFFIX_LENGTH - 1))
if [ "${#CS_NAME_PREFIX}" -gt "$CS_MAX_PREFIX_LENGTH" ]; then
  echo "AZ_CONTENT_SAFETY_NAME is a name PREFIX here: this script appends a ${CS_SUFFIX_LENGTH}-character per-run suffix." >&2
  echo "'$CS_NAME_PREFIX' is ${#CS_NAME_PREFIX} characters; the limit is $CS_MAX_PREFIX_LENGTH (Cognitive Services account names cap at $CS_MAX_NAME_LENGTH)." >&2
  echo "Shorten it — this script will not truncate, because a truncated prefix could collide with an account this run does not own." >&2
  exit 1
fi
if ! [[ "$CS_NAME_PREFIX" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]]; then
  echo "AZ_CONTENT_SAFETY_NAME ('$CS_NAME_PREFIX') must be alphanumerics and hyphens only, starting and ending with an alphanumeric." >&2
  echo "That is the Microsoft.CognitiveServices/accounts naming rule; the name is also used as the account's custom subdomain." >&2
  exit 1
fi
# Cryptographically unpredictable, not $RANDOM (seeded, guessable) and not a
# timestamp (two runs started in the same second would collide, and the value
# is trivially predictable by anything else creating accounts).
CS_RUN_SUFFIX=$(od -An -vN3 -tx1 /dev/urandom | tr -d ' \n') \
  || { echo "Failed to read random bytes for the per-run account-name suffix; aborting rather than falling back to a predictable value." >&2; exit 1; }
if ! [[ "$CS_RUN_SUFFIX" =~ ^[0-9a-f]{6}$ ]]; then
  echo "Unexpected random suffix '$CS_RUN_SUFFIX'; aborting rather than creating an account under an unverified name." >&2
  exit 1
fi
# Exported so BOTH child scripts see the resolved name under the variable
# they already read — their interface does not change.
export AZ_CONTENT_SAFETY_NAME="${CS_NAME_PREFIX}-${CS_RUN_SUFFIX}"
echo "This run's Content Safety account name: $AZ_CONTENT_SAFETY_NAME (prefix '$CS_NAME_PREFIX' + per-run suffix)"

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

# Tells delete-content-safety.sh that a create was ISSUED under this name and
# its outcome may be unknown, so a single reading of "in neither listing" is
# not proof of absence — it must wait (bounded) before concluding there is
# nothing to tear down. Exported BEFORE the create call, precisely because
# the case it covers is a create whose result never came back: setting it
# afterwards would leave it unset on exactly the path that needs it. Safe by
# construction because the name is unique to this run — the wait can only
# ever be waiting on our own account. It is unset when someone runs
# delete-content-safety.sh standalone, which keeps the genuinely-absent fast
# path free of a pointless wait.
export AZ_CS_CREATE_ATTEMPTED=1

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
# Read back the ACTUAL sku, not the requested AZ_CONTENT_SAFETY_SKU: if
# create-content-safety.sh fell back from F0 to S0 internally, that
# assignment happened in its own child process and never reached this
# script's copy of the variable. Forwarding the requested value to the probe
# would then record a SKU the account was never actually created with.
ACTUAL_SKU=$(az cognitiveservices account show --name "$AZ_CONTENT_SAFETY_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP" --subscription "$AZ_SUBSCRIPTION_ID" \
  --query sku.name -o tsv) \
  || { echo "Failed to read back the account SKU (see error above)." >&2; exit 1; }
KEY=$(az cognitiveservices account keys list --name "$AZ_CONTENT_SAFETY_NAME" \
  --resource-group "$AZ_RESOURCE_GROUP" --subscription "$AZ_SUBSCRIPTION_ID" \
  --query key1 -o tsv) \
  || { echo "Failed to read back the account key (see error above)." >&2; exit 1; }

# cd to the repo root so `uv run python -m tools.prompt_shields_probe`
# resolves regardless of the caller's cwd (see cwd note above). Every path
# used from here on (CASES_FILE, EVIDENCE_OUT) is already absolute.
cd "$REPO_ROOT"
CONTENT_SAFETY_ENDPOINT="$ENDPOINT" CONTENT_SAFETY_KEY="$KEY" \
  AZ_LOCATION="$AZ_LOCATION" AZ_CONTENT_SAFETY_SKU="$ACTUAL_SKU" \
  uv run python -m tools.prompt_shields_probe --cases-file "$CASES_FILE" --evidence-out "$EVIDENCE_OUT"

# Explicit teardown; only disarm the trap after it succeeds.
"$SCRIPT_DIR/delete-content-safety.sh"
trap - EXIT
