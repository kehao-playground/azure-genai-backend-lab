"""Tests for scripts/check_doc_anchors.py.

A link checker's worst failure is silence. If it does not understand a
construct it prints "all resolve", which looks exactly like a clean
repository -- so a regression does not go red, it goes quietly green. Two
hand-rolled versions of this gate did that between them for nine constructs,
and one of them (emoji in a heading) is not exotic at all.

So the tests are in three layers:

1. a **differential corpus** against github-slugger's own output, frozen at
   vendoring time so it runs offline;
2. **structure probes**, one per construct that silently passed before;
3. **end-to-end fixtures** where every broken case must exit non-zero and
   every GitHub-legal case must exit zero.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_doc_anchors.py"
CORPUS = Path(__file__).resolve().parent / "data" / "github_slugger_corpus.json"


def _load():
    spec = importlib.util.spec_from_file_location("check_doc_anchors", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_doc_anchors"] = module
    spec.loader.exec_module(module)
    return module


module = _load()
corpus = json.loads(CORPUS.read_text(encoding="utf-8"))


def _run(cwd: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    for name, content in files.items():
        path = cwd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (cwd / "scripts").mkdir(exist_ok=True)
    for name in ("check_doc_anchors.py", "_github_slugger_table.py"):
        (cwd / "scripts" / name).write_text(
            (SCRIPT.parent / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
    return subprocess.run(
        [sys.executable, str(cwd / "scripts" / "check_doc_anchors.py")],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


# --- layer 1: differential against the reference implementation --------------


@pytest.mark.parametrize(("heading", "expected"), corpus["cases"])
def test_slug_matches_github_slugger(heading: str, expected: str) -> None:
    """Every case here was produced by running github-slugger itself."""
    assert module.slug(heading) == expected


def test_emoji_heading_is_the_case_that_settled_it() -> None:
    """`## Emoji 🚀 test` slugs to emoji--test; a hand-rolled class kept the emoji."""
    assert module.slug("Emoji 🚀 test") == "emoji--test"


def test_duplicate_sequence_matches_reference_ordering() -> None:
    """Foo, Foo-1, Foo must become foo, foo-1, foo-2 -- a real collision case."""
    headings = [h for h, _ in corpus["sequence"]]
    expected = {s for _, s in corpus["sequence"]}
    doc = "\n\n".join(f"## {h}" for h in headings)
    tokens = module._parser().parse(doc)
    assert module.anchors_of(tokens) == expected


# --- layer 2: structure probes ----------------------------------------------


def _anchors(doc: str) -> set[str]:
    return module.anchors_of(module._parser().parse(doc))


def test_heading_inside_fenced_code_is_not_an_anchor() -> None:
    assert _anchors("# T\n\n```md\n## Phantom\n```\n") == {"t"}


def test_heading_inside_html_comment_is_not_an_anchor() -> None:
    assert _anchors("# T\n\n<!--\n## Phantom\n-->\n") == {"t"}


def test_illegal_fence_info_does_not_open_a_code_block() -> None:
    """A backtick in a backtick-fence info string is not a fence opener.

    The hand-rolled version treated it as one and swallowed everything after
    it, hiding live links inside a block that does not exist.
    """
    doc = "# T\n\n``` a`b\n## Real\n```\n"
    assert "real" in _anchors(doc)


def test_entity_is_decoded_before_slugging() -> None:
    """`# A &amp; B` renders as `A & B`, so GitHub's anchor is a--b."""
    assert _anchors("# A &amp; B") == {"a--b"}


def test_setext_headings_produce_anchors() -> None:
    assert _anchors("Setext Title\n===\n\nSub Section\n---\n") == {
        "setext-title",
        "sub-section",
    }


def test_unused_reference_definition_is_not_a_link() -> None:
    """A stale definition nobody references must not manufacture a finding."""
    tokens = module._parser().parse("# T\n\n[unused]: #does-not-exist\n")
    assert module.links_of(tokens) == []


def test_used_reference_link_is_resolved() -> None:
    tokens = module._parser().parse("# T\n\n[go][r]\n\n[r]: #target\n")
    assert "#target" in module.links_of(tokens)


# --- layer 3: end-to-end, broken red / legal green ---------------------------


def test_broken_emoji_anchor_fails(tmp_path: Path) -> None:
    """The exact fail-open that refuted the previous docstring."""
    doc = "# T\n\n## Emoji 🚀 test\n\n[broken](#emoji-🚀-test)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 1
    assert "MISSING_ANCHOR" in result.stderr


def test_legal_emoji_anchor_passes(tmp_path: Path) -> None:
    doc = "# T\n\n## Emoji 🚀 test\n\n[valid](#emoji--test)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


def test_phantom_heading_in_fence_does_not_vouch_for_a_link(tmp_path: Path) -> None:
    doc = "# T\n\n```md\n## Phantom\n```\n\n[bad](#phantom)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 1
    assert "MISSING_ANCHOR #phantom" in result.stderr


def test_link_inside_fenced_code_is_not_checked(tmp_path: Path) -> None:
    doc = "# T\n\n## Real\n\n```md\n[example](#not-real)\n```\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


