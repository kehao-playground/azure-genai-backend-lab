#!/usr/bin/env bash
# Tear down what create-github-oidc.sh created, reading every identifier it
# needs from OIDC_RECORD_FILE -- the same file, same contract as
# delete-container-app.sh reading nothing from the caller beyond name knobs.
# This script needs no name knobs at all: D10.1 designed the record file to
# carry every id, and re-deriving any of them here (e.g. re-resolving the ACR
# or the container app by name) would silently break the moment those
# resources are gone, which docs/container-apps.md's teardown runbook allows
# to happen in either order relative to this script.
#
# THE ORDER BELOW IS THE CONTRACT, same discipline as create-github-oidc.sh's
# own seven steps and delete-container-app.sh's seven:
#
#   1. DEPLOY_ENABLED=false -- stops the bleeding, IS NOT A GUARANTEE. It
#      only prevents a *new* run from reaching the `if` gate in ci.yml's
#      deploy job. A run already past that check keeps going.
#   2. delete both federated credentials, read back that they are gone. This
#      proves the CONTROL-PLANE OBJECT IS GONE -- Entra will refuse any FUTURE
#      token exchange against that subject. It does NOT prove Entra has begun
#      rejecting exchanges for a token already issued, and that latency is
#      unmeasured by this project. This is safe to do before checking for
#      in-flight runs, precisely because it only blocks NEW exchanges: a run
#      already holding a valid token is untouched by this step.
#   3. drain check -- every run in the repo not in a terminal state.
#      REPO-SCOPED, not workflow-scoped: any workflow can declare
#      `environment: production`, so filtering by workflow name would miss
#      one. Anything non-terminal found -> print it and ABORT before touching
#      role assignments or app registrations. Nothing is ever cancelled here
#      -- deciding to kill someone else's in-flight run is not this script's
#      call, only a human's.
#   4. delete both role assignments, read back zero remaining (`--all` is
#      load-bearing -- see role_assignment_count below, same Day 24 trap
#      delete-container-app.sh's step 6 already documents).
#   5. delete the app registrations -- ASSIGNMENTS BEFORE PRINCIPALS, or the
#      assignments become "Identity not found" orphans with no principal left
#      to look them up by (Day 24's most severe finding, in a different
#      script). Reached only after step 3's drain check and step 4's
#      confirmed-empty role assignments, because deleting the app
#      registration also deletes its service principal, and Azure evaluates
#      role-assignment authorization against a live principal at request
#      time -- pulling that out from under a still-running job would break it
#      immediately, unlike step 2's federated credentials, which only gate
#      future token exchanges. `az ad app delete` only moves a registration
#      into the directory's recycle bin for 30 days, still holding its name;
#      this step also attempts a best-effort purge (ported from
#      delete-entra-app.sh, keyed on the object id create-github-oidc.sh
#      already records) and reports which of the two outcomes it got, the
#      same two-branch message delete-entra-app.sh prints.
#   6. delete the GitHub environment, the seven identifier repository
#      SECRETS create-github-oidc.sh wrote, and the DEPLOY_ENABLED
#      repository VARIABLE itself (not just flipping it to false), read
#      every deletion back.
#
# THERE IS NO VERIFIED ADMISSION LOCK. After step 3's drain check reads
# empty, nothing stops a new `git push` to main or a new workflow_dispatch
# from starting a fresh run before step 5 finishes. For a single-operator
# repo, "do not push during teardown" is PROCESS CONTROL -- a rule the one
# operator follows -- not a technical guarantee this script enforces or could
# enforce without GitHub Actions concurrency controls this project does not
# have wired to teardown. docs/ci-cd.md must not describe this as a lock.
#
# --verify-teardown is a SEPARATE, READ-ONLY mode, not part of the sequence
# above: it re-runs the same existence checks against everything the record
# file names, deletes nothing in Azure or GitHub, and removes the record file
# itself ONLY when every single item was both checkable and confirmed absent.
# A field missing from the record file makes that one item UNVERIFIABLE, not
# silently skipped -- an unverifiable item keeps the record file, same as a
# resource still found live. Run it any time after the main teardown (or
# stand-alone, to confirm a prior run actually finished). For the two app
# registrations, "confirmed absent" also checks the directory's deletedItems
# endpoint by object id -- `az ad app list` alone cannot see a soft-deleted
# app, so without that check this mode would report a registration "gone"
# while it is still recoverable and still holding its name. The same rule
# governs that check itself: no object id to probe with, or a probe that
# fails for any reason other than a confirmed 404, is UNVERIFIABLE. Not
# being able to check is never evidence of absence, and this is the one
# mode that can delete the only record of what was created.
#
# The seven identifier secrets do not have a soft-delete ambiguity the way
# an app registration does -- `gh secret list` either names a secret or it
# does not, with nothing in between. So for those, and for DEPLOY_ENABLED,
# "checkable" only fails the same way every other UNVERIFIABLE case here
# does: the record field naming which secrets were written is itself
# missing. Presence/absence is all teardown needs to prove for a secret;
# it was never able to prove the VALUE at creation time either (see
# create-github-oidc.sh step 6), so nothing is lost here that create-time
# verification already had.
#
# The record file is parsed by hand, one `KEY=VALUE` line at a time -- it is
# deliberately NEVER `source`d. GH_SECRETS_WRITTEN's value is a
# space-separated list of secret names (e.g. "AZURE_TENANT_ID
# AZURE_SUBSCRIPTION_ID ..."); `source`ing that line would hand bash
# `GH_SECRETS_WRITTEN=AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID ...` as a
# command line, setting the env var to only the first token and then trying
# to EXECUTE the remaining tokens as a command. Splitting on the first `=`
# per line sidesteps that entirely.
#
# GH_SECRETS_WRITTEN and DEPLOY_ENABLED_SET are tracked separately in the
# record file (create-github-oidc.sh writes DEPLOY_ENABLED, the one
# repository VARIABLE, as its own final mutation, after the seven secrets
# GH_SECRETS_WRITTEN names) -- so DEPLOY_ENABLED is torn down here
# unconditionally in step 6, not gated on whichever of those two fields
# happens to be present. Step 1 above already guarantees the variable
# exists by the time step 6 runs, regardless of what the record file says
# about it.
#
# The record file does not carry the federated credentials' NAMES, only their
# ids -- `az ad app federated-credential delete`/`list` both accept an id, so
# this is sufficient.
#
# Required env vars:
#   OIDC_RECORD_FILE - path to the record file create-github-oidc.sh wrote.
#                       GITHUB_REPO, AZ_TENANT_ID and AZ_SUBSCRIPTION_ID must
#                       be present in it -- without those three, nothing here
#                       can safely identify what to touch, and the script
#                       aborts before any az/gh call. Every other field is
#                       read independently: a missing one skips (with a
#                       WARNING, never silently) only the item it names.
# Optional env vars:
#   GH_RUN_LIST_LIMIT - how many of the repo's most recent runs the drain
#                        check inspects, default 1000; must be a positive
#                        integer (validated before anything is mutated).
#                        `gh run list` defaults to 20 -- raised explicitly
#                        here so a busy repo does not hide an in-flight run
#                        outside that window. `gh` paginates internally to
#                        satisfy this limit; it is still a bound, not literal
#                        pagination-to-exhaustion -- so step 3 also checks
#                        whether the fetched window came back EXACTLY full
#                        and fails closed on that (an unprovably-truncated
#                        window), rather than trusting a non-terminal count
#                        that might not have covered every run.
#
# Privileges needed: the same directory and role-assignment permissions
# create-github-oidc.sh's header documents, plus `gh auth login` with
# permission to delete repository secrets/variables/environments and read
# workflow runs across the repo.
set -euo pipefail

