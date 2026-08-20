#!/usr/bin/env bash
# Point an existing Container App at a new image and refuse to report success
# unless three mechanical facts hold afterwards (see step 3/4 below). Note
# what is NOT among them: this script cannot establish which revision is
# actually serving traffic. This is the script the CI/CD deploy
# job runs -- deploy-container-app.sh creates the app once; every deploy after
# that goes through here.
#
# The image is identified by digest (--image <acr>.azurecr.io/repo@sha256:...),
# because the whole point of the pipeline is that the bytes that passed the
# gates are the bytes that get deployed. The value passed to --image reaches
# `az containerapp update` completely unmodified: no tag parsing, no
# normalization, no appending ":latest".
#
# Four steps, in order:
#   1. Pre-mutation snapshot -- read the app's CURRENT TEMPLATE image,
#      exactly as Azure stores it. This is the desired-state field, not
#      proof of what any revision is serving: if an earlier deploy left a
#      failed new revision while the previous one kept serving, the template
#      already holds that failed image. It is a rollback candidate, nothing
#      stronger. The first deployment (by
#      deploy-container-app.sh) writes a TAG (azgenai-lab:${IMAGE_TAG}), so
#      this snapshot is sometimes a tag and sometimes a digest. It is stored
#      and echoed verbatim, never assumed to be one or the other.
#   2. `az containerapp update --image <ref>`.
#   3. Fail-closed read-backs: the app's template reports the requested image,
#      and the latest revision's runningState is watched as a FAILURE
#      DETECTOR -- a known failure state aborts, "Processing" keeps waiting,
#      and any other value (including vocabulary this project has not seen)
#      is treated as "not evidence of failure", never as proof of success.
#      This script does NOT read `active` or `provisioningState`; the only
#      revision field it queries is runningState.
#   4. Data-plane smoke: /health, polled with a bounded deadline, must return
#      the exact body.
#
# So "success" here means: the control plane accepted the requested image and
# echoes it back, nothing reported a known failure state, and the app answers
# /health with the exact expected body. The step-1 snapshot is rollback data,
# not part of that determination. See docs/ci-cd.md section 11 for the gap
# this leaves under single revision mode (open item 14).
#
# There is NO automatic rollback. On any failure after step 2, this script
# prints the manual rollback command built from the step-1 snapshot -- and if
# that snapshot was a tag, says so: the printed command only guarantees
# "whatever that tag points at now", which may no longer be the image that
# was actually running when this script started.
#
# This script never reads or prints a secret and never calls `az containerapp
# secret ...` / anything that triggers listSecrets. The deploy identity's role
# does include that action -- the claim here is that this script does not
# touch them, not that it cannot.
#
# The app runs in single-revision mode, so `properties.latestRevisionName`
# names the revision this update produced. That is not the same as knowing it
# is the one answering traffic: what happens to the previous revision when a
# new one fails to start has never been observed here (docs/ci-cd.md section
# 11, open item 14), so this script does not claim the two coincide. A
# multi-revision app would need a different approach again.
#
# Usage: update-container-app.sh --image <ref>
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID   - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP    - resource group holding the app
#   AZ_ACA_APP_NAME       - the existing Container App to update
#
# Optional env vars:
#   ACA_REVISION_POLL_ATTEMPTS / ACA_REVISION_POLL_INTERVAL
#       Bounds on waiting out a revision stuck reporting "Processing" -- the
#       one runningState value this project treats as still-provisioning.
#       Every other value (a known failure, or anything else, including
#       vocabulary this project has not seen yet) ends the wait on its
#       first read, so a healthy deploy no longer burns this budget; it now
#       bounds only a revision that is genuinely still starting up. Default
#       kept at 30 attempts * 10s = up to 5 minutes unchanged from before
#       this project had a live measurement of how long "Processing" is
#       ever legitimately expected to last -- shrinking it without that
#       measurement would trade one guess for another.
#   HEALTH_POLL_ATTEMPTS / HEALTH_POLL_INTERVAL
#       Bounds on the /health probe. The ingress needs a moment after a
#       revision swaps, so this is a wait with a deadline, not a single
#       request. Default 30 attempts * 5s = up to 2.5 minutes.
set -euo pipefail

: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_ACA_APP_NAME:?Set AZ_ACA_APP_NAME}"

IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image)
      if [ $# -lt 2 ]; then
        echo "--image requires a value." >&2
        exit 1
      fi
      IMAGE="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done
if [ -z "$IMAGE" ]; then
  echo "Usage: update-container-app.sh --image <acr>.azurecr.io/repo@sha256:... (or :tag)" >&2
  exit 1
fi

ACA_REVISION_POLL_ATTEMPTS="${ACA_REVISION_POLL_ATTEMPTS:-30}"
ACA_REVISION_POLL_INTERVAL="${ACA_REVISION_POLL_INTERVAL:-10}"
HEALTH_POLL_ATTEMPTS="${HEALTH_POLL_ATTEMPTS:-30}"
HEALTH_POLL_INTERVAL="${HEALTH_POLL_INTERVAL:-5}"

# --- helpers -----------------------------------------------------------------

# An `az ... -o tsv` call that exits nonzero is already caught by `set -e` on
# the assignment. What that does NOT catch is a call that exits 0 and prints
# nothing. An empty read is a failed read: it must abort, never be treated as
# "absent", "zero" or "not yet" (Day 19, Day 21, Day 24 each shipped this bug).
require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (az returned empty output); aborting." >&2
    exit 1
  fi
}

