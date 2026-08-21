#!/usr/bin/env bash
# Tear down what create-app-insights.sh created, and nothing else.
#
# Order is a contract: the component first, then the workspace it points at.
# The reverse leaves a component bound to a resource that no longer exists.
#
# The workspace is deleted only when the record file says law_owned=true.
# deploy-container-app.sh creates a workspace of its own for the Container Apps
# environment, and delete-container-app.sh deletes it by name; if this script
# also claimed it, whichever teardown ran second would abort fail-closed on a
# resource that was already gone. Day 24 recorded what an aborted teardown
# leaves behind -- an identity and three role assignments nobody could find.
#
# A record file missing law_owned is a hard failure, not a default. Guessing
# true deletes someone else's workspace; guessing false leaves a monthly bill
# under a name no future run will know. Neither is a safe assumption to make
# quietly.
#
# Required env vars:
#   AZ_RECORD_FILE - the record create-app-insights.sh wrote; defaults to
#                    ./.app-insights-record.env
# Optional env vars:
#   AZ_PURGE_SECRET - "true" purges the soft-deleted Key Vault secret after
#                     deleting it. Default false: the vault this lab uses has
#                     purge protection off (create-keyvault.sh) so a purge is
#                     possible, but a name freed early is rarely what an
#                     operator wants mid-session.
set -euo pipefail

AZ_RECORD_FILE="${AZ_RECORD_FILE:-./.app-insights-record.env}"
SECRET_NAME="applicationinsights-connection-string"

if [ ! -f "$AZ_RECORD_FILE" ]; then
  echo "No record file at $AZ_RECORD_FILE." >&2
  echo "Nothing is deleted: this script only removes resources it has a record of." >&2
  echo "If a session left resources behind, list them by name and remove them by hand." >&2
  exit 0
fi

# shellcheck disable=SC1090
. "$AZ_RECORD_FILE"

: "${AZ_SUBSCRIPTION_ID:?record file is missing AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?record file is missing AZ_RESOURCE_GROUP}"
: "${AZ_APPINSIGHTS_NAME:?record file is missing AZ_APPINSIGHTS_NAME}"

if [ -z "${law_owned:-}" ]; then
  echo "Record file has no law_owned flag; refusing to guess." >&2
  echo "true would delete a workspace this session may not own; false would leave" >&2
  echo "a billed resource behind. Set it deliberately and rerun." >&2
  exit 1
fi

require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (az returned empty output); aborting." >&2
    exit 1
  fi
}

# Bounded wait, not a single read-back. Day 24 and Day 25 both hit "the delete
# returned and the resource is still listed" on Azure -- ScheduledForDelete on
# a Container Apps environment, then a CLI long-poll timing out while the
# operation continued server-side. Assume it here rather than learn it a third
# time: fail-closed is right, but "still there" needs a deadline attached.
# Poll knobs exist so the fake-CLI regressions run in milliseconds while the
# production defaults stay at a multi-minute deadline -- the same split
# deploy-container-app.sh uses for its readiness gate. Validated as integers
# before use: `for _ in $(seq 1 "$KNOB")` silently runs zero iterations on a
# malformed knob, which Day 21 learned the expensive way.
APPI_WAIT_SECONDS="${APPI_WAIT_SECONDS:-120}"
APPI_WAIT_INTERVAL="${APPI_WAIT_INTERVAL:-5}"
case "$APPI_WAIT_SECONDS" in ''|*[!0-9]*) echo "APPI_WAIT_SECONDS must be an integer" >&2; exit 1 ;; esac
case "$APPI_WAIT_INTERVAL" in ''|*[!0-9]*) echo "APPI_WAIT_INTERVAL must be an integer" >&2; exit 1 ;; esac

wait_until_gone() {
  local label="$1" query="$2"
  local deadline=$((SECONDS + APPI_WAIT_SECONDS))
  while true; do
    local count
    count=$(eval "$query")
    require_value "$count" "the remaining $label count"
    if [ "$count" = "0" ]; then
      echo "  $label: gone"
      return 0
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "$label still present ${APPI_WAIT_SECONDS}s after delete returned; aborting." >&2
      echo "Nothing further is deleted -- the order below depends on this step." >&2
      exit 1
    fi
    sleep "$APPI_WAIT_INTERVAL"
  done
}

# === step 1: Application Insights component ==================================
echo "== step 1: delete the Application Insights component =="
az monitor app-insights component delete \
  --subscription "$AZ_SUBSCRIPTION_ID" --resource-group "$AZ_RESOURCE_GROUP" \
  --app "$AZ_APPINSIGHTS_NAME" -o none 2>/dev/null || true

wait_until_gone "component $AZ_APPINSIGHTS_NAME" \
  "az monitor app-insights component show \
     --subscription '$AZ_SUBSCRIPTION_ID' --resource-group '$AZ_RESOURCE_GROUP' \
     --app '$AZ_APPINSIGHTS_NAME' -o tsv >/dev/null 2>&1 && echo 1 || echo 0"

# === step 2: the Key Vault secret ============================================
echo "== step 2: delete the connection-string secret =="
if [ -z "${AZ_KEYVAULT_NAME:-}" ]; then
  echo "  skipped: record file names no vault"
else
  az keyvault secret delete --subscription "$AZ_SUBSCRIPTION_ID" \
    --vault-name "$AZ_KEYVAULT_NAME" --name "$SECRET_NAME" -o none 2>/dev/null || true
  if [ "${AZ_PURGE_SECRET:-false}" = "true" ]; then
    az keyvault secret purge --subscription "$AZ_SUBSCRIPTION_ID" \
      --vault-name "$AZ_KEYVAULT_NAME" --name "$SECRET_NAME" -o none 2>/dev/null || true
    echo "  purged"
  else
    echo "  soft-deleted (set AZ_PURGE_SECRET=true to purge)"
  fi
fi

# === step 3: the Log Analytics workspace, if this session created it =========
echo "== step 3: Log Analytics workspace =="
if [ "$law_owned" != "true" ]; then
  echo "  skipped: law_owned=$law_owned -- ${AZ_LAW_NAME:-the workspace} belongs to another script"
else
  : "${AZ_LAW_NAME:?record file says law_owned=true but names no workspace}"
  az monitor log-analytics workspace delete --subscription "$AZ_SUBSCRIPTION_ID" \
    --resource-group "$AZ_RESOURCE_GROUP" --workspace-name "$AZ_LAW_NAME" \
    --yes --force true -o none 2>/dev/null || true

  wait_until_gone "workspace $AZ_LAW_NAME" \
    "az monitor log-analytics workspace list \
       --subscription '$AZ_SUBSCRIPTION_ID' --resource-group '$AZ_RESOURCE_GROUP' \
       --query \"length([?name=='$AZ_LAW_NAME'])\" -o tsv"
fi

rm -f "$AZ_RECORD_FILE"
echo
echo "Teardown complete; record file removed."