VERIFY_TEARDOWN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    # The header block, read from the file itself rather than duplicated as a
    # usage() string that drifts from it -- same trick create-github-oidc.sh uses.
    -h|--help) awk 'NR>1 && /^#/ {print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    --verify-teardown) VERIFY_TEARDOWN=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

: "${OIDC_RECORD_FILE:?Set OIDC_RECORD_FILE (the record file create-github-oidc.sh wrote)}"

if [ ! -e "$OIDC_RECORD_FILE" ]; then
  if [ "$VERIFY_TEARDOWN" -eq 1 ]; then
    echo "Record file '$OIDC_RECORD_FILE' does not exist -- already removed, nothing to verify."
    exit 0
  fi
  echo "OIDC_RECORD_FILE '$OIDC_RECORD_FILE' does not exist. Nothing to tear down." >&2
  exit 1
fi

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# An `az`/`gh ... -o tsv`/`--jq` call that exits nonzero is already caught by
# `set -e` on the assignment. What that does NOT catch is a call that exits 0
# and prints nothing -- the empty-output-inside-a-command-substitution class
# `set -e` never sees. An empty read is a failed read: it must abort, never be
# treated as "absent", "zero" or "not yet" (same discipline every other
# script in this directory uses). Every count/presence query below is chosen
# so it ALWAYS prints something on success -- `length([...])` prints a number
# even for zero matches, and the presence checks print the literal "true" or
# "false" -- so an empty result here really is a broken read, not "0 found".
require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (empty output); aborting." >&2
    exit 1
  fi
}

# === parse the record file (never `source`d -- see header) ==================
declare -A RECORD=()
while IFS= read -r line || [ -n "$line" ]; do
  [[ -z "$line" || "$line" != *"="* ]] && continue
  RECORD["${line%%=*}"]="${line#*=}"
done <"$OIDC_RECORD_FILE"

record_get() { printf '%s' "${RECORD[$1]:-}"; }

GITHUB_REPO="$(record_get GITHUB_REPO)"
AZ_TENANT_ID="$(record_get AZ_TENANT_ID)"
AZ_SUBSCRIPTION_ID="$(record_get AZ_SUBSCRIPTION_ID)"

# These three identify WHERE and WHO -- without them nothing below can safely
# target anything, so this is a hard abort, not a skip-with-warning like every
# other field.
if [ -z "$GITHUB_REPO" ] || [ -z "$AZ_TENANT_ID" ] || [ -z "$AZ_SUBSCRIPTION_ID" ]; then
  echo "Record file '$OIDC_RECORD_FILE' is missing GITHUB_REPO, AZ_TENANT_ID or" >&2
  echo "AZ_SUBSCRIPTION_ID. Cannot safely identify anything to tear down from it." >&2
  echo "Fix the record file by hand, or tear the resources down manually." >&2
  exit 1
