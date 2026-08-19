"""Fake-CLI state regressions for infra/scripts/create-github-oidc.sh.

Runs the real bash script against fake ``az`` and ``gh`` executables that
model the Azure/GitHub states the script claims to handle. Both fakes append
to one shared call log (az calls unprefixed, gh calls prefixed ``"gh "`` --
the same disambiguation test_update_container_app_script_lifecycle.py uses
for its curl fake), so ordering assertions are exact rather than inferred.

D12 names four order contracts as the ones that matter for the `gh`-side
lifecycle (the other scripts already cover az-only patterns):

  1. an environment-protection read-back mismatch aborts before repository
     variables or DEPLOY_ENABLED are ever written
  2. DEPLOY_ENABLED=true is the LAST mutation the script makes
  3. any read-back that comes back empty aborts with nothing further attempted
  4. the two identities get distinct subjects, and their role-assignment
     scopes are the ACR and the app respectively -- never the resource group,
     never the subscription

Each gets its own test below, plus general coverage of step ordering,
fail-closed reads, and the record file.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "scripts" / "create-github-oidc.sh"

GITHUB_REPO = "kehao-playground/azure-genai-backend-lab"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "22222222-2222-2222-2222-222222222222"
ACR_ID = (
    "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg/"
    "providers/Microsoft.ContainerRegistry/registries/acrfaked25"
)
ACA_APP_ID = (
    "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg/"
    "providers/Microsoft.App/containerApps/aca-faked25"
)
BUILD_APP_GUID = "33333333-3333-3333-3333-333333333333"
BUILD_OBJECT_GUID = "44444444-4444-4444-4444-444444444444"
BUILD_SP_GUID = "55555555-5555-5555-5555-555555555555"
DEPLOY_APP_GUID = "66666666-6666-6666-6666-666666666666"
DEPLOY_OBJECT_GUID = "77777777-7777-7777-7777-777777777777"
DEPLOY_SP_GUID = "88888888-8888-8888-8888-888888888888"
REVIEWER_LOGIN = "kehao-playground"
REVIEWER_ID = "1000001"

FAKE_AZ = '''#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["GH_OIDC_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)

args = sys.argv[1:]
state.setdefault("calls", []).append(" ".join(args))

def save():
    with open(state_path, "w") as f:
        json.dump(state, f)

def opt(name):
    return args[args.index(name) + 1] if name in args else ""

def done(out="", code=0):
    save()
    if out != "":
        print(out)
    sys.exit(code)

def fail(msg="fake az: injected failure", code=1):
    save()
    print(msg, file=sys.stderr)
    sys.exit(code)

if args[:2] == ["account", "show"]:
    done(state.get("active_tenant_id", ""))

if args[:2] == ["account", "set"]:
    done()

if args[:2] == ["acr", "show"]:
    done(state.get("acr_id", ""))

if args[:2] == ["containerapp", "show"]:
    done(state.get("aca_app_id", ""))

if args[:3] == ["ad", "app", "create"]:
    calls = state.get("ad_app_create_calls", 0)
    state["ad_app_create_calls"] = calls + 1
    display_name = opt("--display-name")
    state.setdefault("ad_app_create_display_names", []).append(display_name)
    if calls == 0:
        done(f"{state.get('build_app_id', '')} {state.get('build_object_id', '')}")
    done(f"{state.get('deploy_app_id', '')} {state.get('deploy_object_id', '')}")

if args[:3] == ["ad", "sp", "create"]:
    remaining = state.get("sp_create_fail_first_n_calls", 0)
    if remaining > 0:
        state["sp_create_fail_first_n_calls"] = remaining - 1
        fail("fake az: sp not ready yet")
    app_id = opt("--id")
    if app_id == state.get("build_app_id"):
        done(state.get("build_sp_id", ""))
    if app_id == state.get("deploy_app_id"):
        done(state.get("deploy_sp_id", ""))
    done("")

if args[:4] == ["ad", "app", "federated-credential", "create"]:
    app_id = opt("--id")
    params = opt("--parameters")
    body_path = params[1:] if params.startswith("@") else params
    with open(body_path) as f:
        body = json.load(f)
    state.setdefault("fic_bodies", []).append({"app_id": app_id, **body})
    if app_id == state.get("build_app_id"):
        done(state.get("build_fic_id", ""))
    if app_id == state.get("deploy_app_id"):
        done(state.get("deploy_fic_id", ""))
    done("")

if args[:3] == ["role", "assignment", "create"]:
    role = opt("--role")
    if role in state.get("role_assignment_create_fails_for", []):
        fail(f"fake az: role assignment create failed for {role}")
    record = {
        "principal_id": opt("--assignee-object-id"),
        "principal_type": opt("--assignee-principal-type"),
        "role": role,
        "scope": opt("--scope"),
    }
    state.setdefault("role_assignments_created", []).append(record)
    done(f"assignment-{role.replace(' ', '-').lower()}")

if args[:3] == ["role", "assignment", "list"]:
    role = opt("--role")
    if role in state.get("role_assignment_listback_zero_for", []):
        done("0")
    if "--all" not in args:
        # Mirrors the exact Day 24 bug: without --all, resource-scoped
        # assignments never show up, no matter what was actually created.
        done("0")
    done("1")

print(f"fake az: unhandled command: {' '.join(args)}", file=sys.stderr)
done(code=2)
'''

FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["GH_OIDC_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)

args = sys.argv[1:]
state.setdefault("calls", []).append("gh " + " ".join(args))

def save():
    with open(state_path, "w") as f:
        json.dump(state, f)

def done(out="", code=0):
    save()
    if out != "":
        print(out)
    sys.exit(code)

def fail(msg="fake gh: injected failure", code=1):
    save()
    print(msg, file=sys.stderr)
    sys.exit(code)

if args[:2] == ["auth", "status"]:
    if state.get("gh_not_authenticated"):
        fail("fake gh: not logged in")
    done()

if args and args[0] == "api":
    rest = args[1:]
    method = "GET"
    input_file = None
    jq_filter = None
    fields = {}
    path = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--method":
            method = rest[i + 1]
            i += 2
        elif tok == "--input":
            input_file = rest[i + 1]
            i += 2
        elif tok == "--jq":
            jq_filter = rest[i + 1]
            i += 2
        elif tok in ("-f", "-F"):
            k, _, v = rest[i + 1].partition("=")
            fields[k] = v
            i += 2
        elif not tok.startswith("-"):
            if path is None:
                path = tok
            i += 1
        else:
            i += 1

    if path == "user":
        if jq_filter == ".login":
            done(state.get("current_user_login", "kehao-playground"))
        done("")

    if path is not None and path.startswith("users/"):
        if jq_filter == ".id":
            done(state.get("reviewer_user_id", ""))
        done("")

    env_path_prefix = f"repos/{state['github_repo']}/environments/{state['gh_environment_name']}"

    if method == "PUT" and path == env_path_prefix:
        with open(input_file) as f:
            body = json.load(f)
        state["environment_put_body"] = body
        state["environment_put_calls"] = state.get("environment_put_calls", 0) + 1
        done()

    if method == "POST" and path == f"{env_path_prefix}/deployment-branch-policies":
        state.setdefault("branch_policies_list", []).append(fields.get("name", ""))
        done()

    if method == "GET" and path == env_path_prefix:
        if jq_filter and "custom_branch_policies" in jq_filter:
            if "custom_branch_policies_override" in state:
                done(state["custom_branch_policies_override"])
            body = state.get("environment_put_body", {})
            val = body.get("deployment_branch_policy", {}).get("custom_branch_policies")
            done(str(val).lower() if val is not None else "")
        if jq_filter and "protected_branches" in jq_filter:
            if "protected_branches_override" in state:
                done(state["protected_branches_override"])
            body = state.get("environment_put_body", {})
            val = body.get("deployment_branch_policy", {}).get("protected_branches")
            done(str(val).lower() if val is not None else "")
        if jq_filter and "required_reviewers" in jq_filter:
            if "reviewer_ids_override" in state:
                done(state["reviewer_ids_override"])
            body = state.get("environment_put_body", {})
            ids = [str(r["id"]) for r in body.get("reviewers", [])]
            done(",".join(ids))
        done("")

    if method == "GET" and path == f"{env_path_prefix}/deployment-branch-policies":
        if "branch_policies_readback_override" in state:
            done(state["branch_policies_readback_override"])
        done(",".join(state.get("branch_policies_list", [])))

    variables_prefix = f"repos/{state['github_repo']}/actions/variables/"
    if method == "GET" and path and path.startswith(variables_prefix):
        name = path[len(variables_prefix):]
        overrides = state.get("variable_readback_overrides", {})
        if name in overrides:
            done(overrides[name])
        done(state.get("variables", {}).get(name, ""))

    print(f"fake gh: unhandled api call: {rest}", file=sys.stderr)
    done(code=2)

if args[:2] == ["variable", "set"]:
    name = args[2]
    i = 3
    body = ""
    while i < len(args):
        if args[i] == "--body":
            body = args[i + 1]
            i += 2
        else:
            i += 1
    state.setdefault("variables", {})[name] = body
    done()

print(f"fake gh: unhandled command: {' '.join(args)}", file=sys.stderr)
done(code=2)
'''


class Harness:
    def __init__(self, tmp_path: Path, **overrides: object) -> None:
        state: dict[str, object] = {
            "active_tenant_id": TENANT_ID,
            "acr_id": ACR_ID,
            "aca_app_id": ACA_APP_ID,
            "build_app_id": BUILD_APP_GUID,
            "build_object_id": BUILD_OBJECT_GUID,
            "build_sp_id": BUILD_SP_GUID,
            "deploy_app_id": DEPLOY_APP_GUID,
            "deploy_object_id": DEPLOY_OBJECT_GUID,
            "deploy_sp_id": DEPLOY_SP_GUID,
            "build_fic_id": "fic-build-1",
            "deploy_fic_id": "fic-deploy-1",
            "reviewer_user_id": REVIEWER_ID,
            "current_user_login": REVIEWER_LOGIN,
            "github_repo": GITHUB_REPO,
            "gh_environment_name": "production",
        }
        state.update(overrides)
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps(state))

        fake_dir = tmp_path / "bin"
        fake_dir.mkdir()
        for name, body in (("az", FAKE_AZ), ("gh", FAKE_GH)):
            path = fake_dir / name
            path.write_text(body)
            path.chmod(0o755)

        self.record_file = tmp_path / "record.env"

        self.env = {
            **os.environ,
            "PATH": f"{fake_dir}:{os.environ['PATH']}",
            "GH_OIDC_FAKE_STATE": str(self.state_path),
            "OIDC_RECORD_FILE": str(self.record_file),
            "GITHUB_REPO": GITHUB_REPO,
            "AZ_TENANT_ID": TENANT_ID,
            "AZ_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
            "AZ_RESOURCE_GROUP": "rg",
            "AZ_ACR_NAME": "acrfaked25",
            "AZ_ACA_APP_NAME": "aca-faked25",
        }

    def run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT)],
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

    def last_index(self, prefix: str) -> int:
        return max(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def count(self, prefix: str) -> int:
        return sum(1 for call in self.calls if call.startswith(prefix))

    def has(self, prefix: str) -> bool:
        return any(call.startswith(prefix) for call in self.calls)

    def record_lines(self) -> dict[str, str]:
        if not self.record_file.exists():
            return {}
        out: dict[str, str] = {}
        for line in self.record_file.read_text().splitlines():
            if not line or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k] = v
        return out


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_happy_path_creates_and_arms(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert "DEPLOY_ENABLED=true" in result.stdout
    rec = h.record_lines()
    assert rec["BUILD_APP_ID"] == BUILD_APP_GUID
    assert rec["BUILD_SP_ID"] == BUILD_SP_GUID
    assert rec["DEPLOY_APP_ID"] == DEPLOY_APP_GUID
    assert rec["DEPLOY_SP_ID"] == DEPLOY_SP_GUID
    assert rec["BUILD_FIC_ID"] == "fic-build-1"
    assert rec["DEPLOY_FIC_ID"] == "fic-deploy-1"
    assert rec["GITHUB_REPO"] == GITHUB_REPO
    assert rec["DEPLOY_ENABLED_SET"] == "true"
    assert h.state["variables"]["DEPLOY_ENABLED"] == "true"
    assert h.state["variables"]["AZURE_CLIENT_ID_BUILD"] == BUILD_APP_GUID
    assert h.state["variables"]["AZURE_CLIENT_ID_DEPLOY"] == DEPLOY_APP_GUID


def test_step_ordering_is_pinned(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    # Step 1 creates each identity's app registration immediately followed
    # by its own service principal (build, then deploy) -- not both app
    # registrations first, so "app create" and "sp create" interleave.
    fic_create = "ad app federated-credential create"
    role_create = "role assignment create"
    role_list = "role assignment list"
    env_put = "gh api --method PUT"
    env_path = f"repos/{GITHUB_REPO}/environments/production"
    branch_policy_post = f"gh api --method POST {env_path}/deployment-branch-policies"
    env_get = f"gh api {env_path} --jq"
    branch_policy_get = f"gh api {env_path}/deployment-branch-policies --jq"

    assert h.first_index("account set") < h.first_index("ad app create")
    assert h.first_index("ad app create") < h.first_index("ad sp create")
    assert h.last_index("ad sp create") < h.first_index(fic_create)
    assert h.last_index(fic_create) < h.first_index(role_create)
    assert h.last_index(role_create) < h.first_index(role_list)
    assert h.last_index(role_list) < h.first_index(env_put)
    assert h.first_index(env_put) < h.first_index(branch_policy_post)
    # Read-backs (plain "gh api repos/.../environments/production" with no
    # --method, i.e. GET) happen after both writes.
    assert h.last_index(branch_policy_post) < h.first_index(env_get)
    assert h.last_index(branch_policy_get) < h.first_index("gh variable set")


def test_two_identities_created_with_distinct_subjects_and_scopes(tmp_path: Path) -> None:
    """Order contract 4 (D12)."""
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    fic_bodies = h.state["fic_bodies"]
    subjects = {b["subject"] for b in fic_bodies}
    assert len(subjects) == 2, "build and deploy subjects must be distinct"
    assert f"repo:{GITHUB_REPO}:ref:refs/heads/main" in subjects
    assert f"repo:{GITHUB_REPO}:environment:production" in subjects
    for body in fic_bodies:
        assert body["issuer"] == "https://token.actions.githubusercontent.com"
        assert body["audiences"] == ["api://AzureADTokenExchange"]

    assignments = h.state["role_assignments_created"]
    by_role = {a["role"]: a for a in assignments}
    assert by_role["AcrPush"]["scope"] == ACR_ID
    assert by_role["Container Apps Contributor"]["scope"] == ACA_APP_ID
    # Neither scope is the bare resource group or subscription.
    for a in assignments:
        assert a["scope"] != f"/subscriptions/{SUBSCRIPTION_ID}"
        assert a["scope"] != f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg"
    assert by_role["AcrPush"]["principal_id"] == BUILD_SP_GUID
    assert by_role["Container Apps Contributor"]["principal_id"] == DEPLOY_SP_GUID


# ---------------------------------------------------------------------------
# Order contract 1: environment protection read-back mismatch aborts.
# ---------------------------------------------------------------------------


def test_environment_branch_policy_mismatch_aborts_before_variables(tmp_path: Path) -> None:
    # Simulates GitHub silently serving the pre-protection (auto-created,
    # unprotected) state back on read: the PUT reported success, but what
    # comes back on GET disagrees.
    h = Harness(tmp_path, custom_branch_policies_override="false")
    result = h.run()
    assert result.returncode != 0
    assert "custom_branch_policies=false" in result.stderr
    assert "not a gate" in result.stderr
    assert not h.has("gh variable set")


def test_environment_reviewer_mismatch_aborts_before_variables(tmp_path: Path) -> None:
    h = Harness(tmp_path, reviewer_ids_override="9999999")
    result = h.run()
    assert result.returncode != 0
    assert "required reviewers" in result.stderr
    assert "9999999" in result.stderr
    assert not h.has("gh variable set")


def test_branch_policy_list_mismatch_aborts_before_variables(tmp_path: Path) -> None:
    h = Harness(tmp_path, branch_policies_readback_override="some-other-branch")
    result = h.run()
    assert result.returncode != 0
    assert "deployment branch policies" in result.stderr
    assert not h.has("gh variable set")


# ---------------------------------------------------------------------------
# Order contract 2: DEPLOY_ENABLED=true is the LAST mutation.
# ---------------------------------------------------------------------------


def test_deploy_enabled_is_the_last_mutation(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    deploy_enabled_set_index = h.first_index("gh variable set DEPLOY_ENABLED")
    other_mutating_calls = [
        i
        for i, call in enumerate(h.calls)
        if i != deploy_enabled_set_index
        and (
            call.startswith("ad ")
            or call.startswith("role assignment create")
            or call.startswith("gh api --method")
            or (call.startswith("gh variable set") and "DEPLOY_ENABLED" not in call)
        )
    ]
    assert other_mutating_calls, "sanity: there should be earlier mutations to compare against"
    assert deploy_enabled_set_index > max(other_mutating_calls)


def test_variable_readback_mismatch_never_reaches_deploy_enabled(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        variable_readback_overrides={"AZURE_CLIENT_ID_BUILD": "wrong-value"},
    )
    result = h.run()
    assert result.returncode != 0
    assert "AZURE_CLIENT_ID_BUILD" in result.stderr
    assert not h.has("gh variable set DEPLOY_ENABLED")


# ---------------------------------------------------------------------------
# Order contract 3: any empty read-back aborts with nothing further attempted.
# ---------------------------------------------------------------------------


def test_empty_acr_id_readback_aborts_before_any_identity_created(tmp_path: Path) -> None:
    h = Harness(tmp_path, acr_id="")
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not h.has("ad app create")
    assert not h.record_file.exists() or h.record_file.stat().st_size == 0


def test_empty_role_assignment_id_aborts_before_verification(tmp_path: Path) -> None:
    h = Harness(tmp_path, role_assignment_create_fails_for=["AcrPush"])
    result = h.run()
    assert result.returncode != 0
    assert not h.has("role assignment list")
    assert not h.has("gh api --method PUT")


def test_role_assignment_not_listed_aborts_before_github(tmp_path: Path) -> None:
    h = Harness(tmp_path, role_assignment_listback_zero_for=["Container Apps Contributor"])
    result = h.run()
    assert result.returncode != 0
    assert "is not listed" in result.stderr
    assert not h.has("gh api")
    assert not h.has("gh variable")


def test_empty_environment_readback_aborts(tmp_path: Path) -> None:
    h = Harness(tmp_path, custom_branch_policies_override="")
    result = h.run()
    assert result.returncode != 0
    assert "empty output" in result.stderr
    assert not h.has("gh variable set")


# ---------------------------------------------------------------------------
# Preflight and record-file hygiene.
# ---------------------------------------------------------------------------


def test_refuses_to_overwrite_existing_record_file(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.record_file.write_text("PREVIOUS_RUN=yes\n")
    result = h.run()
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert h.calls == []
    assert h.record_file.read_text() == "PREVIOUS_RUN=yes\n"


def test_tenant_mismatch_aborts_before_anything_created(tmp_path: Path) -> None:
    h = Harness(tmp_path, active_tenant_id="99999999-9999-9999-9999-999999999999")
    result = h.run()
    assert result.returncode != 0
    assert "Active az tenant" in result.stderr
    assert not h.has("ad app create")
    assert not h.record_file.exists()


def test_gh_not_authenticated_aborts_before_anything_created(tmp_path: Path) -> None:
    h = Harness(tmp_path, gh_not_authenticated=True)
    result = h.run()
    assert result.returncode != 0
    assert "gh auth login" in result.stderr
    assert not h.has("ad app create")
    assert not h.record_file.exists()


def test_missing_required_env_vars_fail_before_touching_anything(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for missing in (
        "OIDC_RECORD_FILE",
        "GITHUB_REPO",
        "AZ_TENANT_ID",
        "AZ_SUBSCRIPTION_ID",
        "AZ_RESOURCE_GROUP",
        "AZ_ACR_NAME",
        "AZ_ACA_APP_NAME",
    ):
        env = dict(h.env)
        del env[missing]
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, missing
        assert missing in result.stderr, missing
    assert h.calls == []


def test_sp_creation_retries_and_succeeds(tmp_path: Path) -> None:
    h = Harness(tmp_path, sp_create_fail_first_n_calls=2)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.count("ad sp create") >= 3


def test_teardown_hint_printed_on_abort_after_partial_creation(tmp_path: Path) -> None:
    h = Harness(tmp_path, role_assignment_listback_zero_for=["AcrPush"])
    result = h.run()
    assert result.returncode != 0
    assert "delete-github-oidc.sh" in result.stderr
    assert f"OIDC_RECORD_FILE={h.record_file}" in result.stderr


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


# ---------------------------------------------------------------------------
# infra/scripts/delete-github-oidc.sh
#
# D10's sequence: DEPLOY_ENABLED=false, delete both federated credentials,
# drain check (repo-scoped, paginated, abort-not-cancel), delete role
# assignments (--all read-back), delete app registrations (assignments
# before principals), delete the GitHub environment and repository
# variables, and a separate read-only --verify-teardown mode that only
# removes the record file when nothing recorded is still found.
#
# The fakes below model a repository that already has everything
# create-github-oidc.sh would have created (apps, federated credentials,
# role assignments, the environment, the repository variables) plus a
# configurable set of in-flight workflow runs, so delete-github-oidc.sh's
# own read-backs are exercised against real state, not just call counting.
# ---------------------------------------------------------------------------

DELETE_SCRIPT = REPO_ROOT / "infra" / "scripts" / "delete-github-oidc.sh"

BUILD_DISPLAY_NAME = "gh-oidc-build-azgenai-lab-abcd1234"
DEPLOY_DISPLAY_NAME = "gh-oidc-deploy-azgenai-lab-efgh5678"
BUILD_FIC_ID = "fic-build-1"
DEPLOY_FIC_ID = "fic-deploy-1"
BUILD_ASSIGNMENT_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Authorization/"
    "roleAssignments/aaaaaaaa-0000-0000-0000-000000000001"
)
DEPLOY_ASSIGNMENT_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Authorization/"
    "roleAssignments/aaaaaaaa-0000-0000-0000-000000000002"
)

FAKE_AZ_DELETE = '''#!/usr/bin/env python3
import json, os, re, sys

state_path = os.environ["GH_OIDC_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)

args = sys.argv[1:]
state.setdefault("calls", []).append(" ".join(args))

def save():
    with open(state_path, "w") as f:
        json.dump(state, f)

def opt(name):
    return args[args.index(name) + 1] if name in args else ""

def done(out="", code=0):
    save()
    if out != "":
        print(out)
    sys.exit(code)

def fail(msg="fake az: injected failure", code=1):
    save()
    print(msg, file=sys.stderr)
    sys.exit(code)

apps = state.setdefault("apps", {})

if args[:2] == ["account", "show"]:
    done(state.get("active_tenant_id", ""))

if args[:2] == ["account", "set"]:
    done()

if args[:3] == ["ad", "app", "list"]:
    filt = opt("--filter")
    query = opt("--query")
    m = re.match(r"(appId|displayName) eq '([^']*)'", filt)
    if not m:
        fail(f"fake az: unparseable filter {filt!r}", 2)
    field, value = m.group(1), m.group(2)
    matches = []
    for app_id, info in apps.items():
        key = app_id if field == "appId" else info.get("display_name", "")
        if key == value:
            matches.append(app_id)
    if query == "length([])":
        done(str(len(matches)))
    if query == "[0].appId":
        done(matches[0] if matches else "")
    fail(f"fake az: unhandled ad app list query {query!r}", 2)

if args[:3] == ["ad", "app", "delete"]:
    app_id = opt("--id")
    if app_id in apps:
        del apps[app_id]
        done()
    fail("fake az: app not found", 1)

if args[:4] == ["ad", "app", "federated-credential", "delete"]:
    app_id = opt("--id")
    fic_id = opt("--federated-credential-id")
    fics = apps.get(app_id, {}).get("fics", [])
    if fic_id in fics:
        fics.remove(fic_id)
        done()
    fail("fake az: federated credential not found", 1)

if args[:4] == ["ad", "app", "federated-credential", "list"]:
    app_id = opt("--id")
    query = opt("--query")
    if app_id not in apps:
        fail("fake az: app not found (federated-credential list)", 1)
    m = re.search(r"\\[\\?id=='([^']*)'\\]", query)
    target = m.group(1) if m else None
    fics = apps.get(app_id, {}).get("fics", [])
    done(str(1 if target in fics else 0))

if args[:3] == ["role", "assignment", "delete"]:
    assignments = state.setdefault("role_assignments", [])
    if "--ids" in args:
        aid = opt("--ids")
        state["role_assignments"] = [a for a in assignments if a["id"] != aid]
        done()
    pid = opt("--assignee-object-id")
    role = opt("--role")
    scope = opt("--scope")
    state["role_assignments"] = [
        a
        for a in assignments
        if not (a["principal_id"] == pid and a["role"] == role and a["scope"] == scope)
    ]
    done()

if args[:3] == ["role", "assignment", "list"]:
    pid = opt("--assignee-object-id")
    role = opt("--role")
    scope = opt("--scope")
    assignments = state.get("role_assignments", [])
    count = sum(
        1
        for a in assignments
        if a["principal_id"] == pid and a["role"] == role and a["scope"] == scope
    )
    done(str(count))

print(f"fake az: unhandled command: {' '.join(args)}", file=sys.stderr)
done(code=2)
'''

FAKE_GH_DELETE = '''#!/usr/bin/env python3
import json, os, re, sys

state_path = os.environ["GH_OIDC_FAKE_STATE"]
with open(state_path) as f:
    state = json.load(f)

args = sys.argv[1:]
state.setdefault("calls", []).append("gh " + " ".join(args))

def save():
    with open(state_path, "w") as f:
        json.dump(state, f)

def opt(name):
    return args[args.index(name) + 1] if name in args else ""

def done(out="", code=0):
    save()
    if out != "":
        print(out)
    sys.exit(code)

def fail(msg="fake gh: injected failure", code=1):
    save()
    print(msg, file=sys.stderr)
    sys.exit(code)

if args[:2] == ["auth", "status"]:
    if state.get("gh_not_authenticated"):
        fail("fake gh: not logged in")
    done()

if args[:2] == ["run", "list"]:
    jq_filter = opt("--jq")
    runs = state.get("gh_runs", [])
    non_terminal = [r for r in runs if r.get("status") != "completed"]
    if "length" in jq_filter:
        done(str(len(non_terminal)))
    lines = [
        f"  - #{r['databaseId']} {r['workflowName']} ({r['headBranch']}) "
        f"status={r['status']} {r['url']}"
        for r in non_terminal
    ]
    done("\\n".join(lines))

if args[:2] == ["variable", "set"]:
    name = args[2]
    body = opt("--body")
    state.setdefault("variables", {})[name] = body
    done()

if args[:2] == ["variable", "delete"]:
    name = args[2]
    variables = state.setdefault("variables", {})
    if name in variables:
        del variables[name]
        done()
    fail("fake gh: variable not found", 1)

if args[:2] == ["variable", "list"]:
    jq_filter = opt("--jq")
    m = re.search(r'index\\("([^"]*)"\\)', jq_filter)
    if not m:
        fail(f"fake gh: unparseable jq {jq_filter!r}", 2)
    name = m.group(1)
    present = name in state.get("variables", {})
    done("true" if present else "false")

if args and args[0] == "api":
    rest = args[1:]
    method = "GET"
    jq_filter = None
    path = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--method":
            method = rest[i + 1]
            i += 2
        elif tok == "--jq":
            jq_filter = rest[i + 1]
            i += 2
        elif not tok.startswith("-"):
            if path is None:
                path = tok
            i += 1
        else:
            i += 1

    variables_prefix = f"repos/{state[\'github_repo\']}/actions/variables/"
    if method == "GET" and path and path.startswith(variables_prefix):
        name = path[len(variables_prefix):]
        done(state.get("variables", {}).get(name, ""))

    environments_path = f"repos/{state[\'github_repo\']}/environments"
    if method == "GET" and path == environments_path:
        m = re.search(r\'index\\("([^"]*)"\\)\', jq_filter or "")
        if not m:
            fail(f"fake gh: unparseable environments jq {jq_filter!r}", 2)
        name = m.group(1)
        present = name in state.get("environments", [])
        done("true" if present else "false")

    if method == "DELETE" and path and path.startswith(f"{environments_path}/"):
        name = path[len(environments_path) + 1:]
        envs = state.setdefault("environments", [])
        if name in envs:
            envs.remove(name)
        done()

    print(f"fake gh: unhandled api call: {rest}", file=sys.stderr)
    done(code=2)

print(f"fake gh: unhandled command: {' '.join(args)}", file=sys.stderr)
done(code=2)
'''


class DeleteHarness:
    def __init__(
        self,
        tmp_path: Path,
        record_overrides: dict[str, str] | None = None,
        **overrides: object,
    ) -> None:
        state: dict[str, object] = {
            "active_tenant_id": TENANT_ID,
            "github_repo": GITHUB_REPO,
            "apps": {
                BUILD_APP_GUID: {"display_name": BUILD_DISPLAY_NAME, "fics": [BUILD_FIC_ID]},
                DEPLOY_APP_GUID: {"display_name": DEPLOY_DISPLAY_NAME, "fics": [DEPLOY_FIC_ID]},
            },
            "role_assignments": [
                {
                    "id": BUILD_ASSIGNMENT_ID,
                    "principal_id": BUILD_SP_GUID,
                    "role": "AcrPush",
                    "scope": ACR_ID,
                },
                {
                    "id": DEPLOY_ASSIGNMENT_ID,
                    "principal_id": DEPLOY_SP_GUID,
                    "role": "Container Apps Contributor",
                    "scope": ACA_APP_ID,
                },
            ],
            "environments": ["production"],
            "variables": {
                "AZURE_TENANT_ID": TENANT_ID,
                "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
                "AZURE_CLIENT_ID_BUILD": BUILD_APP_GUID,
                "AZURE_CLIENT_ID_DEPLOY": DEPLOY_APP_GUID,
                "AZURE_ACR_NAME": "acrfaked25",
                "AZURE_RESOURCE_GROUP": "rg",
                "AZURE_CONTAINER_APP_NAME": "aca-faked25",
                "DEPLOY_ENABLED": "true",
            },
            "gh_runs": [],
        }
        state.update(overrides)
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps(state))

        fake_dir = tmp_path / "bin"
        fake_dir.mkdir()
        for name, body in (("az", FAKE_AZ_DELETE), ("gh", FAKE_GH_DELETE)):
            path = fake_dir / name
            path.write_text(body)
            path.chmod(0o755)

        self.record_file = tmp_path / "record.env"
        record: dict[str, str] = {
            "CREATED_AT": "2026-08-19T00:00:00Z",
            "GITHUB_REPO": GITHUB_REPO,
            "AZ_TENANT_ID": TENANT_ID,
            "AZ_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
            "AZ_RESOURCE_GROUP": "rg",
            "AZ_ACR_NAME": "acrfaked25",
            "AZ_ACR_ID": ACR_ID,
            "AZ_ACA_APP_NAME": "aca-faked25",
            "AZ_ACA_APP_ID": ACA_APP_ID,
            "GH_ENVIRONMENT_NAME": "production",
            "GH_DEPLOY_BRANCH": "main",
            "BUILD_APP_DISPLAY_NAME": BUILD_DISPLAY_NAME,
            "DEPLOY_APP_DISPLAY_NAME": DEPLOY_DISPLAY_NAME,
            "BUILD_APP_ID": BUILD_APP_GUID,
            "BUILD_APP_OBJECT_ID": BUILD_OBJECT_GUID,
            "BUILD_SP_ID": BUILD_SP_GUID,
            "DEPLOY_APP_ID": DEPLOY_APP_GUID,
            "DEPLOY_APP_OBJECT_ID": DEPLOY_OBJECT_GUID,
            "DEPLOY_SP_ID": DEPLOY_SP_GUID,
            "BUILD_FIC_ID": BUILD_FIC_ID,
            "DEPLOY_FIC_ID": DEPLOY_FIC_ID,
            "BUILD_ROLE_ASSIGNMENT_ID": BUILD_ASSIGNMENT_ID,
            "DEPLOY_ROLE_ASSIGNMENT_ID": DEPLOY_ASSIGNMENT_ID,
            "GH_ENVIRONMENT_CREATED": "true",
            "GH_REQUIRED_REVIEWER_LOGIN": REVIEWER_LOGIN,
            "GH_VARIABLES_WRITTEN": (
                "AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID AZURE_CLIENT_ID_BUILD "
                "AZURE_CLIENT_ID_DEPLOY AZURE_ACR_NAME AZURE_RESOURCE_GROUP "
                "AZURE_CONTAINER_APP_NAME"
            ),
            "DEPLOY_ENABLED_SET": "true",
        }
        if record_overrides:
            record.update(record_overrides)
        lines = [f"{k}={v}\n" for k, v in record.items() if v is not None]
        self.record_file.write_text("".join(lines))

        self.env = {
            **os.environ,
            "PATH": f"{fake_dir}:{os.environ['PATH']}",
            "GH_OIDC_FAKE_STATE": str(self.state_path),
            "OIDC_RECORD_FILE": str(self.record_file),
        }

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(DELETE_SCRIPT), *args],
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

    def last_index(self, prefix: str) -> int:
        return max(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def has(self, prefix: str) -> bool:
        return any(call.startswith(prefix) for call in self.calls)


def test_delete_script_exists_and_is_executable() -> None:
    assert DELETE_SCRIPT.is_file()
    assert os.access(DELETE_SCRIPT, os.X_OK)


def test_delete_happy_path_tears_everything_down(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr

    assert h.state["apps"] == {}
    assert h.state["role_assignments"] == []
    assert h.state["environments"] == []
    assert h.state["variables"] == {}
    # The record file is left in place by a plain teardown run -- only
    # --verify-teardown removes it.
    assert h.record_file.exists()

    calls_before_verify = len(h.calls)
    verify = h.run("--verify-teardown")
    assert verify.returncode == 0, verify.stderr
    assert "Removed" in verify.stdout
    assert not h.record_file.exists()
    # Read-only: none of the calls verify made were deletes.
    verify_only_calls = h.calls[calls_before_verify:]
    assert not any(
        "federated-credential delete" in c
        or "role assignment delete" in c
        or "ad app delete" in c
        or "gh variable delete" in c
        or "DELETE repos" in c
        for c in verify_only_calls
    )


def test_fic_deletion_precedes_drain_check(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.last_index("ad app federated-credential delete") < h.first_index("gh run list")


def test_role_assignment_deletion_precedes_app_deletion(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.last_index("role assignment delete") < h.first_index("ad app delete")


def test_drain_check_requests_more_than_the_default_20(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    result = h.run()
    assert result.returncode == 0, result.stderr
    run_list_calls = [c for c in h.calls if c.startswith("gh run list")]
    assert run_list_calls
    for call in run_list_calls:
        assert "--limit 20" not in call
        assert "--limit" in call


def test_non_terminal_run_aborts_before_role_assignment_or_app_deletion(tmp_path: Path) -> None:
    h = DeleteHarness(
        tmp_path,
        gh_runs=[
            {
                "databaseId": 42,
                "workflowName": "CI",
                "headBranch": "feature/x",
                "status": "in_progress",
                "url": "https://github.com/x/y/actions/runs/42",
            }
        ],
    )
    result = h.run()
    assert result.returncode != 0
    assert "non-terminal run" in result.stderr
    assert "#42" in result.stderr
    stderr_lower = result.stderr.lower()
    assert "not cancelled" in stderr_lower or "nothing is cancelled" in stderr_lower
    # FIC deletion (step 2) already ran -- it only blocks future token
    # exchanges, so it is safe before the drain check. Role assignments and
    # app registrations must NOT have been touched.
    assert h.has("ad app federated-credential delete")
    assert not h.has("role assignment delete")
    assert not h.has("ad app delete")
    assert h.state["apps"] != {}
    assert h.state["role_assignments"] != []


def test_incomplete_record_skips_with_warning_not_silently(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path, record_overrides={"BUILD_FIC_ID": ""})
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert "build federated credential not fully recorded" in result.stderr
    # The build FIC was never targeted; the deploy one still was.
    build_fic_deletes = [
        c
        for c in h.calls
        if c.startswith("ad app federated-credential delete") and f"--id {BUILD_APP_GUID}" in c
    ]
    assert build_fic_deletes == []
    assert h.has("ad app federated-credential delete")
    # The rest of the teardown still completed.
    assert h.state["environments"] == []


def test_verify_teardown_nonempty_keeps_record_file(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    result = h.run("--verify-teardown")
    assert result.returncode != 0
    assert "STILL PRESENT" in result.stdout
    assert h.record_file.exists()
    # Read-only -- nothing was deleted.
    assert h.state["apps"] != {}
    assert h.state["role_assignments"] != []
    assert h.state["environments"] != []
    assert h.state["variables"] != {}


def test_multiple_same_named_apps_aborts_without_deleting_either(tmp_path: Path) -> None:
    duplicate_app_id = "99999999-9999-9999-9999-999999999999"
    h = DeleteHarness(
        tmp_path,
        record_overrides={"BUILD_APP_ID": ""},
        apps={
            BUILD_APP_GUID: {"display_name": BUILD_DISPLAY_NAME, "fics": [BUILD_FIC_ID]},
            duplicate_app_id: {"display_name": BUILD_DISPLAY_NAME, "fics": []},
            DEPLOY_APP_GUID: {"display_name": DEPLOY_DISPLAY_NAME, "fics": [DEPLOY_FIC_ID]},
        },
    )
    result = h.run()
    assert result.returncode != 0
    assert "Cannot tell which one is ours" in result.stderr
    assert not h.has("ad app delete")
    assert h.state["apps"][BUILD_APP_GUID] is not None
    assert h.state["apps"][duplicate_app_id] is not None


def test_missing_oidc_record_file_env_var(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    env = dict(h.env)
    del env["OIDC_RECORD_FILE"]
    result = subprocess.run(
        ["bash", str(DELETE_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "OIDC_RECORD_FILE" in result.stderr


def test_nonexistent_record_file_fails_closed(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    missing = tmp_path / "does-not-exist.env"
    result = h.run(OIDC_RECORD_FILE=str(missing))
    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert h.calls == []


def test_verify_teardown_on_missing_record_file_is_a_clean_noop(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    missing = tmp_path / "does-not-exist.env"
    result = h.run("--verify-teardown", OIDC_RECORD_FILE=str(missing))
    assert result.returncode == 0, result.stderr
    assert h.calls == []


def test_incomplete_record_missing_required_fields_aborts_before_any_call(tmp_path: Path) -> None:
    h = DeleteHarness(tmp_path)
    h.record_file.write_text("AZ_TENANT_ID=" + TENANT_ID + "\n")
    result = h.run()
    assert result.returncode != 0
    assert "GITHUB_REPO" in result.stderr
    assert h.calls == []
