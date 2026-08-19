#!/usr/bin/env bash
# Provision the two federated (secret-less) identities GitHub Actions uses to
# reach Azure -- one that can push an image to the registry, one that can
# update the container app -- and wire up the GitHub side that turns "a
# credential exists" into "a credential a human approved".
#
# Run ONCE, AFTER provisioning (create-acr.sh, deploy-container-app.sh). This
# is a single phase, not two: an earlier design split "create the principals"
# from "bind the role that needs the container app to already exist", but the
# only real constraint is that the role assignment comes after the app is
# provisioned -- putting the whole script after provisioning satisfies that
# trivially, without a --phase flag and its own resume logic.
#
# THE ORDER BELOW IS THE CONTRACT, not a convenience -- same discipline as
# deploy-container-app.sh's eleven stages and delete-container-app.sh's seven:
#
#   0. open the record file (required, refuses to overwrite) -- names,
#      tenant, subscription, ACR id, app id
#   1. two app registrations + service principals -- id appended to the
#      record file the moment each one exists
#   2. two federated credentials (issuer/audience below) -- id appended
#   3. two role assignments (AcrPush on the ACR; Container Apps Contributor
#      on the one app) -- id appended
#   4. persistence verification: read the assignments back with
#      `az role assignment list --all`. This proves the CONTROL-PLANE OBJECT
#      EXISTS. It does not prove the permission is effective --
#      docs/managed-identity.md:89-94 measured 14 minutes 44 seconds of
#      propagation on Day 20, against Microsoft's documented "up to 5". No
#      `sleep` is added here to wait for it: an unmeasured wait is not a
#      guarantee, and the first real workflow run is the actual readiness
#      probe (see the summary this script prints at the end).
#   5. create the GitHub environment "production", set its required
#      reviewer and its deployment-branch policy, then READ EVERY SETTING
#      BACK AND COMPARE. This step exists because GitHub silently
#      auto-creates an UNPROTECTED environment the first time any workflow
#      references a name that does not exist yet -- "the environment named
#      production exists" proves nothing about whether it is actually
#      gated.
#   6. write the GitHub repository variables the workflow reads, then read
#      each one back
#   7. only after 5 and 6 have both verified clean: arm the pipeline by
#      setting DEPLOY_ENABLED=true. This is deliberately the LAST mutation
#      this script makes -- if anything earlier failed, the pipeline must
#      never be armed.
#
# These are repository VARIABLES (`gh variable set`, not `gh secret set`) on
# purpose, following this project's own decision (docs/ci-cd.md, D9): the
# credential is the federated-identity trust relationship itself, and it does
# not live in the repo. Client ids, tenant/subscription ids and resource
# names are not secrets -- putting them in `secrets` would only obscure them
# in logs, not protect anything, at the cost of every debugging session
# reading `***`. This is a KNOWING DEVIATION from Microsoft's own OIDC
# tutorial, which puts these same three identifiers in secrets "for security
# reasons" -- the reasoning for the deviation belongs in docs/ci-cd.md, not
# silently done here.
#
# Two identities, not one, because the alternative (one identity, subject
# bound to the environment, holding both AcrPush and the deploy role) forces
# a choice between rebuilding the image after approval (a *different* set of
# bytes gets deployed than the one boot-smoke-tested in CI) or shipping an
# artifact across the approval boundary. This project's D2 decision is that
# the bytes that passed CI are the bytes that get deployed, so two identities
# it is -- and the residual this buys is real and undocumented if left
# unsaid: BEFORE approval, the build identity genuinely can write to the ACR.
# What it writes is inert (nothing ever runs an image nobody's --image names)
# but "approval gates all Azure access" is not literally true under this
# design. docs/ci-cd.md says so.
#
# Subject binding (issuer https://token.actions.githubusercontent.com,
# audience api://AzureADTokenExchange -- the audience is `azure/login`'s
# INPUT DEFAULT, not an OIDC constant; other clouds need a different value):
#
#   build:  repo:<owner>/<repo>:ref:refs/heads/<branch>
#   deploy: repo:<owner>/<repo>:environment:<environment>
#
# THE SUBJECT CARRIES NO REF. `repo:owner/name:environment:production` says
# nothing about which branch triggered the run that reached that
# environment -- it is present in the claim only because the job that
# requested the token declared `environment: production`, which GitHub does
# not allow before an approval. "Only main deploys" is enforced by the
# environment's deployment-branch policy (a GitHub-side setting, step 5
# below), not by anything in this subject string. A comment claiming
# otherwise would be wrong.
#
# Reversible by construction: everything created here is named, its id is
# appended to OIDC_RECORD_FILE the instant it exists, and delete-github-oidc.sh
# (this project's Task 6) tears it down FROM THAT FILE -- it does not need to
# be told any of these names again, because D10.1 designed the record file to
# carry every identifier the teardown needs. On any abort, the exact teardown
# invocation is printed with its one required knob filled in.
#
# Required env vars:
#   OIDC_RECORD_FILE   - path to write the creation record to. Refused if it
#                         already exists -- overwriting it over a live run
#                         would lose the only list of what to tear down.
#   GITHUB_REPO         - "owner/repo" slug, e.g. kehao-playground/azure-genai-backend-lab
#   AZ_TENANT_ID        - the Azure AD tenant these app registrations and role
#                          assignments belong to (compared against the active
#                          az session before the first mutation, same
#                          discipline as create-entra-app.sh)
#   AZ_SUBSCRIPTION_ID  - target subscription (never rely on the default context)
#   AZ_RESOURCE_GROUP   - resource group holding the ACR and the container app
#   AZ_ACR_NAME         - existing registry (create-acr.sh prints it) -- the
#                          build identity's AcrPush scope
#   AZ_ACA_APP_NAME     - existing container app (deploy-container-app.sh) --
#                          the deploy identity's Container Apps Contributor scope
# Optional env vars:
#   GH_ENVIRONMENT_NAME        - defaults to production. MUST MATCH ci.yml's
#                                 own hardcoded `environment: production` --
#                                 changing this here alone produces a deploy
#                                 identity whose federated subject
#                                 (repo:...:environment:<this value>) the
#                                 workflow can never request a matching token
#                                 for. A silent auth failure at the first
#                                 push, not an error at configuration time.
#   GH_DEPLOY_BRANCH           - defaults to main; the only branch allowed to
#                                 deploy AND the branch named in the build
#                                 identity's subject. MUST MATCH ci.yml's own
#                                 hardcoded `github.ref == 'refs/heads/main'`
#                                 and `check_freshness.sh "$CURRENT_SHA" main`
#                                 -- same failure mode as above if it drifts.
#   GH_REQUIRED_REVIEWER_LOGIN - GitHub login required to approve a deployment;
#                                 defaults to the currently authenticated
#                                 `gh` user (this is a single-operator repo)
#   BUILD_APP_NAME_PREFIX      - defaults to gh-oidc-build-azgenai-lab
#   DEPLOY_APP_NAME_PREFIX     - defaults to gh-oidc-deploy-azgenai-lab
#
# Privileges needed: Microsoft.Authorization/roleAssignments/write on the ACR
# and on the container app (e.g. Owner or User Access Administrator at a
# scope covering both), directory permission to create app registrations and
# service principals, and `gh auth login` with `repo` scope plus permission
# to manage this repository's environments and Actions variables.
set -euo pipefail