fi

# Validated here, before any mutation, not where it is first used in step 3:
# this project has shipped a knob that silently ran zero iterations on a
# malformed value (Day 21's `seq` bug) -- a bad GH_RUN_LIST_LIMIT must fail
# loudly up front, not fall through to a comparison that misbehaves quietly.
GH_RUN_LIST_LIMIT="${GH_RUN_LIST_LIMIT:-1000}"
if ! [[ "$GH_RUN_LIST_LIMIT" =~ ^[1-9][0-9]*$ ]]; then
  echo "GH_RUN_LIST_LIMIT must be a positive integer; got '$GH_RUN_LIST_LIMIT'." >&2
  exit 1
fi

# === preflight: tenant, gh auth -- same discipline as create-github-oidc.sh =
if ! ACTIVE_TENANT="$(az account show --query tenantId -o tsv)"; then
  echo "Not signed in to Azure. Run: az login --tenant $AZ_TENANT_ID" >&2
  exit 1
fi
require_value "$ACTIVE_TENANT" "the active az tenant id"
if [[ "$(lower "$ACTIVE_TENANT")" != "$(lower "$AZ_TENANT_ID")" ]]; then
  echo "Active az tenant is $ACTIVE_TENANT, but the record file says $AZ_TENANT_ID." >&2
  echo "Run 'az login --tenant $AZ_TENANT_ID' first. Nothing was torn down." >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Not signed in to gh. Run: gh auth login" >&2
  exit 1
fi

az account set --subscription "$AZ_SUBSCRIPTION_ID"
echo "  default az context now points at $AZ_SUBSCRIPTION_ID"

# === pure read helpers -- NEVER call `exit` (bash runs a command
# substitution in a subshell, so `exit` inside one only kills the subshell,
# silently swallowing the abort). Every abort below happens in a caller that
# invokes these directly, not through `$(...)`. ============================

app_count_by_id() {
  # A plain `az ad app list --filter` query -- it does not require the app to
  # exist, so it never errors on an absent id, just returns an empty result.
  az ad app list --filter "appId eq '$1'" --query "length([])" -o tsv
}
app_count_by_name() {
  az ad app list --filter "displayName eq '$1'" --query "length([])" -o tsv
}
app_id_by_name() {
  az ad app list --filter "displayName eq '$1'" --query "[0].appId" -o tsv
}
app_deleted_item_present() {
  # `az ad app list` never sees a soft-deleted app -- once app_count_by_id
  # reads 0, this is the only way to tell "purged / never existed" apart
  # from "in the recycle bin, still recoverable, still holding its name"
  # (I3). GET on the object id: 200 if it is still there.
  #
  # A non-2xx response is NOT automatically "absent" -- a failed *probe*
  # (network error, a 403 on a tenant that will not let this operator read
  # deletedItems, a malformed id) must not be folded into the same verdict
  # as a genuinely confirmed absence, because the caller (--verify-teardown)
  # uses this to decide whether to delete the only record of what this run
  # created. So exactly one failure shape is read as a confirmed "false" --
  # a 404 -- and everything else comes back "unknown".
  #
  # Recognising that 404 means matching az's own error text, because
  # `az rest` surfaces no status code any other way. The shape comes from
  # azure-cli's source, not from a live capture: `send_raw_request`
  # (azure/cli/core/util.py, azure-cli 2.89.0) does
  # `reason = r.reason; if r.text: reason += '({})'.format(r.text)` and
  # raises that, so a Graph 404 reaches stderr as
  # `Not Found({"error":{"code":"Request_ResourceNotFound",...}})`. Both
  # halves are matched: the HTTP reason phrase and Graph's own error code.
  #
  # THIS MATCH IS FAIL-CLOSED BY CONSTRUCTION. If either text ever changes,
  # a real 404 stops matching and falls into "unknown" -- which makes the
  # item UNVERIFIABLE and KEEPS the record file. The failure mode of a
  # stale pattern here is an over-cautious verdict, never a false "gone".
  local err
  if err="$(az rest --method GET \
      --uri "https://graph.microsoft.com/v1.0/directory/deletedItems/$1" \
      --query id -o tsv 2>&1 >/dev/null)"; then
    echo true
  elif [[ "$err" == *"Request_ResourceNotFound"* || "$err" == *"ERROR: Not Found("* ]]; then
    echo false
  else
    echo unknown
  fi
}
fic_count() {
  # Unlike app_count_by_id, this DOES require the app itself to resolve --
  # `--id` on federated-credential list/show/delete triggers an app lookup as
  # part of resolving the nested resource. Callers check app_count_by_id
  # first so a rerun after the app is already gone does not hit this.
  az ad app federated-credential list --id "$1" \
    --query "length([?id=='$2'])" -o tsv
}
role_assignment_count() {
  # --all is LOAD-BEARING, not noise -- same Day 24 trap
  # delete-container-app.sh's step 6 documents at length: `az role assignment
  # list` documents its own default as subscription-scope only. Every
  # assignment create-github-oidc.sh grants is resource-scoped (the ACR, the
  # container app), so without --all this returns 0 unconditionally and every
  # check built on it -- the existence check, the post-delete confirmation,
  # and --verify-teardown -- is vacuous.
  az role assignment list --subscription "$AZ_SUBSCRIPTION_ID" \
    --assignee-object-id "$1" --all --fill-principal-name false \
    --role "$2" --scope "$3" --query "length([])" -o tsv
}
env_present() {
  # Prints the literal "true"/"false", never an empty string -- an empty
  # `.environments` array (a repo with zero environments) must not look like
  # a broken read the way a raw name-list join would.
  gh api "repos/${GITHUB_REPO}/environments" \
    --jq "([.environments[]?.name] | index(\"$1\")) != null"
}
variable_present() {
  gh variable list --repo "$GITHUB_REPO" --json name \
    --jq "([.[].name] | index(\"$1\")) != null"
}
secret_present() {
  # `gh secret list` returns name and timestamps only, never a value -- that
  # is exactly the limit create-github-oidc.sh's own step 6 documents. It is
  # still enough for teardown: presence/absence is unambiguous for a secret
  # (no soft-delete state the way an app registration has), so this is a
  # complete check for what deletion needs to prove, not a weakened one.
  gh secret list --repo "$GITHUB_REPO" --json name \
    --jq "([.[].name] | index(\"$1\")) != null"
}

