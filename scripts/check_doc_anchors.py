#!/usr/bin/env python3
"""Fail if any Markdown file links to a heading anchor that does not exist.

This gate exists because the same bug shipped twice in one review topic: a
section in `docs/ci-cd.md` was retitled and the `#…` fragment naming it went
stale in three places. The first manual sweep found two, because its scope was
*the file being retitled* -- and the third link lived in another file, which is
exactly where a scoped sweep cannot look. A cross-file reference cannot be
checked by reading one file, so this reads all of them.

The gate's own failure mode is worse than the bug: a checker that does not
recognise a link syntax reports "all resolve" and is indistinguishable from a
clean repository. Its first version did exactly that for CJK fragments, for
links carrying a title, for reference-style links, and for headings that only
existed inside a fenced code block -- while also rejecting two *legitimate*
GitHub anchors, because it treated duplicate headings as ambiguous when GitHub
deterministically assigns `foo`, `foo-1`, `foo-2`. Every one of those is now a
fixture in tests/unit/test_check_doc_anchors.py.

What it models, and how faithfully:

* Front matter and fenced code (``` and ~~~, any fence length, with info
  strings) are removed before anything is parsed, so neither headings nor
  links inside them count.
* Headings: ATX (`#`..`######`, up to three leading spaces, optional closing
  hashes) and Setext (`===` / `---` underlines).
* Slugs follow github-slugger: lowercase, drop HTML tags, remove the same
  punctuation ranges, then one hyphen per space -- runs are NOT collapsed
  (`Exactly-once & delivery` becomes `exactly-once--delivery`) and leading or
  trailing hyphens are NOT trimmed.
* Repeated slugs get GitHub's `-1`, `-2` suffixes in document order, so both
  `#notes` and `#notes-1` resolve against two `## Notes` headings.
* Links: inline (with optional title, and `<…>` destinations) and
  reference-style definitions. Fragments may be Unicode or percent-encoded;
  they are decoded before comparison.

Where it is still an approximation: emphasis and inline HTML in heading text
are unwrapped by regex rather than by a Markdown parser, so an exotic heading
could slug differently here than on GitHub. That direction fails *closed* --
it reports a missing anchor for a link that works -- which is noisy but safe.

Usage: scripts/check_doc_anchors.py  (exit 0 clean, 1 with findings)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

# --- structure stripping -----------------------------------------------------

FRONT_MATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)
FENCE = re.compile(r"^(?P<indent>[ ]{0,3})(?P<fence>```+|~~~+)(?P<info>[^\r\n]*)$")

# --- headings ----------------------------------------------------------------

ATX = re.compile(r"^[ ]{0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
SETEXT_UNDERLINE = re.compile(r"^[ ]{0,3}(=+|-+)[ \t]*$")

# --- links -------------------------------------------------------------------

# Inline: [text](dest "title") / [text](<dest> 'title') / [text](#frag)
INLINE_LINK = re.compile(
    r"\]\(\s*"
    r"(?:<(?P<angle>[^>\n]*)>|(?P<bare>[^()\s]*))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?"
    r"\s*\)"
)
# Reference definition: [label]: dest "title"
REF_DEF = re.compile(
    r"^[ ]{0,3}\[[^\]]+\]:\s*"
    r"(?:<(?P<angle>[^>\n]*)>|(?P<bare>\S+))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*$",
    re.MULTILINE,
)

# github-slugger removes this punctuation set before hyphenating. Written as
# explicit escapes: the ranges include control characters, and pasting them
# literally puts NUL bytes in the source (this file would not even parse).
# Notably absent from the set: "-" and "_", which survive into the slug.
SLUG_STRIP = re.compile(
    "["
    "\u0000-\u001f"          # control
    "\u0021-\u002c"          # ! " # $ % & ' ( ) * + ,
    "\u002e\u002f"           # . /
    "\u003a-\u0040"          # : ; < = > ? @
    "\u005b-\u005e"          # [ \ ] ^
    "\u0060"                  # `
    "\u007b-\u00a0"          # { | } ~ DEL and C1
    "\u00a1\u00a7\u00ab\u00b6\u00b7\u00bb\u00bf"
    "\u2010-\u2027\u2030-\u205e"   # general punctuation
    "\u3001-\u3003\u3008-\u3011\u3014-\u301f"   # CJK punctuation/brackets
    "\uff01-\uff03\uff05-\uff0a\uff0c-\uff0f"   # fullwidth
    "\uff1a\uff1b\uff1f\uff20\uff3b-\uff3d\uff5f\uff60"
    "]"
)
HTML_TAG = re.compile(r"<[!/a-zA-Z][^>]*>")


def strip_structure(text: str) -> str:
    """Remove front matter and fenced code, preserving line count."""
    text = FRONT_MATTER.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out: list[str] = []
    closing: str | None = None
    for line in text.split("\n"):
        if closing is None:
            m = FENCE.match(line)
            if m:
                closing = m.group("fence")[0] * len(m.group("fence"))
                out.append("")
                continue
            out.append(line)
        else:
            stripped = line.strip()
            if stripped.startswith(closing) and set(stripped) <= {closing[0]}:
                closing = None
            out.append("")
    return "\n".join(out)


def heading_text(raw: str) -> str:
    text = HTML_TAG.sub("", raw)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links/images -> their text
    text = re.sub(r"!?\[([^\]]*)\]\[[^\]]*\]", r"\1", text)  # reference links
    text = re.sub(r"`+([^`]*)`+", r"\1", text)  # inline code -> its content
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)  # strong
    text = re.sub(r"(?<![\w\\])([*_])(?!\s)(.+?)(?<!\s)\1(?![\w])", r"\2", text)  # emphasis
    return text


def slug(raw: str) -> str:
    text = heading_text(raw).strip().lower()
    text = HTML_TAG.sub("", text)
    text = SLUG_STRIP.sub("", text)
    # One hyphen per space, NOT per run: GitHub keeps the gap that dropped
    # punctuation leaves behind, so "Exactly-once & delivery" becomes
    # "exactly-once--delivery". Collapsing runs made this gate's first run
    # report thirteen false MISSING_ANCHORs. Leading/trailing hyphens stay.
    return text.replace(" ", "-")


def headings_of(text: str) -> list[str]:
    lines = strip_structure(text).split("\n")
    found: list[str] = []
    for i, line in enumerate(lines):
        m = ATX.match(line)
        if m:
            found.append(m.group(2))
            continue
        # A Setext underline only applies to a preceding non-blank line that is
        # not itself a heading or a list item -- otherwise a `- item` followed
        # by `---` would be read as a heading.
        if (
            i + 1 < len(lines)
            and SETEXT_UNDERLINE.match(lines[i + 1])
            and line.strip()
            and not re.match(r"^[ ]{0,3}([-*+]|\d+\.)\s", line)
        ):
            found.append(line.strip())
    return found


def anchors_of(text: str) -> set[str]:
    """Slugs GitHub would generate, including its -1/-2 duplicate sequence."""
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for raw in headings_of(text):
        base = slug(raw)
        if not base:
            continue
        n = seen.get(base, 0)
        anchors.add(base if n == 0 else f"{base}-{n}")
        seen[base] = n + 1
    return anchors


def fragments_of(text: str) -> list[tuple[str, str]]:
    """(destination, fragment) for every link carrying a fragment."""
    body = strip_structure(text)
    out: list[tuple[str, str]] = []
    for pattern in (INLINE_LINK, REF_DEF):
        for m in pattern.finditer(body):
            dest = m.group("angle")
            if dest is None:
                dest = m.group("bare") or ""
            if "#" not in dest:
                continue
            target, _, fragment = dest.partition("#")
            if fragment:
                out.append((target, unquote(fragment)))
    return out


def tracked_markdown(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=root, capture_output=True, text=True, check=True
    )
    return [root / line for line in out.stdout.splitlines() if line]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    files = tracked_markdown(root)
    texts = {f.resolve(): f.read_text(encoding="utf-8") for f in files}
    anchors = {path: anchors_of(text) for path, text in texts.items()}

    problems: list[str] = []
    checked = 0
    for f in files:
        for target, fragment in fragments_of(texts[f.resolve()]):
            if target.startswith(("http://", "https://", "mailto:")):
                continue  # external; this gate does not fetch
            dest = (f.parent / target).resolve() if target else f.resolve()
            if dest.suffix != ".md":
                continue
            checked += 1
            rel = f.relative_to(root)
            if dest not in anchors:
                problems.append(f"{rel}: link to untracked or missing file {target}")
            elif fragment not in anchors[dest]:
                problems.append(
                    f"{rel}: MISSING_ANCHOR #{fragment} in {target or f.name}"
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