while [[ $# -gt 0 ]]; do
  case "$1" in
    # The header block, read from the file itself rather than duplicated as a
    # usage() string that drifts from it -- same trick create-entra-app.sh uses.
    -h|--help) awk 'NR>1 && /^#/ {print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

: "${OIDC_RECORD_FILE:?Set OIDC_RECORD_FILE (path to write the creation record to)}"
: "${GITHUB_REPO:?Set GITHUB_REPO (owner/repo slug)}"
: "${AZ_TENANT_ID:?Set AZ_TENANT_ID (the tenant these registrations and role assignments belong to)}"
: "${AZ_SUBSCRIPTION_ID:?Set AZ_SUBSCRIPTION_ID}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
: "${AZ_ACR_NAME:?Set AZ_ACR_NAME (create-acr.sh prints the generated name)}"
: "${AZ_ACA_APP_NAME:?Set AZ_ACA_APP_NAME (deploy-container-app.sh created it)}"

GH_ENVIRONMENT_NAME="${GH_ENVIRONMENT_NAME:-production}"
GH_DEPLOY_BRANCH="${GH_DEPLOY_BRANCH:-main}"
GH_REQUIRED_REVIEWER_LOGIN="${GH_REQUIRED_REVIEWER_LOGIN:-}"
BUILD_APP_NAME_PREFIX="${BUILD_APP_NAME_PREFIX:-gh-oidc-build-azgenai-lab}"
DEPLOY_APP_NAME_PREFIX="${DEPLOY_APP_NAME_PREFIX:-gh-oidc-deploy-azgenai-lab}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# Refusing to overwrite an existing record file is the whole reason step 0 is
# a step: a rerun over a live record loses the only list of what a previous
# run created, i.e. loses the ability to tear it down (D10.1).
if [ -e "$OIDC_RECORD_FILE" ]; then
  echo "OIDC_RECORD_FILE '$OIDC_RECORD_FILE' already exists." >&2
  echo "Refusing to overwrite it: that file is the only list of what a previous run created." >&2
  echo "Move it aside, or run delete-github-oidc.sh against it first." >&2
  exit 1
fi

# An `az`/`gh ... -o tsv`/`--jq` call that exits nonzero is already caught by
# set -e on the assignment. What that does NOT catch is a call that exits 0
# and prints nothing -- the empty-output-inside-a-command-substitution class
# that `set -e` never sees. An empty read is a failed read: it must abort,
# never be treated as "absent", "zero" or "not yet" (same discipline as every
# other script in this directory).
require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (empty output); aborting." >&2
    exit 1
  fi
}
require_guid() {
  local label="$1" value="$2"
  if [[ ! "$value" =~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$ ]]; then
    echo "Expected a GUID for $label; got '$value'." >&2
    exit 1
  fi
}

