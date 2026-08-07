"""Fake-CLI state regressions for the Day 21 Content Safety lifecycle scripts
(create-content-safety.sh, delete-content-safety.sh,
run-content-safety-probe.sh).

Mirrors tests/unit/test_keyvault_script_lifecycle.py: a fake `az` (and, for
the orchestrator, a fake `uv`) is shimmed onto PATH and records every
invocation to a call log, so ordering and argument assertions are exact and
the whole suite runs in milliseconds — no real Azure call is ever made.

Three things this repo has been bitten by before (Day 20 review) and this
suite guards against here too:
  - a `$(query)` used directly inside a conditional swallows the query's own
    failure and misreads it as a benign value — every state query here is
    `VAR=$(query) || fail_query ...` on its own line;
  - a destructive/irreversible flag should be proven by read-back, not by
    passing an explicit boolean (n/a for a key-based resource, but the
    SKU-fallback allowlist follows the same "safe until proven" posture:
    it starts empty and stays empty unless a code is explicitly allowlisted);
  - a fresh script file is mode 0644 until `chmod +x`, so a regression that
    drops the executable bit must fail a test that runs the script directly,
    not through `bash script.sh`.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "infra" / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_CASES_FILE = REPO_ROOT / "tools" / "prompt_shields_cases.json"

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

def fail(body: dict, code: int = 1) -> None:
    save()
    print(json.dumps(body), file=sys.stderr)
    sys.exit(code)

def registered() -> bool:
    return state["provider"] == "Registered"

joined = " ".join(args)

if args[:2] == ["provider", "show"]:
    done(state["provider"])
if args[:2] == ["provider", "register"]:
    state["provider"] = "Registered"
    done()
if args[:3] == ["cognitiveservices", "account", "create"]:
    sku = args[args.index("--sku") + 1] if "--sku" in args else None
    fail_code = state.get("create_fail_code")
    if fail_code and sku != "S0":
        if state.get("create_partial_leak"):
            state["account"] = "live"
        fail({"error": {"code": fail_code, "message": "synthetic failure for tests"}})
    state["account"] = "live"
    state["created_sku"] = sku
    done("{}")
if args[:3] == ["cognitiveservices", "account", "list-deleted"]:
    if not registered():
        print("ERROR: (MissingSubscriptionRegistration)", file=sys.stderr)
        done(code=1)
    if state.get("fail_list_deleted"):
        print("ERROR: injected list-deleted failure", file=sys.stderr)
        done(code=1)
    done("1" if state["account"] == "deleted" else "0")
if args[:3] == ["cognitiveservices", "account", "list"]:
    if not registered():
        print("ERROR: (MissingSubscriptionRegistration)", file=sys.stderr)
        done(code=1)
    if state.get("fail_list"):
        print("ERROR: injected list failure", file=sys.stderr)
        done(code=1)
    done("1" if state["account"] == "live" else "0")
if args[:3] == ["cognitiveservices", "account", "delete"]:
    if state.get("fail_account_delete_once"):
        # Consumed after one shot: simulates `az cognitiveservices account
        # delete` itself exiting 3 (Azure CLI's own resource-not-found
        # status) on exactly the FIRST call this run makes, unrelated to
        # create-content-safety.sh's pre-existence guard. The account is
        # NOT mutated to "deleted" here -- the delete did not succeed -- so
        # a subsequent retry call still sees it as "live".
        state["fail_account_delete_once"] = False
        print("ERROR: injected account delete exit 3 (resource-not-found)", file=sys.stderr)
        done(code=3)
    state["account"] = "limbo" if state.get("delete_limbo") else "deleted"
    done()
if args[:3] == ["cognitiveservices", "account", "show"]:
    if state.get("fail_show"):
        print("ERROR: injected show failure", file=sys.stderr)
        done(code=1)
    answers = {
        "properties.endpoint": "https://fake.cognitiveservices.azure.com/",
        # Reflects whatever SKU the create call actually landed on (may
        # differ from the requested AZ_CONTENT_SAFETY_SKU if a fallback
        # retry fired), so tests can assert the orchestrator reads back and
        # forwards the ACTUAL sku, not the requested one.
        "sku.name": state.get("created_sku", ""),
    }
    done(answers.get(query_value(), ""))
if args[:4] == ["cognitiveservices", "account", "keys", "list"]:
    if state.get("fail_keys"):
        print("ERROR: injected keys failure", file=sys.stderr)
        done(code=1)
    done("FAKE-KEY-VALUE" if query_value() == "key1" else "")
if args[:2] == ["resource", "delete"]:
    if state.get("fail_purge"):
        print("ERROR: injected purge conflict", file=sys.stderr)
        done(code=1)
    state["account"] = "absent"
    done()

print(f"fake az: unhandled command: {joined}", file=sys.stderr)
done(code=2)
'''

FAKE_UV = '''#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["AZ_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)

args = sys.argv[1:]
state.setdefault("calls", []).append(" ".join(args))

def save() -> None:
    with open(state_path, "w") as f:
        json.dump(state, f)

if args[:4] == ["run", "python", "-m", "tools.prompt_shields_probe"]:
    # Recorded so tests can assert the orchestrator cd'd to the repo root
    # before invoking uv: `tools/` is not an installed package, so the real
    # `uv run python -m tools.prompt_shields_probe` only resolves against
    # its own cwd, not against wherever the caller started the script.
    state["uv_cwd"] = os.getcwd()
    # Recorded so tests can assert which SKU value the orchestrator actually
    # forwarded to the probe -- the requested one or the account's real one.
    state["probe_env_sku"] = os.environ.get("AZ_CONTENT_SAFETY_SKU")
    exit_code = state.get("probe_exit_code", 0)
    if exit_code == 0 and "--evidence-out" in args:
        evidence_path = args[args.index("--evidence-out") + 1]
        with open(evidence_path, "w") as f:
            json.dump({"results": []}, f)
    save()
    sys.exit(exit_code)

save()
print(f"fake uv: unhandled command: {' '.join(args)}", file=sys.stderr)
sys.exit(2)
'''


class Harness:
    def __init__(
        self, tmp_path: Path, *, provider: str, account: str, **flags: object
    ) -> None:
        self.tmp_path = tmp_path
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps({"provider": provider, "account": account, **flags}))
        fake_dir = tmp_path / "bin"
        fake_dir.mkdir()
        fake_az = fake_dir / "az"
        fake_az.write_text(FAKE_AZ)
        fake_az.chmod(0o755)
        fake_uv = fake_dir / "uv"
        fake_uv.write_text(FAKE_UV)
        fake_uv.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{fake_dir}:{os.environ['PATH']}",
            "AZ_FAKE_STATE": str(self.state_path),
            "AZ_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
            "AZ_CONTENT_SAFETY_NAME": "cs-fake-d21",
            "AZ_CS_POLL_ATTEMPTS": "2",
            "AZ_CS_POLL_INTERVAL": "0",
            "AZ_CS_RETRY_INTERVAL": "0",
        }

    def run(
        self, script: str, *, cwd: str | None = None, **extra_env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPTS_DIR / script)],
            env={**self.env, **extra_env},
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def run_direct(
        self, script: str, *, cwd: str | None = None, **extra_env: str
    ) -> subprocess.CompletedProcess[str]:
        # No "bash" prefix: this is the one path that fails closed (exit 126 /
        # PermissionError) if a regression drops the executable bit.
        return subprocess.run(
            [str(SCRIPTS_DIR / script)],
            env={**self.env, **extra_env},
            cwd=cwd,
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


# --- create-content-safety.sh -----------------------------------------------


def test_create_registers_provider_before_create_and_every_call_carries_subscription(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, provider="NotRegistered", account="absent")
    result = h.run("create-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode == 0, result.stderr
    assert h.first_index("provider register") < h.first_index("cognitiveservices account create")
    assert h.state["account"] == "live"
    assert h.calls, "expected at least one az call"
    for call in h.calls:
        assert "--subscription" in call, call


def test_create_f0_success_path(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent")
    result = h.run("create-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode == 0, result.stderr
    assert h.state["account"] == "live"
    assert h.state["created_sku"] == "F0"
    assert sum(call.startswith("cognitiveservices account create") for call in h.calls) == 1


def test_create_sku_fallback_retries_on_allowlisted_code(tmp_path: Path) -> None:
    h = Harness(
        tmp_path, provider="Registered", account="absent", create_fail_code="TESTFALLBACK"
    )
    result = h.run(
        "create-content-safety.sh",
        AZ_RESOURCE_GROUP="rg",
        CONTENT_SAFETY_SKU_FALLBACK_CODES="TESTFALLBACK",
    )
    assert result.returncode == 0, result.stderr
    create_calls = [c for c in h.calls if c.startswith("cognitiveservices account create")]
    assert len(create_calls) == 2
    assert "--sku S0" in create_calls[1]
    assert h.state["account"] == "live"
    assert h.state["created_sku"] == "S0"


def test_create_sku_fallback_ignores_override_for_non_matching_code(tmp_path: Path) -> None:
    # The allowlist override is present but the observed code does not match
    # it: still aborts, no retry. The mechanism checks the code, not merely
    # whether an override was supplied.
    h = Harness(
        tmp_path, provider="Registered", account="absent", create_fail_code="OTHER_CODE"
    )
    result = h.run(
        "create-content-safety.sh",
        AZ_RESOURCE_GROUP="rg",
        CONTENT_SAFETY_SKU_FALLBACK_CODES="TESTFALLBACK",
    )
    assert result.returncode != 0
    create_calls = [c for c in h.calls if c.startswith("cognitiveservices account create")]
    assert len(create_calls) == 1
    assert h.state["account"] == "absent"


@pytest.mark.parametrize(
    "synthetic_code",
    ["SIMULATED_UNKNOWN_CODE", "SIMULATED_AUTH_DENIED", "SIMULATED_WRONG_KIND"],
)
def test_create_aborts_on_any_failure_with_default_empty_allowlist(
    tmp_path: Path, synthetic_code: str
) -> None:
    # These are synthetic stand-ins, not real Azure error codes: the point is
    # that the DEFAULT (unset/empty) allowlist rejects every code, whatever
    # its category. No CONTENT_SAFETY_SKU_FALLBACK_CODES override is set.
    h = Harness(tmp_path, provider="Registered", account="absent", create_fail_code=synthetic_code)
    result = h.run("create-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert not any(
        call.startswith("cognitiveservices account create") and "--sku S0" in call
        for call in h.calls
    )
    assert sum(call.startswith("cognitiveservices account create") for call in h.calls) == 1
    assert h.state["account"] == "absent"


def test_create_aborts_when_name_already_live_no_create_call(tmp_path: Path) -> None:
    # Mirrors create-keyvault.sh's own precedent: the exposure here is worse
    # (the orchestrator's EXIT trap deletes AND purges unconditionally), so a
    # name collision with an existing account must never reach `create`.
    h = Harness(tmp_path, provider="Registered", account="live")
    result = h.run("create-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "already exists live" in result.stderr
    assert not any(call.startswith("cognitiveservices account create") for call in h.calls)


def test_create_aborts_when_name_soft_deleted_no_create_call(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="deleted")
    result = h.run("create-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "soft-deleted" in result.stderr
    assert not any(call.startswith("cognitiveservices account create") for call in h.calls)


def test_create_proceeds_to_create_when_name_is_absent_from_both_listings(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent")
    result = h.run("create-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode == 0, result.stderr
    assert any(call.startswith("cognitiveservices account create") for call in h.calls)


def test_create_aborts_on_live_listing_query_failure_not_assume_absent(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent", fail_list=True)
    result = h.run("create-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "Failed to query" in result.stderr
    assert not any(call.startswith("cognitiveservices account create") for call in h.calls)


def test_create_aborts_on_deleted_listing_query_failure_not_assume_absent(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent", fail_list_deleted=True)
    result = h.run("create-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "Failed to query" in result.stderr
    assert not any(call.startswith("cognitiveservices account create") for call in h.calls)


# --- delete-content-safety.sh -----------------------------------------------


def test_delete_requires_resource_group_even_when_soft_deleted_only(tmp_path: Path) -> None:
    # Deviation from delete-keyvault.sh: the Cognitive Services purge resource
    # ID embeds the ORIGINAL resource group, so it cannot be constructed from
    # name + location alone even when the account is only soft-deleted.
    h = Harness(tmp_path, provider="Registered", account="deleted")
    result = h.run("delete-content-safety.sh")  # no AZ_RESOURCE_GROUP
    assert result.returncode != 0
    assert "AZ_RESOURCE_GROUP" in result.stderr
    assert not h.calls


def test_delete_purges_soft_deleted_directly(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="deleted")
    result = h.run("delete-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode == 0, result.stderr
    assert h.state["account"] == "absent"
    assert not any(call.startswith("cognitiveservices account delete") for call in h.calls)
    assert any(call.startswith("resource delete") for call in h.calls)
    assert "no active or soft-deleted account" in result.stdout


def test_delete_of_absent_account_is_a_noop_success(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent")
    result = h.run("delete-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode == 0, result.stderr
    assert "Nothing to do" in result.stdout
    assert not any(
        call.startswith(("cognitiveservices account delete", "resource delete")) for call in h.calls
    )


def test_delete_full_cycle_from_live(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="live")
    result = h.run("delete-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode == 0, result.stderr
    assert h.first_index("cognitiveservices account delete") < h.first_index("resource delete")
    assert h.state["account"] == "absent"
    assert "no active or soft-deleted account" in result.stdout


def test_delete_times_out_bounded_when_proxy_never_appears(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="live", delete_limbo=True)
    result = h.run("delete-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "did not appear within the deadline" in result.stderr
    assert not any(call.startswith("resource delete") for call in h.calls)
    assert sum(call.startswith("cognitiveservices account list-deleted") for call in h.calls) == 2


def test_delete_purge_failure_is_bounded_to_three_attempts(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="deleted", fail_purge=True)
    result = h.run("delete-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "Purge failed after 3 attempts" in result.stderr
    assert sum(call.startswith("resource delete") for call in h.calls) == 3


def test_delete_aborts_on_query_failure_before_mutating(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="live", fail_list=True)
    result = h.run("delete-content-safety.sh", AZ_RESOURCE_GROUP="rg")
    assert result.returncode != 0
    assert "Failed to query account state" in result.stderr
    assert not any(
        call.startswith(("cognitiveservices account delete", "resource delete")) for call in h.calls
    )


# --- run-content-safety-probe.sh --------------------------------------------


def test_orchestrator_happy_path_full_lifecycle_direct_execution(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent")
    evidence = tmp_path / "evidence.json"
    result = h.run_direct(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    assert result.returncode == 0, result.stderr
    assert h.state["account"] == "absent"  # explicit teardown ran, trap did not re-fire
    probe_calls = [c for c in h.calls if c.startswith("run python -m tools.prompt_shields_probe")]
    assert len(probe_calls) == 1
    assert f"--cases-file {CANONICAL_CASES_FILE}" in probe_calls[0]
    assert f"--evidence-out {evidence}" in probe_calls[0]
    # trap-before-create ordering: create is the first mutating call.
    assert h.first_index("cognitiveservices account create") < h.first_index(
        "run python -m tools.prompt_shields_probe"
    )
    # Exactly one full delete->purge cycle: the explicit teardown, not a
    # second cleanup-triggered one (which would mean the trap misfired).
    assert sum(call.startswith("cognitiveservices account delete") for call in h.calls) == 1
    assert sum(call.startswith("resource delete") for call in h.calls) == 1


def test_orchestrator_probe_invoked_from_repo_root_and_relative_evidence_out_lands_at_caller_cwd(
    tmp_path: Path,
) -> None:
    # The documented invocation is `cd infra/scripts && ./run-content-safety-
    # probe.sh`. `tools/` is not an installed package, so `uv run python -m
    # tools.prompt_shields_probe` only resolves when uv's own cwd is the repo
    # root — the script must cd there before invoking the probe, no matter
    # what directory the caller started in. A relative EVIDENCE_OUT must
    # still be interpreted against the CALLER's cwd, not the repo root the
    # script cd's into.
    stray = REPO_ROOT / "evidence.json"
    assert not stray.exists(), "stray file from a previous failed run; remove it and rerun"
    h = Harness(tmp_path, provider="Registered", account="absent")
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    try:
        result = h.run_direct(
            "run-content-safety-probe.sh",
            cwd=str(caller_dir),
            AZ_RESOURCE_GROUP="rg",
            EVIDENCE_OUT="evidence.json",
        )
        assert result.returncode == 0, result.stderr
        assert h.state.get("uv_cwd") == str(REPO_ROOT)
        assert (caller_dir / "evidence.json").exists()
        assert not stray.exists()
    finally:
        stray.unlink(missing_ok=True)


def test_orchestrator_forwards_actual_created_sku_not_requested_to_probe(tmp_path: Path) -> None:
    # create-content-safety.sh's own SKU-fallback retry (F0 -> S0) happens
    # inside its own child process, so this orchestrator's copy of
    # AZ_CONTENT_SAFETY_SKU (the REQUESTED value) never sees it. The
    # orchestrator must read back the account's ACTUAL sku via
    # `account show` and forward that to the probe instead.
    h = Harness(
        tmp_path, provider="Registered", account="absent", create_fail_code="TESTFALLBACK"
    )
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
        AZ_CONTENT_SAFETY_SKU="F0",
        CONTENT_SAFETY_SKU_FALLBACK_CODES="TESTFALLBACK",
    )
    assert result.returncode == 0, result.stderr
    assert h.state["created_sku"] == "S0"
    assert h.state["probe_env_sku"] == "S0"  # not "F0", the requested value


def test_orchestrator_does_not_purge_preexisting_live_account_on_refusal(tmp_path: Path) -> None:
    # The pre-existence guard in create-content-safety.sh REFUSES when an
    # account already exists live under this name — it does not create
    # anything. But the orchestrator's EXIT trap is armed before create runs,
    # so without a way to distinguish "refused, nothing of mine exists" from
    # every other failure, the trap would still delete+purge the pre-existing
    # account it never created. That is the bug this test catches: it must
    # fail (non-zero) but leave the account, and the subscription, untouched.
    h = Harness(tmp_path, provider="Registered", account="live")
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    assert result.returncode != 0
    assert not any(call.startswith("cognitiveservices account delete") for call in h.calls)
    assert not any(call.startswith("resource delete") for call in h.calls)
    assert h.state["account"] == "live"  # untouched: this run never owned it
    assert not any(call.startswith("run python -m tools.prompt_shields_probe") for call in h.calls)


def test_orchestrator_does_not_purge_preexisting_soft_deleted_account_on_refusal(
    tmp_path: Path,
) -> None:
    # Same bug, soft-deleted variant: create-content-safety.sh refuses
    # because the name is held by a soft-deleted account. The orchestrator
    # must not purge it either — purge is irreversible.
    h = Harness(tmp_path, provider="Registered", account="deleted")
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    assert result.returncode != 0
    assert not any(call.startswith("cognitiveservices account delete") for call in h.calls)
    assert not any(call.startswith("resource delete") for call in h.calls)
    assert h.state["account"] == "deleted"  # untouched: this run never owned it
    assert not any(call.startswith("run python -m tools.prompt_shields_probe") for call in h.calls)


def test_orchestrator_retries_teardown_when_explicit_teardown_itself_exits_3(
    tmp_path: Path,
) -> None:
    # A bug this test catches: create-content-safety.sh's pre-existence guard
    # is not the only thing that can exit 3. `az cognitiveservices account
    # delete` can itself exit 3 (Azure CLI's own resource-not-found status)
    # for reasons that have nothing to do with that guard. Here: create
    # succeeds, the probe succeeds, and the EXPLICIT end-of-run teardown call
    # near the bottom of the orchestrator fails because its internal
    # `account delete` call exits 3. `set -e` propagates that 3 into the EXIT
    # trap. A cleanup() that infers "create refused, nothing to tear down"
    # from a bare `status -eq 3` comparison would misread this and skip the
    # retry teardown -- leaving the account this run DID create still live,
    # while printing a message that says the opposite of what happened. The
    # fix must instead retry teardown and never print the refusal message.
    h = Harness(
        tmp_path, provider="Registered", account="absent", fail_account_delete_once=True
    )
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    # The original (explicit-teardown) failure status survives -- this run's
    # own step failed even though the trap's retry eventually cleaned up.
    assert result.returncode != 0
    assert "refused" not in result.stderr
    assert "cleanup: delete-content-safety.sh failed" not in result.stderr
    assert h.state["account"] == "absent"  # the trap's retry actually tore it down
    assert sum(call.startswith("cognitiveservices account delete") for call in h.calls) == 2
    assert sum(call.startswith("resource delete") for call in h.calls) == 1


def test_orchestrator_teardown_runs_when_create_leaves_partial_resource(tmp_path: Path) -> None:
    # The trap is armed BEFORE create runs: a create that fails after the
    # resource actually got created must not leave it behind.
    h = Harness(
        tmp_path,
        provider="Registered",
        account="absent",
        create_fail_code="SIMULATED_PARTIAL_FAILURE",
        create_partial_leak=True,
    )
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    assert result.returncode != 0
    assert h.state["account"] == "absent"  # cleanup trap purged the leaked resource
    assert any(call.startswith("cognitiveservices account delete") for call in h.calls)
    assert any(call.startswith("resource delete") for call in h.calls)
    assert not any(call.startswith("run python -m tools.prompt_shields_probe") for call in h.calls)


def test_orchestrator_teardown_runs_when_endpoint_retrieval_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent", fail_show=True)
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    assert result.returncode != 0
    # The account must actually have been created, then torn down by the
    # trap — not merely "still absent" because nothing ran at all.
    assert any(call.startswith("cognitiveservices account create") for call in h.calls)
    assert any(call.startswith("cognitiveservices account show") for call in h.calls)
    assert any(call.startswith("cognitiveservices account delete") for call in h.calls)
    assert any(call.startswith("resource delete") for call in h.calls)
    assert h.state["account"] == "absent"  # trap still tore down the created account
    assert not any(call.startswith("run python -m tools.prompt_shields_probe") for call in h.calls)


def test_orchestrator_teardown_runs_when_key_retrieval_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent", fail_keys=True)
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    assert result.returncode != 0
    assert any(call.startswith("cognitiveservices account create") for call in h.calls)
    assert any(call.startswith("cognitiveservices account keys list") for call in h.calls)
    assert any(call.startswith("cognitiveservices account delete") for call in h.calls)
    assert any(call.startswith("resource delete") for call in h.calls)
    assert h.state["account"] == "absent"
    assert not any(call.startswith("run python -m tools.prompt_shields_probe") for call in h.calls)


def test_orchestrator_teardown_runs_and_status_preserved_when_probe_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent", probe_exit_code=5)
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    # The original probe exit status survives the cleanup trap verbatim.
    assert result.returncode == 5, result.stderr
    assert h.state["account"] == "absent"
    assert any(call.startswith("cognitiveservices account delete") for call in h.calls)
    assert any(call.startswith("resource delete") for call in h.calls)


def test_orchestrator_explicit_teardown_failure_is_not_masked_by_trap_disarm(
    tmp_path: Path,
) -> None:
    # Purge fails every time (both the explicit end-of-run teardown and the
    # cleanup trap's own teardown attempt): the run must still end non-zero.
    # A prior success followed by a masked failure would be exactly the
    # double-failure-becomes-success bug this test exists to catch.
    h = Harness(tmp_path, provider="Registered", account="absent", fail_purge=True)
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    assert result.returncode != 0
    assert "cleanup: delete-content-safety.sh failed" in result.stderr
    # Explicit teardown (3 attempts) + cleanup-triggered retry (3 attempts).
    assert sum(call.startswith("resource delete") for call in h.calls) == 6
    # The account was actually created and deleted (soft-deleted), just never
    # successfully purged — never silently reported as success.
    assert h.state["account"] == "deleted"


# --- fixture resolution ------------------------------------------------------


def test_orchestrator_resolves_canonical_cases_file_when_unset(tmp_path: Path) -> None:
    h = Harness(tmp_path, provider="Registered", account="absent")
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
    )
    assert result.returncode == 0, result.stderr
    probe_calls = [c for c in h.calls if c.startswith("run python -m tools.prompt_shields_probe")]
    assert f"--cases-file {CANONICAL_CASES_FILE}" in probe_calls[0]


def test_orchestrator_honors_cases_file_override(tmp_path: Path) -> None:
    custom_cases = tmp_path / "custom-cases.json"
    custom_cases.write_text("{}")
    h = Harness(tmp_path, provider="Registered", account="absent")
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
        PROMPT_SHIELDS_CASES_FILE=str(custom_cases),
    )
    assert result.returncode == 0, result.stderr
    probe_calls = [c for c in h.calls if c.startswith("run python -m tools.prompt_shields_probe")]
    assert f"--cases-file {custom_cases}" in probe_calls[0]


def test_orchestrator_fails_fast_on_missing_resolved_cases_file_before_any_mutation(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.json"
    h = Harness(tmp_path, provider="Registered", account="absent")
    evidence = tmp_path / "evidence.json"
    result = h.run(
        "run-content-safety-probe.sh",
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
        PROMPT_SHIELDS_CASES_FILE=str(missing),
    )
    assert result.returncode != 0
    assert "cases file not readable" in result.stderr
    assert not h.calls  # validated before create-content-safety.sh (or anything else) ran
    assert not evidence.exists()


def test_orchestrator_honors_relative_cases_file_override_from_foreign_cwd(
    tmp_path: Path,
) -> None:
    # PROMPT_SHIELDS_CASES_FILE can be overridden with a RELATIVE path. The
    # readability check (before the script cd's to REPO_ROOT) resolves it
    # against the caller's cwd, but the probe used to receive the same
    # relative string unmodified after the cd — silently reinterpreted
    # against the repo root instead of where the file actually is. Use a
    # caller cwd that is neither the repo root nor tmp_path itself so a
    # regression can't coincidentally pass.
    h = Harness(tmp_path, provider="Registered", account="absent")
    caller_dir = tmp_path / "caller-relative-cases"
    caller_dir.mkdir()
    custom_cases = caller_dir / "custom-cases.json"
    custom_cases.write_text("{}")
    evidence = tmp_path / "evidence-relative-cases.json"
    result = h.run(
        "run-content-safety-probe.sh",
        cwd=str(caller_dir),
        AZ_RESOURCE_GROUP="rg",
        EVIDENCE_OUT=str(evidence),
        PROMPT_SHIELDS_CASES_FILE="custom-cases.json",
    )
    assert result.returncode == 0, result.stderr
    probe_calls = [c for c in h.calls if c.startswith("run python -m tools.prompt_shields_probe")]
    assert len(probe_calls) == 1
    assert f"--cases-file {custom_cases}" in probe_calls[0]


@pytest.mark.parametrize(
    "script",
    ["create-content-safety.sh", "delete-content-safety.sh", "run-content-safety-probe.sh"],
)
def test_scripts_exist_and_are_executable(script: str) -> None:
    path = SCRIPTS_DIR / script
    assert path.is_file()
    assert os.access(path, os.X_OK)
