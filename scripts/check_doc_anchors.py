#!/usr/bin/env python3
"""Fail if any Markdown file links to a heading anchor that does not exist.

This gate exists because the same bug shipped twice in one review topic. A
section in `docs/ci-cd.md` was retitled, and the `#…` fragment that named it
went stale in three places. The first sweep found two of them, because its
scope was *the file being retitled* -- and the third link lived in a different
file entirely (`docs/diagrams/cicd-defense-boundaries.md`), which is exactly
where a scoped sweep cannot look. A cross-file reference cannot be checked by
reading one file, so this check reads all of them.

What it does: collect every ATX heading in every tracked Markdown file, turn
each into the fragment GitHub would generate, then resolve every
`path.md#fragment` link in the repo against that set. Same-file links (`#foo`)
are checked too.

Slugging follows GitHub's rule closely enough for this repo's headings:
lowercase, drop everything that is not a word character / space / hyphen,
then spaces to hyphens. Headings that would collide (GitHub appends `-1`,
`-2`) are reported, since a collision makes the fragment ambiguous rather
than broken -- better to rename the heading.

Usage: scripts/check_doc_anchors.py  (exit 0 clean, 1 with findings)
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
# Markdown inline links whose target carries a fragment. Bare autolinks and
# reference-style links are out of scope: this repo does not use them for
# intra-doc anchors, and guessing at them would produce false positives.
LINK = re.compile(r"\]\(\s*([^)\s]*?)#([A-Za-z0-9_-]+)\s*\)")


def slug(heading: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", heading)  # inline code contributes its text
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # link text, not the URL
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    # One hyphen per whitespace character, NOT per run: GitHub keeps the gap a
    # dropped punctuation mark leaves behind, so "Exactly-once & delivery"
    # becomes "exactly-once--delivery" with two hyphens. Collapsing runs here
    # made this gate report thirteen false MISSING_ANCHORs on its first run.
    return re.sub(r"\s", "-", text).strip("-")


def tracked_markdown(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / line for line in out.stdout.splitlines() if line]


def anchors_of(path: Path) -> tuple[set[str], list[str]]:
    """Return (fragment set, duplicate fragments) for one file."""
    text = path.read_text(encoding="utf-8")
    slugs = [slug(m.group(2)) for m in HEADING.finditer(text)]
    counts = Counter(s for s in slugs if s)
    return set(counts), sorted(s for s, n in counts.items() if n > 1)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    files = tracked_markdown(root)
    anchors: dict[Path, set[str]] = {}
    dupe_frags: dict[Path, set[str]] = {}
    for f in files:
        found, dupes = anchors_of(f)
        anchors[f.resolve()] = found
        dupe_frags[f.resolve()] = set(dupes)

    problems: list[str] = []
    checked = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        for m in LINK.finditer(text):
            target, fragment = m.group(1), m.group(2)
            if target.startswith(("http://", "https://")):
                continue  # external; this gate does not fetch
            dest = (f.parent / target).resolve() if target else f.resolve()
            if dest.suffix != ".md":
                continue
            checked += 1
            if dest not in anchors:
                problems.append(
                    f"{f.relative_to(root)}: link to untracked or missing file {target}"
                )
            elif fragment not in anchors[dest]:
                problems.append(
                    f"{f.relative_to(root)}: MISSING_ANCHOR #{fragment} "
                    f"in {target or f.name}"
                )
            elif fragment in dupe_frags[dest]:
                # A duplicate nobody links to is harmless; one that is linked
                # is ambiguous, because GitHub disambiguates with -1/-2 and
                # the bare fragment silently resolves to the first heading.
                problems.append(
                    f"{f.relative_to(root)}: AMBIGUOUS_ANCHOR #{fragment} "
                    f"in {target or f.name} (heading appears more than once)"
                )

    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        print(f"\n{len(problems)} anchor problem(s) across {len(files)} files.", file=sys.stderr)
        return 1
    print(f"{checked} anchor link(s) across {len(files)} markdown files: all resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