# Appended to immediately after each successful create (D10, step 2 of the
# implementation checklist). A resource that exists but was never recorded is
# the exact failure mode this ordering exists to prevent -- if the process is
# killed between an `az`/`gh` call returning and this line running, the id is
# already lost either way, so append-as-early-as-possible is the best this
# can do without a two-phase commit this script deliberately does not build
# (D10.1: no state machine, no reconcile -- a one-time interactive script).
record() {
  printf '%s=%s\n' "$1" "$2" >>"$OIDC_RECORD_FILE"
}

# Printed on any abort after something has been recorded, and again in the
# success summary. Day 24's teardown printer omitted two knobs the
# destination script actually needed and would have aborted teardown BEFORE
# the identity delete, leaving orphans. The record file's whole design point
# (D10.1) is that delete-github-oidc.sh needs nothing else -- every
# identifier it could need is already inside it -- so the invocation below
# really is complete with its one required variable, not an abbreviated
# reconstruction of one that omits something.
print_teardown_hint() {
  echo "  OIDC_RECORD_FILE=$OIDC_RECORD_FILE $SCRIPT_DIR/delete-github-oidc.sh"
}

teardown_hint() {
  local status=$?
  if [ "$status" -ne 0 ] && [ -e "$OIDC_RECORD_FILE" ] && [ -s "$OIDC_RECORD_FILE" ]; then
    echo >&2
    echo "ABORTED after creating some resources. Tear them down with:" >&2
    print_teardown_hint >&2
  fi
}
trap teardown_hint EXIT
# Signals turned into an exit rather than added to the EXIT trap's own list:
# create-entra-app.sh measured SIGHUP/SIGTERM running an EXIT trap TWICE with
# `$?` left at 0 on this bash, which would swallow the hint above at exactly
# the moment ("operator's terminal closed") it exists for.
trap 'exit 130' INT TERM HUP

