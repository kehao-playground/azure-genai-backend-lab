"""Fake-CLI state regressions for infra/scripts/update-container-app.sh.

This is the script the CI/CD deploy job runs against an app that already
exists (deploy-container-app.sh creates it once; every deploy after that goes
through here). Runs the real bash script against fake ``az`` and ``curl``
executables that model the Azure/HTTP states the script claims to handle.

``curl`` already had precedent as a faked executable in
test_container_app_script_lifecycle.py (deploy-container-app.sh's gate 2
status-code probe); this fake extends that precedent to also serve a
configurable response body via the ``-o`` target file, which is what this
script's exact-body /health check needs and the status-only precedent did
not.

Both fakes append to one shared call log, so ordering assertions -- "the
image read-back ran before the revision was polled", "curl only ran after
the update call" -- are exact rather than inferred.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "scripts" / "update-container-app.sh"

HEALTHY_BODY = '{"status":"ok","service":"azure-genai-backend-lab"}'
DIGEST_IMAGE = (
    "acrfaked25.azurecr.io/azgenai-lab@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)
TAG_IMAGE = "acrfaked25.azurecr.io/azgenai-lab:day-24"

FAKE_AZ = '''#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["AZ_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)

args = sys.argv[1:]
state.setdefault("calls", []).append(" ".join(args))

def save() -> None:
    with open(state_path, "w") as f:
        json.dump(state, f)

def opt(name: str) -> str:
    return args[args.index(name) + 1] if name in args else ""

def query_value() -> str:
    return opt("--query")

def done(out: str = "", code: int = 0) -> None:
    save()
    if out != "":
        print(out)
    sys.exit(code)

if args[:2] == ["account", "set"]:
    done()

if args[:2] == ["containerapp", "show"]:
    field = query_value().strip('"')
    if field == "properties.template.containers[0].image":
        done(state.get("served_image", ""))
    if field == "properties.latestRevisionName":
        done(state.get("revision_name", ""))
    if field == "properties.configuration.ingress.fqdn":
        done(state.get("fqdn", ""))
    done("")

if args[:2] == ["containerapp", "update"]:
    if state.get("update_fails"):
        print("ERROR: injected update failure", file=sys.stderr)
        done(code=1)
    state["served_image"] = opt("--image")
    done("{}")

if args[:3] == ["containerapp", "revision", "show"]:
    field = query_value().strip('"')
    if field == "properties.runningState":
        states = state.get("running_states")
        if states is None:
            done(state.get("running_state", "Running"))
        idx = state.get("running_state_call_index", 0)
        value = states[min(idx, len(states) - 1)]
        state["running_state_call_index"] = idx + 1
        done(value)
    done("")

print(f"fake az: unhandled command: {' '.join(args)}", file=sys.stderr)
done(code=2)
'''

FAKE_CURL = '''#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["AZ_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)
state.setdefault("calls", []).append("curl " + " ".join(sys.argv[1:]))

args = sys.argv[1:]
out_path = args[args.index("-o") + 1] if "-o" in args else None
if out_path:
    with open(out_path, "w") as f:
        f.write(state.get("health_body", ""))

with open(state_path, "w") as f:
    json.dump(state, f)

code = state.get("health_status", "200")
if state.get("curl_fails"):
    print("curl: (28) Connection timed out", file=sys.stderr)
    sys.exit(28)
print(code, end="")
'''


class Harness:
    def __init__(self, tmp_path: Path, **overrides: object) -> None:
        state: dict[str, object] = {
            "served_image": TAG_IMAGE,
            "revision_name": "aca-faked25--abc123",
            "running_state": "Running",
            "fqdn": "aca-faked25.japaneast.azurecontainerapps.io",
            "health_status": "200",
            "health_body": HEALTHY_BODY,
        }
        state.update(overrides)
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps(state))

        fake_dir = tmp_path / "bin"
        fake_dir.mkdir()
        for name, body in (("az", FAKE_AZ), ("curl", FAKE_CURL)):
            path = fake_dir / name
            path.write_text(body)
            path.chmod(0o755)

        self.env = {
            **os.environ,
            "PATH": f"{fake_dir}:{os.environ['PATH']}",
            "AZ_FAKE_STATE": str(self.state_path),
            "AZ_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
            "AZ_RESOURCE_GROUP": "rg",
            "AZ_ACA_APP_NAME": "aca-faked25",
            # Fast knobs: no real sleeps, bounded loops observable in the log.
            "ACA_REVISION_POLL_ATTEMPTS": "3",
            "ACA_REVISION_POLL_INTERVAL": "0",
            "HEALTH_POLL_ATTEMPTS": "3",
            "HEALTH_POLL_INTERVAL": "0",
        }

    def run(self, image: str = DIGEST_IMAGE, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), "--image", image],
            env={**self.env, **extra_env},
            capture_output=True,
            text=True,
            timeout=30,
        )

    @property
    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text())  # type: ignore[no-any-return]

    @property
    def calls(self) -> list[str]:
        return self.state.get("calls", [])  # type: ignore[return-value]

    def first_index(self, prefix: str) -> int:
        return next(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def count(self, prefix: str) -> int:
        return sum(1 for call in self.calls if call.startswith(prefix))

    def has(self, prefix: str) -> bool:
        return any(call.startswith(prefix) for call in self.calls)


# ---------------------------------------------------------------------------
# Happy path and ordering.
# ---------------------------------------------------------------------------


def test_happy_path_updates_and_verifies(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert f"requesting: {DIGEST_IMAGE}" in result.stdout
    assert "app reports the requested image" in result.stdout
    assert "is Running" in result.stdout
    assert "/health returned the expected body" in result.stdout
    assert h.state["fqdn"] in result.stdout
    assert DIGEST_IMAGE in result.stdout


def test_stage_ordering_is_pinned(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    # "containerapp show" is called at least four times (step 1 snapshot,
    # step 3 image read-back, step 3 revision name, step 4 fqdn) with
    # "containerapp update" and "containerapp revision show" interleaved
    # among them, so ordering is checked by index of each named call rather
    # than by first_index() of a repeated prefix.
    show_indices = [i for i, call in enumerate(h.calls) if call.startswith("containerapp show")]
    assert len(show_indices) >= 4
    assert h.first_index("account set") < show_indices[0]
    assert show_indices[0] < h.first_index("containerapp update")
    assert h.first_index("containerapp update") < show_indices[1]
    assert show_indices[1] < h.first_index("containerapp revision show")
    assert h.first_index("containerapp revision show") < h.first_index("curl ")


def test_image_reaches_az_unmodified_including_digest_form(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run(image=DIGEST_IMAGE)
    assert result.returncode == 0, result.stderr

    update_call = next(call for call in h.calls if call.startswith("containerapp update"))
    assert f"--image {DIGEST_IMAGE}" in update_call
    # No tag parsing, no normalization, no appended :latest.
    assert not update_call.endswith(":latest")


# ---------------------------------------------------------------------------
# Snapshot is stored and echoed verbatim -- tag or digest, never normalized.
# ---------------------------------------------------------------------------


def test_snapshot_that_is_a_tag_is_echoed_verbatim_in_the_rollback_line(tmp_path: Path) -> None:
    h = Harness(tmp_path, served_image=TAG_IMAGE, running_state="Failed")
    result = h.run()
    assert result.returncode != 0
    assert f"currently serving: {TAG_IMAGE}" in result.stdout
    assert f"--image {TAG_IMAGE}" in result.stderr
    assert "TAG reference, not a digest" in result.stderr


def test_snapshot_that_is_a_digest_gets_no_tag_warning(tmp_path: Path) -> None:
    snapshot_digest = (
        "acrfaked25.azurecr.io/azgenai-lab@sha256:"
        "2222222222222222222222222222222222222222222222222222222222222222"
    )
    h = Harness(tmp_path, served_image=snapshot_digest, running_state="Failed")
    result = h.run()
    assert result.returncode != 0
    assert f"--image {snapshot_digest}" in result.stderr
    assert "TAG reference" not in result.stderr


# ---------------------------------------------------------------------------
# Fail-closed: an empty read-back aborts before any mutation.
# ---------------------------------------------------------------------------


def test_empty_snapshot_read_back_aborts_before_any_mutation(tmp_path: Path) -> None:
    h = Harness(tmp_path, served_image="")
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not h.has("containerapp update")
    # No rollback hint either: nothing was mutated.
    assert "No automatic rollback" not in result.stderr


# ---------------------------------------------------------------------------
# Fail-closed: update reported success but the read-back disagrees.
# ---------------------------------------------------------------------------


def test_update_succeeds_but_readback_shows_old_image_fails(tmp_path: Path) -> None:
    # The fake az's "containerapp update" handler normally records --image
    # into served_image; injecting a stuck value simulates a read-back that
    # still reports the pre-update image even though the update call itself
    # returned 0.
    h = Harness(tmp_path, stuck_served_image=True)
    # Patch the fake az inline for this one test: served_image never changes.
    fake_az_stuck = FAKE_AZ.replace(
        'state["served_image"] = opt("--image")\n    done("{}")',
        'done("{}")',
    )
    (tmp_path / "bin" / "az").write_text(fake_az_stuck)
    (tmp_path / "bin" / "az").chmod(0o755)

    result = h.run()
    assert result.returncode != 0
    assert "app reports image" in result.stderr
    assert TAG_IMAGE in result.stderr
    assert DIGEST_IMAGE in result.stderr
    assert "No automatic rollback" in result.stderr
    # Never got as far as polling the revision or probing /health.
    assert not h.has("containerapp revision show")
    assert not h.has("curl ")


# ---------------------------------------------------------------------------
# Fail-closed: the new revision never reaches Running.
# ---------------------------------------------------------------------------


def test_revision_failed_state_fails_the_update(tmp_path: Path) -> None:
    h = Harness(tmp_path, running_state="Failed")
    result = h.run()
    assert result.returncode != 0
    assert "runningState is 'Failed'" in result.stderr
    assert "No automatic rollback" in result.stderr
    assert not h.has("curl ")
    # A terminal Failed state stops polling immediately rather than burning
    # the rest of the attempt budget.
    assert h.count("containerapp revision show") == 1


def test_revision_stuck_provisioning_fails_after_bounded_polling(tmp_path: Path) -> None:
    h = Harness(tmp_path, running_state="Provisioning")
    result = h.run()
    assert result.returncode != 0
    assert "runningState is 'Provisioning'" in result.stderr
    assert h.count("containerapp revision show") == 3  # ACA_REVISION_POLL_ATTEMPTS
    assert not h.has("curl ")


def test_revision_becomes_running_after_transient_polls(tmp_path: Path) -> None:
    h = Harness(tmp_path, running_states=["Provisioning", "Provisioning", "Running"])
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.count("containerapp revision show") == 3


def test_empty_revision_name_read_aborts_before_polling(tmp_path: Path) -> None:
    h = Harness(tmp_path, revision_name="")
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not h.has("containerapp revision show")


# ---------------------------------------------------------------------------
# Data-plane smoke: exact body, never "any 200".
# ---------------------------------------------------------------------------


def test_health_wrong_body_fails_with_the_rollback_line(tmp_path: Path) -> None:
    h = Harness(tmp_path, health_body='{"status":"ok","service":"wrong-service"}')
    result = h.run()
    assert result.returncode != 0
    assert "did not return the expected body" in result.stderr
    assert "No automatic rollback" in result.stderr


def test_health_non_200_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path, health_status="503")
    result = h.run()
    assert result.returncode != 0
    assert "did not return the expected body" in result.stderr
    assert "last status: 503" in result.stderr


def test_health_never_resolves_within_the_attempt_budget(tmp_path: Path) -> None:
    h = Harness(tmp_path, health_status="503")
    result = h.run()
    assert result.returncode != 0
    assert h.count("curl ") == 3  # HEALTH_POLL_ATTEMPTS


def test_empty_fqdn_read_aborts_before_probing(tmp_path: Path) -> None:
    h = Harness(tmp_path, fqdn="")
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not h.has("curl ")


# ---------------------------------------------------------------------------
# Secret hygiene.
# ---------------------------------------------------------------------------


def test_never_calls_or_prints_secrets(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert not h.has("containerapp secret")
    for call in h.calls:
        assert "listSecrets" not in call
    assert "listSecrets" not in result.stdout
    assert "listSecrets" not in result.stderr


# ---------------------------------------------------------------------------
# Argument handling.
# ---------------------------------------------------------------------------


def test_missing_image_flag_fails_before_touching_az(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=h.env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "Usage: update-container-app.sh" in result.stderr
    assert h.calls == []


def test_missing_required_env_vars_fail_before_touching_az(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for missing in ("AZ_SUBSCRIPTION_ID", "AZ_RESOURCE_GROUP", "AZ_ACA_APP_NAME"):
        env = dict(h.env)
        del env[missing]
        result = subprocess.run(
            ["bash", str(SCRIPT), "--image", DIGEST_IMAGE],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, missing
        assert missing in result.stderr, missing
    assert h.calls == []


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