# === mutating steps -- called directly (never via `$(...)`), so their own
# `exit 1` calls actually terminate the script. ==============================

teardown_fic() {
  local label="$1" app_id="$2" fic_id="$3"
  if [ -z "$app_id" ] || [ -z "$fic_id" ]; then
    echo "WARNING: $label federated credential not fully recorded (app id or" >&2
    echo "  credential id missing) -- skipping its explicit deletion. If the app" >&2
    echo "  registration itself gets deleted in step 5, its federated credentials" >&2
    echo "  go with it -- this only affects the EARLY revocation this step exists" >&2
    echo "  for, not final cleanup completeness." >&2
    return 0
  fi
  local app_exists
  app_exists="$(app_count_by_id "$app_id")"
  require_value "$app_exists" "the $label app existence check (for its federated credential)"
  if [ "$app_exists" = "0" ]; then
    echo "  $label app registration no longer exists -- its federated credential is already gone with it"
    return 0
  fi
  local before
  before="$(fic_count "$app_id" "$fic_id")"
  require_value "$before" "the $label federated credential existence check"
  if [ "$before" = "0" ]; then
    echo "  $label federated credential already gone -- nothing to do"
    return 0
  fi
  az ad app federated-credential delete --id "$app_id" --federated-credential-id "$fic_id"
  local after
  after="$(fic_count "$app_id" "$fic_id")"
  require_value "$after" "the $label federated credential post-delete count"
  if [ "$after" != "0" ]; then
    echo "Federated credential for $label ($fic_id) still listed after delete." >&2
    exit 1
  fi
  echo "  $label federated credential deleted -- control-plane object confirmed gone (does NOT prove Entra has begun rejecting a token already issued)"
}

teardown_role_assignment() {
  local label="$1" assignment_id="$2" sp_id="$3" role="$4" scope="$5"
  if [ -z "$sp_id" ] || [ -z "$scope" ]; then
    echo "WARNING: $label role assignment not fully recorded (principal id or scope" >&2
    echo "  missing) -- skipping. Check manually for a leftover '$role' assignment." >&2
    return 0
  fi
  local before
  before="$(role_assignment_count "$sp_id" "$role" "$scope")"
  require_value "$before" "the $label role assignment existence check"
  if [ "$before" = "0" ]; then
    echo "  $label role assignment already gone -- nothing to do"
    return 0
  fi
  if [ -n "$assignment_id" ]; then
    az role assignment delete --subscription "$AZ_SUBSCRIPTION_ID" --ids "$assignment_id" >/dev/null
  else
    echo "WARNING: $label role assignment id not recorded -- deleting by principal/role/scope match instead." >&2
    az role assignment delete --subscription "$AZ_SUBSCRIPTION_ID" \
      --assignee-object-id "$sp_id" --role "$role" --scope "$scope" >/dev/null
  fi
  local after
  after="$(role_assignment_count "$sp_id" "$role" "$scope")"
  require_value "$after" "the $label role assignment post-delete count"
  if [ "$after" != "0" ]; then
    echo "$label role assignment still listed at $scope after delete (checked with --all)." >&2
    exit 1
  fi
  echo "  $label role assignment deleted -- confirmed zero remain (--all)"
}