# --- preflight: tenant, gh auth, nothing mutated yet ------------------------

if ! ACTIVE_TENANT="$(az account show --query tenantId -o tsv)"; then
  echo "Not signed in to Azure. Run: az login --tenant $AZ_TENANT_ID" >&2
  exit 1
fi
require_value "$ACTIVE_TENANT" "the active az tenant id"
if [[ "$(lower "$ACTIVE_TENANT")" != "$(lower "$AZ_TENANT_ID")" ]]; then
  echo "Active az tenant is $ACTIVE_TENANT, but AZ_TENANT_ID is $AZ_TENANT_ID." >&2
  echo "Run 'az login --tenant $AZ_TENANT_ID' first. Nothing was created." >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Not signed in to gh. Run: gh auth login" >&2
  exit 1
fi

# Repoints the default az context, which every bare `az ad`/`az role` call
# below reads even though `--subscription` is also passed explicitly on the
# ones that take it -- announced because it is a side effect on shared
# mutable state, same as deploy-container-app.sh stage 1.
az account set --subscription "$AZ_SUBSCRIPTION_ID"
echo "  default az context now points at $AZ_SUBSCRIPTION_ID"

ACR_ID="$(az acr show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_ACR_NAME" --query id -o tsv)"
require_value "$ACR_ID" "the container registry resource id"

ACA_APP_ID="$(az containerapp show --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_ACA_APP_NAME" --query id -o tsv)"
require_value "$ACA_APP_ID" "the container app resource id"

suffix() { python3 -c "import secrets; print(secrets.token_hex(4))"; }
BUILD_APP_DISPLAY_NAME="${BUILD_APP_NAME_PREFIX}-$(suffix)"
DEPLOY_APP_DISPLAY_NAME="${DEPLOY_APP_NAME_PREFIX}-$(suffix)"

WORK_DIR="$(mktemp -d)"
# bash EXIT traps replace, they don't stack -- so the work-dir cleanup is
# folded into the same trap as the abort hint rather than set separately,
# which would silently drop whichever one was set first.
trap 'teardown_hint; rm -rf "$WORK_DIR"' EXIT

# === step 0: open the record file ===========================================
echo "== step 0: open the record file =="
record CREATED_AT "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
record GITHUB_REPO "$GITHUB_REPO"
record AZ_TENANT_ID "$AZ_TENANT_ID"
record AZ_SUBSCRIPTION_ID "$AZ_SUBSCRIPTION_ID"
record AZ_RESOURCE_GROUP "$AZ_RESOURCE_GROUP"
record AZ_ACR_NAME "$AZ_ACR_NAME"
record AZ_ACR_ID "$ACR_ID"
record AZ_ACA_APP_NAME "$AZ_ACA_APP_NAME"
record AZ_ACA_APP_ID "$ACA_APP_ID"
record GH_ENVIRONMENT_NAME "$GH_ENVIRONMENT_NAME"
record GH_DEPLOY_BRANCH "$GH_DEPLOY_BRANCH"
record BUILD_APP_DISPLAY_NAME "$BUILD_APP_DISPLAY_NAME"
record DEPLOY_APP_DISPLAY_NAME "$DEPLOY_APP_DISPLAY_NAME"
echo "  record file: $OIDC_RECORD_FILE"

# === step 1: app registrations + service principals =========================
echo "== step 1: app registrations + service principals =="

