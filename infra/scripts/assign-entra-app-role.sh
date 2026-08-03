#!/usr/bin/env bash
# Assign the API's application role to the client's service principal.
#
# Split out of create-entra-app.sh so the two tenant states the live smoke
# needs are reachable on purpose: run create-entra-app.sh
# --defer-app-role-assignment, prove the 403, then run this and prove the 200.
#
# Idempotent: an assignment that already exists is reported and left alone,
# never duplicated.
#
# Required env vars:
#   ENTRA_TENANT_ID       - the tenant both registrations live in
#   ENTRA_API_APP_ID      - the API application's client id
#   ENTRA_CLIENT_APP_ID   - the client application's client id
# Optional env vars:
#   ENTRA_APP_ROLE_VALUE  - application role value (default: Api.Access)
set -euo pipefail

: "${ENTRA_TENANT_ID:?Set ENTRA_TENANT_ID}"
: "${ENTRA_API_APP_ID:?Set ENTRA_API_APP_ID (the API application's client id)}"
: "${ENTRA_CLIENT_APP_ID:?Set ENTRA_CLIENT_APP_ID (the client application's client id)}"
ENTRA_APP_ROLE_VALUE="${ENTRA_APP_ROLE_VALUE:-Api.Access}"

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# Same guard as create-entra-app.sh: `az ad` follows the active account's
# tenant, which another terminal's `az login` can repoint.
if ! ACTIVE_TENANT="$(az account show --query tenantId -o tsv)"; then
  echo "Not signed in to Azure. Run: az login --tenant $ENTRA_TENANT_ID" >&2
  exit 1
fi
if [[ "$(lower "$ACTIVE_TENANT")" != "$(lower "$ENTRA_TENANT_ID")" ]]; then
  echo "Active az tenant is $ACTIVE_TENANT, but ENTRA_TENANT_ID is $ENTRA_TENANT_ID." >&2
  echo "Run 'az login --tenant $ENTRA_TENANT_ID' first. Nothing was assigned." >&2
  exit 1
fi

API_SP_ID="$(az ad sp show --id "$ENTRA_API_APP_ID" --query id -o tsv)"
CLIENT_SP_ID="$(az ad sp show --id "$ENTRA_CLIENT_APP_ID" --query id -o tsv)"
ROLE_ID="$(az ad sp show --id "$ENTRA_API_APP_ID" \
  --query "appRoles[?value=='${ENTRA_APP_ROLE_VALUE}'] | [0].id" -o tsv)"

if [[ -z "$ROLE_ID" || "$ROLE_ID" == "None" ]]; then
  echo "The API service principal exposes no app role with value '$ENTRA_APP_ROLE_VALUE'." >&2
  exit 1
fi

# The assignment is listed under the PRINCIPAL (the client SP); it is created
# under the RESOURCE (the API SP). Both halves name the same triple, which is
# what makes "already assigned" answerable without creating a duplicate.
EXISTING="$(az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${CLIENT_SP_ID}/appRoleAssignments" \
  --query "value[?appRoleId=='${ROLE_ID}' && resourceId=='${API_SP_ID}'] | [0].id" -o tsv)"

if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
  echo "App role '$ENTRA_APP_ROLE_VALUE' is already assigned (assignment $EXISTING) — nothing to do."
  exit 0
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
cat >"$WORK_DIR/assignment.json" <<JSON
{
  "principalId": "${CLIENT_SP_ID}",
  "resourceId": "${API_SP_ID}",
  "appRoleId": "${ROLE_ID}"
}
JSON

az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${API_SP_ID}/appRoleAssignedTo" \
  --headers "Content-Type=application/json" \
  --body @"$WORK_DIR/assignment.json" \
  --output none

echo "Assigned app role '$ENTRA_APP_ROLE_VALUE' to the client service principal."
echo "Tokens already issued keep their old claims; acquire a new one before re-testing."