teardown_app() {
  local label="$1" recorded_id="$2" display_name="$3" recorded_object_id="$4"
  local app_id="$recorded_id"
  if [ -z "$app_id" ]; then
    if [ -z "$display_name" ]; then
      echo "WARNING: $label app registration not recorded (no app id, no display" >&2
      echo "  name) -- skipping. Check manually for a leftover app registration." >&2
      return 0
    fi
    echo "  $label app id not recorded; falling back to a display-name lookup for '$display_name'"
    local count
    count="$(app_count_by_name "$display_name")"
    require_value "$count" "the $label display-name lookup count"
    if [ "$count" = "0" ]; then
      echo "  no application named '$display_name' exists -- nothing to do"
      return 0
    fi
    if [ "$count" != "1" ]; then
      # Entra ID display names are NOT unique. Guessing which of several
      # same-named applications is ours risks deleting the wrong one --
      # abort instead, same discipline as the drain check: when this script
      # cannot be sure, it stops rather than acts.
      echo "Found $count applications named '$display_name'. Cannot tell which one is ours." >&2
      echo "Aborting rather than guessing -- delete the right one by object id yourself." >&2
      exit 1
    fi
    app_id="$(app_id_by_name "$display_name")"
    require_value "$app_id" "the $label resolved app id"
  fi

  local before
  before="$(app_count_by_id "$app_id")"
  require_value "$before" "the $label app existence check"
  if [ "$before" = "0" ]; then
    echo "  $label app registration already gone -- nothing to do"
    return 0
  fi

  # Resolved BEFORE the delete, not after: once the app is gone,
  # `az ad app list` (which app_count_by_id and the display-name fallback
  # above both use) can no longer resolve its object id, and the purge
  # below needs it. Prefer the recorded value -- it is exactly the dead
  # field create-github-oidc.sh already writes -- and only fall back to a
  # lookup when it is missing (an older record file, or the display-name
  # path above).
  local object_id="$recorded_object_id"
  if [ -z "$object_id" ]; then
    object_id="$(az ad app list --filter "appId eq '$app_id'" --query "[0].id" -o tsv)"
  fi

  az ad app delete --id "$app_id"
  local after
  after="$(app_count_by_id "$app_id")"
  require_value "$after" "the $label app post-delete count"
  if [ "$after" != "0" ]; then
    echo "$label app registration still listed after delete." >&2
    exit 1
  fi
  echo "  $label app registration ($app_id) deleted -- its service principal and any remaining federated credentials go with it"

  # `az ad app delete` only moves the registration into the directory's
  # recycle bin for 30 days, still holding its name. Purge is best effort --
  # it needs a permission the deletion itself does not, so a failure here is
  # reported, not fatal -- ported from delete-entra-app.sh, same two-branch
  # message.
  if [ -n "$object_id" ]; then
    if az rest --method DELETE \
        --uri "https://graph.microsoft.com/v1.0/directory/deletedItems/${object_id}" \
        --output none 2>/dev/null; then
      echo "  $label: purged from deleted items."
    else
      echo "  $label: still in 'Deleted applications' -- auto-purges after 30 days."
    fi
  else
    echo "  $label: could not resolve an object id to purge -- still in 'Deleted applications', auto-purges after 30 days."
  fi
}

teardown_environment() {
  local name="$1"
  if [ -z "$name" ]; then
    echo "WARNING: GH_ENVIRONMENT_NAME not recorded -- skipping the environment delete." >&2
    return 0
  fi
  local present
  present="$(env_present "$name")"
  require_value "$present" "the environment listing read-back"
  if [ "$present" = "false" ]; then
    echo "  environment '$name' already gone -- nothing to do"
    return 0
  fi
  gh api --method DELETE "repos/${GITHUB_REPO}/environments/${name}" >/dev/null
  local after
  after="$(env_present "$name")"
  require_value "$after" "the environment post-delete read-back"
  if [ "$after" != "false" ]; then
    echo "Environment '$name' still listed after delete." >&2
    exit 1
  fi
  echo "  environment '$name' deleted"
}

teardown_variable() {
  local name="$1"
  local present
  present="$(variable_present "$name")"
  require_value "$present" "the $name variable listing read-back"
  if [ "$present" = "false" ]; then
    echo "  variable $name already gone -- nothing to do"
    return 0
  fi
  gh variable delete "$name" --repo "$GITHUB_REPO" >/dev/null
  local after
  after="$(variable_present "$name")"
  require_value "$after" "the $name variable post-delete read-back"
  if [ "$after" != "false" ]; then
    echo "Repository variable $name still listed after delete." >&2
    exit 1
  fi
  echo "  deleted $name"
}

teardown_secret() {
  local name="$1"
  local present
  present="$(secret_present "$name")"
  require_value "$present" "the $name secret listing read-back"
  if [ "$present" = "false" ]; then
    echo "  secret $name already gone -- nothing to do"
    return 0
  fi
  gh secret delete "$name" --repo "$GITHUB_REPO" >/dev/null
  local after
  after="$(secret_present "$name")"
  require_value "$after" "the $name secret post-delete read-back"
  if [ "$after" != "false" ]; then
    echo "Repository secret $name still listed after delete." >&2
    exit 1
  fi
  echo "  deleted $name"
}

# === --verify-teardown: read-only, never deletes an Azure/GitHub resource ==

