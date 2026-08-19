#!/usr/bin/env bash
# Freshness guard for the deploy job.
#
# Deployment approval takes human time. A run can sit waiting for a reviewer
# while main moves on underneath it -- the commit that gets approved is not
# guaranteed to still be main's HEAD by the time approval lands. Without this
# guard, an approved-but-stale run would deploy an old commit and still
# report success: a "green, deployed" run and a "green, did nothing useful"
# run would look identical afterwards. This script exists so they don't --
# staleness is a hard failure, not a silent no-op.
#
# Fails closed: if the query to GitHub itself fails, or returns nothing, that
# is treated exactly like staleness, never like "probably fine". An unknown
# HEAD is never treated as a match.
#
# Usage: scripts/check_freshness.sh <current-sha> <branch>
#
# Required env vars:
#   GITHUB_REPO - "owner/repo" slug to query (e.g. github.repository)
#   GH_TOKEN or GITHUB_TOKEN - read by `gh` for authentication (the workflow
#                              step sets this from secrets.GITHUB_TOKEN)
set -euo pipefail

CURRENT_SHA="${1:?usage: scripts/check_freshness.sh current-sha branch}"
BRANCH="${2:?usage: scripts/check_freshness.sh current-sha branch}"
: "${GITHUB_REPO:?Set GITHUB_REPO to the owner/repo slug to query}"

# An `az`/`gh ... --jq` call that exits nonzero is already caught by set -e
# on the assignment below. What that does NOT catch is a call that exits 0
# and prints nothing -- same discipline as every other script in this repo:
# an empty read is a failed read, never "absent" or "not yet".
require_value() {
  local val="$1" label="$2"
  if [ -z "$val" ]; then
    echo "Failed to read $label (empty output); failing closed." >&2
    exit 1
  fi
}

HEAD_SHA=""
if ! HEAD_SHA="$(gh api "repos/${GITHUB_REPO}/commits/${BRANCH}" --jq .sha)"; then
  echo "Failed to query ${BRANCH}'s HEAD sha for ${GITHUB_REPO} from the GitHub API." >&2
  echo "Failing closed: an unqueryable HEAD is never treated as fresh." >&2
  exit 1
fi
require_value "$HEAD_SHA" "${BRANCH}'s HEAD sha"

if [ "$CURRENT_SHA" != "$HEAD_SHA" ]; then
  echo "This run's commit is no longer ${BRANCH}'s HEAD." >&2
  echo "  this run:    $CURRENT_SHA" >&2
  echo "  current HEAD: $HEAD_SHA" >&2
  echo "main moved on, most likely while this run was waiting for deployment" >&2
  echo "approval. Refusing to deploy a commit main has already moved past." >&2
  exit 1
fi

echo "Fresh: $CURRENT_SHA is still ${BRANCH}'s HEAD."
