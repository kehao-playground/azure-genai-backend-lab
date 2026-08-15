#!/usr/bin/env bash
# Syntax-gate every mermaid diagram in docs/diagrams/*.md by dry-rendering
# with a pinned mermaid-cli. mmdc reads Markdown directly and extracts fences.
#
# The pin must match the one the article figures are rendered with — the
# planning repo's scripts/render_diagram.sh. A gate on an older mermaid-cli
# than the renderer only proves the diagram parsed under something nobody
# publishes with, so a syntax difference between the two would surface at
# render time instead of here. Bump both together, or not at all.
set -euo pipefail
MMDC_VERSION=11.16.0
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# GitHub's Ubuntu 24.04 runners restrict unprivileged user namespaces via
# AppArmor, so Chromium's default sandbox cannot start there. The gate only
# renders repo-authored diagrams, so --no-sandbox is acceptable.
printf '{"args":["--no-sandbox"]}\n' > "$tmp/puppeteer-config.json"
status=0
for f in docs/diagrams/*.md docs/diagrams/*.mmd; do
  [ -e "$f" ] || continue
  if ! npx --yes --package "@mermaid-js/mermaid-cli@${MMDC_VERSION}" \
      mmdc -i "$f" -o "$tmp/$(basename "$f").png" --quiet \
      -p "$tmp/puppeteer-config.json"; then
    echo "mermaid syntax failure: $f" >&2
    status=1
  fi
done
exit $status
