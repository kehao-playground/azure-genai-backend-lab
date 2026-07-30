"""The test suite must not depend on the local .env or shell environment.

The app builds its chat, search, and embedding clients at import time
(fail fast in production) via build_rag_service, so a hostile local
environment — any fake adapter switched off, Azure config absent — would
previously crash pytest during collection or behave's before_scenario hook
(review r01 fix 2; extended to search/embeddings for Day 14 review
finding 3).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

AZURE_PREFIXES = ("AZURE_OPENAI_", "AZURE_SEARCH_")


def _hostile_env(*, flag: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(AZURE_PREFIXES)}
    env[flag] = "false"
    return env


@pytest.mark.parametrize("flag", ["USE_FAKE_LLM", "USE_FAKE_SEARCH", "USE_FAKE_EMBEDDINGS"])
def test_suite_survives_hostile_local_env(flag: str) -> None:
    env = _hostile_env(flag=flag)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_chat_api.py::test_chat_returns_reply_conversation_and_correlation_id",
            "-q",
        ],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("flag", ["USE_FAKE_LLM", "USE_FAKE_SEARCH", "USE_FAKE_EMBEDDINGS"])
def test_rag_bdd_survives_hostile_local_env(flag: str) -> None:
    env = _hostile_env(flag=flag)

    result = subprocess.run(
        [sys.executable, "-m", "behave", "tests/bdd/features/rag_no_answer_policy.feature", "-q"],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