if [ "$VERIFY_TEARDOWN" -eq 1 ]; then
  echo "== --verify-teardown: querying what still exists; deletes nothing but the record file =="
  REMAINING=0
  UNVERIFIABLE=0

  check_fic() {
    local label="$1" app_id="$2" fic_id="$3"
    if [ -z "$app_id" ] || [ -z "$fic_id" ]; then
      echo "  UNVERIFIABLE: $label federated credential (app id or credential id not recorded)"
      UNVERIFIABLE=$((UNVERIFIABLE + 1))
      return
    fi
    local app_exists
    app_exists="$(app_count_by_id "$app_id")"
    require_value "$app_exists" "the $label app existence check (for its federated credential)"
    if [ "$app_exists" = "0" ]; then
      echo "  gone: $label federated credential (its app registration is gone)"
      return
    fi
    local count
    count="$(fic_count "$app_id" "$fic_id")"
    require_value "$count" "the $label federated credential existence check"
    if [ "$count" != "0" ]; then
      echo "  STILL PRESENT: $label federated credential ($fic_id)"
      REMAINING=$((REMAINING + 1))
    else
      echo "  gone: $label federated credential"
    fi
  }
  check_fic "build" "$(record_get BUILD_APP_ID)" "$(record_get BUILD_FIC_ID)"
  check_fic "deploy" "$(record_get DEPLOY_APP_ID)" "$(record_get DEPLOY_FIC_ID)"

  check_role_assignment() {
    local label="$1" sp_id="$2" role="$3" scope="$4"
    if [ -z "$sp_id" ] || [ -z "$scope" ]; then
      echo "  UNVERIFIABLE: $label role assignment (principal id or scope not recorded)"
      UNVERIFIABLE=$((UNVERIFIABLE + 1))
      return
    fi
    local count
    count="$(role_assignment_count "$sp_id" "$role" "$scope")"
    require_value "$count" "the $label role assignment existence check"
    if [ "$count" != "0" ]; then
      echo "  STILL PRESENT: $label role assignment ('$role' at $scope)"
      REMAINING=$((REMAINING + 1))
    else
      echo "  gone: $label role assignment"
    fi
  }
  check_role_assignment "build" "$(record_get BUILD_SP_ID)" "AcrPush" "$(record_get AZ_ACR_ID)"
  check_role_assignment "deploy" "$(record_get DEPLOY_SP_ID)" "Container Apps Contributor" "$(record_get AZ_ACA_APP_ID)"

  check_app() {
    local label="$1" recorded_id="$2" display_name="$3" recorded_object_id="$4"
    local app_id="$recorded_id"
    if [ -z "$app_id" ]; then
      if [ -z "$display_name" ]; then
        echo "  UNVERIFIABLE: $label app registration (no app id, no display name recorded)"
        UNVERIFIABLE=$((UNVERIFIABLE + 1))
        return
      fi
      local count
      count="$(app_count_by_name "$display_name")"
      require_value "$count" "the $label display-name lookup count"
      if [ "$count" = "0" ]; then
        # `az ad app list` cannot see a soft-deleted app, so this 0 is
        # exactly what a soft-deleted registration produces too -- same
        # discipline as the app_id path below: check deletedItems before
        # calling it gone.
        if [ -n "$recorded_object_id" ]; then
          local deleted_present_by_name
          deleted_present_by_name="$(app_deleted_item_present "$recorded_object_id")"
          case "$deleted_present_by_name" in
            true)
              echo "  STILL PRESENT (soft-deleted, recoverable for 30 days): $label app registration (by name '$display_name')"
              REMAINING=$((REMAINING + 1))
              ;;
            false)
              echo "  gone: $label app registration (by name '$display_name')"
              ;;
            *)
              echo "  UNVERIFIABLE: $label app registration (by name '$display_name'; the deletedItems check failed, cannot confirm absence)"
              UNVERIFIABLE=$((UNVERIFIABLE + 1))
              ;;
          esac
        else
          echo "  UNVERIFIABLE: $label app registration (by name '$display_name'; no object id recorded, cannot check soft-delete state)"
          UNVERIFIABLE=$((UNVERIFIABLE + 1))
        fi
        return
      fi
      if [ "$count" != "1" ]; then
        echo "  UNVERIFIABLE: $count applications named '$display_name' -- cannot verify without guessing which is ours"
        UNVERIFIABLE=$((UNVERIFIABLE + 1))
        return
      fi
      app_id="$(app_id_by_name "$display_name")"
      require_value "$app_id" "the $label resolved app id"
    fi
    local count2
    count2="$(app_count_by_id "$app_id")"
    require_value "$count2" "the $label app existence check"
    if [ "$count2" != "0" ]; then
      echo "  STILL PRESENT: $label app registration ($app_id)"
      REMAINING=$((REMAINING + 1))
      return
    fi
    # `az ad app list` cannot see a soft-deleted app -- count2==0 alone
    # cannot tell "purged / never existed" apart from "in the recycle bin
    # for 30 days, still recoverable, still holding its name" (I3). Check
    # the directory's deletedItems endpoint by object id when one is known.
    # A "cannot check" outcome is UNVERIFIABLE, never "gone" -- the same
    # rule this script's own header states for a missing record field, and
    # --verify-teardown only removes the record file when nothing is left
    # UNVERIFIABLE either.
    if [ -n "$recorded_object_id" ]; then
      local deleted_present
      deleted_present="$(app_deleted_item_present "$recorded_object_id")"
      case "$deleted_present" in
        true)
          echo "  STILL PRESENT (soft-deleted, recoverable for 30 days): $label app registration ($app_id)"
          REMAINING=$((REMAINING + 1))
          ;;
        false)
          echo "  gone: $label app registration"
          ;;
        *)
          echo "  UNVERIFIABLE: $label app registration ($app_id; the deletedItems check failed, cannot confirm absence)"
          UNVERIFIABLE=$((UNVERIFIABLE + 1))
          ;;
      esac
    else
      echo "  UNVERIFIABLE: $label app registration (soft-delete state not checked -- object id not recorded)"
      UNVERIFIABLE=$((UNVERIFIABLE + 1))
    fi
  }
  check_app "build" "$(record_get BUILD_APP_ID)" "$(record_get BUILD_APP_DISPLAY_NAME)" "$(record_get BUILD_APP_OBJECT_ID)"
  check_app "deploy" "$(record_get DEPLOY_APP_ID)" "$(record_get DEPLOY_APP_DISPLAY_NAME)" "$(record_get DEPLOY_APP_OBJECT_ID)"

  ENV_NAME="$(record_get GH_ENVIRONMENT_NAME)"
  if [ -z "$ENV_NAME" ]; then
    echo "  UNVERIFIABLE: GitHub environment (name not recorded)"
    UNVERIFIABLE=$((UNVERIFIABLE + 1))
  else
    PRESENT="$(env_present "$ENV_NAME")"
    require_value "$PRESENT" "the environment listing read-back"
    if [ "$PRESENT" = "true" ]; then
      echo "  STILL PRESENT: GitHub environment '$ENV_NAME'"
      REMAINING=$((REMAINING + 1))
    else
      echo "  gone: GitHub environment '$ENV_NAME'"
    fi
  fi

  SECRETS_WRITTEN="$(record_get GH_SECRETS_WRITTEN)"
  if [ -z "$SECRETS_WRITTEN" ]; then
    echo "  UNVERIFIABLE: the identifier repository secrets (GH_SECRETS_WRITTEN not recorded)"
    UNVERIFIABLE=$((UNVERIFIABLE + 1))
  else
    read -ra SECRET_NAMES_TO_CHECK <<<"$SECRETS_WRITTEN"
    for name in "${SECRET_NAMES_TO_CHECK[@]}"; do
      PRESENT="$(secret_present "$name")"
      require_value "$PRESENT" "the $name secret listing read-back"
      if [ "$PRESENT" = "true" ]; then
        echo "  STILL PRESENT: repository secret $name"
        REMAINING=$((REMAINING + 1))
      else
        echo "  gone: repository secret $name"
      fi
    done
  fi
  DEPLOY_ENABLED_PRESENT="$(variable_present "DEPLOY_ENABLED")"
  require_value "$DEPLOY_ENABLED_PRESENT" "the DEPLOY_ENABLED variable listing read-back"
  if [ "$DEPLOY_ENABLED_PRESENT" = "true" ]; then
    echo "  STILL PRESENT: repository variable DEPLOY_ENABLED"
    REMAINING=$((REMAINING + 1))
  else
    echo "  gone: repository variable DEPLOY_ENABLED"
  fi

  echo
  if [ "$REMAINING" -eq 0 ] && [ "$UNVERIFIABLE" -eq 0 ]; then
    rm -f "$OIDC_RECORD_FILE"
    echo "Nothing remains. Removed $OIDC_RECORD_FILE."
    exit 0
  fi
  echo "$REMAINING resource(s) still present, $UNVERIFIABLE item(s) could not be checked." >&2
  echo "Record file kept at $OIDC_RECORD_FILE." >&2
  exit 1
