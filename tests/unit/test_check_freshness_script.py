"""Fake-CLI regressions for scripts/check_freshness.sh (Day 25).

The deploy job's freshness guard exists because deployment approval takes
human time: the commit that gets approved is not guaranteed to still be
main's HEAD by the time approval lands. Three cases matter (design doc D12):
`github.sha` still equals HEAD (pass), `github.sha` no longer equals HEAD
(fail, message names both shas), and the HEAD query itself fails (fail
closed -- an unknown HEAD is never treated as fresh).

The fake `gh` here is stateless -- the script makes exactly one call to it,
so there is nothing worth threading through a shared state file the way
test_boot_smoke_script.py's fake `docker` does. Behavior is controlled
directly through env vars the harness sets per test.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_freshness.sh"

FAKE_GH = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]

# The only shape this script uses:
#   gh api repos/<owner>/<repo>/commits/<branch> --jq .sha
if len(args) < 2 or args[0] != "api" or not args[1].startswith("repos/"):
    print(f"fake gh: unhandled command: {' '.join(args)}", file=sys.stderr)
    sys.exit(2)

if os.environ.get("FAKE_GH_QUERY_FAILS"):
    print("fake gh: simulated API failure", file=sys.stderr)
    sys.exit(1)

print(os.environ.get("FAKE_GH_HEAD_SHA", ""))
"""


class Harness:
    def __init__(self, tmp_path: Path, **env: str) -> None:
        fake_dir = tmp_path / "bin"
        fake_dir.mkdir()
        path = fake_dir / "gh"
        path.write_text(FAKE_GH)
        path.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{fake_dir}:{os.environ['PATH']}",
            "GITHUB_REPO": "example-owner/example-repo",
            **env,
        }

    def run(self, current_sha: str, branch: str = "main") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), current_sha, branch],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_current_sha_matches_head_passes(tmp_path: Path) -> None:
    h = Harness(tmp_path, FAKE_GH_HEAD_SHA="aaaaaaa")
    result = h.run("aaaaaaa")
    assert result.returncode == 0, result.stderr
    assert "Fresh" in result.stdout


def test_stale_sha_fails_and_message_names_both_shas(tmp_path: Path) -> None:
    h = Harness(tmp_path, FAKE_GH_HEAD_SHA="bbbbbbb")
    result = h.run("aaaaaaa")
    assert result.returncode != 0
    assert "aaaaaaa" in result.stderr
    assert "bbbbbbb" in result.stderr


def test_query_failure_fails_closed_not_treated_as_fresh(tmp_path: Path) -> None:
    h = Harness(tmp_path, FAKE_GH_QUERY_FAILS="1")
    result = h.run("aaaaaaa")
    assert result.returncode != 0
    assert "Failing closed" in result.stderr


def test_empty_head_sha_fails_closed_not_treated_as_a_match(tmp_path: Path) -> None:
    # A `gh api` call that exits 0 and prints nothing is a distinct failure
    # mode from a nonzero exit -- same class of bug this repo's other
    # scripts guard against with require_value.
    h = Harness(tmp_path, FAKE_GH_HEAD_SHA="")
    result = h.run("aaaaaaa")
    assert result.returncode != 0
    assert "empty output" in result.stderr


def test_missing_arguments_fail_before_touching_gh(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=h.env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "usage: scripts/check_freshness.sh" in result.stderr


def test_missing_github_repo_env_fails_before_touching_gh(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    del h.env["GITHUB_REPO"]
    result = h.run("aaaaaaa")
    assert result.returncode != 0
    assert "GITHUB_REPO" in result.stderr


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
