#!/usr/bin/env bash
# Syntax-gate every mermaid diagram in docs/diagrams/*.md by dry-rendering
# with a pinned mermaid-cli. mmdc reads Markdown directly and extracts fences.
set -euo pipefail
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# GitHub's Ubuntu 24.04 runners restrict unprivileged user namespaces via
# AppArmor, so Chromium's default sandbox cannot start there. The gate only
# renders repo-authored diagrams, so --no-sandbox is acceptable.
printf '{"args":["--no-sandbox"]}\n' > "$tmp/puppeteer-config.json"
status=0
for f in docs/diagrams/*.md; do
  if ! npx --yes --package @mermaid-js/mermaid-cli@11.12.0 \
      mmdc -i "$f" -o "$tmp/$(basename "$f")" --quiet \
      -p "$tmp/puppeteer-config.json"; then
    echo "mermaid syntax failure: $f" >&2
    status=1
  fi
done
exit $status
