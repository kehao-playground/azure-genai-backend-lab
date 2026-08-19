"""Fake-CLI state regressions for scripts/boot_smoke.sh (Day 25 extraction).

Ported verbatim from the inline `docker` job step in .github/workflows/ci.yml
(Day 23 review r03 R3): the script has to keep BOTH assertions, because
neither substitutes for the other. Polling `docker inspect` proves the
Dockerfile's own declared HEALTHCHECK actually reports healthy; a separate
`docker exec` probe with no involvement from HEALTHCHECK at all would pass
even if that instruction were broken. The exact-body check afterwards proves
what /health actually returns -- weakening it to a status-code or substring
check would let a broken handler that still returns 200 sail through.

The fake `docker` here drives one shared state file plus a call log, the same
shape as the fake `az`/`curl`/`uv` executables in
test_container_app_script_lifecycle.py: state in, ordered calls out, so
assertions about what ran (and in what order) are exact rather than inferred.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "boot_smoke.sh"

HEALTHY_BODY = '{"status":"ok","service":"azure-genai-backend-lab"}'

FAKE_DOCKER = '''#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["BOOT_SMOKE_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)

args = sys.argv[1:]
state.setdefault("calls", []).append(" ".join(args))

def save() -> None:
    with open(state_path, "w") as f:
        json.dump(state, f)

def done(out: str = "", err: str = "", code: int = 0) -> None:
    save()
    if out != "":
        print(out)
    if err != "":
        print(err, file=sys.stderr)
    sys.exit(code)

def opt_after(flag: str) -> str:
    return args[args.index(flag) + 1] if flag in args else ""

if args[:2] == ["run", "-d"]:
    name = opt_after("--name")
    state.setdefault("containers", []).append(name)
    done("fake-container-id")

if args[:1] == ["inspect"]:
    name = args[-1]
    if state.get("inspect_fails"):
        done(err="Error: No such object: " + name, code=1)
    fmt = opt_after("--format")
    if fmt == "{{json .State.Health}}":
        done(json.dumps({"Status": state.get("health_status", "starting")}))
    # {{.State.Health.Status}} -- the only other format this script uses.
    done(state.get("health_status", "starting"))

if args[:1] == ["logs"]:
    done(state.get("container_logs", "fake container log output"))

if args[:1] == ["exec"]:
    # docker exec NAME python -c "..." -- the probe body is the fake's own
    # to hand back, not something worth actually parsing out of argv.
    done(state.get("health_body", ""))

if args[:2] == ["rm", "-f"]:
    name = args[-1]
    if name in state.get("containers", []):
        state["containers"].remove(name)
    done()

print(f"fake docker: unhandled command: {' '.join(args)}", file=sys.stderr)
done(code=2)
'''


class Harness:
    def __init__(self, tmp_path: Path, **overrides: object) -> None:
        state: dict[str, object] = {
            "health_status": "healthy",
            "health_body": HEALTHY_BODY,
        }
        state.update(overrides)
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps(state))

        fake_dir = tmp_path / "bin"
        fake_dir.mkdir()
        path = fake_dir / "docker"
        path.write_text(FAKE_DOCKER)
        path.chmod(0o755)

        self.env = {
            **os.environ,
            "PATH": f"{fake_dir}:{os.environ['PATH']}",
            "BOOT_SMOKE_FAKE_STATE": str(self.state_path),
            # Fast knobs: no real sleeps, bounded loops observable in the log.
            "BOOT_SMOKE_ATTEMPTS": "3",
            "BOOT_SMOKE_INTERVAL_SECONDS": "0",
        }

    def run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), "azgenai-lab:test"],
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

    def has(self, prefix: str) -> bool:
        return any(call.startswith(prefix) for call in self.calls)

    def count(self, prefix: str) -> int:
        return sum(1 for call in self.calls if call.startswith(prefix))


def container_name(result: subprocess.CompletedProcess[str]) -> str:
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("boot smoke container: ")
    )
    return line.removeprefix("boot smoke container: ")


def test_healthy_on_first_poll_runs_the_exact_body_probe(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    assert h.count("inspect --format {{.State.Health.Status}}") == 1
    assert h.has("exec ")
    assert "health body: " + HEALTHY_BODY in result.stdout


def test_unhealthy_status_exits_non_zero_and_dumps_logs(tmp_path: Path) -> None:
    h = Harness(tmp_path, health_status="unhealthy")
    result = h.run()
    assert result.returncode != 0
    assert "never reported healthy" in result.stderr
    assert "status: unhealthy" in result.stderr
    assert h.has("logs ")
    # It never got as far as the exact-body probe.
    assert not h.has("exec ")


def test_health_never_resolves_within_the_attempt_budget(tmp_path: Path) -> None:
    h = Harness(tmp_path, health_status="starting")
    result = h.run()
    assert result.returncode != 0
    assert "never reported healthy" in result.stderr
    assert "status: starting" in result.stderr
    # Exactly BOOT_SMOKE_ATTEMPTS (3) polls, not fewer and not looping forever.
    assert h.count("inspect --format {{.State.Health.Status}}") == 3
    assert not h.has("exec ")


def test_different_health_body_fails_even_though_healthcheck_passed(tmp_path: Path) -> None:
    # The assertion this test pins must not degrade to "any 200": a handler
    # that returns healthy AND a 200 with the wrong payload is still a
    # failure this script has to catch.
    h = Harness(tmp_path, health_body='{"status":"ok","service":"wrong-service"}')
    result = h.run()
    assert result.returncode != 0
    assert h.has("exec ")
    # The mismatch is what `test "$body" = ...` catches -- bash reports it
    # only as a non-zero exit, not a message, so the log line with the wrong
    # body is the evidence this ran and disagreed.
    assert "wrong-service" in result.stdout


def test_docker_inspect_error_fails_closed_not_assume_healthy(tmp_path: Path) -> None:
    h = Harness(tmp_path, inspect_fails=True)
    result = h.run()
    assert result.returncode != 0
    assert "docker inspect failed on attempt 1" in result.stderr
    assert "never reported healthy" in result.stderr
    assert "status: inspect-error" in result.stderr
    # A single attempt: an inspect error breaks the poll loop immediately
    # rather than burning the rest of the attempt budget on a broken read.
    assert h.count("inspect --format {{.State.Health.Status}}") == 1
    assert not h.has("exec ")


def test_container_is_removed_on_the_happy_path(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr
    name = container_name(result)
    assert h.state["containers"] == []
    assert any(call == f"rm -f {name}" for call in h.calls)


def test_container_is_removed_on_a_failure_path(tmp_path: Path) -> None:
    # The trap must fire on failure too, not only on a clean exit -- proven
    # here against the unhealthy-status failure path.
    h = Harness(tmp_path, health_status="unhealthy")
    result = h.run()
    assert result.returncode != 0
    name = container_name(result)
    assert h.state["containers"] == []
    assert any(call == f"rm -f {name}" for call in h.calls)


# ---------------------------------------------------------------------------
# Poll-knob validation: malformed or zero values fail closed before touching
# docker at all, not via a `seq`-shaped loop that could run zero iterations
# silently on a bad value (Day 21's shape) -- same require_count/
# require_seconds pair infra/scripts/update-container-app.sh uses.
# ---------------------------------------------------------------------------


def test_malformed_attempts_knob_fails_before_touching_docker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for bad in ("0", "-5", "abc", "3.5"):
        result = h.run(BOOT_SMOKE_ATTEMPTS=bad)
        assert result.returncode != 0, bad
        assert "BOOT_SMOKE_ATTEMPTS" in result.stderr, bad
        assert "positive integer" in result.stderr, bad
    assert h.calls == []


def test_malformed_interval_knob_fails_before_touching_docker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for bad in ("-5", "abc", "3.5"):
        result = h.run(BOOT_SMOKE_INTERVAL_SECONDS=bad)
        assert result.returncode != 0, bad
        assert "BOOT_SMOKE_INTERVAL_SECONDS" in result.stderr, bad
        assert "non-negative integer" in result.stderr, bad
    assert h.calls == []


def test_zero_interval_is_valid_but_zero_attempts_is_not(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run(BOOT_SMOKE_INTERVAL_SECONDS="0")
    assert result.returncode == 0, result.stderr

    result = h.run(BOOT_SMOKE_ATTEMPTS="0")
    assert result.returncode != 0
    assert "BOOT_SMOKE_ATTEMPTS" in result.stderr
    assert "positive integer" in result.stderr


def test_default_container_name_is_unique_per_run(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    r1 = Harness(dir_a).run()
    r2 = Harness(dir_b).run()
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    assert container_name(r1) != container_name(r2)
    assert container_name(r1).startswith("azgenai-lab-boot-smoke-")


def test_explicit_container_name_overrides_the_default(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run(BOOT_SMOKE_CONTAINER_NAME="my-fixed-name")
    assert result.returncode == 0, result.stderr
    assert container_name(result) == "my-fixed-name"
    assert h.has("run -d --name my-fixed-name")


def test_missing_image_argument_fails_before_touching_docker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=h.env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "usage: scripts/boot_smoke.sh" in result.stderr
    assert h.calls == []


def test_attempt_and_interval_knobs_are_honored(tmp_path: Path) -> None:
    h = Harness(tmp_path, health_status="starting")
    result = h.run(BOOT_SMOKE_ATTEMPTS="5", BOOT_SMOKE_INTERVAL_SECONDS="0")
    assert result.returncode != 0
    assert h.count("inspect --format {{.State.Health.Status}}") == 5


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
