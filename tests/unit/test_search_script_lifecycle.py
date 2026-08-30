"""Fake-CLI regressions for the Bonus 7 semantic-search flag on
create-search.sh.

Runs the real bash against a fake ``az`` that records every invocation
verbatim, so the assertions below are exact about argument order and
presence -- no real Azure call is ever made.

This repo has a standing, recurring failure mode: a fake CLI that accepts a
flag combination the real tool rejects makes the whole suite green against a
command that cannot run (Day 25 F1; two dormant instances of the same gap are
tracked in CLAUDE.md as of 2026-08-20). ``az search service create
--semantic-search`` only accepts ``disabled``, ``free``, or ``standard``
(verified against the installed ``az`` 2.89.1: ``az search service create
--help`` documents exactly those three, and a live invocation with
``--semantic-search premium`` exits 2 with "not a valid value for
'--semantic-search'"). The fake below refuses anything else the same way, so
a test that exercises an out-of-range value proves the fake would fail
alongside the real tool, not instead of it.

The other property under test is the default path: when AZ_SEARCH_SEMANTIC
is unset, every existing caller depends on the emitted ``az search service
create`` command being byte-for-byte what it was before this flag existed.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "infra" / "scripts"

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
RESOURCE_GROUP = "rg"
SEARCH_NAME = "srch-fake-b7"

# The exact argv the script has always sent to `az search service create`,
# before this task's flag existed. Any change here is a byte-for-byte
# regression against every caller that never set AZ_SEARCH_SEMANTIC.
BASELINE_CREATE_ARGS = [
    "search", "service", "create",
    "--subscription", SUBSCRIPTION_ID,
    "--resource-group", RESOURCE_GROUP,
    "--name", SEARCH_NAME,
    "--location", "japaneast",
    "--sku", "free",
]

FAKE_AZ = '''#!/usr/bin/env python3
import json, os, sys

STATE = os.environ["FAKE_AZ_STATE"]

def load():
    with open(STATE) as f:
        return json.load(f)

def save(s):
    with open(STATE, "w") as f:
        json.dump(s, f)

args = sys.argv[1:]
s = load()
s.setdefault("calls", []).append(args)
save(s)

if args[:3] == ["search", "service", "create"]:
    # az 2.89.1: `--semantic-search` only accepts these three values and
    # exits 2 (argparse-style) before touching the network when it does not.
    ALLOWED = {"disabled", "free", "standard"}
    if "--semantic-search" in args:
        value = args[args.index("--semantic-search") + 1]
        if value not in ALLOWED:
            sys.stderr.write(
                "ERROR: az search service create: '"
                + value
                + "' is not a valid value for '--semantic-search'. "
                "Allowed values: disabled, free, standard.\\n"
            )
            sys.exit(2)
    sys.exit(0)

if args[:3] == ["search", "service", "show"]:
    sys.stdout.write(json.dumps(
        {"sku": "free", "location": "japaneast", "semanticSearch": None}
    ) + "\\n")
    sys.exit(0)

if args[:3] == ["search", "admin-key", "show"]:
    sys.stdout.write("fake-admin-key\\n")
    sys.exit(0)

sys.stderr.write("fake az: unhandled command: " + " ".join(args) + "\\n")
sys.exit(1)
'''


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        az = bindir / "az"
        az.write_text(FAKE_AZ)
        az.chmod(0o755)
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps({"calls": []}))
        self.env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "FAKE_AZ_STATE": str(self.state_path),
            "AZ_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
            "AZ_RESOURCE_GROUP": RESOURCE_GROUP,
            "AZ_SEARCH_NAME": SEARCH_NAME,
        }

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPTS_DIR / "create-search.sh")],
            env={**self.env, **extra}, capture_output=True, text=True, timeout=60,
        )

    @property
    def calls(self) -> list[list[str]]:
        return json.loads(self.state_path.read_text())["calls"]

    def create_call(self) -> list[str]:
        for call in self.calls:
            if call[:3] == ["search", "service", "create"]:
                return call
        raise AssertionError(f"no 'search service create' call recorded: {self.calls}")


def pairs(tokens: list[str]) -> list[list[str]]:
    return [list(pair) for pair in zip(tokens, tokens[1:], strict=False)]


def test_semantic_flag_is_absent_by_default(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    assert h.create_call() == BASELINE_CREATE_ARGS
    assert "--semantic-search" not in h.create_call()


def test_semantic_flag_is_passed_when_requested(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run(AZ_SEARCH_SEMANTIC="free")
    assert result.returncode == 0, result.stderr

    assert ["--semantic-search", "free"] in pairs(h.create_call())
    # Nothing else about the baseline invocation moves.
    assert h.create_call() == BASELINE_CREATE_ARGS + ["--semantic-search", "free"]


def test_fake_cli_rejects_an_unsupported_plan(tmp_path: Path) -> None:
    # az 2.89.1: "Allowed values: disabled, free, standard".
    h = Harness(tmp_path)
    result = h.run(AZ_SEARCH_SEMANTIC="premium")
    assert result.returncode != 0
    assert "not a valid value for '--semantic-search'" in result.stderr
