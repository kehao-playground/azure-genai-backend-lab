#!/usr/bin/env bash
# Delete the two Entra ID application registrations create-entra-app.sh made —
# and only those two.
#
# Deliberately targeted, by application id rather than by display name: a name
# match would happily delete somebody else's registration that happens to be
# called the same thing. Deleting an application also removes its service
# principal, its secret, its app-role assignments and its permission grants,
# so this one command is the whole teardown.
#
# Succeeds if either registration was already removed.
#
# Required env vars:
#   ENTRA_TENANT_ID      - the tenant both registrations live in
#   ENTRA_API_APP_ID     - the API application's client id
#   ENTRA_CLIENT_APP_ID  - the client application's client id
set -euo pipefail

: "${ENTRA_TENANT_ID:?Set ENTRA_TENANT_ID}"
: "${ENTRA_API_APP_ID:?Set ENTRA_API_APP_ID (the API application's client id)}"
: "${ENTRA_CLIENT_APP_ID:?Set ENTRA_CLIENT_APP_ID (the client application's client id)}"

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

if ! ACTIVE_TENANT="$(az account show --query tenantId -o tsv)"; then
  echo "Not signed in to Azure. Run: az login --tenant $ENTRA_TENANT_ID" >&2
  exit 1
fi
if [[ "$(lower "$ACTIVE_TENANT")" != "$(lower "$ENTRA_TENANT_ID")" ]]; then
  echo "Active az tenant is $ACTIVE_TENANT, but ENTRA_TENANT_ID is $ENTRA_TENANT_ID." >&2
  echo "Run 'az login --tenant $ENTRA_TENANT_ID' first. Nothing was deleted." >&2
  exit 1
fi

delete_app() {
  local app_id="$1" label="$2" object_id=""
  # `az ad app list --filter` rather than `az ad app show`: an absent
  # registration comes back as an empty result, while a permissions or network
  # failure is still an error. `show` returns non-zero for both, which would
  # let a real failure be reported as "already removed".
  object_id="$(az ad app list --filter "appId eq '${app_id}'" --query "[0].id" -o tsv)"
  if [[ -z "$object_id" || "$object_id" == "None" ]]; then
    echo "$label ($app_id): already removed."
    return 0
  fi

  az ad app delete --id "$app_id"
  echo "$label ($app_id): deleted."

  # A deleted registration sits in the directory's recycle bin for 30 days and
  # keeps holding its name. Purging is best effort — it needs a permission the
  # deletion itself does not, so a failure here is reported, not fatal.
  if az rest --method DELETE \
      --uri "https://graph.microsoft.com/v1.0/directory/deletedItems/${object_id}" \
      --output none 2>/dev/null; then
    echo "$label: purged from deleted items."
  else
    echo "$label: still in 'Deleted applications' — auto-purges after 30 days."
  fi
}

delete_app "$ENTRA_API_APP_ID" "API app"
delete_app "$ENTRA_CLIENT_APP_ID" "client app"

echo "Done. Unset ENTRA_CLIENT_SECRET in any shell that still has it exported."
