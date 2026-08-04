#!/usr/bin/env bash
# Create the two Microsoft Entra ID application registrations Day 19 needs:
# an API app (the audience the server validates) and a client app (the caller).
#
# Reversible by construction: everything created here is named, printed, and
# deleted again by delete-entra-app.sh. Nothing else in the tenant is touched.
#
# Required env vars:
#   ENTRA_TENANT_ID        - the tenant these registrations belong to
#   ENTRA_API_APP_NAME     - display name for the API registration
#   ENTRA_CLIENT_APP_NAME  - display name for the client registration
# Optional env vars:
#   ENTRA_SCOPE_VALUE      - delegated scope value (default: access_as_user)
#   ENTRA_APP_ROLE_VALUE   - application role value (default: Api.Access)
#
# Flags:
#   --defer-app-role-assignment
#       Create everything EXCEPT the application-role assignment, so
#       `tools/entra_smoke.py --phase no-role` can obtain a valid-audience,
#       role-less token and watch the API reject it with 403. Run
#       assign-entra-app-role.sh afterwards to complete the setup.
#
# Usage:
#   ENTRA_TENANT_ID=... ENTRA_API_APP_NAME=... ENTRA_CLIENT_APP_NAME=... \
#     ./create-entra-app.sh [--defer-app-role-assignment]
set -euo pipefail

DEFER_APP_ROLE_ASSIGNMENT=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --defer-app-role-assignment) DEFER_APP_ROLE_ASSIGNMENT=true; shift ;;
    # The header block, read from the file itself rather than duplicated as a
    # usage() string that drifts from it.
    -h|--help) awk 'NR>1 && /^#/ {print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

: "${ENTRA_TENANT_ID:?Set ENTRA_TENANT_ID (the tenant these registrations belong to)}"
: "${ENTRA_API_APP_NAME:?Set ENTRA_API_APP_NAME (display name for the API registration)}"
: "${ENTRA_CLIENT_APP_NAME:?Set ENTRA_CLIENT_APP_NAME (display name for the client registration)}"
ENTRA_SCOPE_VALUE="${ENTRA_SCOPE_VALUE:-access_as_user}"
ENTRA_APP_ROLE_VALUE="${ENTRA_APP_ROLE_VALUE:-Api.Access}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# The az CLI has no --subscription equivalent for directory objects: every
# `az ad` call goes to whichever tenant the active account belongs to, which is
# shared mutable state an `az login` in another terminal can silently repoint.
# So the tenant is compared before the first mutation, not assumed.
if ! ACTIVE_TENANT="$(az account show --query tenantId -o tsv)"; then
  echo "Not signed in to Azure. Run: az login --tenant $ENTRA_TENANT_ID" >&2
  exit 1
fi
if [[ "$(lower "$ACTIVE_TENANT")" != "$(lower "$ENTRA_TENANT_ID")" ]]; then
  echo "Active az tenant is $ACTIVE_TENANT, but ENTRA_TENANT_ID is $ENTRA_TENANT_ID." >&2
  echo "Run 'az login --tenant $ENTRA_TENANT_ID' first. Nothing was created." >&2
  exit 1
fi

if ! command -v uuidgen >/dev/null 2>&1; then
  echo "uuidgen is required to mint the scope and role ids." >&2
  exit 1
fi
new_guid() { uuidgen | tr '[:upper:]' '[:lower:]'; }

# A scope value or role value is interpolated straight into the JSON bodies
# below, so it is validated before anything is created: a value containing a
# quote would produce invalid JSON and a Graph error that says nothing about
# the cause. The character class is what Entra accepts for these anyway.
for pair in "ENTRA_SCOPE_VALUE:$ENTRA_SCOPE_VALUE" "ENTRA_APP_ROLE_VALUE:$ENTRA_APP_ROLE_VALUE"; do
  if [[ ! "${pair#*:}" =~ ^[A-Za-z0-9._-]{1,120}$ ]]; then
    echo "${pair%%:*} must match [A-Za-z0-9._-]{1,120}; got '${pair#*:}'." >&2
    exit 1
  fi
done

SCOPE_ID="$(new_guid)"
ROLE_ID="$(new_guid)"