fi

# === step 1: DEPLOY_ENABLED=false ===========================================
echo "== step 1: DEPLOY_ENABLED=false =="
gh variable set DEPLOY_ENABLED --repo "$GITHUB_REPO" --body false >/dev/null
DEPLOY_ENABLED_NOW="$(gh api "repos/${GITHUB_REPO}/actions/variables/DEPLOY_ENABLED" --jq .value)"
require_value "$DEPLOY_ENABLED_NOW" "the DEPLOY_ENABLED read-back"
if [ "$DEPLOY_ENABLED_NOW" != "false" ]; then
  echo "DEPLOY_ENABLED read back as '$DEPLOY_ENABLED_NOW', not 'false'." >&2
  exit 1
fi
echo "  DEPLOY_ENABLED=false -- stops NEW runs from reaching the deploy job's if-gate."
echo "  Does not touch a run already past that check, and revokes nothing already issued."

# === step 2: federated credentials ==========================================
echo "== step 2: delete both federated credentials =="
teardown_fic "build" "$(record_get BUILD_APP_ID)" "$(record_get BUILD_FIC_ID)"
teardown_fic "deploy" "$(record_get DEPLOY_APP_ID)" "$(record_get DEPLOY_FIC_ID)"

# === step 3: drain check =====================================================
echo "== step 3: drain check (repo-scoped -- every run, not just this workflow) =="
# GH_RUN_LIST_LIMIT was validated as a positive integer during preflight.

