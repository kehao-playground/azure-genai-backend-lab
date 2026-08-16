"""Regression tests for a guard-message bug in two Day 24 Entra ID scripts.

An apostrophe inside a `${VAR:?word}` guard message is significant to bash's
parser even inside enclosing double quotes. Two such guards on consecutive
lines, each containing one apostrophe, pair their apostrophes with each
other -- merging both `: "${...}"` statements into one swallowed literal, so
the *second* guard never actually expands or checks anything, in any mode.

`deploy-container-app.sh` hit and fixed this exact pattern (see the
explanatory comment at infra/scripts/deploy-container-app.sh:91-101).
`assign-entra-app-role.sh` and `delete-entra-app.sh` had the identical
two-guard-in-a-row shape (ENTRA_API_APP_ID immediately followed by
ENTRA_CLIENT_APP_ID, each message containing "application's"); this file
pins the fix.

Both guards, when they work, fire before either script makes any `az` call
-- so a fake `az` on PATH that errors loudly if invoked is enough to prove
"never touched Azure". No state file or call log is needed for that.
"""

import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "infra" / "scripts"

FAKE_AZ = """#!/usr/bin/env bash
echo "fake az: unexpectedly invoked: $*" >&2
exit 2
"""

TENANT_ID = "11111111-1111-1111-1111-111111111111"
API_APP_ID = "22222222-2222-2222-2222-222222222222"


def _env(tmp_path: Path) -> dict[str, str]:
    fake_dir = tmp_path / "bin"
    fake_dir.mkdir()
    fake_az = fake_dir / "az"
    fake_az.write_text(FAKE_AZ)
    fake_az.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{fake_dir}:{os.environ['PATH']}",
        "ENTRA_TENANT_ID": TENANT_ID,
        "ENTRA_API_APP_ID": API_APP_ID,
    }


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPTS_DIR / script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_assign_entra_app_role_missing_client_id_fails_before_any_az_call(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    env.pop("ENTRA_CLIENT_APP_ID", None)
    result = _run("assign-entra-app-role.sh", env)
    assert result.returncode != 0
    assert "ENTRA_CLIENT_APP_ID" in result.stderr
    # If the guard was swallowed, the script instead ran on to the first `az`
    # call this fake would have caught.
    assert "unexpectedly invoked" not in result.stderr


def test_delete_entra_app_missing_client_id_fails_before_any_az_call(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    env.pop("ENTRA_CLIENT_APP_ID", None)
    result = _run("delete-entra-app.sh", env)
    assert result.returncode != 0
    assert "ENTRA_CLIENT_APP_ID" in result.stderr
    assert "unexpectedly invoked" not in result.stderr


def test_scripts_exist_and_are_executable() -> None:
    for script in ["assign-entra-app-role.sh", "delete-entra-app.sh"]:
        path = SCRIPTS_DIR / script
        assert path.is_file()
        assert os.access(path, os.X_OK)