# Graph request bodies only — no secret is ever written to a file, here or
# anywhere else in this script.
WORK_DIR="$(mktemp -d)"

# Everything below this line creates directory objects, and delete-entra-app.sh
# needs both application ids to remove them. Any abort in between — a tenant
# policy refusing the public-client flag, admin consent refused, the service
# principal retry exhausted — would otherwise leave live registrations whose
# ids the operator never saw, i.e. a teardown that cannot be performed. So the
# ids are echoed the moment each one exists, and this trap repeats the exact
# teardown command for whatever got created before the failure.
API_APP_ID=""
CLIENT_APP_ID=""
teardown_hint() {
  local status=$?
  rm -rf "$WORK_DIR"
  if [[ $status -ne 0 && ( -n "$API_APP_ID" || -n "$CLIENT_APP_ID" ) ]]; then
    echo >&2
    echo "ABORTED after creating registrations. Tear them down with:" >&2
    echo "  ENTRA_TENANT_ID=$ENTRA_TENANT_ID \\" >&2
    echo "    ENTRA_API_APP_ID=${API_APP_ID:-none} \\" >&2
    echo "    ENTRA_CLIENT_APP_ID=${CLIENT_APP_ID:-none} \\" >&2
    echo "    $SCRIPT_DIR/delete-entra-app.sh" >&2
    echo "('none' means that registration was never created; the script skips it.)" >&2
  fi
}
trap teardown_hint EXIT
# Signals are turned into an exit rather than added to the EXIT trap's list.
# `trap teardown_hint EXIT INT TERM HUP` looks equivalent and is not: measured
# on this bash, SIGHUP and SIGTERM run the handler TWICE and leave `$?` at 0,
# so the `status -ne 0` guard below would swallow the teardown hint at exactly
# the moment it is needed — a closed terminal is the "operator went away" case
# this whole mechanism exists for. Exiting first gives one invocation with a
# non-zero status.
trap 'exit 130' INT TERM HUP

# `az ad app create` returns the whole object; both ids are read from that one
# response rather than following up with `az ad app show`, which can 404
# against a freshly created object while the directory replicates.
#
# The projection is `join(' ', [...])` and not `[appId,id]`, which looks like it
# yields one tab-separated line and does not: measured on az 2.88.0, an array
# projection prints one value PER LINE. Reading field 1 of that with `cut` hands
# back BOTH ids joined by a newline, and the next request URI is malformed —
# Graph answers with an HTML "Bad Request - Invalid URL" page that says nothing
# about the cause, after the registration already exists. join() collapses the
# pair onto one space-separated line.
#
# Shape is then checked rather than trusted, so a future CLI change fails here,
# loudly, instead of somewhere downstream as an unrecognisable error.
require_guid() {
  local label="$1" value="$2" display_name="$3"
  if [[ ! "$value" =~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$ ]]; then
    echo "Expected a GUID for $label; got '$value'." >&2
    echo "The az CLI output shape may have changed. A registration named" >&2
    echo "'$display_name' may exist; find its app id with:" >&2
    echo "  az ad app list --display-name '$display_name' --query '[].appId' -o tsv" >&2
    exit 1
  fi
}

create_service_principal() {
  # Directory replication makes this the flakiest call in the script: the
  # application exists, the service principal endpoint has not seen it yet.
  # Bounded retry, and progress goes to stderr because stdout is the return
  # value.
  local app_id="$1" label="$2" sp_id=""
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if sp_id="$(az ad sp create --id "$app_id" --query id -o tsv)"; then
      printf '%s' "$sp_id"
      return 0
    fi
    echo "  $label service principal not ready (attempt $attempt) — retrying in 5s" >&2
    sleep 5
  done
  return 1
}

echo "Creating API app registration '$ENTRA_API_APP_NAME'..."
API_APP_PAIR="$(az ad app create \
  --display-name "$ENTRA_API_APP_NAME" \
  --sign-in-audience AzureADMyOrg \
  --query "join(' ', [appId, id])" -o tsv)"