# ONE call answers two questions -- no second `gh run list` call, no second
# endpoint. `--json status` is the only field requested (smaller response,
# and its shape is what tells this call apart from the detail query a few
# lines down). The jq program's comma operator prints TWO lines: the raw
# window size (unfiltered `length`), then the non-terminal count within that
# same window. `gh run list` defaults to 20 results -- GH_RUN_LIST_LIMIT
# raises that explicitly, and `gh` paginates internally to satisfy it.
#
# GH_RUN_LIST_LIMIT is a FETCH CAP, not exhaustive pagination -- so the raw
# window size is checked FIRST. If the window came back exactly full, an
# older run (most plausibly one stuck `waiting` behind an unapproved
# `production` environment, sitting underneath a pile of newer completed CI
# runs) could exist entirely outside what was just fetched, and the
# non-terminal count below would then be reading a possibly-truncated view,
# not the whole repo. That is UNPROVABLE from this window alone, so an exactly
# full window fails closed -- it does not proceed on an optimistic "probably
# fine". (An earlier version of this check compared the repo's LIFETIME total
# run count against GH_RUN_LIST_LIMIT -- a different, wrong quantity: any
# repo whose all-time run count ever exceeded the cap would abort teardown
# permanently, whether or not anything was in flight. Comparing the window's
# own size against its own cap is the quantity that actually matters.)
RUN_LIST_COUNTS="$(gh run list --repo "$GITHUB_REPO" --limit "$GH_RUN_LIST_LIMIT" \
  --json status --jq 'length, ([.[] | select(.status != "completed")] | length)')"
require_value "$RUN_LIST_COUNTS" "the workflow run drain-check counts"
mapfile -t RUN_LIST_COUNT_LINES <<<"$RUN_LIST_COUNTS"
RAW_WINDOW_COUNT="${RUN_LIST_COUNT_LINES[0]:-}"
NON_TERMINAL_COUNT="${RUN_LIST_COUNT_LINES[1]:-}"
require_value "$RAW_WINDOW_COUNT" "the workflow run drain-check window size"
require_value "$NON_TERMINAL_COUNT" "the workflow run drain-check non-terminal count"

if [ "$RAW_WINDOW_COUNT" -eq "$GH_RUN_LIST_LIMIT" ]; then
  echo "gh run list returned exactly GH_RUN_LIST_LIMIT=$GH_RUN_LIST_LIMIT runs -- this window may be" >&2
  echo "truncated, and an older non-terminal run could exist outside it. Cannot prove the repo is" >&2
  echo "drained from a possibly-incomplete window. Raise GH_RUN_LIST_LIMIT and re-run." >&2
  exit 1
fi

if [ "$NON_TERMINAL_COUNT" != "0" ]; then
  echo "Found $NON_TERMINAL_COUNT non-terminal run(s) in the repo (repo-scoped, not filtered by" >&2
  echo "workflow -- any workflow may declare environment: production). Aborting before any" >&2
  echo "further Azure deletion. Nothing is cancelled -- that decision belongs to a human." >&2
  gh run list --repo "$GITHUB_REPO" --limit "$GH_RUN_LIST_LIMIT" \
    --json databaseId,workflowName,headBranch,status,url \
    --jq '.[] | select(.status != "completed") | "  - #\(.databaseId) \(.workflowName) (\(.headBranch)) status=\(.status) \(.url)"' >&2
  exit 1
fi
echo "  drain check clean: 0 non-terminal runs among $RAW_WINDOW_COUNT total (window not truncated)"

# === step 4: role assignments ================================================
echo "== step 4: delete role assignments =="
teardown_role_assignment "build" "$(record_get BUILD_ROLE_ASSIGNMENT_ID)" \
  "$(record_get BUILD_SP_ID)" "AcrPush" "$(record_get AZ_ACR_ID)"
teardown_role_assignment "deploy" "$(record_get DEPLOY_ROLE_ASSIGNMENT_ID)" \
  "$(record_get DEPLOY_SP_ID)" "Container Apps Contributor" "$(record_get AZ_ACA_APP_ID)"

# === step 5: app registrations (assignments already gone -- see step 4) =====
echo "== step 5: delete the app registrations =="
teardown_app "build" "$(record_get BUILD_APP_ID)" "$(record_get BUILD_APP_DISPLAY_NAME)" "$(record_get BUILD_APP_OBJECT_ID)"
teardown_app "deploy" "$(record_get DEPLOY_APP_ID)" "$(record_get DEPLOY_APP_DISPLAY_NAME)" "$(record_get DEPLOY_APP_OBJECT_ID)"

# === step 6: GitHub environment, repository secrets and DEPLOY_ENABLED ======
echo "== step 6: delete the GitHub environment, repository secrets and DEPLOY_ENABLED =="
teardown_environment "$(record_get GH_ENVIRONMENT_NAME)"

SECRETS_WRITTEN="$(record_get GH_SECRETS_WRITTEN)"
if [ -z "$SECRETS_WRITTEN" ]; then
  echo "WARNING: GH_SECRETS_WRITTEN not recorded -- skipping cleanup of the identifier" >&2
  echo "  secrets (AZURE_TENANT_ID etc). DEPLOY_ENABLED is still handled below." >&2
else
  read -ra SECRET_NAMES_TO_DELETE <<<"$SECRETS_WRITTEN"
  for name in "${SECRET_NAMES_TO_DELETE[@]}"; do
    teardown_secret "$name"
  done
fi
# DEPLOY_ENABLED is the one repository VARIABLE this pair of scripts writes,
# tracked separately from GH_SECRETS_WRITTEN -- see header -- and step 1
# above guarantees it exists by the time this runs, regardless of what the
# record file says, so it is attempted unconditionally.
teardown_variable "DEPLOY_ENABLED"

echo
echo "Torn down. The record file at $OIDC_RECORD_FILE was left in place --"
echo "run with --verify-teardown to confirm nothing remains and remove it:"
echo "  OIDC_RECORD_FILE=$OIDC_RECORD_FILE $0 --verify-teardown"
