"""Fake-CLI state regressions for the Day 24 Container Apps deploy script.

Runs the real bash script against fake ``az``, ``curl`` and ``uv`` executables
that model the Azure states the script claims to handle. Everything the script
does in a live session -- register providers, create a user-assigned managed
identity, grant it three roles, materialize the Search admin key into Key
Vault, build the image in ACR, create the environment and the app from a YAML
single source of truth, then gate on readiness -- is ordered, and the ordering
is the contract: a role granted before the identity exists, or a readiness gate
run before provisioning finished, is a deploy that reports success it did not
earn.

All three fakes append to one shared call log (the ``az`` calls verbatim, the
others prefixed with their own name), so cross-tool ordering assertions -- "the
gate ran after provisioningState came back Succeeded" -- are exact rather than
inferred.

Poll knobs (``ACA_POLL_ATTEMPTS``/``ACA_POLL_INTERVAL``, ``ROLE_POLL_ATTEMPTS``)
exist so these tests run in milliseconds while production defaults stay at
multi-minute deadlines. There are no real sleeps and no network here.

Secret hygiene has its own case: the Search admin key passes through this
script, and the one place it is allowed to appear is the argv of the
``keyvault secret set`` call that stores it. Never stdout, never stderr.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "infra" / "scripts"

SEARCH_KEY = "s3cr3t-search-admin-key-value"


def resource_id(provider: str, kind: str, name: str) -> str:
    return (
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
        f"/providers/{provider}/{kind}/{name}"
    )


MI_RESOURCE_ID = resource_id(
    "Microsoft.ManagedIdentity", "userAssignedIdentities", "mi-faked24"
)
MI_PRINCIPAL_ID = "33333333-3333-3333-3333-333333333333"
MI_CLIENT_ID = "44444444-4444-4444-4444-444444444444"

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

def opt(name: str) -> str:
    return args[args.index(name) + 1] if name in args else ""

def query_value() -> str:
    return opt("--query")

def done(out: str = "", code: int = 0) -> None:
    save()
    if out != "":
        print(out)
    sys.exit(code)

joined = " ".join(args)

if args[:2] == ["account", "set"]:
    done()
if args[:2] == ["provider", "show"]:
    ns = opt("--namespace")
    done(state["providers"].get(ns, "Registered"))
if args[:2] == ["provider", "register"]:
    state["providers"][opt("--namespace")] = "Registered"
    done()

# --- user-assigned managed identity ----------------------------------------
if args[:2] == ["identity", "list"]:
    m = re.search(r"name=='([^']*)'", query_value())
    done("1" if m and m.group(1) in state["identities"] else "0")
if args[:2] == ["identity", "create"]:
    state["identities"].append(opt("--name"))
    done("{}")
if args[:2] == ["identity", "show"]:
    field = query_value()
    value = state["identity_reads"].get(field, "")
    done(value)

# --- scope resource ids ------------------------------------------------------
if args[:2] == ["acr", "show"]:
    done(state["acr_id"])
if args[:2] == ["keyvault", "show"]:
    done(state["kv_id"])
if args[:3] == ["cognitiveservices", "account", "show"]:
    field = query_value()
    done(state["aoai_reads"].get(field, ""))

# --- role assignments --------------------------------------------------------
if args[:3] == ["role", "assignment", "create"]:
    if state.get("role_create_fails"):
        print("ERROR: injected role assignment create failure", file=sys.stderr)
        done(code=1)
    state.setdefault("roles", []).append(opt("--role"))
    done("{}")
if args[:3] == ["role", "assignment", "list"]:
    if state.get("role_readback_empty"):
        done("")
    done("1" if opt("--role") in state.get("roles", []) else "0")

# --- Search admin key --------------------------------------------------------
if args[:3] == ["search", "admin-key", "show"]:
    done(state["search_key"])
if args[:3] == ["keyvault", "secret", "set"]:
    state["secret_names"] = state.get("secret_names", []) + [opt("--name")]
    done(state["secret_id"])

# --- image -------------------------------------------------------------------
if args[:2] == ["acr", "build"]:
    done("{}")

# --- Container Apps environment ---------------------------------------------
if args[:3] == ["containerapp", "env", "list"]:
    m = re.search(r"name=='([^']*)'", query_value())
    done("1" if m and m.group(1) in state["envs"] else "0")
if args[:3] == ["containerapp", "env", "create"]:
    state["envs"].append(opt("--name"))
    done("{}")
if args[:3] == ["containerapp", "env", "show"]:
    done(state["env_state"])

# --- the app -----------------------------------------------------------------
if args[:2] == ["containerapp", "list"]:
    m = re.search(r"name=='([^']*)'", query_value())
    done("1" if m and m.group(1) in state["apps"] else "0")
if args[:2] == ["containerapp", "create"]:
    with open(opt("--yaml")) as f:
        state["app_yaml"] = f.read()
    state["apps"].append(opt("--name"))
    done("{}")
if args[:2] == ["containerapp", "show"]:
    field = query_value()
    if field.endswith("provisioningState"):
        done(state["app_state"])
    if field.endswith("fqdn"):
        done(state["fqdn"])
    done("")

print(f"fake az: unhandled command: {joined}", file=sys.stderr)
done(code=2)
'''

FAKE_CURL = '''#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["AZ_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)
state.setdefault("calls", []).append("curl " + " ".join(sys.argv[1:]))
with open(state_path, "w") as f:
    json.dump(state, f)
print(state.get("health_status", "200"))
'''

FAKE_UV = '''#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["AZ_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)
state.setdefault("calls", []).append("uv " + " ".join(sys.argv[1:]))
with open(state_path, "w") as f:
    json.dump(state, f)
sys.exit(int(state.get("gate_exit", 0)))
'''


class Harness:
    def __init__(self, tmp_path: Path, **overrides: object) -> None:
        state: dict[str, object] = {
            "providers": {},
            "identities": [],
            "identity_reads": {
                "id": MI_RESOURCE_ID,
                "principalId": MI_PRINCIPAL_ID,
                "clientId": MI_CLIENT_ID,
            },
            "acr_id": resource_id(
                "Microsoft.ContainerRegistry", "registries", "acrfaked24"
            ),
            "kv_id": resource_id("Microsoft.KeyVault", "vaults", "kvfaked24"),
            "aoai_reads": {
                "id": resource_id(
                    "Microsoft.CognitiveServices", "accounts", "aoaifaked24"
                ),
                "properties.endpoint": "https://aoaifaked24.openai.azure.com/",
            },
            "search_key": SEARCH_KEY,
            "secret_id": "https://kvfaked24.vault.azure.net/secrets/azure-search-admin-key/abc123",
            "envs": [],
            "env_state": "Succeeded",
            "apps": [],
            "app_state": "Succeeded",
            "fqdn": "aca-faked24.japaneast.azurecontainerapps.io",
        }
        state.update(overrides)
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps(state))

        fake_dir = tmp_path / "bin"
        fake_dir.mkdir()
        for name, body in (("az", FAKE_AZ), ("curl", FAKE_CURL), ("uv", FAKE_UV)):
            path = fake_dir / name
            path.write_text(body)
            path.chmod(0o755)

        self.env = {
            **os.environ,
            "PATH": f"{fake_dir}:{os.environ['PATH']}",
            "AZ_FAKE_STATE": str(self.state_path),
            "AZ_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
            "AZ_RESOURCE_GROUP": "rg",
            "AZ_ACR_NAME": "acrfaked24",
            "AZ_KEYVAULT_NAME": "kvfaked24",
            "AZ_SEARCH_NAME": "srchfaked24",
            "AZ_OPENAI_NAME": "aoaifaked24",
            "AZ_MI_NAME": "mi-faked24",
            "AZ_ACA_ENV_NAME": "acaenv-faked24",
            "AZ_ACA_APP_NAME": "aca-faked24",
            "ENTRA_TENANT_ID": "55555555-5555-5555-5555-555555555555",
            "ENTRA_AUDIENCE": "66666666-6666-6666-6666-666666666666",
            "ENTRA_CLIENT_APP_ID": "77777777-7777-7777-7777-777777777777",
            "ENTRA_CLIENT_SECRET": "not-a-real-secret",  # noqa: S105 - fake, fake-uv never reads it
            # Fast knobs: no real sleeps, bounded loops observable in the log.
            "ACA_POLL_ATTEMPTS": "2",
            "ACA_POLL_INTERVAL": "0",
            "ROLE_POLL_ATTEMPTS": "2",
        }

    def run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPTS_DIR / "deploy-container-app.sh")],
            env={**self.env, **extra_env},
            capture_output=True,
            text=True,
            timeout=60,
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
# 1. Stage ordering is the contract, not a comment.
# ---------------------------------------------------------------------------


def test_stage_ordering_is_pinned_end_to_end(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    order = [
        "provider show",
        "identity create",
        "role assignment create",
        "search admin-key show",
        "keyvault secret set",
        "acr build",
        "containerapp env create",
        "containerapp create",
    ]
    indices = [h.first_index(prefix) for prefix in order]
    assert indices == sorted(indices), list(zip(order, indices, strict=True))

    # Exactly three role grants, one per scope.
    assert h.count("role assignment create") == 3
    assert sorted(h.state["roles"]) == sorted(  # type: ignore[arg-type]
        ["AcrPull", "Key Vault Secrets User", "Cognitive Services OpenAI User"]
    )


# ---------------------------------------------------------------------------
# 2/3/5. Fail-closed reads: an empty `-o tsv` is a failed read, never a value.
# ---------------------------------------------------------------------------


def test_empty_identity_read_back_aborts_before_any_role_assignment(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        identity_reads={"id": "", "principalId": MI_PRINCIPAL_ID, "clientId": MI_CLIENT_ID},
    )
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not h.has("role assignment create")
    assert not h.has("containerapp create")


def test_empty_search_key_aborts_before_writing_the_secret(tmp_path: Path) -> None:
    h = Harness(tmp_path, search_key="")
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not h.has("keyvault secret set")
    assert not h.has("acr build")


def test_role_assignment_read_back_empty_aborts_immediately(tmp_path: Path) -> None:
    # `length([])` always prints a number when the query succeeds, so empty
    # output is a failed read, not "not assigned yet" -- it must abort on the
    # first attempt rather than burn the retry budget on a read that is broken.
    h = Harness(tmp_path, role_readback_empty=True)
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert h.count("role assignment list") == 1
    assert not h.has("search admin-key show")


def test_role_that_never_lands_aborts_after_bounded_retries(tmp_path: Path) -> None:
    # A valid "0" read-back IS retryable: directory replication right after the
    # identity is created makes the first create fail transiently. Bounded, and
    # a failure at the end -- never a warning that lets the deploy continue.
    h = Harness(tmp_path, role_create_fails=True)
    result = h.run()
    assert result.returncode != 0
    assert "AcrPull" in result.stderr
    assert h.count("role assignment list") == 2  # ROLE_POLL_ATTEMPTS
    assert not h.has("search admin-key show")


# ---------------------------------------------------------------------------
# 4. Secret hygiene.
# ---------------------------------------------------------------------------


def test_search_key_reaches_key_vault_but_never_the_terminal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    secret_call = next(call for call in h.calls if call.startswith("keyvault secret set"))
    assert "--name azure-search-admin-key" in secret_call
    assert h.state["secret_names"] == ["azure-search-admin-key"]

    # The one place the value is allowed to be is that call's argv. Not the
    # operator's terminal, and not a shell trace.
    assert SEARCH_KEY not in result.stdout
    assert SEARCH_KEY not in result.stderr

    # Again under `set -x`, which prints every assignment WITH its substituted
    # value -- the one mechanism that would leak the key without any line of
    # this script ever echoing it. A fresh harness, because the first run left
    # the app created and this script only creates from scratch.
    traced_dir = tmp_path / "traced"
    traced_dir.mkdir()
    traced_h = Harness(traced_dir)
    traced = traced_h.run(SHELLOPTS="xtrace")
    assert traced.returncode == 0, traced.stderr
    assert SEARCH_KEY not in traced.stdout
    assert SEARCH_KEY not in traced.stderr
    # ENTRA_CLIENT_SECRET is never assigned or read by name anywhere in this
    # script -- it reaches the gate subprocess by plain environment
    # inheritance -- so no line of it can ever appear in a trace either.
    assert "not-a-real-secret" not in traced.stdout
    assert "not-a-real-secret" not in traced.stderr
    # Tracing really was on before the secret block and again after it, so the
    # clean stderr above is suppression working rather than xtrace never
    # starting -- and the restore is proven, not assumed.
    assert "+ az account set" in traced.stderr
    assert "+ az acr build" in traced.stderr
    # And the block itself is genuinely dark: not even the command that reads
    # the key is traced, because bash would print the assignment's value.
    assert "az search admin-key show" not in traced.stderr


# ---------------------------------------------------------------------------
# 6. Provisioning that never succeeds is a failure, not a wait forever.
# ---------------------------------------------------------------------------


def test_stuck_provisioning_state_exits_non_zero_after_bounded_polling(tmp_path: Path) -> None:
    h = Harness(tmp_path, app_state="InProgress")
    result = h.run()
    assert result.returncode != 0
    assert "InProgress" in result.stderr
    provisioning_polls = [
        call for call in h.calls if call.startswith("containerapp show") and "provisioning" in call
    ]
    assert len(provisioning_polls) == 2  # ACA_POLL_ATTEMPTS
    assert not h.has("uv ")


# ---------------------------------------------------------------------------
# 7. The generated YAML is the app's single source of truth.
# ---------------------------------------------------------------------------


def test_generated_yaml_carries_the_whole_app_contract(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    yaml_text = h.state["app_yaml"]
    assert isinstance(yaml_text, str)

    # Attachment, not reference: only the top-level identity block attaches a
    # user-assigned identity to the app.
    assert "identity:" in yaml_text
    assert "type: UserAssigned" in yaml_text
    assert f'"{MI_RESOURCE_ID}": {{}}' in yaml_text

    assert "terminationGracePeriodSeconds: 30" in yaml_text
    for probe in ("type: Startup", "type: Liveness", "type: Readiness"):
        assert probe in yaml_text
    assert "minReplicas: 1" in yaml_text
    assert "maxReplicas: 1" in yaml_text
    assert "secretRef: search-admin-key" in yaml_text
    assert "keyVaultUrl: https://kvfaked24.vault.azure.net/secrets/azure-search-admin-key" in (
        yaml_text
    )
    assert "acrfaked24.azurecr.io/azgenai-lab:day-24" in yaml_text

    # The versioned secret id is never what the app references: a versionless
    # URL is what lets a rotated secret be picked up without a redeploy.
    assert "/abc123" not in yaml_text
    # And the key itself is nowhere near this file.
    assert SEARCH_KEY not in yaml_text

    # Everything Settings needs to construct itself in entra mode. A missing
    # one of these is not a YAML nit: the app fails validation at import time
    # and the readiness gate reports a deploy that never served a request.
    for name in (
        "AZURE_OPENAI_AUTH",
        "AZURE_CLIENT_ID",
        "AUTH_MODE",
        "ENTRA_TENANT_ID",
        "ENTRA_AUDIENCE",
        "ENTRA_REQUIRED_SCOPE",
        "ENTRA_REQUIRED_APP_ROLE",
        "USE_FAKE_LLM",
        "USE_FAKE_SEARCH",
        "USE_FAKE_EMBEDDINGS",
        "SAMPLE_DOCS_DIR",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
        "AZURE_SEARCH_ENDPOINT",
        "AZURE_SEARCH_ADMIN_KEY",
    ):
        assert f"- name: {name}\n" in yaml_text, name
    assert f'value: "{MI_CLIENT_ID}"' in yaml_text


# ---------------------------------------------------------------------------
# 8. The readiness gate runs last, or it is measuring nothing.
# ---------------------------------------------------------------------------


def test_gate_runs_only_after_provisioning_succeeded(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    gate_index = h.first_index("uv ")
    assert h.first_index("containerapp create") < gate_index
    assert h.first_index("curl ") < gate_index

    gate_call = next(call for call in h.calls if call.startswith("uv "))
    assert "tools/entra_smoke.py" in gate_call
    args = gate_call.split()
    assert "--gate" in args
    assert "aca-faked24.japaneast.azurecontainerapps.io" in gate_call
    # The gate calls an AUTH_MODE=entra app: with no credentials at all it can
    # only ever see a 401 this API decides before touching Azure OpenAI, which
    # no amount of backoff turns into a 200. So it needs the same
    # client-credentials inputs --phase no-role/full already use.
    assert "--tenant-id" in args and "55555555-5555-5555-5555-555555555555" in args
    assert "--client-id" in args and "77777777-7777-7777-7777-777777777777" in args
    assert "--api-app-id" in args and "66666666-6666-6666-6666-666666666666" in args
    # The secret travels through the environment, never the argv the fake `uv`
    # call log records.
    assert "not-a-real-secret" not in gate_call


def test_failing_gate_fails_the_deploy(tmp_path: Path) -> None:
    h = Harness(tmp_path, gate_exit=1)
    result = h.run()
    assert result.returncode != 0
    assert h.has("uv ")


def test_script_exists_and_is_executable() -> None:
    path = SCRIPTS_DIR / "deploy-container-app.sh"
    assert path.is_file()
    assert os.access(path, os.X_OK)
