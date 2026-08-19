"""Fake-CLI state regressions for the Day 24 Azure Container Registry lifecycle
scripts.

Runs the real bash scripts against a fake ``az`` executable that models the
subscription states the scripts claim to handle: provider NotRegistered ->
register -> state query -> create ordering, a provider read that comes back
empty (as opposed to a nonzero exit) must abort instead of being read as "not
registered", per-run unique default names, an existing registry making create
a no-op, and delete's absent/idempotent and post-delete read-back paths.

The fake records every invocation to a call log so ordering assertions are
exact. There are no poll/retry knobs here (unlike the Key Vault scripts): ACR
delete is synchronous and has no soft-delete/purge step, so these tests need
no timing knobs to run in milliseconds.
"""

import json
import os
import re
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "infra" / "scripts"

FAKE_AZ = '''#!/usr/bin/env python3
import json, os, re, sys

state_path = os.environ["AZ_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)

args = sys.argv[1:]
state.setdefault("calls", []).append(" ".join(args))

def save() -> None:
    with open(state_path, "w") as f:
        json.dump(state, f)

def query_value() -> str:
    return args[args.index("--query") + 1] if "--query" in args else ""

def name_value() -> str:
    return args[args.index("--name") + 1] if "--name" in args else ""

def target_name_from_query() -> str | None:
    m = re.search(r"name=='([^']*)'", query_value())
    return m.group(1) if m else None

def done(out: str = "", code: int = 0) -> None:
    save()
    if out != "":
        print(out)
    sys.exit(code)

joined = " ".join(args)

if args[:2] == ["provider", "show"]:
    done(state["provider_state"])
if args[:2] == ["provider", "register"]:
    state["provider_state"] = "Registered"
    done()
if args[:2] == ["acr", "list"]:
    if state.get("fail_list"):
        print("ERROR: injected list failure", file=sys.stderr)
        done(code=1)
    if state.get("fail_list_after_delete") and state.get("just_deleted"):
        print("ERROR: injected post-delete list failure", file=sys.stderr)
        done(code=1)
    target = target_name_from_query()
    done("1" if target in state["registries"] else "0")
if args[:2] == ["acr", "create"]:
    state["registries"].append(name_value())
    done("{}")
if args[:2] == ["acr", "delete"]:
    name = name_value()
    if name in state["registries"]:
        state["registries"].remove(name)
    state["just_deleted"] = True
    done()

print(f"fake az: unhandled command: {joined}", file=sys.stderr)
done(code=2)
'''


class Harness:
    def __init__(
        self, tmp_path: Path, *, provider_state: str, registries: list[str], **flags: bool
    ) -> None:
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(
            json.dumps({"provider_state": provider_state, "registries": registries, **flags})
        )
        fake_dir = tmp_path / "bin"
        fake_dir.mkdir()
        fake_az = fake_dir / "az"
        fake_az.write_text(FAKE_AZ)
        fake_az.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{fake_dir}:{os.environ['PATH']}",
            "AZ_FAKE_STATE": str(self.state_path),
            "AZ_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
        }

    def run(self, script: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPTS_DIR / script)],
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
        return self.state["calls"]  # type: ignore[return-value]

    def first_index(self, prefix: str) -> int:
        return next(i for i, call in enumerate(self.calls) if call.startswith(prefix))


def test_fresh_subscription_registers_provider_before_create(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider_state="NotRegistered", registries=[])
    result = h.run(
        "create-acr.sh", AZ_RESOURCE_GROUP="rg", AZ_ACR_NAME="acrfaked24"
    )
    assert result.returncode == 0, result.stderr
    assert h.first_index("provider register") < h.first_index("acr list")
    assert h.first_index("acr list") < h.first_index("acr create")
    assert "acrfaked24" in h.state["registries"]


def test_create_aborts_on_empty_provider_state_without_creating(tmp_path: Path) -> None:
    # An empty (but zero-exit) provider read must be treated as a failed read,
    # not as "not registered" -> silently proceed to register/create.
    h = Harness(tmp_path, provider_state="", registries=[])
    result = h.run(
        "create-acr.sh", AZ_RESOURCE_GROUP="rg", AZ_ACR_NAME="acrfaked24"
    )
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not any(call.startswith("provider register") for call in h.calls)
    assert not any(call.startswith("acr create") for call in h.calls)