read -r PARSED_APP_ID PARSED_OBJECT_ID <<<"$API_APP_PAIR"
require_guid "API app id" "${PARSED_APP_ID:-}" "$ENTRA_API_APP_NAME"
require_guid "API app object id" "${PARSED_OBJECT_ID:-}" "$ENTRA_API_APP_NAME"
API_APP_ID="$PARSED_APP_ID"
API_OBJECT_ID="$PARSED_OBJECT_ID"
# Printed here, not only in the summary at the end: from this line on there is
# something in the tenant that has to be deleted, and the id is the only handle
# on it.
echo "  API app id: $API_APP_ID (object $API_OBJECT_ID)"

# requestedAccessTokenVersion 2 is what makes `aud` the application id GUID and
# `iss` the v2.0 issuer — the exact pair the server's verifier is configured
# with. groupMembershipClaims emits `groups` for Day 15's ACL filter.
cat >"$WORK_DIR/api-app.json" <<JSON
{
  "signInAudience": "AzureADMyOrg",
  "identifierUris": ["api://${API_APP_ID}"],
  "api": {
    "requestedAccessTokenVersion": 2,
    "oauth2PermissionScopes": [{
      "id": "${SCOPE_ID}",
      "value": "${ENTRA_SCOPE_VALUE}",
      "type": "Admin",
      "isEnabled": true,
      "adminConsentDisplayName": "Access the Azure GenAI lab API",
      "adminConsentDescription": "Allows delegated access to the Azure GenAI lab API"
    }]
  },
  "appRoles": [{
    "id": "${ROLE_ID}",
    "value": "${ENTRA_APP_ROLE_VALUE}",
    "displayName": "Access the Azure GenAI lab API",
    "description": "Allows application access to the Azure GenAI lab API",
    "allowedMemberTypes": ["Application"],
    "isEnabled": true
  }],
  "groupMembershipClaims": "SecurityGroup"
}
JSON
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/${API_OBJECT_ID}" \
  --headers "Content-Type=application/json" \
  --body @"$WORK_DIR/api-app.json" \
  --output none

# After the appRoles PATCH, never before: a service principal copies the
# application's roles when it is created, so an SP created first would not
# carry the role assign-entra-app-role.sh looks up by value.
echo "Creating API service principal..."
API_SP_ID="$(create_service_principal "$API_APP_ID" "API")" || {
  echo "Could not create the API service principal." >&2
  exit 1
}

# Explicitly false, not merely left at its default. This is what lets the
# deferred phase obtain a valid-audience token with no `roles` claim, so the
# 403 comes from the API refusing the credential rather than from the token
# endpoint refusing to issue it. Flip this to true and `--phase no-role`
# proves nothing about the server.
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${API_SP_ID}" \
  --headers "Content-Type=application/json" \
  --body '{"appRoleAssignmentRequired": false}' \
  --output none

echo "Creating client app registration '$ENTRA_CLIENT_APP_NAME'..."
CLIENT_APP_PAIR="$(az ad app create \
  --display-name "$ENTRA_CLIENT_APP_NAME" \
  --sign-in-audience AzureADMyOrg \
  --query "join(' ', [appId, id])" -o tsv)"
read -r PARSED_APP_ID PARSED_OBJECT_ID <<<"$CLIENT_APP_PAIR"
require_guid "client app id" "${PARSED_APP_ID:-}" "$ENTRA_CLIENT_APP_NAME"
require_guid "client app object id" "${PARSED_OBJECT_ID:-}" "$ENTRA_CLIENT_APP_NAME"
CLIENT_APP_ID="$PARSED_APP_ID"
CLIENT_OBJECT_ID="$PARSED_OBJECT_ID"
echo "  client app id: $CLIENT_APP_ID (object $CLIENT_OBJECT_ID)"

# isFallbackPublicClient: the device code flow is a public-client grant, and
# this client also holds a secret for the app-only leg — one registration
# serving both legs of the smoke test.
cat >"$WORK_DIR/client-app.json" <<JSON
{
  "isFallbackPublicClient": true,
  "requiredResourceAccess": [{
    "resourceAppId": "${API_APP_ID}",
    "resourceAccess": [
      {"id": "${SCOPE_ID}", "type": "Scope"},
      {"id": "${ROLE_ID}", "type": "Role"}
    ]
  }]
}
JSON
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/${CLIENT_OBJECT_ID}" \
  --headers "Content-Type=application/json" \
  --body @"$WORK_DIR/client-app.json" \
  --output none