require_count() {
  local name="$1" value="$2"
  if ! [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "$name must be a positive integer; got '$value'." >&2
    exit 1
  fi
}
require_seconds() {
  local name="$1" value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$name must be a non-negative integer number of seconds; got '$value'." >&2
    exit 1
  fi
}
require_count ACA_REVISION_POLL_ATTEMPTS "$ACA_REVISION_POLL_ATTEMPTS"
require_seconds ACA_REVISION_POLL_INTERVAL "$ACA_REVISION_POLL_INTERVAL"
require_count HEALTH_POLL_ATTEMPTS "$HEALTH_POLL_ATTEMPTS"
require_seconds HEALTH_POLL_INTERVAL "$HEALTH_POLL_INTERVAL"

MUTATED=false
SNAPSHOT_IMAGE=""
HEALTH_BODY_FILE=""
on_exit() {
  local status=$?
  if [ -n "$HEALTH_BODY_FILE" ]; then
    rm -f "$HEALTH_BODY_FILE"
  fi
  if [ "$status" -ne 0 ] && [ "$MUTATED" = true ]; then
    echo "" >&2
    echo "update-container-app.sh failed (exit $status) after requesting the image change." >&2
    echo "No automatic rollback is performed. To roll back manually:" >&2
    # `az` reads none of AZ_SUBSCRIPTION_ID / AZ_RESOURCE_GROUP -- those are
    # this repo's own script-level conventions, not az env fallbacks (az
    # configure --defaults group=... is the only alternative az itself
    # documents). The printed line must therefore carry --subscription and
    # --resource-group as flags, or it fails with a missing-argument error
    # exactly when an operator is under pressure and least likely to debug
    # the recovery instruction itself -- the same shape as Day 24's teardown
    # printer that omitted two knobs.
    echo "  az containerapp update --subscription $AZ_SUBSCRIPTION_ID --resource-group $AZ_RESOURCE_GROUP \\" >&2
    echo "    --name $AZ_ACA_APP_NAME --image $SNAPSHOT_IMAGE" >&2
    case "$SNAPSHOT_IMAGE" in
      *@sha256:*) ;;
      *)
        echo "  Warning: that snapshot is a TAG reference, not a digest. The command" >&2
        echo "  above redeploys whatever the tag resolves to right NOW, which may no" >&2
        echo "  longer be the image that was actually running when this script started." >&2
        ;;
    esac
  fi
}
trap on_exit EXIT

az account set --subscription "$AZ_SUBSCRIPTION_ID"

