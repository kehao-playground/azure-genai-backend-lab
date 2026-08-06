"""Fake-CLI state regressions for the Day 20 Key Vault lifecycle scripts.

Runs the real bash scripts against a fake ``az`` executable that models the
subscription states the scripts claim to handle (Day 20 review r05, F1 and the
A3 verification plan): provider NotRegistered -> register -> state query ->
create ordering, query failures that must abort instead of being read as
counts, existing live vaults, soft-deleted-only (resource group gone), absent
delete no-ops, and bounded timeout/retry paths.

The fake records every invocation to a call log so ordering assertions are
exact. Poll/retry knobs (AZ_KV_POLL_ATTEMPTS/INTERVAL, AZ_KV_RETRY_INTERVAL)
exist in the delete script precisely so these tests run in milliseconds while
production defaults stay at ~2-minute deadlines.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "infra" / "scripts"

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

def query_value() -> str:
    return args[args.index("--query") + 1] if "--query" in args else ""

def done(out: str = "", code: int = 0) -> None:
    save()
    if out:
        print(out)
    sys.exit(code)

def registered() -> bool:
    return state["provider"] == "Registered"

joined = " ".join(args)

if args[:2] == ["account", "show"]:
    done("11111111-1111-1111-1111-111111111111")
if args[:3] == ["ad", "signed-in-user", "show"]:
    done("22222222-2222-2222-2222-222222222222")
if args[:2] == ["provider", "show"]:
    done(state["provider"])
if args[:2] == ["provider", "register"]:
    state["provider"] = "Registered"
    done()
if args[:2] == ["keyvault", "list-deleted"]:
    if not registered():
        print("ERROR: (MissingSubscriptionRegistration)", file=sys.stderr)
        done(code=1)
    if state.get("fail_list_deleted"):
        print("ERROR: injected list-deleted failure", file=sys.stderr)
        done(code=1)
    done("1" if state["vault"] == "deleted" else "0")
if args[:2] == ["keyvault", "list"]:
    if not registered():
        print("ERROR: (MissingSubscriptionRegistration)", file=sys.stderr)
        done(code=1)
    if state.get("fail_list"):
        print("ERROR: injected list failure", file=sys.stderr)
        done(code=1)
    done("1" if state["vault"] == "live" else "0")
if args[:2] == ["keyvault", "create"]:
    # Live API constraint (2026-08-06): enablePurgeProtection cannot be set
    # to false explicitly — only omitted or true. The fake enforces it so a
    # regression reintroducing the flag fails here before a live run does.
    if "--enable-purge-protection" in args:
        idx = args.index("--enable-purge-protection")
        if idx + 1 < len(args) and args[idx + 1] == "false":
            print(
                'ERROR: The property "enablePurgeProtection" cannot be set to false.',
                file=sys.stderr,
            )
            done(code=1)
    state["vault"] = "live"
    done("{}")
if args[:2] == ["keyvault", "show"]:
    answers = {
        "id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv",
        "properties.enableRbacAuthorization": "true",
        "properties.enablePurgeProtection": "",
        "properties.softDeleteRetentionInDays": "7",
        "location": "japaneast",
    }
    done(answers.get(query_value(), ""))
if joined.startswith("role assignment list"):
    done("0")
if joined.startswith("role assignment create"):
    done("{}")
if args[:2] == ["keyvault", "delete"]:
    state["vault"] = "limbo" if state.get("delete_limbo") else "deleted"
    done()
if args[:2] == ["keyvault", "purge"]:
    if state.get("fail_purge"):
        print("ERROR: injected purge conflict", file=sys.stderr)
        done(code=1)
    state["vault"] = "absent"
    done()

print(f"fake az: unhandled command: {joined}", file=sys.stderr)
done(code=2)
'''


class Harness:
    def __init__(self, tmp_path: Path, *, provider: str, vault: str, **flags: bool) -> None:
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps({"provider": provider, "vault": vault, **flags}))
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
            "AZ_KEYVAULT_NAME": "kv-fake-d20",
            "AZ_KV_POLL_ATTEMPTS": "2",
            "AZ_KV_POLL_INTERVAL": "0",
            "AZ_KV_RETRY_INTERVAL": "0",
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


def test_fresh_subscription_registers_provider_before_any_vault_query(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="NotRegistered", vault="absent")
    result = h.run("create-keyvault.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode == 0, result.stderr
    # The F1 bug: list-deleted ran first, died on MissingSubscriptionRegistration,
    # and registration was never reached. Order must be register -> queries -> create.
    assert h.first_index("provider register") < h.first_index("keyvault list")
    assert h.first_index("keyvault list") < h.first_index("keyvault create")
    assert h.state["vault"] == "live"


def test_create_aborts_on_query_failure_without_misreporting_a_collision(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", vault="absent", fail_list_deleted=True)
    result = h.run("create-keyvault.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "Failed to query soft-deleted vaults" in result.stderr
    # The failure must not be misread as a name collision (empty-count bug).
    assert "held by a soft-deleted vault" not in result.stderr
    assert not any(call.startswith("keyvault create") for call in h.calls)


def test_create_fails_closed_on_existing_live_vault(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", vault="live")
    result = h.run("create-keyvault.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "already exists live" in result.stderr
    assert not any(call.startswith("keyvault create") for call in h.calls)


def test_create_fails_closed_on_soft_deleted_name(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", vault="deleted")
    result = h.run("create-keyvault.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "held by a soft-deleted vault" in result.stderr
    assert not any(call.startswith("keyvault create") for call in h.calls)


def test_delete_purges_soft_deleted_without_resource_group(tmp_path: Path) -> None:
    # The resource-group-gone path: purge needs only name + location.
    h = Harness(tmp_path, provider="Registered", vault="deleted")
    result = h.run("delete-keyvault.sh")
    assert result.returncode == 0, result.stderr
    assert h.state["vault"] == "absent"
    assert "no active or soft-deleted vault" in result.stdout


def test_delete_of_absent_vault_is_a_noop_success(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", vault="absent")
    result = h.run("delete-keyvault.sh")
    assert result.returncode == 0, result.stderr
    assert "Nothing to do" in result.stdout
    assert not any(call.startswith(("keyvault delete", "keyvault purge")) for call in h.calls)


def test_delete_full_cycle_from_live(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", vault="live")
    result = h.run("delete-keyvault.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode == 0, result.stderr
    assert h.first_index("keyvault delete") < h.first_index("keyvault purge")
    assert h.state["vault"] == "absent"
    assert "no active or soft-deleted vault" in result.stdout


def test_delete_times_out_bounded_when_proxy_never_appears(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", vault="live", delete_limbo=True)
    result = h.run("delete-keyvault.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "did not appear within the deadline" in result.stderr
    assert not any(call.startswith("keyvault purge") for call in h.calls)
    # Bounded: exactly AZ_KV_POLL_ATTEMPTS(=2) proxy polls, not an endless loop.
    assert sum(call.startswith("keyvault list-deleted") for call in h.calls) == 2


def test_delete_purge_failure_is_bounded_to_three_attempts(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", vault="deleted", fail_purge=True)
    result = h.run("delete-keyvault.sh")
    assert result.returncode != 0
    assert "Purge failed after 3 attempts" in result.stderr
    assert sum(call.startswith("keyvault purge") for call in h.calls) == 3


def test_delete_aborts_on_query_failure_before_mutating(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", vault="live", fail_list=True)
    result = h.run("delete-keyvault.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "Failed to query vault state" in result.stderr
    assert not any(call.startswith(("keyvault delete", "keyvault purge")) for call in h.calls)


@pytest.mark.parametrize("script", ["create-keyvault.sh", "delete-keyvault.sh"])
def test_scripts_exist_and_are_executable(script: str) -> None:
    path = SCRIPTS_DIR / script
    assert path.is_file()
    assert os.access(path, os.X_OK)
