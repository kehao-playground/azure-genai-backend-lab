"""Tests for scripts/check_doc_anchors.py.

The gate's failure mode is silence: a slug rule that is subtly wrong either
rejects good links (its first run produced thirteen false MISSING_ANCHORs by
collapsing whitespace runs) or, worse, accepts stale ones and reports clean
forever. Every test here therefore pins a behaviour that must be observable
in the output, and the broken cases assert the gate actually fails.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

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


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Exactly-once & delivery", "exactly-once--delivery"),
        ("3.1 G1 — the fences were forgeable (fixed)", "31-g1--the-fences-were-forgeable-fixed"),
        ("11. What the live session settled, and what is still open",
         "11-what-the-live-session-settled-and-what-is-still-open"),
        ("`needs` is the gate", "needs-is-the-gate"),
        ("**Bold** heading", "bold-heading"),
    ],
)
def test_slug_matches_github_rules(heading: str, expected: str) -> None:
    assert module.slug(heading) == expected


def test_dropped_punctuation_leaves_its_gap() -> None:
    """The bug that made this gate's first run useless.

    GitHub emits one hyphen per space, so punctuation removed from between two
    words leaves two hyphens behind. Collapsing runs to a single hyphen turns
    every such heading into a false missing anchor.
    """
    assert module.slug("a & b") == "a--b"
    assert module.slug("a b") == "a-b"


def _run(cwd: Path) -> subprocess.CompletedProcess[str]:
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
    script_dir = cwd / "scripts"
    script_dir.mkdir(exist_ok=True)
    (script_dir / "check_doc_anchors.py").write_text(
        SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
    return subprocess.run(
        [sys.executable, str(script_dir / "check_doc_anchors.py")],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def test_resolving_cross_file_anchor_passes(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "target.md").write_text("# Title\n\n## Some Section\n", encoding="utf-8")
    (tmp_path / "docs" / "linker.md").write_text(
        "See [it](target.md#some-section).\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "all resolve" in result.stdout


def test_stale_cross_file_anchor_fails(tmp_path: Path) -> None:
    """The exact shape that shipped: a heading renamed, a link in another file left behind."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "target.md").write_text(
        "# Title\n\n## Renamed Section\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "linker.md").write_text(
        "See [it](target.md#some-section).\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "MISSING_ANCHOR #some-section" in result.stderr
    assert "linker.md" in result.stderr


def test_same_file_anchor_is_checked(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "# Title\n\n## Real Section\n\nJump to [nowhere](#not-a-section).\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "MISSING_ANCHOR #not-a-section" in result.stderr


def test_external_links_are_not_fetched(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "# Title\n\n[spec](https://example.com/page#fragment)\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_linked_duplicate_heading_is_reported_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "# Title\n\n## Notes\n\ntext\n\n## Notes\n\n[go](#notes)\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "AMBIGUOUS_ANCHOR #notes" in result.stderr


def test_unlinked_duplicate_heading_is_not_a_finding(tmp_path: Path) -> None:
    """Sample corpora repeat headings; only a duplicate someone links to is ambiguous."""
    (tmp_path / "doc.md").write_text("# Title\n\n## Notes\n\ntext\n\n## Notes\n", encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_repo_itself_is_clean() -> None:
    """The gate must pass on this repository, or CI is about to go red."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