# Directory replication makes service principal creation the flakiest call
# here: the application exists, the service principal endpoint has not seen
# it yet. Bounded retry, ported from create-entra-app.sh. Progress goes to
# stderr because stdout is the return value.
create_service_principal() {
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

# `az ad app create`'s whole response is read in one call rather than
# following up with `az ad app show` (which can 404 against a freshly
# created object while the directory replicates). The projection is
# `join(' ', [...])`, not `[appId,id]` -- create-entra-app.sh measured that an
# array projection with -o tsv prints one value PER LINE on az 2.88.0, and
# `join()` is what collapses the pair onto one space-separated line.
create_identity() {
  local label="$1" display_name="$2" record_prefix="$3"
  echo "Creating $label app registration '$display_name'..."
  local pair app_id object_id sp_id
  pair="$(az ad app create \
    --display-name "$display_name" \
    --sign-in-audience AzureADMyOrg \
    --query "join(' ', [appId, id])" -o tsv)"
  read -r app_id object_id <<<"$pair"
  require_guid "$label app id" "${app_id:-}"
  require_guid "$label app object id" "${object_id:-}"
  record "${record_prefix}_APP_ID" "$app_id"
  record "${record_prefix}_APP_OBJECT_ID" "$object_id"
  printf -v "${record_prefix}_APP_ID" '%s' "$app_id"
  echo "  $label app id: $app_id (object $object_id)"

  echo "Creating $label service principal..."
  sp_id="$(create_service_principal "$app_id" "$label")" || {
    echo "Could not create the $label service principal." >&2
    exit 1
  }
  require_guid "$label service principal id" "$sp_id"
  record "${record_prefix}_SP_ID" "$sp_id"
  printf -v "${record_prefix}_SP_ID" '%s' "$sp_id"
  echo "  $label service principal id: $sp_id"
}

create_identity "build" "$BUILD_APP_DISPLAY_NAME" BUILD
create_identity "deploy" "$DEPLOY_APP_DISPLAY_NAME" DEPLOY

# === step 2: federated credentials ===========================================
echo "== step 2: federated credentials =="

create_federated_credential() {
  local label="$1" app_id="$2" fic_name="$3" subject="$4" record_prefix="$5"
  local body_file="$WORK_DIR/${fic_name}.json"
  cat >"$body_file" <<JSON
{
  "name": "${fic_name}",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "${subject}",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
  local fic_id
  fic_id="$(az ad app federated-credential create --id "$app_id" \
    --parameters "@${body_file}" --query id -o tsv)"
  require_value "$fic_id" "the $label federated credential id"
  record "${record_prefix}_FIC_ID" "$fic_id"
  echo "  $label federated credential '$fic_name': subject=$subject"
}

BUILD_SUBJECT="repo:${GITHUB_REPO}:ref:refs/heads/${GH_DEPLOY_BRANCH}"
DEPLOY_SUBJECT="repo:${GITHUB_REPO}:environment:${GH_ENVIRONMENT_NAME}"
create_federated_credential "build" "$BUILD_APP_ID" "github-actions-build" "$BUILD_SUBJECT" BUILD
create_federated_credential "deploy" "$DEPLOY_APP_ID" "github-actions-deploy" "$DEPLOY_SUBJECT" DEPLOY

# === step 3: role assignments ================================================
echo "== step 3: role assignments =="

# --assignee-object-id + --assignee-principal-type, not --assignee: the plain
# form resolves the principal through Microsoft Graph, and a just-created
# service principal is not reliably there yet (same reasoning as
# deploy-container-app.sh's assign_role).
assign_role() {
  local label="$1" principal_id="$2" role="$3" scope="$4" record_prefix="$5"
  local assignment_id
  assignment_id="$(az role assignment create \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --assignee-object-id "$principal_id" \
    --assignee-principal-type ServicePrincipal \
    --role "$role" \
    --scope "$scope" \
    --query id -o tsv)"
  require_value "$assignment_id" "the $label role assignment id"
  record "${record_prefix}_ROLE_ASSIGNMENT_ID" "$assignment_id"
  echo "  $label: '$role' assignment created at $scope"
}

# Scopes are the ACR's and the container app's own resource ids -- not the
# resource group, not the subscription. A wider scope would let either
# identity touch every other resource that group holds, which is exactly the
# blast radius this two-identity design exists to avoid.
assign_role "build" "$BUILD_SP_ID" "AcrPush" "$ACR_ID" BUILD
assign_role "deploy" "$DEPLOY_SP_ID" "Container Apps Contributor" "$ACA_APP_ID" DEPLOY

# === step 4: persistence verification ========================================
echo "== step 4: persistence verification (control-plane only) =="

# --all is LOAD-BEARING, not noise -- Day 24's most severe finding was
# exactly this: `az role assignment list` documents its own default as
# subscription-scope only, so without --all this query returns 0
# unconditionally for every assignment here (both are resource-scoped) and
# this whole verification step is vacuous.
#
# What this DOES prove: the control-plane object is listed. What it does NOT
# prove: that the permission is effective. docs/managed-identity.md:89-94
# measured 14 minutes 44 seconds of propagation on Day 20 against Microsoft's
# documented "up to 5 minutes" -- so no sleep is added here to wait for
# effectiveness, because there is no measured number to wait for, and a
# guess dressed up as a wait is worse than no wait at all. The first real
# GitHub Actions run is the actual readiness probe; the summary this script
# prints says so.
verify_role_assignment_listed() {
  local label="$1" principal_id="$2" role="$3" scope="$4"
  local count
  count="$(az role assignment list \
    --subscription "$AZ_SUBSCRIPTION_ID" \
    --assignee-object-id "$principal_id" \
    --all \
    --fill-principal-name false \
    --role "$role" \
    --scope "$scope" \
    --query "length([])" -o tsv)"
  require_value "$count" "the $label role assignment read-back"
  if [ "$count" = "0" ]; then
    echo "The $label role assignment is not listed at $scope. Aborting before touching GitHub." >&2
    exit 1
  fi
  echo "  $label: listed ($count) — control-plane object confirmed, effectiveness NOT measured"
}
verify_role_assignment_listed "build" "$BUILD_SP_ID" "AcrPush" "$ACR_ID"
verify_role_assignment_listed "deploy" "$DEPLOY_SP_ID" "Container Apps Contributor" "$ACA_APP_ID"

# === step 5: GitHub environment ==============================================
echo "== step 5: GitHub environment '$GH_ENVIRONMENT_NAME' =="

REVIEWER_LOGIN="$GH_REQUIRED_REVIEWER_LOGIN"
if [ -z "$REVIEWER_LOGIN" ]; then
  REVIEWER_LOGIN="$(gh api user --jq .login)"
  require_value "$REVIEWER_LOGIN" "the authenticated GitHub user's login (set GH_REQUIRED_REVIEWER_LOGIN to override)"
fi
REVIEWER_ID="$(gh api "users/${REVIEWER_LOGIN}" --jq .id)"
require_value "$REVIEWER_ID" "the numeric user id for reviewer '$REVIEWER_LOGIN'"
record GH_REQUIRED_REVIEWER_LOGIN "$REVIEWER_LOGIN"
echo "  required reviewer: $REVIEWER_LOGIN (id $REVIEWER_ID)"

# No `gh environment` subcommand exists (checked: gh's command groups cover
# secret/variable but not environment protection settings), so this step is
# `gh api` against the documented REST endpoints throughout.
#
# prevent_self_review is sent explicitly as false rather than left to
# default: this is a single-operator repo, and "we deliberately did not
# check this box" is a decision this project's own discipline (Day 20's
# purge-protection flag, Day 24's allowInsecure) says to write down, not
# imply.
ENV_BODY_FILE="$WORK_DIR/environment.json"
cat >"$ENV_BODY_FILE" <<JSON
{
  "reviewers": [{"type": "User", "id": ${REVIEWER_ID}}],
  "prevent_self_review": false,
  "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}
}
JSON
gh api --method PUT "repos/${GITHUB_REPO}/environments/${GH_ENVIRONMENT_NAME}" \
  --input "$ENV_BODY_FILE" >/dev/null

gh api --method POST "repos/${GITHUB_REPO}/environments/${GH_ENVIRONMENT_NAME}/deployment-branch-policies" \
  -f name="$GH_DEPLOY_BRANCH" -f type=branch >/dev/null
record GH_ENVIRONMENT_CREATED "true"

# READ EVERY SETTING BACK AND COMPARE. This is the actual gate, not the PUT
# above: GitHub auto-creates an environment with NO protection rules the
# moment any workflow references a name that does not exist yet, so "the
# name matched" proves nothing about whether required-reviewer or the
# branch restriction actually took. Each field is its own `gh api --jq` call
# rather than one call parsed for everything, so a truncated or wrong-shaped
# response fails closed on `require_value` at the specific field that broke,
# not three checks later on a value that was silently empty.
CUSTOM_POLICY="$(gh api "repos/${GITHUB_REPO}/environments/${GH_ENVIRONMENT_NAME}" \
  --jq '.deployment_branch_policy.custom_branch_policies')"
require_value "$CUSTOM_POLICY" "the environment's custom_branch_policies read-back"
PROTECTED_BRANCHES="$(gh api "repos/${GITHUB_REPO}/environments/${GH_ENVIRONMENT_NAME}" \
  --jq '.deployment_branch_policy.protected_branches')"
require_value "$PROTECTED_BRANCHES" "the environment's protected_branches read-back"
REVIEWER_IDS="$(gh api "repos/${GITHUB_REPO}/environments/${GH_ENVIRONMENT_NAME}" \
  --jq '[.protection_rules[]? | select(.type=="required_reviewers") | .reviewers[]?.reviewer.id] | join(",")')"
require_value "$REVIEWER_IDS" "the environment's required-reviewer read-back"

if [ "$CUSTOM_POLICY" != "true" ] || [ "$PROTECTED_BRANCHES" != "false" ]; then
  echo "Environment '$GH_ENVIRONMENT_NAME' read back deployment_branch_policy as" >&2
  echo "  custom_branch_policies=$CUSTOM_POLICY protected_branches=$PROTECTED_BRANCHES," >&2
  echo "expected custom_branch_policies=true protected_branches=false. Aborting." >&2
  echo "(GitHub auto-creates an UNPROTECTED environment when referenced by a missing name --" >&2
  echo " the name existing is not a gate.)" >&2
  exit 1
fi
if [ "$REVIEWER_IDS" != "$REVIEWER_ID" ]; then
  echo "Environment '$GH_ENVIRONMENT_NAME' read back required reviewers as [$REVIEWER_IDS]," >&2
  echo "expected exactly [$REVIEWER_ID]. Aborting." >&2
  exit 1
fi

BRANCH_POLICIES="$(gh api "repos/${GITHUB_REPO}/environments/${GH_ENVIRONMENT_NAME}/deployment-branch-policies" \
  --jq '[.branch_policies[]?.name] | join(",")')"
require_value "$BRANCH_POLICIES" "the environment's deployment branch policy list read-back"
if [ "$BRANCH_POLICIES" != "$GH_DEPLOY_BRANCH" ]; then
  echo "Environment '$GH_ENVIRONMENT_NAME' read back deployment branch policies as [$BRANCH_POLICIES]," >&2
  echo "expected exactly [$GH_DEPLOY_BRANCH]. Aborting." >&2
  exit 1
fi
echo "  verified: required reviewer=$REVIEWER_LOGIN, deployment branch policy=$GH_DEPLOY_BRANCH only"

# === step 6: repository variables ============================================
echo "== step 6: repository variables =="

gh_var_set() {
  local name="$1" value="$2"
  gh variable set "$name" --repo "$GITHUB_REPO" --body "$value" >/dev/null
}
# `gh variable get` does exist and could serve this read-back (checked
# against the installed CLI: it supports the same --json/--jq contract this
# needs). This script uses `gh api` against the documented REST endpoint
# instead as a preference, not a necessity -- for consistency with step 5
# above, which already reads every piece of GitHub state through `gh api`
# rather than mixing subcommands and raw endpoints call by call.
gh_var_get() {
  gh api "repos/${GITHUB_REPO}/actions/variables/${1}" --jq .value
}

declare -a VAR_NAMES=(
  AZURE_TENANT_ID
  AZURE_SUBSCRIPTION_ID
  AZURE_CLIENT_ID_BUILD
  AZURE_CLIENT_ID_DEPLOY
  AZURE_ACR_NAME
  AZURE_RESOURCE_GROUP
  AZURE_CONTAINER_APP_NAME
)
declare -a VAR_VALUES=(
  "$AZ_TENANT_ID"
  "$AZ_SUBSCRIPTION_ID"
  "$BUILD_APP_ID"
  "$DEPLOY_APP_ID"
  "$AZ_ACR_NAME"
  "$AZ_RESOURCE_GROUP"
  "$AZ_ACA_APP_NAME"
)
for i in "${!VAR_NAMES[@]}"; do
  gh_var_set "${VAR_NAMES[$i]}" "${VAR_VALUES[$i]}"
  echo "  set ${VAR_NAMES[$i]}"
done
record GH_VARIABLES_WRITTEN "${VAR_NAMES[*]}"

# Read every one back rather than trusting the write's exit code -- the same
# fail-closed discipline this whole directory uses for `az`.
for i in "${!VAR_NAMES[@]}"; do
  name="${VAR_NAMES[$i]}"
  expected="${VAR_VALUES[$i]}"
  actual="$(gh_var_get "$name")"
  require_value "$actual" "the $name variable read-back"
  if [ "$actual" != "$expected" ]; then
    echo "Repository variable $name read back as '$actual', expected '$expected'. Aborting." >&2
    exit 1
  fi
done
echo "  all ${#VAR_NAMES[@]} variables verified"

# === step 7: arm the pipeline ================================================
echo "== step 7: arm the pipeline (DEPLOY_ENABLED) =="
# Deliberately the LAST mutation this script makes. Only reached after step
# 5's environment protection and step 6's variables have both read back
# clean -- if anything earlier had failed, execution would already have
# aborted and DEPLOY_ENABLED would never be set.
gh_var_set DEPLOY_ENABLED "true"
ARMED="$(gh_var_get DEPLOY_ENABLED)"
require_value "$ARMED" "the DEPLOY_ENABLED read-back"
if [ "$ARMED" != "true" ]; then
  echo "DEPLOY_ENABLED read back as '$ARMED', not 'true'. The pipeline may not be armed." >&2
  exit 1
fi
record DEPLOY_ENABLED_SET "true"
echo "  DEPLOY_ENABLED=true"

cat <<SUMMARY

Created and armed.
  build identity:  $BUILD_APP_DISPLAY_NAME (client id $BUILD_APP_ID)
                   AcrPush on $AZ_ACR_NAME
  deploy identity: $DEPLOY_APP_DISPLAY_NAME (client id $DEPLOY_APP_ID)
                   Container Apps Contributor on $AZ_ACA_APP_NAME
  environment:     $GH_ENVIRONMENT_NAME (reviewer $REVIEWER_LOGIN, branch $GH_DEPLOY_BRANCH only)
  record file:     $OIDC_RECORD_FILE

Persistence was verified at the control-plane only (step 4) -- role
propagation is not, and this project has measured it take up to ~15 minutes
(docs/managed-identity.md). The first real GitHub Actions run against this
pipeline is the actual readiness probe: if it fails on an authorization
error, that is what step 4 said it could not rule out, and the fix is to
re-run the workflow once propagation has had time to happen, not to change
this script.

This is ephemeral. Tear it down with:
SUMMARY
print_teardown_hint
