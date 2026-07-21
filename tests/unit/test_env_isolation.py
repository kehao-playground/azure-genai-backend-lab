"""The test suite must not depend on the local .env or shell environment.

The app builds its chat service at import time (fail fast in production), so a
hostile local environment — fake adapter switched off, Azure config absent —
would previously crash pytest during collection (review r01 fix 2).
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_suite_survives_hostile_local_env() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AZURE_OPENAI_")}
    env["USE_FAKE_LLM"] = "false"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_chat_api.py::test_chat_returns_reply_and_correlation_id",
            "-q",
        ],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
