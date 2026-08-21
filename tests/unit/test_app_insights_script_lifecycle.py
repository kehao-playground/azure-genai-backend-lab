"""Fake-CLI state regressions for the Day 27 Application Insights scripts.

Runs the real bash against a fake ``az`` that models the states these scripts
claim to handle. The property under test is ownership: deploy-container-app.sh
creates a Log Analytics workspace for the Container Apps environment and
delete-container-app.sh deletes it by name, so a second script that believed it
owned that workspace would make whichever teardown ran second abort
fail-closed on a resource that was already gone. Day 24 recorded what an
aborted teardown leaves behind.

Day 25's lesson is applied to the fake itself: a fake that accepts a flag
combination the real CLI rejects turns a broken command green across the whole
suite. This one refuses `--all` together with `--scope`, the exact pair that
cost Day 25 a live run.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "infra" / "scripts"

CONNECTION_STRING = "InstrumentationKey=11111111-1111-1111-1111-111111111111"
SECRET_URI = "https://kv-fake.vault.azure.net/secrets/applicationinsights-connection-string/v1"

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
s.setdefault("calls", []).append(" ".join(args))

# Day 25 F1: the real CLI refuses --all together with --scope. A fake that
# accepts it lets a command that cannot run in production pass every test.
if "--all" in args and "--scope" in args:
    save(s)
    sys.stderr.write("--all cannot be used with --scope\\n")
    sys.exit(1)

def out(value):
    save(s)
    sys.stdout.write(str(value) + "\\n")
    sys.exit(0)

def ok():
    save(s)
    sys.exit(0)

joined = " ".join(args)

if args[:2] == ["provider", "show"]:
    out("Registered")
if args[:2] == ["provider", "register"]:
    ok()

# --- Log Analytics workspace ------------------------------------------------
if args[:4] == ["monitor", "log-analytics", "workspace", "list"]:
    name = None
    for a in args:
        if a.startswith("length([?name=='"):
            name = a.split("'")[1]
    out(1 if name in s.get("workspaces", []) else 0)
if args[:4] == ["monitor", "log-analytics", "workspace", "create"]:
    name = args[args.index("--workspace-name") + 1]
    s.setdefault("workspaces", []).append(name)
    ok()
if args[:4] == ["monitor", "log-analytics", "workspace", "show"]:
    name = args[args.index("--workspace-name") + 1]
    if name not in s.get("workspaces", []):
        save(s); sys.stderr.write("workspace not found\\n"); sys.exit(1)
    out("/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.OperationalInsights/workspaces/" + name)
if args[:4] == ["monitor", "log-analytics", "workspace", "delete"]:
    name = args[args.index("--workspace-name") + 1]
    if name in s.get("workspaces", []):
        s["workspaces"].remove(name)
    ok()

# --- Application Insights component -----------------------------------------
if args[:4] == ["monitor", "app-insights", "component", "create"]:
    name = args[args.index("--app") + 1]
    workspace = args[args.index("--workspace") + 1]
    s.setdefault("components", {})[name] = {"workspace": workspace}
    ok()
if args[:4] == ["monitor", "app-insights", "component", "show"]:
    name = args[args.index("--app") + 1]
    comp = s.get("components", {}).get(name)
    if comp is None:
        save(s); sys.stderr.write("component not found\\n"); sys.exit(1)
    if "workspaceResourceId" in joined:
        out(os.environ.get("FAKE_BOUND_WORKSPACE") or comp["workspace"])
    if "connectionString" in joined:
        out(os.environ["FAKE_CONNECTION_STRING"])
    ok()
if args[:4] == ["monitor", "app-insights", "component", "delete"]:
    name = args[args.index("--app") + 1]
    if os.environ.get("FAKE_COMPONENT_STICKY") == "true":
        ok()  # delete returns, resource stays -- the Day 24/25 shape
    s.get("components", {}).pop(name, None)
    ok()

# --- Key Vault --------------------------------------------------------------
if args[:3] == ["keyvault", "secret", "set"]:
    name = args[args.index("--name") + 1]
    value = args[args.index("--value") + 1]
    s.setdefault("secrets", {})[name] = value
    out(os.environ["FAKE_SECRET_URI"])
if args[:3] == ["keyvault", "secret", "delete"]:
    name = args[args.index("--name") + 1]
    s.setdefault("deleted_secrets", []).append(name)
    ok()
if args[:3] == ["keyvault", "secret", "purge"]:
    name = args[args.index("--name") + 1]
    s.setdefault("purged_secrets", []).append(name)
    ok()

save(s)
sys.stderr.write("fake az: unhandled command: " + joined + "\\n")
sys.exit(1)
'''


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        bindir = tmp_path / "bin"
        bindir.mkdir()
        az = bindir / "az"
        az.write_text(FAKE_AZ)
        az.chmod(0o755)
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps({"calls": []}))
        self.record_path = tmp_path / "record.env"
        self.env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "FAKE_AZ_STATE": str(self.state_path),
            "FAKE_CONNECTION_STRING": CONNECTION_STRING,
            "FAKE_SECRET_URI": SECRET_URI,
            "AZ_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
            "AZ_RESOURCE_GROUP": "rg",
            "AZ_APPINSIGHTS_NAME": "appi-fake27",
            "AZ_KEYVAULT_NAME": "kv-fake",
            "AZ_RECORD_FILE": str(self.record_path),
            # Milliseconds here, minutes in production.
            "APPI_WAIT_SECONDS": "1",
            "APPI_WAIT_INTERVAL": "0",
        }

    def create(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPTS_DIR / "create-app-insights.sh")],
            env={**self.env, **extra}, capture_output=True, text=True, timeout=60,
        )

    def delete(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPTS_DIR / "delete-app-insights.sh")],
            env={**self.env, **extra}, capture_output=True, text=True, timeout=60,
        )

    @property
    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    @property
    def calls(self) -> list[str]:
        return self.state.get("calls", [])

    @property
    def record(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self.record_path.read_text().splitlines():
            key, _, value = line.partition("=")
            out[key] = value
        return out


def test_create_makes_and_owns_a_workspace_when_none_is_named(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.create()
    assert result.returncode == 0, result.stderr

    assert h.record["law_owned"] == "true"
    assert h.record["AZ_LAW_NAME"].startswith("lawappi")
    assert any("workspace create" in call for call in h.calls)


def test_create_reuses_a_named_workspace_and_disclaims_it(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    state = h.state
    state["workspaces"] = ["law-owned-by-someone-else"]
    h.state_path.write_text(json.dumps(state))

    result = h.create(AZ_LAW_NAME="law-owned-by-someone-else")
    assert result.returncode == 0, result.stderr

    assert h.record["law_owned"] == "false"
    assert not any("workspace create" in call for call in h.calls)


def test_create_refuses_a_named_workspace_that_does_not_exist(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.create(AZ_LAW_NAME="law-does-not-exist")
    assert result.returncode != 0
    assert "does not exist" in result.stderr
    # Nothing created, so nothing to tear down.
    assert not any("component create" in call for call in h.calls)


def test_create_aborts_when_the_component_lands_on_another_workspace(tmp_path: Path) -> None:
    # The read-back is the point of naming a workspace at all: a component
    # bound elsewhere sends this session's telemetry somewhere teardown never
    # looks.
    h = Harness(tmp_path)
    result = h.create(FAKE_BOUND_WORKSPACE="/subscriptions/sub/.../workspaces/somewhere-else")
    assert result.returncode != 0
    assert "expected" in result.stderr


def test_connection_string_reaches_key_vault_and_not_the_record_file(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.create()
    assert result.returncode == 0, result.stderr

    assert h.state["secrets"]["applicationinsights-connection-string"] == CONNECTION_STRING
    # The record file is read in terminals and one .gitignore mistake from a
    # commit. Names and a flag only.
    record_text = h.record_path.read_text()
    assert CONNECTION_STRING not in record_text
    assert "InstrumentationKey" not in record_text
    # And never on stdout or stderr either.
    assert CONNECTION_STRING not in result.stdout
    assert CONNECTION_STRING not in result.stderr


def test_delete_removes_a_workspace_it_owns_after_the_component(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.create().returncode == 0
    result = h.delete()
    assert result.returncode == 0, result.stderr

    calls = h.calls
    component_at = next(i for i, c in enumerate(calls) if "component delete" in c)
    workspace_at = next(i for i, c in enumerate(calls) if "workspace delete" in c)
    # Order is the contract: the reverse leaves a component bound to a resource
    # that no longer exists.
    assert component_at < workspace_at


def test_delete_leaves_a_workspace_it_does_not_own(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    state = h.state
    state["workspaces"] = ["law-owned-by-someone-else"]
    h.state_path.write_text(json.dumps(state))
    assert h.create(AZ_LAW_NAME="law-owned-by-someone-else").returncode == 0

    result = h.delete()
    assert result.returncode == 0, result.stderr
    assert not any("workspace delete" in call for call in h.calls)


def test_delete_refuses_a_record_file_with_no_ownership_flag(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.record_path.write_text(
        "AZ_SUBSCRIPTION_ID=sub\nAZ_RESOURCE_GROUP=rg\nAZ_APPINSIGHTS_NAME=appi\n"
    )
    result = h.delete()
    assert result.returncode != 0
    # Guessing true deletes someone else's workspace; guessing false leaves a
    # monthly bill under a name no future run will know.
    assert "refusing to guess" in result.stderr
    assert not any("component delete" in call for call in h.calls)


def test_delete_without_a_record_file_deletes_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.delete()
    assert result.returncode == 0
    assert "No record file" in result.stderr
    assert h.calls == []


def test_delete_fails_closed_when_the_component_survives_its_delete(tmp_path: Path) -> None:
    # Day 24 and Day 25 both hit "the delete returned and the resource is still
    # there". Fail-closed is right; what this pins is that the wait has a
    # deadline and the steps after it do not run.
    h = Harness(tmp_path)
    assert h.create().returncode == 0
    result = h.delete(FAKE_COMPONENT_STICKY="true")
    assert result.returncode != 0
    assert "still present" in result.stderr
    assert not any("workspace delete" in call for call in h.calls)