# === step 1: pre-mutation snapshot ===========================================
echo "== step 1: pre-mutation snapshot =="
# Read exactly as stored -- no normalization, no assumption it is a digest.
# The first deployment (deploy-container-app.sh) writes a tag; later runs of
# this script write a digest. Both forms have to survive this read unchanged.
SNAPSHOT_IMAGE=$(az containerapp show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACA_APP_NAME" \
  --query "properties.template.containers[0].image" -o tsv)
require_value "$SNAPSHOT_IMAGE" "the app's current template image reference"
echo "  prior template image (rollback candidate): $SNAPSHOT_IMAGE"

# === step 2: update ==========================================================
echo "== step 2: update the image =="
echo "  requesting: $IMAGE"
# From here on, an Azure-side mutation has been requested; a failure below
# prints the rollback hint above.
MUTATED=true
az containerapp update \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACA_APP_NAME" \
  --image "$IMAGE" >/dev/null

# === step 3: fail-closed read-backs ==========================================
echo "== step 3: verify the update landed =="

NEW_IMAGE=$(az containerapp show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACA_APP_NAME" \
  --query "properties.template.containers[0].image" -o tsv)
require_value "$NEW_IMAGE" "the updated image reference"
if [ "$NEW_IMAGE" != "$IMAGE" ]; then
  echo "The app reports image '$NEW_IMAGE' but '$IMAGE' was requested; aborting." >&2
  exit 1
fi
echo "  app reports the requested image"

REVISION_NAME=$(az containerapp show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACA_APP_NAME" \
  --query "properties.latestRevisionName" -o tsv)
require_value "$REVISION_NAME" "the latest revision name"

# Which field authoritatively reports "the new revision failed to start" was
# an open question this project had not settled, resolved by the Task 10
# live session (2026-08-20, japaneast). It settled the field choice, not the
# vocabulary: properties.runningState is still the most specific field
# `az containerapp revision show` exposes for whether the container is
# actually running, more specific than the app-level
# properties.provisioningState (which reflects ARM-level resource
# provisioning and would not obviously reflect a container that provisioned
# fine but crashed on startup). What the live session actually observed is
# that a healthy, correctly-deployed single-replica revision reported
# runningState "RunningAtMaxScale", not "Running"; a second deployment in the
# same session reported "Activating". Neither string appears in the SDK enum
# shipped by the containerapp CLI extension installed on that machine
# (azext_containerapp/_sdk_enums.py, RevisionRunningState, which lists only
# Running / Processing / Stopped / Degraded / Failed / Unknown). Consulting
# that enum before writing this check would have produced the exact same bug:
# the service returned values its own installed SDK does not enumerate, twice,
# so an allow-list of "success" strings cannot be grounded in it. That is what
# these two observations establish -- not what the service's full vocabulary
# is, and not that any particular further value will appear.
#
# The check is therefore failure-shaped, not success-shaped: it fails fast
# on the two states this project has evidence are terminal failures
# (Failed, Degraded -- no point burning the poll budget on those), keeps
# waiting only through "Processing" (the one state named for still being
# under way), and treats every other value -- Running, RunningAtMaxScale,
# Stopped, Unknown, and whatever undocumented string Azure returns next --
# as not evidence of failure. It does not treat that as proof of success
# either. What stands in for success is the rest of what this script actually
# checks: the step-3 read-back that the app's template now carries the exact
# requested image, and step 4's exact-body /health probe. Those two, plus the
# absence of a known failure state here, are the whole of it -- there is no
# `active` or `provisioningState` read-back in this script, and describing one
# would be describing code that does not exist. One caveat this does not
# close: this app runs in
# single revision mode, and it has never been observed here what happens when
# a new revision fails to start -- whether /health would then be answered by
# the previous revision, returning the expected body for the wrong reason.
# See docs/ci-cd.md section 11 ("Still open").
RUNNING_STATE=""
for ((ATTEMPT = 1; ATTEMPT <= ACA_REVISION_POLL_ATTEMPTS; ATTEMPT++)); do
  RUNNING_STATE=$(az containerapp revision show \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --name "$AZ_ACA_APP_NAME" \
    --revision "$REVISION_NAME" \
    --query "properties.runningState" -o tsv)
  require_value "$RUNNING_STATE" "the new revision running state"
  if [ "$RUNNING_STATE" = "Failed" ] || [ "$RUNNING_STATE" = "Degraded" ]; then
    echo "Revision '$REVISION_NAME' runningState is '$RUNNING_STATE'; aborting." >&2
    exit 1
  fi
  if [ "$RUNNING_STATE" != "Processing" ]; then
    break
  fi
  if ((ATTEMPT < ACA_REVISION_POLL_ATTEMPTS)); then
    sleep "$ACA_REVISION_POLL_INTERVAL"
  fi
done
if [ "$RUNNING_STATE" = "Processing" ]; then
  echo "Revision '$REVISION_NAME' runningState is still 'Processing' after $ACA_REVISION_POLL_ATTEMPTS attempts; aborting." >&2
  exit 1
fi
echo "  revision '$REVISION_NAME' reports runningState '$RUNNING_STATE' (not a known failure state; /health decides next)"

# === step 4: data-plane smoke ================================================
echo "== step 4: /health smoke =="
FQDN=$(az containerapp show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$AZ_ACA_APP_NAME" \
  --query "properties.configuration.ingress.fqdn" -o tsv)
require_value "$FQDN" "the app ingress FQDN"
BASE_URL="https://${FQDN}"
echo "  fqdn: $FQDN"
echo "  probing ${BASE_URL}/health"

EXPECTED_BODY='{"status":"ok","service":"azure-genai-backend-lab"}'
HEALTH_BODY_FILE="$(mktemp)"
HTTP_CODE=""
HEALTH_OK=false
for ((ATTEMPT = 1; ATTEMPT <= HEALTH_POLL_ATTEMPTS; ATTEMPT++)); do
  # The status is compared explicitly, and the body is written to a file
  # rather than parsed out of a combined response -- both avoid the fragile
  # string-splitting a single curl -w output would need. Same -sS/--max-time
  # discipline deploy-container-app.sh's gate 2 uses.
  if HTTP_CODE=$(curl -sS -o "$HEALTH_BODY_FILE" -w '%{http_code}' --max-time 10 "${BASE_URL}/health"); then
    if [ "$HTTP_CODE" = "200" ] && [ "$(cat "$HEALTH_BODY_FILE")" = "$EXPECTED_BODY" ]; then
      HEALTH_OK=true
      break
    fi
  fi
  if ((ATTEMPT < HEALTH_POLL_ATTEMPTS)); then
    sleep "$HEALTH_POLL_INTERVAL"
  fi
done
if [ "$HEALTH_OK" != true ]; then
  echo "/health did not return the expected body after $HEALTH_POLL_ATTEMPTS attempts (last status: ${HTTP_CODE:-none})." >&2
  exit 1
fi
echo "  /health returned the expected body"

cat <<SUMMARY

Update requested and read back. Verified, precisely:
  - the app template now reports the requested image
  - the latest revision reported no known failure state
  - /health returned the exact expected body
Not verified: which revision served that /health response. Under single
revision mode with an image-pull failure, that is an open question -- see
docs/ci-cd.md section 11.
  app:   $AZ_ACA_APP_NAME
  url:   $BASE_URL
  image: $IMAGE
SUMMARY
