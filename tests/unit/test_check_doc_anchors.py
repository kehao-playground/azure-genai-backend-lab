"""Tests for scripts/check_doc_anchors.py.

A link checker's worst failure is silence. If it does not recognise a syntax,
it prints "all resolve" and looks exactly like a clean repository -- so a
regression here does not go red, it goes quietly green. The first version of
this gate failed open on four syntaxes at once and simultaneously rejected two
*legitimate* GitHub anchors. Every one of those is a fixture below, and every
broken case asserts a non-zero exit, because a test that only checks the happy
path cannot tell a working checker from a blind one.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_doc_anchors.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_doc_anchors", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_doc_anchors"] = module
    spec.loader.exec_module(module)
    return module


module = _load()


def _run(cwd: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    for name, content in files.items():
        path = cwd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (cwd / "scripts").mkdir(exist_ok=True)
    (cwd / "scripts" / "check_doc_anchors.py").write_text(
        SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
    return subprocess.run(
        [sys.executable, str(cwd / "scripts" / "check_doc_anchors.py")],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


# --- slug fidelity -----------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Exactly-once & delivery", "exactly-once--delivery"),
        ("3.1 G1 — the fences were forgeable (fixed)", "31-g1--the-fences-were-forgeable-fixed"),
        (
            "11. What the live session settled, and what is still open",
            "11-what-the-live-session-settled-and-what-is-still-open",
        ),
        ("`needs` is the gate", "needs-is-the-gate"),
        ("**Bold** heading", "bold-heading"),
        ("*emphasis* and _underscore emphasis_", "emphasis-and-underscore-emphasis"),
        ("<em>html</em> emphasis", "html-emphasis"),
        ("[linked](https://example.com) heading", "linked-heading"),
        ("中文段落", "中文段落"),
        ("keep_the_underscores", "keep_the_underscores"),
    ],
)
def test_slug_matches_github_rules(heading: str, expected: str) -> None:
    assert module.slug(heading) == expected


def test_dropped_punctuation_leaves_its_gap() -> None:
    """The bug that made this gate's first run useless: collapsing space runs."""
    assert module.slug("a & b") == "a--b"
    assert module.slug("a b") == "a-b"


def test_duplicate_headings_follow_githubs_numbering() -> None:
    """GitHub assigns foo, foo-1, foo-2 -- it does not make the bare slug ambiguous."""
    anchors = module.anchors_of("# T\n\n## Notes\n\n## Notes\n\n## Notes\n")
    assert {"notes", "notes-1", "notes-2"} <= anchors


# --- fail-open probes: each of these silently passed before ------------------


def test_non_ascii_fragment_is_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": "# T\n\n## Real\n\n[bad](#不存在)\n"})
    assert result.returncode == 1
    assert "MISSING_ANCHOR #不存在" in result.stderr


def test_percent_encoded_fragment_resolves(tmp_path: Path) -> None:
    frag = quote("中文段落")
    result = _run(tmp_path, {"doc.md": f"# T\n\n## 中文段落\n\n[ok](#{frag})\n"})
    assert result.returncode == 0, result.stderr


def test_link_with_title_is_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": '# T\n\n## Real\n\n[bad](#missing "a title")\n'})
    assert result.returncode == 1
    assert "MISSING_ANCHOR #missing" in result.stderr


def test_angle_bracket_destination_is_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": "# T\n\n## Real\n\n[bad](<#missing>)\n"})
    assert result.returncode == 1
    assert "MISSING_ANCHOR #missing" in result.stderr


def test_reference_style_link_is_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": "# T\n\n## Real\n\n[bad][r]\n\n[r]: #missing\n"})
    assert result.returncode == 1
    assert "MISSING_ANCHOR #missing" in result.stderr


def test_heading_inside_fenced_code_is_not_an_anchor(tmp_path: Path) -> None:
    """A ```md block documenting a heading must not create a real anchor."""
    doc = "# T\n\n```md\n## Phantom\n```\n\n[bad](#phantom)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 1
    assert "MISSING_ANCHOR #phantom" in result.stderr


def test_link_inside_fenced_code_is_not_checked(tmp_path: Path) -> None:
    """Sample markdown in a code block is illustration, not a live link."""
    doc = "# T\n\n## Real\n\n```md\n[example](#not-real)\n```\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


def test_tilde_fence_is_also_stripped(tmp_path: Path) -> None:
    doc = "# T\n\n~~~md\n## Phantom\n~~~\n\n[bad](#phantom)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 1


def test_front_matter_is_not_parsed_as_content(tmp_path: Path) -> None:
    doc = "---\ntitle: x\n---\n\n# T\n\n## Real\n\n[ok](#real)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


# --- false-positive probes: each of these wrongly failed before --------------


def test_setext_headings_produce_anchors(tmp_path: Path) -> None:
    doc = "Setext Title\n============\n\nSub Section\n-----------\n\n[ok](#sub-section)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


def test_both_duplicate_anchors_resolve(tmp_path: Path) -> None:
    doc = "# T\n\n## Notes\n\n## Notes\n\n[first](#notes)\n[second](#notes-1)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


def test_unlinked_duplicate_heading_is_not_a_finding(tmp_path: Path) -> None:
    result = _run(tmp_path, {"doc.md": "# T\n\n## Notes\n\ntext\n\n## Notes\n"})
    assert result.returncode == 0, result.stderr


# --- the shape that shipped twice -------------------------------------------


def test_stale_cross_file_anchor_fails(tmp_path: Path) -> None:
    """Heading renamed in one file, link left behind in another."""
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
    assert "MISSING_ANCHOR #not-a-section" in result.stderr


def test_external_links_are_not_fetched(tmp_path: Path) -> None:
    doc = "# Title\n\n[spec](https://example.com/page#fragment)\n"
    result = _run(tmp_path, {"doc.md": doc})
    assert result.returncode == 0, result.stderr


def test_repo_itself_is_clean() -> None:
    """The gate must pass on this repository, or CI is about to go red."""
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