echo "Creating client service principal..."
CLIENT_SP_ID="$(create_service_principal "$CLIENT_APP_ID" "client")" || {
  echo "Could not create the client service principal." >&2
  exit 1
}

# Delegated admin consent, written directly as an oauth2PermissionGrant rather
# than through `az ad app permission admin-consent`. That command consents to
# EVERY entry in requiredResourceAccess, which includes the application role —
# it would create the very appRoleAssignment --defer-app-role-assignment exists
# to withhold, and the deferred phase would silently test nothing.
cat >"$WORK_DIR/grant.json" <<JSON
{
  "clientId": "${CLIENT_SP_ID}",
  "consentType": "AllPrincipals",
  "resourceId": "${API_SP_ID}",
  "scope": "${ENTRA_SCOPE_VALUE}"
}
JSON
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants" \
  --headers "Content-Type=application/json" \
  --body @"$WORK_DIR/grant.json" \
  --output none

# Seven days, not the `--years 1` default shape: the header calls these
# registrations ephemeral, and a secret that outlives the experiment by eleven
# months contradicts that. `date -u -v+7d` is BSD/macOS, `date -u -d` is GNU —
# the fallback covers both without a third dependency.
SECRET_END_DATE="$(date -u -v+7d '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
  || date -u -d '+7 days' '+%Y-%m-%dT%H:%M:%SZ')"
CLIENT_SECRET="$(az ad app credential reset \
  --id "$CLIENT_APP_ID" \
  --display-name "azgenai-lab-smoke" \
  --end-date "$SECRET_END_DATE" \
  --query password -o tsv)"

if [[ "$DEFER_APP_ROLE_ASSIGNMENT" == "true" ]]; then
  echo
  echo "Application-role assignment DEFERRED (--defer-app-role-assignment)."
  echo "Run tools/entra_smoke.py --phase no-role now, then assign-entra-app-role.sh."
else
  echo
  echo "Assigning the application role..."
  ENTRA_TENANT_ID="$ENTRA_TENANT_ID" \
  ENTRA_API_APP_ID="$API_APP_ID" \
  ENTRA_CLIENT_APP_ID="$CLIENT_APP_ID" \
  ENTRA_APP_ROLE_VALUE="$ENTRA_APP_ROLE_VALUE" \
    bash "$SCRIPT_DIR/assign-entra-app-role.sh"
fi

cat <<SUMMARY

Created (record these; they are not secrets, but mask them in screenshots):
  API app id (audience):    $API_APP_ID
  API app object id:        $API_OBJECT_ID
  API service principal id: $API_SP_ID
  client app id:            $CLIENT_APP_ID
  client app object id:     $CLIENT_OBJECT_ID
  client service principal: $CLIENT_SP_ID
  delegated scope id:       $SCOPE_ID ($ENTRA_SCOPE_VALUE)
  application role id:      $ROLE_ID ($ENTRA_APP_ROLE_VALUE)

Server environment:
  AUTH_MODE=entra
  ENTRA_TENANT_ID=$ENTRA_TENANT_ID
  ENTRA_AUDIENCE=$API_APP_ID
  ENTRA_REQUIRED_SCOPE=$ENTRA_SCOPE_VALUE
  ENTRA_REQUIRED_APP_ROLE=$ENTRA_APP_ROLE_VALUE

Teardown:
  ENTRA_TENANT_ID=$ENTRA_TENANT_ID ENTRA_API_APP_ID=$API_APP_ID \\
    ENTRA_CLIENT_APP_ID=$CLIENT_APP_ID ./delete-entra-app.sh
SUMMARY

echo
echo "Client secret (export as ENTRA_CLIENT_SECRET; for immediate use only—never paste into evidence files, screenshots, or committed text):"
echo "$CLIENT_SECRET"
echo
echo "These registrations are ephemeral. Run delete-entra-app.sh when finished."