def test_non_ascii_fragment_is_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": "# T\n\n## Real\n\n[bad](#不存在)\n"})
    assert result.returncode == 1
    assert "MISSING_ANCHOR #不存在" in result.stderr


def test_percent_encoded_fragment_resolves(tmp_path: Path) -> None:
    frag = quote("中文段落")
    result = _run(tmp_path, {"doc.md": f"# T\n\n## 中文段落\n\n[ok](#{frag})\n"})
    assert result.returncode == 0, result.stderr


def test_percent_encoded_target_path_resolves(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {
            "docs/a b.md": "# Title\n\n## Some Section\n",
            "docs/linker.md": "[ok](a%20b.md#some-section)\n",
        },
    )
    assert result.returncode == 0, result.stderr


def test_escaped_parenthesis_destination_is_checked(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {
            "docs/foo(bar).md": "# Title\n\n## Real\n",
            "docs/linker.md": "[bad](foo\\(bar\\).md#missing)\n",
        },
    )
    assert result.returncode == 1
    assert "MISSING_ANCHOR #missing" in result.stderr


def test_link_with_title_is_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": '# T\n\n## Real\n\n[bad](#missing "a title")\n'})
    assert result.returncode == 1


def test_angle_bracket_destination_is_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": "# T\n\n## Real\n\n[bad](<#missing>)\n"})
    assert result.returncode == 1


def test_reference_style_link_is_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": "# T\n\n## Real\n\n[bad][r]\n\n[r]: #missing\n"})
    assert result.returncode == 1


def test_both_duplicate_anchors_resolve(tmp_path: Path) -> None:
    doc = "# T\n\n## Notes\n\n## Notes\n\n[first](#notes)\n[second](#notes-1)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


def test_stale_cross_file_anchor_fails(tmp_path: Path) -> None:
    """Heading renamed in one file, link left behind in another -- shipped twice."""
    result = _run(
        tmp_path,
        {
            "docs/target.md": "# Title\n\n## Renamed Section\n",
            "docs/linker.md": "See [it](target.md#some-section).\n",
        },
    )
    assert result.returncode == 1
    assert "MISSING_ANCHOR #some-section" in result.stderr
    assert "linker.md" in result.stderr


def test_resolving_cross_file_anchor_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {
            "docs/target.md": "# Title\n\n## Some Section\n",
            "docs/linker.md": "See [it](target.md#some-section).\n",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "all resolve" in result.stdout


def test_same_file_anchor_is_checked(tmp_path: Path) -> None:
    doc = "# Title\n\n## Real Section\n\nJump to [nowhere](#not-a-section).\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 1


def test_external_links_are_not_fetched(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": "# Title\n\n[spec](https://example.com/p#frag)\n"})
    assert result.returncode == 0, result.stderr


def test_front_matter_is_not_parsed_as_content(tmp_path: Path) -> None:
    doc = "---\ntitle: x\n---\n\n# T\n\n## Real\n\n[ok](#real)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


def test_repo_itself_is_clean() -> None:
    """The gate must pass on this repository, or CI is about to go red."""
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- the vendored table must stay traceable to its source -------------------


# The sha256 of scripts/_github_slugger_table.py as generated by
# scripts/vendor_github_slugger.py against github-slugger 2.0.0, frozen here
# at the same moment as the differential corpus below it.
#
# What pinning it buys, stated exactly: the 40-case corpus only exercises the
# code points those headings touch, so a hand edit to a range nobody tests
# would otherwise pass. This makes ANY byte change to the table fail, offline.
#
# What it does not buy: proof that the table matches upstream github-slugger.
# Both this hash and the table live in this repository, so a determined edit
# could move them together. Only `scripts/vendor_github_slugger.py --check`,
# which regenerates from upstream and needs the network, can prove that -- and
# CI deliberately does not run it.
TABLE_SHA256 = "04982c19b035f4d0a83f0804d823992246bbb1209a4579b4749c61e8ba3be18f"


def test_vendored_table_is_unmodified_since_it_was_generated() -> None:
    """Offline integrity guard. See TABLE_SHA256 above for its exact scope."""
    table = SCRIPT.parent / "_github_slugger_table.py"
    digest = hashlib.sha256(table.read_bytes()).hexdigest()
    assert digest == TABLE_SHA256, (
        "scripts/_github_slugger_table.py changed. If that was deliberate, "
        "regenerate it with scripts/vendor_github_slugger.py, refresh the "
        "differential corpus alongside it, and update TABLE_SHA256 in the same "
        "commit so the three stay provably in step."
    )


def test_vendored_table_records_its_provenance() -> None:
    spec = importlib.util.spec_from_file_location(
        "_github_slugger_table", SCRIPT.parent / "_github_slugger_table.py"
    )
    assert spec and spec.loader
    table = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(table)
    assert table.SLUGGER_VERSION == "2.0.0"
    assert len(table.REGEX_JS_SHA256) == 64
    assert len(table.INDEX_JS_SHA256) == 64
    # Sanity: the ranges must actually cover the case that motivated vendoring.
    assert any(lo <= 0x1F680 <= hi for lo, hi in table.REMOVED_RANGES)  # 🚀 dropped
    assert not any(lo <= 0x4E2D <= hi for lo, hi in table.REMOVED_RANGES)  # 中 kept
