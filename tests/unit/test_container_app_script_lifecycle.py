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
LAW_SHARED_KEY = "s3cr3t-law-shared-key-value"  # noqa: S105 - fake, never a real credential


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

# The same three scope ids delete-container-app.sh's fake-CLI tests grant the
# identity, built the same way the Harness default state below builds them --
# so a delete test can assert against the exact resource id the fake `az acr
# show` / `az keyvault show` / `az cognitiveservices account show` calls
# would read back for the default resource names.
ACR_ID = resource_id("Microsoft.ContainerRegistry", "registries", "acrfaked24")
KV_ID = resource_id("Microsoft.KeyVault", "vaults", "kvfaked24")
AOAI_ID = resource_id("Microsoft.CognitiveServices", "accounts", "aoaifaked24")

ENV_ID = resource_id("Microsoft.App", "managedEnvironments", "acaenv-faked24")

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
if args[:2] == ["extension", "show"]:
    # Absent extension: az exits non-zero and prints nothing to stdout.
    if opt("--name") in state.get("extensions", []):
        done("{}")
    done(code=1)
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
if args[:2] == ["identity", "delete"]:
    name = opt("--name")
    if not state.get("identity_delete_ineffective") and name in state.get(
        "identities", []
    ):
        state["identities"].remove(name)
    done()

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
    if opt("--role"):
        # deploy-container-app.sh's per-role read-back (scoped by --role).
        if state.get("role_readback_empty"):
            done("")
        done("1" if opt("--role") in state.get("roles", []) else "0")
    # delete-container-app.sh's final read-back: ALL assignments still held
    # by the assignee, regardless of role or scope.
    if state.get("delete_readback_query_fails"):
        done("")
    if "leftover_override" in state:
        done(str(state["leftover_override"]))
    done(str(len(state.get("assigned_scopes", []))))
if args[:3] == ["role", "assignment", "delete"]:
    if state.get("role_delete_fails"):
        print("ERROR: injected role assignment delete failure", file=sys.stderr)
        done(code=1)
    scope = opt("--scope")
    assigned = state.get("assigned_scopes", [])
    if scope in assigned:
        assigned.remove(scope)
    state["assigned_scopes"] = assigned
    done()

# --- Search admin key --------------------------------------------------------
if args[:3] == ["search", "admin-key", "show"]:
    done(state["search_key"])
if args[:3] == ["keyvault", "secret", "set"]:
    state["secret_names"] = state.get("secret_names", []) + [opt("--name")]
    done(state["secret_id"])

# --- image -------------------------------------------------------------------
if args[:2] == ["acr", "build"]:
    done("{}")

# --- Log Analytics workspace --------------------------------------------------
if args[:4] == ["monitor", "log-analytics", "workspace", "list"]:
    m = re.search(r"name=='([^']*)'", query_value())
    done("1" if m and m.group(1) in state.get("law_workspaces", []) else "0")
if args[:4] == ["monitor", "log-analytics", "workspace", "create"]:
    state.setdefault("law_workspaces", []).append(opt("--workspace-name"))
    done("{}")
if args[:4] == ["monitor", "log-analytics", "workspace", "show"]:
    done(state.get("law_customer_id", ""))
if args[:4] == ["monitor", "log-analytics", "workspace", "get-shared-keys"]:
    done(state.get("law_shared_key", ""))
if args[:4] == ["monitor", "log-analytics", "workspace", "delete"]:
    name = opt("--workspace-name")
    if not state.get("law_delete_ineffective") and name in state.get(
        "law_workspaces", []
    ):
        state["law_workspaces"].remove(name)
    done()

# --- Container Apps environment ---------------------------------------------
if args[:3] == ["containerapp", "env", "list"]:
    m = re.search(r"name=='([^']*)'", query_value())
    name = m.group(1) if m else None
    # A deleted environment lingers in the list (provisioningState
    # ScheduledForDelete) for some seconds before it disappears. With
    # env_delete_lingering_reads set, this fake keeps reporting it for that
    # many further list calls, then lets it go -- which is what real Azure
    # did on 2026-08-17.
    lingering = state.get("envs_lingering", {})
    if name and name in lingering:
        left = lingering[name] - 1
        if left <= 0:
            del lingering[name]
            if name in state["envs"]:
                state["envs"].remove(name)
        else:
            lingering[name] = left
        state["envs_lingering"] = lingering
        done("1" if name in state["envs"] else "0")
    done("1" if name and name in state["envs"] else "0")
if args[:3] == ["containerapp", "env", "create"]:
    state["envs"].append(opt("--name"))
    done("{}")
if args[:3] == ["containerapp", "env", "show"]:
    if query_value() == "id":
        done(state.get("env_id", ""))
    done(state["env_state"])
if args[:3] == ["containerapp", "env", "delete"]:
    name = opt("--name")
    if not state.get("env_delete_ineffective") and name in state.get("envs", []):
        linger = state.get("env_delete_lingering_reads", 0)
        if linger:
            state.setdefault("envs_lingering", {})[name] = linger
        else:
            state["envs"].remove(name)
    done()

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
if args[:2] == ["containerapp", "delete"]:
    name = opt("--name")
    if not state.get("app_delete_ineffective") and name in state.get("apps", []):
        state["apps"].remove(name)
    done()

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
            "extensions": ["containerapp"],
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
            "law_workspaces": [],
            "law_customer_id": "88888888-8888-8888-8888-888888888888",
            "law_shared_key": LAW_SHARED_KEY,
            "envs": [],
            "env_state": "Succeeded",
            "env_id": ENV_ID,
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
            "AZ_LAW_NAME": "law-faked24",
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

    def run_delete(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPTS_DIR / "delete-container-app.sh")],
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
        # The "app already exists" refusal is preflight's, ahead of every
        # mutation -- see test_existing_app_is_refused_in_preflight_before_any_mutation.
        "containerapp list",
        "identity create",
        "role assignment create",
        "search admin-key show",
        "keyvault secret set",
        "acr build",
        "monitor log-analytics workspace create",
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