def test_default_name_gets_a_fresh_suffix_each_run(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider_state="Registered", registries=[])
    result1 = h.run("create-acr.sh", AZ_RESOURCE_GROUP="rg")
    assert result1.returncode == 0, result1.stderr
    name1 = re.search(r"^ACR name: (\S+)$", result1.stdout, re.MULTILINE)
    assert name1 is not None
    assert re.fullmatch(r"acrazgenai[0-9a-f]{8}", name1.group(1))

    run2_dir = tmp_path / "run2"
    run2_dir.mkdir()
    h2 = Harness(run2_dir, provider_state="Registered", registries=[])
    result2 = h2.run("create-acr.sh", AZ_RESOURCE_GROUP="rg")
    assert result2.returncode == 0, result2.stderr
    name2 = re.search(r"^ACR name: (\S+)$", result2.stdout, re.MULTILINE)
    assert name2 is not None
    assert re.fullmatch(r"acrazgenai[0-9a-f]{8}", name2.group(1))

    assert name1.group(1) != name2.group(1)


def test_create_skips_when_registry_already_exists(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider_state="Registered", registries=["acrfaked24"])
    result = h.run(
        "create-acr.sh", AZ_RESOURCE_GROUP="rg", AZ_ACR_NAME="acrfaked24"
    )
    assert result.returncode == 0, result.stderr
    assert "already exists" in result.stdout
    assert not any(call.startswith("acr create") for call in h.calls)


def test_create_pins_rbac_role_assignment_mode(tmp_path: Path) -> None:
    # ABAC-enabled registries do not honor the classic AcrPush role our CI
    # federated identity is assigned. The CLI defaults to rbac today, but
    # that default is Microsoft's to change; pinning it explicitly means a
    # future default flip can't silently break the push step without this
    # test going red first.
    h = Harness(tmp_path, provider_state="Registered", registries=[])
    result = h.run(
        "create-acr.sh", AZ_RESOURCE_GROUP="rg", AZ_ACR_NAME="acrfaked24"
    )
    assert result.returncode == 0, result.stderr
    create_call = next(call for call in h.calls if call.startswith("acr create"))
    assert "--role-assignment-mode rbac" in create_call


def test_delete_of_absent_registry_is_a_noop_success(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider_state="Registered", registries=[])
    result = h.run(
        "delete-acr.sh", AZ_RESOURCE_GROUP="rg", AZ_ACR_NAME="acrfaked24"
    )
    assert result.returncode == 0, result.stderr
    assert "Nothing to do" in result.stdout
    assert not any(call.startswith("acr delete") for call in h.calls)


def test_delete_calls_delete_yes_and_confirms_gone(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider_state="Registered", registries=["acrfaked24"])
    result = h.run(
        "delete-acr.sh", AZ_RESOURCE_GROUP="rg", AZ_ACR_NAME="acrfaked24"
    )
    assert result.returncode == 0, result.stderr
    delete_call = next(call for call in h.calls if call.startswith("acr delete"))
    assert "--yes" in delete_call.split()
    assert "acrfaked24" not in h.state["registries"]
    assert sum(call.startswith("acr list") for call in h.calls) >= 2


def test_delete_read_back_failure_is_non_zero(tmp_path: Path) -> None:
    # The injected failure is a nonzero az exit (not empty output), so
    # `set -e` on the read-back assignment is what aborts the script — the
    # delete call itself must still have happened, proving this is a
    # post-delete read-back failure and not a pre-delete abort.
    h = Harness(
        tmp_path,
        provider_state="Registered",
        registries=["acrfaked24"],
        fail_list_after_delete=True,
    )
    result = h.run(
        "delete-acr.sh", AZ_RESOURCE_GROUP="rg", AZ_ACR_NAME="acrfaked24"
    )
    assert result.returncode != 0
    assert any(call.startswith("acr delete") for call in h.calls)


def test_scripts_exist_and_are_executable() -> None:
    for script in ["create-acr.sh", "delete-acr.sh"]:
        path = SCRIPTS_DIR / script
        assert path.is_file()
        assert os.access(path, os.X_OK)