def test_acr_build_passes_the_dockerfile_by_absolute_path(tmp_path: Path) -> None:
    # This one has to be an argv assertion rather than a behavioural one,
    # because the fake `az` never opens the file -- only the real CLI can fail
    # the way this guards against, and it fails before any build starts:
    #   ERROR: Unable to find 'docker/Dockerfile'.
    #
    # `az acr build --help` claims --file is relative to the source location.
    # The implementation resolves it against the caller's working directory
    # instead (azure-cli 2.89.0, acr/build.py). A relative value therefore
    # depends on where the operator stood when they typed the command, which
    # for this script is its own directory, not the repo root. Asserting the
    # path is absolute is asserting that it does not depend on the caller's cwd.
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    build_call = next(call for call in h.calls if call.startswith("acr build"))
    file_arg = build_call.split("--file ", 1)[1].split(" ")[0]
    assert file_arg.startswith("/"), build_call
    assert file_arg.endswith("/docker/Dockerfile"), build_call


def test_log_analytics_workspace_created_explicitly_and_wired_into_the_env(
    tmp_path: Path,
) -> None:
    # The whole point of creating this workspace ourselves rather than
    # letting `az containerapp env create` auto-provision one: the
    # environment must be told to use THIS workspace (by its customerId and
    # shared key), so delete-container-app.sh can later find and delete it
    # by the name printed here.
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    create_call = next(
        call
        for call in h.calls
        if call.startswith("monitor log-analytics workspace create")
    )
    assert "--workspace-name law-faked24" in create_call

    env_create_call = next(call for call in h.calls if call.startswith("containerapp env create"))
    assert "--logs-destination log-analytics" in env_create_call
    assert f"--logs-workspace-id {h.state['law_customer_id']}" in env_create_call
    assert f"--logs-workspace-key {LAW_SHARED_KEY}" in env_create_call


def test_law_shared_key_reaches_env_create_but_never_the_terminal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert LAW_SHARED_KEY not in result.stdout
    assert LAW_SHARED_KEY not in result.stderr

    # Again under `set -x`, which prints every assignment WITH its
    # substituted value -- the same leak vector the Search admin key test
    # below exercises. A fresh harness: the first run already created the
    # app, and this script only creates from scratch.
    traced_dir = tmp_path / "traced"
    traced_dir.mkdir()
    traced_h = Harness(traced_dir)
    traced = traced_h.run(SHELLOPTS="xtrace")
    assert traced.returncode == 0, traced.stderr
    assert LAW_SHARED_KEY not in traced.stdout
    assert LAW_SHARED_KEY not in traced.stderr
    # Tracing really was on before and after the suspended block, so the
    # clean stderr above is suppression working, not xtrace never starting.
    assert "+ az account set" in traced.stderr
    assert "+ az acr build" in traced.stderr
    # And the read that fetches the key is itself dark.
    assert "az monitor log-analytics workspace get-shared-keys" not in traced.stderr


def test_environment_binding_is_in_the_yaml_not_only_the_flag(tmp_path: Path) -> None:
    """`az containerapp create --help` states that with ``--yaml`` "all other
    parameters will be ignored", so ``--environment`` on that call cannot be
    what binds the app to its environment. The YAML has to say so itself.

    Getting this wrong fails at stage 8 -- i.e. after the identity, its three
    role assignments, the Key Vault secret, the image, the Log Analytics
    workspace and the environment have all been created and are all billing.
    """
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    yaml_text = h.state["app_yaml"]
    assert isinstance(yaml_text, str)
    assert f"environmentId: {ENV_ID}" in yaml_text

    # The id is read back from Azure, not assembled from a naming convention,
    # and fails closed like every other read in this script.
    assert any(
        call.startswith("containerapp env show") and "--query id" in call for call in h.calls
    )


def test_empty_environment_id_read_aborts_before_the_app_is_created(tmp_path: Path) -> None:
    h = Harness(tmp_path, env_id="")
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not h.has("containerapp create")


def test_terminal_environment_state_stops_polling_immediately(tmp_path: Path) -> None:
    """A Failed or Canceled environment is a verdict Azure has already
    reached; polling it for the rest of the budget spends ten minutes of a
    paid live session restating it. Stage 9 breaks out on the app's terminal
    states -- stage 7 must do the same for the environment's.
    """
    h = Harness(tmp_path, env_state="Failed")
    result = h.run()
    assert result.returncode != 0
    assert "Failed" in result.stderr

    provisioning_polls = [
        call
        for call in h.calls
        if call.startswith("containerapp env show") and "provisioning" in call
    ]
    assert len(provisioning_polls) == 1  # not ACA_POLL_ATTEMPTS (2)
    assert not h.has("containerapp create")


# ---------------------------------------------------------------------------
# 1b. Preflight is where a doomed run has to die: everything after it bills.
# ---------------------------------------------------------------------------


def test_existing_app_is_refused_in_preflight_before_any_mutation(tmp_path: Path) -> None:
    """The refusal used to live in stage 8, which was too late to be safe.

    Stage 6 generates a FRESH randomly-named Log Analytics workspace on every
    run. Refusing at stage 8 meant a rerun against an existing deployment
    created workspace B, left the reused environment still wired to workspace
    A, aborted, and then printed a teardown command naming B -- so A survived,
    billing for ingestion and retention, under a random name the operator no
    longer had. Checked in stage 1, the entire run is a no-op (and skips the
    `az acr build` it was never going to use).
    """
    h = Harness(tmp_path, apps=["aca-faked24"])
    result = h.run()
    assert result.returncode != 0
    assert "already exists" in result.stderr

    # Nothing was created. Not the identity, not its roles, not the secret,
    # not the image, and above all not a second Log Analytics workspace.
    for mutation in (
        "identity create",
        "role assignment create",
        "keyvault secret set",
        "acr build",
        "monitor log-analytics workspace create",
        "containerapp env create",
    ):
        assert not h.has(mutation), mutation
    # Nor was anything even read that only a mutating stage needs.
    assert not h.has("search admin-key show")
    # The refusal really is in preflight, not merely early: stage 2 never
    # started, so not even its existence read ran.
    assert not h.has("identity list")


def test_non_guid_entra_audience_fails_before_any_azure_call(tmp_path: Path) -> None:
    """`api://<guid>` is exactly what the portal displays as the Application
    ID URI, and core/config.py parses ENTRA_AUDIENCE with ``UUID()``. Pasting
    the URI makes ``Settings()`` raise at import time, so the container exits
    before binding a port -- and without this check the deploy learns about it
    at stage 11, after all eleven stages of mutation.
    """
    h = Harness(tmp_path)
    result = h.run(ENTRA_AUDIENCE="api://66666666-6666-6666-6666-666666666666")
    assert result.returncode != 0
    assert "ENTRA_AUDIENCE" in result.stderr
    assert "api://<guid>" in result.stderr
    assert not h.has("account set")


def test_non_guid_entra_tenant_id_fails_before_any_azure_call(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run(ENTRA_TENANT_ID="contoso.onmicrosoft.com")
    assert result.returncode != 0
    assert "ENTRA_TENANT_ID" in result.stderr
    assert not h.has("account set")


def test_missing_containerapp_extension_warns_but_does_not_abort(tmp_path: Path) -> None:
    """A check, deliberately not a gate.

    `az containerapp` lives in an extension that recent CLI versions install
    on first use, so its absence is not proof this deploy will fail -- a hard
    abort here would kill runs that dynamic install handles fine. What the
    warning buys is that the answer arrives in stage 1 with the fix in it,
    instead of as an opaque stage 7 error after the identity, its roles and
    the secret already exist.
    """
    h = Harness(tmp_path, extensions=[])
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert "containerapp" in result.stderr
    assert "az extension add --name containerapp" in result.stderr
    # And it really did check, rather than the message being unconditional:
    # with the extension present the same run says nothing about it.
    present_dir = tmp_path / "present"
    present_dir.mkdir()
    present = Harness(present_dir).run()
    assert present.returncode == 0, present.stderr
    assert "az extension add" not in present.stderr


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
    # ENTRA_CLIENT_SECRET IS read by name (the `${ENTRA_CLIENT_SECRET:?...}`
    # guard near the top of the script) -- tracing is suspended around that
    # one line, the same XTRACE_RESTORE pattern as the Search admin key
    # block below, because `${VAR:?msg}` traces as `+ : <value>` whenever the
    # variable is set: the message is dead code on that path, so nothing
    # about wording could make an untraced guard safe on its own.
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
    # The ENTRA_CLIENT_SECRET guard itself must not appear as `+ : <secret>`
    # either -- proving THIS suspension window, not just the Search key's.
    assert "+ : not-a-real-secret" not in traced.stderr


def test_missing_entra_client_secret_fails_before_any_azure_call(tmp_path: Path) -> None:
    """A regression test for a bug worse than a trace leak: an apostrophe
    inside `${ENTRA_CLIENT_SECRET:?message}` previously corrupted bash's
    parsing of this guard together with the one above it, so with both
    guards present the secret guard silently never ran at all -- a missing
    `ENTRA_CLIENT_SECRET` sailed straight through to the first `az` call
    instead of failing here. Confirmed by hand while diagnosing the trace
    finding above; this test pins the fix (both guards reworded with no
    apostrophe).
    """
    h = Harness(tmp_path)
    env = dict(h.env)
    del env["ENTRA_CLIENT_SECRET"]
    result = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "deploy-container-app.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "ENTRA_CLIENT_SECRET" in result.stderr
    # Failed at the top of the script, before touching Azure at all.
    assert not h.has("account set")


def test_missing_entra_client_app_id_fails_before_any_azure_call(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    env = dict(h.env)
    del env["ENTRA_CLIENT_APP_ID"]
    result = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "deploy-container-app.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "ENTRA_CLIENT_APP_ID" in result.stderr
    assert not h.has("account set")


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


def test_ingress_states_allow_insecure_explicitly(tmp_path: Path) -> None:
    # Omitting an optional field from this YAML does not mean the field is not
    # sent. The containerapp extension round-trips the document through its SDK
    # model, so everything unstated goes out as an explicit JSON null -- and the
    # API version it targets rejects null for this non-nullable boolean:
    #   The JSON value could not be converted to System.Boolean.
    #   Path: $ | LineNumber: 0 | BytePositionInLine: 4
    # That reached the live session as a 400 with no field name in it, after
    # the identity, its three roles, the secret, the image, the workspace and
    # the environment had all been created.
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    yaml_text = h.state["app_yaml"]
    assert isinstance(yaml_text, str)
    assert "allowInsecure: false" in yaml_text


def test_the_app_yaml_heredoc_contains_no_command_substitution(tmp_path: Path) -> None:
    # A source-level test, because the failure it guards against is invisible in
    # the generated YAML: the heredoc delimiter is unquoted on purpose, so the
    # ${...} values expand -- and so a backtick pair anywhere in the body is a
    # command substitution. A prose backtick around a flag name in a YAML
    # comment ran that flag as a command and printed
    # "line 632: --yaml: command not found" mid-deploy, while still producing a
    # YAML file that parsed cleanly, which is what made it hard to see.
    #
    # $(...) is included for the same reason, and neither has any legitimate
    # use in this block: every value it needs is already in a shell variable.
    #
    # APP_YAML is assembled from several heredocs in sequence (one per
    # AZ_SEARCH_MODE=fake-skippable section), all sharing the delimiter
    # "YAML" -- so every <<YAML/YAML pair in the script is scanned, not just
    # the first, or the sections added for fake mode would go unchecked.
    script = (SCRIPTS_DIR / "deploy-container-app.sh").read_text().splitlines()
    starts = [i for i, line in enumerate(script) if line.endswith("<<YAML")]
    assert starts, "expected at least one <<YAML heredoc"
    offenders = []
    for start in starts:
        end = next(i for i, line in enumerate(script) if i > start and line == "YAML")
        body = script[start + 1 : end]
        offenders.extend(
            f"{start + 2 + n}: {line}"
            for n, line in enumerate(body)
            if "`" in line or "$(" in line
        )
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# 7b. AZ_SEARCH_MODE=fake drops five Search/Key Vault couplings; AZ_SEARCH_MODE
# =real (the default) must not change so much as a byte of what this script
# already produced. The five pieces below are transcribed verbatim from the
# heredoc as it existed BEFORE AZ_SEARCH_MODE was introduced (commit 1fad6f0,
# infra/scripts/deploy-container-app.sh lines 632-726) -- not re-derived from
# the current, split-into-several-heredocs script -- so a real-mode test built
# from them proves byte-identity against the pre-switch contract, not just
# internal self-consistency.
# ---------------------------------------------------------------------------

YAML_PART_A = """identity:
  type: UserAssigned
  userAssignedIdentities:
    "${MI_ID}": {}
properties:
  # The binding to the environment, stated in the file rather than left to
  # the --environment flag on the create call below, which is documented to
  # ignore "all other parameters".
  #
  # No backticks anywhere in this heredoc. The delimiter is deliberately
  # unquoted so the variables above expand -- which also means backticks in
  # here are command substitutions, not punctuation. A prose backtick pair
  # around a flag name in this very comment used to run that flag as a
  # command: "line 632: --yaml: command not found".
  environmentId: ${ENV_ID}
  configuration:
    activeRevisionsMode: single
    ingress:
      external: true
      targetPort: 8000
      transport: auto
      # Stated explicitly because omitting it does not mean "do not send it".
      # The containerapp extension deserializes this YAML into its SDK model
      # and serializes the WHOLE model back out, so every field left out here
      # is transmitted as an explicit JSON null -- 84 of them in this app's
      # PUT body. The API version the extension targets (2025-10-02-preview)
      # rejects null for this non-nullable boolean, and says so from the
      # server's own parse context rather than ours:
      #   The JSON value could not be converted to System.Boolean.
      #   Path: $ | LineNumber: 0 | BytePositionInLine: 4
      # Position 4 is the end of the four characters of "null"; Path $ is the
      # root of that value, not of the document, which is why the message
      # names no field. allowInsecure is the only boolean among those nulls.
      allowInsecure: false
    registries:
      - server: ${AZ_ACR_NAME}.azurecr.io
        identity: ${MI_ID}
"""

YAML_PART_B_SECRETS = """    secrets:
      - name: search-admin-key
        keyVaultUrl: ${KV_SECRET_URI}
        identity: ${MI_ID}
"""

YAML_PART_C1 = """  template:
    terminationGracePeriodSeconds: 30
    containers:
      - image: ${AZ_ACR_NAME}.azurecr.io/azgenai-lab:${IMAGE_TAG}
        name: azgenai-lab
        env:
          - name: AZURE_OPENAI_AUTH
            value: "entra"
          - name: AZURE_CLIENT_ID
            value: "${MI_CLIENT_ID}"
          - name: AUTH_MODE
            value: "entra"
          - name: ENTRA_TENANT_ID
            value: "${ENTRA_TENANT_ID}"
          - name: ENTRA_AUDIENCE
            value: "${ENTRA_AUDIENCE}"
          - name: ENTRA_REQUIRED_SCOPE
            value: "${ENTRA_REQUIRED_SCOPE}"
          - name: ENTRA_REQUIRED_APP_ROLE
            value: "${ENTRA_REQUIRED_APP_ROLE}"
          - name: USE_FAKE_LLM
            value: "false"
          - name: USE_FAKE_SEARCH
            value: "false"
          - name: USE_FAKE_EMBEDDINGS
            value: "false"
          - name: SAMPLE_DOCS_DIR
            value: "/app/data/sample-docs"
          - name: AZURE_OPENAI_ENDPOINT
            value: "${AZURE_OPENAI_ENDPOINT}"
          - name: AZURE_OPENAI_DEPLOYMENT_NAME
            value: "${AZURE_OPENAI_DEPLOYMENT_NAME}"
          - name: AZURE_OPENAI_EMBEDDING_DEPLOYMENT
            value: "${AZURE_OPENAI_EMBEDDING_DEPLOYMENT}"
"""

YAML_PART_D_SEARCH_ENV = """          - name: AZURE_SEARCH_ENDPOINT
            value: "${AZURE_SEARCH_ENDPOINT}"
          - name: AZURE_SEARCH_ADMIN_KEY
            secretRef: search-admin-key
"""

YAML_PART_E = """        probes:
          - type: Startup
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 2
            periodSeconds: 3
          - type: Liveness
            httpGet: { path: /health, port: 8000 }
            periodSeconds: 10
          - type: Readiness
            httpGet: { path: /health, port: 8000 }
            periodSeconds: 10
    scale:
      minReplicas: 1
      maxReplicas: 1
"""


def render_yaml(template: str, mapping: dict[str, str]) -> str:
    result = template
    for key, value in mapping.items():
        result = result.replace(f"${{{key}}}", value)
    return result


def with_use_fake_search(part_c1: str, value: str) -> str:
    old = '          - name: USE_FAKE_SEARCH\n            value: "false"\n'
    new = f'          - name: USE_FAKE_SEARCH\n            value: "{value}"\n'
    assert old in part_c1
    return part_c1.replace(old, new, 1)


def yaml_var_map(h: "Harness") -> dict[str, str]:
    # Pulled from the harness's own env/state rather than retyped, so this map
    # cannot drift from what the fake `az` calls actually returned. The two
    # ENTRA_REQUIRED_* values are the script's own defaults (not set by the
    # harness env), documented in its "Optional env vars" header.
    return {
        "MI_ID": MI_RESOURCE_ID,
        "ENV_ID": ENV_ID,
        "AZ_ACR_NAME": h.env["AZ_ACR_NAME"],
        "IMAGE_TAG": "day-24",
        "MI_CLIENT_ID": MI_CLIENT_ID,
        "ENTRA_TENANT_ID": h.env["ENTRA_TENANT_ID"],
        "ENTRA_AUDIENCE": h.env["ENTRA_AUDIENCE"],
        "ENTRA_REQUIRED_SCOPE": "access_as_user",
        "ENTRA_REQUIRED_APP_ROLE": "Api.Access",
        "AZURE_OPENAI_ENDPOINT": h.state["aoai_reads"]["properties.endpoint"],  # type: ignore[index]
        "AZURE_OPENAI_DEPLOYMENT_NAME": "chat-mini",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "embed-small",
        "KV_SECRET_URI": f"https://{h.env['AZ_KEYVAULT_NAME']}.vault.azure.net/secrets/azure-search-admin-key",
        "AZURE_SEARCH_ENDPOINT": f"https://{h.env['AZ_SEARCH_NAME']}.search.windows.net",
    }


def run_with_env(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPTS_DIR / "deploy-container-app.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_real_mode_yaml_is_byte_identical_to_the_pre_switch_contract(
    tmp_path: Path,
) -> None:
    """AZ_SEARCH_MODE unset (the default) and AZ_SEARCH_MODE=real must both
    produce exactly what this script produced before AZ_SEARCH_MODE existed --
    the whole safety property this task promises, since real mode is shared
    with earlier days' runbooks.
    """
    implicit_dir = tmp_path / "implicit"
    implicit_dir.mkdir()
    h_implicit = Harness(implicit_dir)
    result_implicit = h_implicit.run()
    assert result_implicit.returncode == 0, result_implicit.stderr

    expected = render_yaml(
        YAML_PART_A + YAML_PART_B_SECRETS + YAML_PART_C1 + YAML_PART_D_SEARCH_ENV + YAML_PART_E,
        yaml_var_map(h_implicit),
    )
    assert h_implicit.state["app_yaml"] == expected

    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    h_explicit = Harness(explicit_dir)
    result_explicit = h_explicit.run(AZ_SEARCH_MODE="real")
    assert result_explicit.returncode == 0, result_explicit.stderr
    assert h_explicit.state["app_yaml"] == expected


def test_fake_mode_drops_all_five_search_keyvault_couplings(tmp_path: Path) -> None:
    """The five couplings AZ_SEARCH_MODE=fake exists to drop, checked
    together: (1) AZ_SEARCH_NAME not required, (2) AZ_KEYVAULT_NAME not
    required, (3) no Search-key-into-Key-Vault stage, (4) no secrets: block /
    AZURE_SEARCH_ENDPOINT / secretRef in the app YAML and USE_FAKE_SEARCH
    flips to true, (5) no Key Vault role assignment.
    """
    h = Harness(tmp_path)
    env = dict(h.env)
    del env["AZ_KEYVAULT_NAME"]
    del env["AZ_SEARCH_NAME"]
    env["AZ_SEARCH_MODE"] = "fake"
    result = run_with_env(env)
    assert result.returncode == 0, result.stderr  # (1) and (2): neither required

    # (3): the whole stage is skipped, not silently -- no Search admin key
    # read, no secret written.
    assert "skipped (AZ_SEARCH_MODE=fake)" in result.stdout
    assert not h.has("search admin-key show")
    assert not h.has("keyvault secret set")

    # (5): the identity is only ever granted two roles, and Key Vault is never
    # even looked up.
    assert not h.has("keyvault show")
    assert h.count("role assignment create") == 2
    assert sorted(h.state["roles"]) == sorted(  # type: ignore[arg-type]
        ["AcrPull", "Cognitive Services OpenAI User"]
    )

    # (4): byte-identity, the same discipline as the real-mode test above --
    # everything Search-shaped drops out and nothing else moves.
    expected = render_yaml(
        YAML_PART_A + with_use_fake_search(YAML_PART_C1, "true") + YAML_PART_E,
        yaml_var_map(h),
    )
    assert h.state["app_yaml"] == expected
    yaml_text = h.state["app_yaml"]
    assert isinstance(yaml_text, str)
    assert "secrets:" not in yaml_text
    assert "keyVaultUrl" not in yaml_text
    assert "AZURE_SEARCH_ENDPOINT" not in yaml_text
    assert "AZURE_SEARCH_ADMIN_KEY" not in yaml_text
    assert "secretRef" not in yaml_text


def test_fake_mode_only_touches_the_five_search_keyvault_couplings(tmp_path: Path) -> None:
    """Everything unrelated to Search/Key Vault -- identity, ACR pull, ingress,
    Log Analytics, the AOAI configuration -- still has to be there in fake
    mode. Belt-and-braces alongside the byte-identity check above.
    """
    h = Harness(tmp_path)
    env = dict(h.env)
    del env["AZ_KEYVAULT_NAME"]
    del env["AZ_SEARCH_NAME"]
    env["AZ_SEARCH_MODE"] = "fake"
    result = run_with_env(env)
    assert result.returncode == 0, result.stderr

    yaml_text = h.state["app_yaml"]
    assert isinstance(yaml_text, str)
    assert "identity:" in yaml_text
    assert "type: UserAssigned" in yaml_text
    assert f'"{MI_RESOURCE_ID}": {{}}' in yaml_text
    assert "terminationGracePeriodSeconds: 30" in yaml_text
    for probe in ("type: Startup", "type: Liveness", "type: Readiness"):
        assert probe in yaml_text
    for name in (
        "AZURE_OPENAI_AUTH",
        "AZURE_CLIENT_ID",
        "AUTH_MODE",
        "ENTRA_TENANT_ID",
        "ENTRA_AUDIENCE",
        "ENTRA_REQUIRED_SCOPE",
        "ENTRA_REQUIRED_APP_ROLE",
        "USE_FAKE_LLM",
        "USE_FAKE_EMBEDDINGS",
        "SAMPLE_DOCS_DIR",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    ):
        assert f"- name: {name}\n" in yaml_text, name
    assert h.has("monitor log-analytics workspace create")
    assert h.has("containerapp env create")


def test_invalid_search_mode_fails_before_any_azure_call(tmp_path: Path) -> None:
    """Not real, not fake: a hard error, never a silent fall-through to
    either mode.
    """
    h = Harness(tmp_path)
    result = h.run(AZ_SEARCH_MODE="mock")
    assert result.returncode != 0
    assert "AZ_SEARCH_MODE" in result.stderr
    assert not h.has("account set")


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


def test_both_teardown_printers_name_every_scope_knob(tmp_path: Path) -> None:
    """A printed teardown command that omits a scope knob is a broken teardown.

    delete-container-app.sh SKIPS the role-assignment delete for any scope
    whose name knob is unset (warned, not silently), and then its step 6
    read-back -- by assignee alone -- finds the leftover and aborts BEFORE
    deleting the identity. So an operator pasting an incomplete command gets
    a teardown that stops halfway and leaves the orphaned assignments the
    ordering exists to prevent. Both printers are pinned: the success summary
    and the EXIT-trap recovery hint.
    """
    knobs = (
        "AZ_SUBSCRIPTION_ID=",
        "AZ_RESOURCE_GROUP=",
        "AZ_ACA_APP_NAME=",
        "AZ_ACA_ENV_NAME=",
        "AZ_MI_NAME=",
        "AZ_KEYVAULT_NAME=",
        "AZ_ACR_NAME=",
        "AZ_OPENAI_NAME=",
        "AZ_LAW_NAME=",
    )

    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    fail_dir = tmp_path / "fail"
    fail_dir.mkdir()

    success = Harness(ok_dir).run()
    assert success.returncode == 0, success.stderr
    summary = success.stdout.split("Tear it down in the same session:")[-1]
    for knob in knobs:
        assert knob in summary, f"success summary omits {knob}"

    # A failure late enough to have mutated something: the EXIT trap's hint is
    # the only teardown command that run will ever print.
    failed = Harness(fail_dir, role_create_fails=True).run()
    assert failed.returncode != 0
    hint = failed.stderr.split("Tear down whatever exists with:")[-1]
    for knob in knobs:
        assert knob in hint, f"failure recovery hint omits {knob}"


def test_script_exists_and_is_executable() -> None:
    path = SCRIPTS_DIR / "deploy-container-app.sh"
    assert path.is_file()
    assert os.access(path, os.X_OK)


# ---------------------------------------------------------------------------
# delete-container-app.sh -- the ordering is the contract here too, in
# reverse: role assignments are deleted (and proven gone) BEFORE the
# identity that named them, because deleting the identity first destroys the
# only handle Azure gives back for finding those assignments again. A
# deployed-and-granted starting state is the default for every test below;
# individual tests narrow it to exercise one branch.
# ---------------------------------------------------------------------------


def deployed_harness(tmp_path: Path, **overrides: object) -> Harness:
    """A Harness whose fake state already looks like a completed deploy:
    app, environment, Log Analytics workspace and identity all present, and
    the identity holding all three role assignments deploy-container-app.sh
    would have granted it.
    """
    state = {
        "apps": ["aca-faked24"],
        "envs": ["acaenv-faked24"],
        "law_workspaces": ["law-faked24"],
        "identities": ["mi-faked24"],
        "assigned_scopes": [ACR_ID, KV_ID, AOAI_ID],
    }
    state.update(overrides)
    return Harness(tmp_path, **state)


def test_delete_stage_ordering_is_pinned_end_to_end(tmp_path: Path) -> None:
    h = deployed_harness(tmp_path)
    result = h.run_delete()
    assert result.returncode == 0, result.stderr

    order = [
        "containerapp delete",
        "containerapp env delete",
        "monitor log-analytics workspace delete",
        "identity show",
        "role assignment delete",
        "role assignment list",
        "identity delete",
    ]
    indices = [h.first_index(prefix) for prefix in order]
    assert indices == sorted(indices), list(zip(order, indices, strict=True))

    # All three grants undone, and the read-back that gates the identity
    # delete ran exactly once, after all three deletes.
    assert h.count("role assignment delete") == 3
    assert h.count("role assignment list") == 1
    assert h.state["assigned_scopes"] == []
    assert h.state["law_workspaces"] == []


def test_delete_readback_queries_every_scope_not_just_the_subscription(
    tmp_path: Path,
) -> None:
    """Step 6's read-back must pass ``--all``, and only an argv assertion can
    prove it.

    `az role assignment list` documents its own default as subscription scope
    only: "By default, only assignments scoped to subscription will be
    displayed. To view assignments scoped by resource or group, use --all."
    All three assignments deploy-container-app.sh creates are at RESOURCE
    scope (the ACR, the vault, the Azure OpenAI account), so without --all
    this query returns 0 unconditionally and step 6 -- the guarantee the whole
    seven-step ordering exists to provide -- is vacuous. The concrete failure:
    with AZ_ACR_NAME unset (easy; it is a per-run random name), step 5 warns
    and skips the ACR delete, a scope-blind step 6 reports zero remain, step 7
    deletes the identity, and the AcrPull assignment is orphaned forever with
    no principal left to find it by.

    No behavioural test can catch this. The fake `az` answers with a scripted
    count regardless of which scopes it was asked about, exactly as a real
    subscription-scoped query would answer 0 regardless of what is assigned at
    resource scope -- which is precisely why the missing flag survived four
    reviews of tests that all pass either way. So this asserts on the argv.
    """
    h = deployed_harness(tmp_path)
    result = h.run_delete()
    assert result.returncode == 0, result.stderr

    readback = next(call for call in h.calls if call.startswith("role assignment list"))
    assert " --all" in f" {readback}", readback
    # And it stays scope-blind in the other direction too: narrowing it by
    # --scope or --role would re-hide exactly the leftovers it exists to find.
    assert "--scope" not in readback
    assert "--role" not in readback


def test_delete_app_readback_failure_aborts_before_env_delete(tmp_path: Path) -> None:
    # The delete call itself reports success, but the resource is still
    # listed afterward -- the same "don't trust the exit code alone" case
    # delete-acr.sh guards against. This must stop the teardown right there,
    # not just log a warning and carry on to the environment.
    h = deployed_harness(tmp_path, app_delete_ineffective=True)
    result = h.run_delete()
    assert result.returncode != 0
    assert "still listed after delete" in result.stderr
    assert not h.has("containerapp env delete")
    assert not h.has("monitor log-analytics workspace delete")
    assert not h.has("identity delete")


def test_delete_env_readback_failure_aborts_before_workspace_delete(tmp_path: Path) -> None:
    # Poll knobs shrunk to one immediate attempt: this environment never goes
    # away, so the point of the test is the abort, not the waiting.
    h = deployed_harness(tmp_path, env_delete_ineffective=True)
    result = h.run_delete(ENV_DELETE_POLL_ATTEMPTS="1", ENV_DELETE_POLL_INTERVAL="0")
    assert result.returncode != 0
    assert "still listed after delete" in result.stderr
    assert h.has("containerapp delete")
    assert not h.has("monitor log-analytics workspace delete")
    assert not h.has("identity delete")


def test_delete_env_tolerates_an_environment_that_lingers_before_disappearing(
    tmp_path: Path,
) -> None:
    # `az containerapp env delete` returns while the environment is still
    # listed, in provisioningState ScheduledForDelete. Reading back once turns
    # a working delete into a failed teardown -- on 2026-08-17 it stopped with
    # the identity and its three role assignments still live, and the
    # environment was gone 26 seconds later.
    h = deployed_harness(tmp_path, env_delete_lingering_reads=3)
    result = h.run_delete(ENV_DELETE_POLL_ATTEMPTS="10", ENV_DELETE_POLL_INTERVAL="0")
    assert result.returncode == 0, result.stderr
    assert "still listed after delete" not in result.stderr

    # It waited rather than gave up, and then went on to finish the teardown --
    # which is the whole point: the steps after this one are the ones that
    # prevent orphaned role assignments.
    assert h.has("monitor log-analytics workspace delete")
    assert h.count("role assignment delete") == 3
    assert h.has("identity delete")


def test_delete_workspace_readback_failure_aborts_before_identity_steps(
    tmp_path: Path,
) -> None:
    h = deployed_harness(tmp_path, law_delete_ineffective=True)
    result = h.run_delete()
    assert result.returncode != 0
    assert "still listed after delete" in result.stderr
    assert h.has("containerapp delete")
    assert h.has("containerapp env delete")
    # Never even reaches reading the identity's principal id.
    assert not h.has("identity show")
    assert not h.has("role assignment")
    assert not h.has("identity delete")


def test_delete_identity_readback_failure_is_reported(tmp_path: Path) -> None:
    # The last step: nothing downstream to protect, but a delete that didn't
    # actually take must still be reported as a failure, not a silent success.
    h = deployed_harness(tmp_path, identity_delete_ineffective=True)
    result = h.run_delete()
    assert result.returncode != 0
    assert "still listed after delete" in result.stderr
    assert h.count("role assignment delete") == 3


def test_delete_skips_workspace_with_missing_name_knob_and_warns(tmp_path: Path) -> None:
    h = deployed_harness(tmp_path)
    result = h.run_delete(AZ_LAW_NAME="")
    assert result.returncode == 0, result.stderr
    assert "AZ_LAW_NAME not set" in result.stderr
    assert not h.has("monitor log-analytics workspace")
    # The rest of the teardown is unaffected by the skip.
    assert h.has("identity delete")
    assert h.state["law_workspaces"] == ["law-faked24"]  # untouched, not deleted


def test_delete_workspace_absent_is_a_clean_noop(tmp_path: Path) -> None:
    h = deployed_harness(tmp_path, law_workspaces=[])
    result = h.run_delete()
    assert result.returncode == 0, result.stderr
    assert not h.has("monitor log-analytics workspace delete")
    assert h.has("identity delete")


def test_delete_role_assignment_failure_prevents_identity_delete(tmp_path: Path) -> None:
    h = deployed_harness(tmp_path, role_delete_fails=True)
    result = h.run_delete()
    assert result.returncode != 0

    # The first delete call fails, `set -e` stops the script right there:
    # no second or third scope attempted, no read-back, no identity delete.
    # A deploy that reports the assignments as gone when even one delete
    # call errored would be exactly the false confidence this ordering
    # exists to prevent.
    assert h.count("role assignment delete") == 1
    assert not h.has("role assignment list")
    assert not h.has("identity delete")


def test_delete_readback_leftover_aborts_before_identity_delete(tmp_path: Path) -> None:
    # All three known scopes delete cleanly, but the read-back reports one
    # assignment still held anyway -- e.g. a scope this script does not know
    # about. The read-back's verdict overrides three apparently-successful
    # deletes; the identity must not be deleted while it does.
    h = deployed_harness(tmp_path, leftover_override=1)
    result = h.run_delete()
    assert result.returncode != 0

    assert h.count("role assignment delete") == 3
    assert h.has("role assignment list")
    assert not h.has("identity delete")


def test_delete_absent_app_and_env_continues_to_identity_cleanup(tmp_path: Path) -> None:
    h = deployed_harness(tmp_path, apps=[], envs=[])
    result = h.run_delete()
    assert result.returncode == 0, result.stderr

    assert not h.has("containerapp delete")
    assert not h.has("containerapp env delete")
    # The rest of the teardown is unaffected by the app/env already being gone.
    assert h.count("role assignment delete") == 3
    assert h.has("identity delete")


def test_delete_absent_identity_is_a_clean_noop(tmp_path: Path) -> None:
    h = deployed_harness(tmp_path, identities=[], assigned_scopes=[])
    result = h.run_delete()
    assert result.returncode == 0, result.stderr

    # App and env cleanup still ran -- those are independent of the identity.
    assert h.has("containerapp delete")
    assert h.has("containerapp env delete")
    # But nothing identity-related was touched: no scope lookups, no role
    # calls, no identity delete.
    assert not h.has("role assignment")
    assert not h.has("acr show")
    assert not h.has("keyvault show")
    assert not h.has("cognitiveservices account show")
    assert not h.has("identity delete")


def test_delete_skips_scope_with_missing_name_knob_and_warns(tmp_path: Path) -> None:
    # ACR is genuinely not assigned in this fixture (mirrors a deploy that
    # never granted it, or one already cleaned up by hand) -- so skipping it
    # here is correct, not merely tolerated, and the teardown still finishes.
    h = deployed_harness(tmp_path, assigned_scopes=[KV_ID, AOAI_ID])
    result = h.run_delete(AZ_ACR_NAME="")
    assert result.returncode == 0, result.stderr

    assert "AZ_ACR_NAME not set" in result.stderr
    assert h.count("role assignment delete") == 2
    assert not h.has("acr show")
    assert h.has("identity delete")


def test_delete_skipped_scope_leftover_is_caught_by_final_readback(tmp_path: Path) -> None:
    # This time the ACR assignment DOES still exist when AZ_ACR_NAME is
    # unset -- the exact scenario the assignee-only (no --scope) read-back in
    # step 5 exists to catch. Skipping a scope must not silently let its
    # assignment survive the teardown.
    h = deployed_harness(tmp_path)
    result = h.run_delete(AZ_ACR_NAME="")
    assert result.returncode != 0

    assert "AZ_ACR_NAME not set" in result.stderr
    assert h.count("role assignment delete") == 2  # Key Vault + Azure OpenAI only
    assert not h.has("identity delete")


def test_delete_readback_query_failure_aborts_before_identity_delete(tmp_path: Path) -> None:
    # An empty read-back is a failed query, never "zero found" -- `length([])`
    # always prints a number when the call actually worked.
    h = deployed_harness(tmp_path, delete_readback_query_fails=True)
    result = h.run_delete()
    assert result.returncode != 0

    assert "empty output" in result.stderr
    assert h.count("role assignment delete") == 3
    assert not h.has("identity delete")


def test_delete_script_exists_and_is_executable() -> None:
    path = SCRIPTS_DIR / "delete-container-app.sh"
    assert path.is_file()
    assert os.access(path, os.X_OK)


# --- Day 27: Key Vault need is two independent questions --------------------
#
# The whole Key Vault coupling used to hang off AZ_SEARCH_MODE alone: the vault
# lookup, the "Key Vault Secrets User" assignment, the secrets: block. That
# made "fake Search plus telemetry" deploy with no vault at all -- and a
# tracing session is exactly the run that wants fake Search and a connection
# string. Four states, because three of them were previously unreachable.

_APPI_SECRET_URI = (
    "https://kv-fake.vault.azure.net/secrets/applicationinsights-connection-string/abc123"
)


def test_fake_search_with_telemetry_still_wires_key_vault(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run(AZ_SEARCH_MODE="fake", AZ_APPINSIGHTS_SECRET_URI=_APPI_SECRET_URI)
    assert result.returncode == 0, result.stderr

    yaml_text = h.state["app_yaml"]
    assert "secrets:" in yaml_text
    assert "applicationinsights-connection-string" in yaml_text
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" in yaml_text
    # No Search key anywhere: fake Search still has nothing to store.
    assert "search-admin-key" not in yaml_text
    # And the identity is still granted read access, or the reference above
    # resolves to nothing at runtime.
    assert any("Key Vault Secrets User" in call for call in h.calls)


def test_fake_search_without_telemetry_touches_no_key_vault(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run(AZ_SEARCH_MODE="fake")
    assert result.returncode == 0, result.stderr

    yaml_text = h.state["app_yaml"]
    assert "secrets:" not in yaml_text
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" not in yaml_text
    assert not any("Key Vault Secrets User" in call for call in h.calls)


def test_real_search_with_telemetry_wires_both_secrets(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run(AZ_APPINSIGHTS_SECRET_URI=_APPI_SECRET_URI)
    assert result.returncode == 0, result.stderr

    yaml_text = h.state["app_yaml"]
    # One secrets: block, two entries under it -- not two blocks, which is what
    # a naive second heredoc would have produced.
    assert yaml_text.count("secrets:") == 1
    assert "search-admin-key" in yaml_text
    assert "applicationinsights-connection-string" in yaml_text


def test_real_search_without_telemetry_is_unchanged(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    yaml_text = h.state["app_yaml"]
    assert "search-admin-key" in yaml_text
    assert "applicationinsights-connection-string" not in yaml_text
