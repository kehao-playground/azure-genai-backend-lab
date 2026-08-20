# CI/CD Pipeline (Day 25)

Day 24 was a script an operator runs by hand, once, in a terminal that
already trusts them. Day 25 puts the same deploy step behind a GitHub
Actions workflow that anyone reading the repository can watch run, with two
identities that hold no long-lived secret, one human approval gate, and a
digest — not a tag — carrying the exact bytes from build through to the
running app.

Companion documents: [docker.md](docker.md) (the image this pipeline
builds), [container-apps.md](container-apps.md) (the deployment target and
its own teardown contract), [managed-identity.md](managed-identity.md) (the
one existing measurement of Azure AD role-assignment propagation this
pipeline inherits but has not itself repeated).

Microsoft Learn pages cited below were checked 2026-08-19 unless a
different date is given; each citation carries the page's own `ms.date`
where the page publishes one.

This pipeline has been built: `ci.yml` triggers `python`, `site` and `image`
on every push and pull request against this repository, and `deploy` after
approval on `main`. **No job in this file has run against Azure state, and
`deploy` has never run at all.** That is the scope of what is still
unverified — not whether `python`, `site` and `image`'s own gates execute on
GitHub-hosted runners, which they are configured to do on every push and
pull request the same as any workflow in any repository, and which this
document does not need a live run to support. Every claim below that
depends on a live run *against Azure*, or on the required-reviewer gate
actually pausing `deploy`, is marked as open in
[§11](#11-open-questions-settled-only-by-the-live-session), which also
carries the specific mechanisms this leaves unverified.

---

## 1. Pipeline shape: one workflow, `needs` is the gate

`.github/workflows/ci.yml` is a single workflow with four jobs:

```
python  ─┐
site    ─┼─▶ deploy   (needs: [python, site, image])
image   ─┘
```

`python` runs ruff, mypy, pytest, behave, the OpenAPI/index-schema/audit-schema
drift checks, the mermaid syntax gate, and `actionlint` against this workflow
itself. `site` builds the Astro site. `image` builds the Docker image and
runs `scripts/boot_smoke.sh` against it — the same two-assertion boot smoke
(HEALTHCHECK reports `healthy`, a separate `docker exec` proves what
`/health` actually returns) that used to live as an inline step in a job
named `docker`; Day 25 extracted it into its own script and renamed that job
`image`, because it now does more than build. All three of those jobs are
configured to trigger on every push and every pull request, including from
a branch that will never touch Azure — that is the point of a gate: the
correctness bar does not depend on `DEPLOY_ENABLED` or on which branch
triggered the run.

`deploy` is declared with `needs: [python, site, image]`, and that is the
entire coordination mechanism. There is no separate "all green" status
check, no external CI dashboard, no branch-protection rule this document
depends on: GitHub Actions will not start the `deploy` job until all three
named jobs have reported success, and if any of them fails or is skipped,
`deploy` is skipped too. `needs` *is* the "all gates green" constraint —
built as a property of the job graph, not layered on top of it.

Two things narrow that further. `deploy` also carries
`if: github.ref == 'refs/heads/main' && vars.DEPLOY_ENABLED == 'true'` — so
even a fully green run on a pull-request branch never reaches it — and a
`concurrency` block (`group: production, cancel-in-progress: false`) means
at most one deploy runs at a time, with at most one more queued behind it (a
newer pending run displaces an older queued one, never the other way
around); a run already `az containerapp update`-ing is never cancelled out
from under itself by a second push.

`actionlint` (pinned `v1.7.12`, downloaded by tarball and verified by
checksum — no new package manager, same discipline
[docker.md](docker.md) uses for the `uv` image) cannot catch the class of
bug Day 24 shipped: bugs only visible once the workflow actually runs
against real Azure and GitHub state. What it does catch is narrower and
still worth having — a typo'd `needs.image.outputs.digest` reference, which
GitHub Actions itself would otherwise evaluate to a silently empty string
that flows straight into the `deploy` job's `--image` argument.

## 2. Two identities, and the residual approval does not remove

Two federated (secret-less) Entra app registrations, each with its own
role, its own scope, and its own OIDC subject:

| Identity | Role | Scope | Subject | Used by |
|---|---|---|---|---|
| build | `AcrPush` | the container registry | `repo:<owner>/<repo>:ref:refs/heads/main` | `image` job — `docker push` |
| deploy | `Container Apps Contributor` | the one container app | `repo:<owner>/<repo>:environment:production` | `deploy` job — `update-container-app.sh` |

Both audiences are `api://AzureADTokenExchange` — worth knowing because that
is `azure/login`'s own input default, not a universal OIDC constant; a
non-Azure cloud would need a different value. Both scopes are the specific
resource, not the resource group and not the subscription: a wider scope
would let either identity touch everything else that group holds, which is
exactly the blast radius two narrow identities exist to avoid.

**The residual this buys is real, and it is not a corner case — by design,
it happens on every ordinary push to `main`.** The `image` job's Azure-touching steps
(`az acr login`, tag, push) are gated only on `github.ref == 'refs/heads/main'
&& vars.DEPLOY_ENABLED == 'true'`, with no approval requirement at all —
approval belongs to the `deploy` job, three jobs downstream. So the build
identity's `AcrPush` grant is exercised, and an image really is pushed to
the registry, before any human has approved anything. What that write is
*not* is dangerous on its own: nothing runs an image that no `--image`
argument names, so a pushed-but-never-approved image just sits in the
registry as a tag-and-digest pair until the next `delete-acr.sh`. But
"approval gates all Azure access" is not literally true under this design,
and this document does not claim it is.

The stricter alternative is a single identity, holding both roles, with its
federated-credential subject bound only to `environment:production` — so no
token is mintable at all before a human approves. That removes the residual
above entirely, at a real cost: it forces a choice between (a) rebuilding
the image *after* approval, inside the `deploy` job, which means the
object that reaches ACR and the object `scripts/boot_smoke.sh` actually
tested are no longer provably the same bytes (Docker builds are not
bit-for-bit reproducible run to run — base-image drift, package-index
state, timestamps), quietly undoing the guarantee §5 depends on; or (b)
carrying the already-built image as a GitHub Actions artifact across the
approval boundary (`docker save` / `actions/upload-artifact`, then
`docker load` and push only after approval), which keeps the bytes exact
but adds artifact storage, a save/load round trip on every run whether or
not it is ever approved, and — the part worth naming plainly — a single
credential now capable of *both* writing images and mutating the live app,
a strictly larger blast radius for whatever holds it than either of this
pipeline's two narrowly-scoped identities alone. This project accepted the
pre-approval write residual instead of that trade. Neither option is
presented here as strictly safer than the other; they trade different
things away.

One build-identity mechanic worth a plain warning, because it looks like
the kind of thing a reader would "clean up": the `az acr login` step in
`ci.yml` deliberately does **not** pass `-g`/`--resource-group`, even
though that looks like the more explicit, tidier form. Without `-g`, a
failed control-plane lookup raises `ResourceNotFound`, which the CLI
tolerates and falls back to `<name>.azurecr.io` plus a data-plane token
exchange — the path `AcrPush` actually covers. With `-g`, the CLI instead
calls `registries/read` directly, an action `AcrPush` does not grant, and
that failure is not on the tolerated fallback path. Adding `-g` "for
tidiness" would break the push. What the CLI actually prints in that
failure path, under a real `AcrPush`-only identity, has not been observed
in this project — see [§11](#11-open-questions-settled-only-by-the-live-session).

## 3. Subject binding: what it stops, and what it does not

The subject carried by the OIDC token GitHub mints for a job is a fixed
string GitHub composes from claims about *that job* — not a fact this
project asserts, but what the token exchange checks against each
federated credential's registered subject. The build identity's subject,
`repo:<owner>/<repo>:ref:refs/heads/main`, genuinely does encode a branch:
a workflow run triggered from any other ref produces a token whose subject
does not match, and the exchange fails before a build-identity token is
ever minted.

**The deploy identity's subject does not.**
`repo:<owner>/<repo>:environment:production` names the repository and the
environment the job declared — it says nothing about which branch
triggered the run that reached that environment. It is present in the
claim only because the job requested `environment: production`, and
GitHub does not allow that request to proceed to token-minting before an
approval; the ref that started the run is simply not part of this string.
Reading it as "this subject *is* the branch restriction" is the most
likely misreading a reader of this pipeline would take away, and it would
be wrong.

**"Only `main` deploys" is enforced entirely by a separate, GitHub-side
setting**: the `production` environment's deployment-branch policy
(`custom_branch_policies: true`, `protected_branches: false`, one policy
named `main` — set by `create-github-oidc.sh` step 5, then **read back and
compared**, because GitHub silently auto-creates an *unprotected*
environment with no restriction at all the first time any workflow
references a name that does not yet exist; the name existing proves
nothing about whether it is gated). That policy is what stops a run
triggered from a feature branch — `ci.yml` only triggers on `push` and
`pull_request`, but the same policy would equally stop a `workflow_dispatch`
on any workflow this repository might add later — from ever reaching the
point of requesting an `environment: production` token in the first place —
it is enforced upstream of the subject, not encoded inside it. If that policy
were ever removed or misconfigured back to "any branch," the deploy
identity's federated credential would trust an `environment:production`
token requested by a run from any branch, because nothing in the subject
string itself would object.

## 4. Layered controls, and each layer's limit

| Layer | Mechanism | What it checks | Its limit |
|---|---|---|---|
| Gates | `needs: [python, site, image]` | Lint, types, tests, behave, three schema-drift checks, mermaid syntax, `actionlint`, the Astro build, and a build-plus-boot-smoke of the image all reported success | Only checks that these specific automated checks passed — a correctness bug none of them cover reaches `main` regardless; this is not a human having read the diff |
| Job `if:` condition | `deploy`'s own `if: github.ref == 'refs/heads/main' && vars.DEPLOY_ENABLED == 'true'` | Refuses to even queue the job — so no environment protection is evaluated at all — unless the run is on `main` and the pipeline is armed | A single boolean, evaluated once per job; §8 covers what it does and does not mean for a run already past that evaluation |
| Required reviewer | GitHub environment protection (`reviewers: [...]`, `prevent_self_review: false`) | A human with write access approves before the `deploy` job's OIDC token can even be requested | Single-operator repo: the reviewer is whichever GitHub login `create-github-oidc.sh` was run under (`gh api user`), not necessarily the repository owner, and self-review is explicitly not prevented — see below |
| Deployment branch policy | Environment setting, one custom policy named `main` | Only a run triggered from `main` can request a token whose claims match the deploy identity's subject | Enforced entirely at this layer — see §3. It says nothing about the *content* of that commit, only its ref |
| Freshness guard | `scripts/check_freshness.sh`, first step of the `deploy` job | This run's commit is still `main`'s current HEAD, queried live from the GitHub API | Fails closed on any query failure or empty read, but it answers "is this commit still current," not "was it reviewed" — approval already happened by the time this check runs |
| Federated subject | Deploy identity's federated credential, subject `repo:<owner>/<repo>:environment:production` (§3) | Entra will not exchange the OIDC token `azure/login@v2` presents for this identity's own token unless the claimed subject matches exactly | Carries no ref of its own (§3) — branch restriction is enforced entirely by the deployment branch policy layer above, not by anything in this string |

The live session's negative tests (§11) produce evidence for two of these
rows directly: the side-branch job that declares `environment: production`
with no ref condition targets the deployment branch policy layer; the job
that presents the deploy identity without declaring `environment:` at all
targets the federated subject layer.

The required-reviewer layer is the one most worth reading carefully rather
than trusting the name. **Single-operator self-approval is not two-person
review**, and this pipeline does not claim it is: `prevent_self_review` is
sent explicitly as `false`, on the record, because this is a
single-operator repository and the alternative (leaving the default,
implicitly) would obscure a decision this project's own discipline says to
write down. The required reviewer's identity is also not fixed to "the
repo owner" by anything in the code — `GH_REQUIRED_REVIEWER_LOGIN`
defaults to whichever account `gh api user --jq .login` reports at the
moment `create-github-oidc.sh` runs (a separate, earlier `gh auth status`
check only confirms *some* account is logged in; it is never queried for
the login value itself). That default happens to be the same person in
this project but is not guaranteed to be by the mechanism itself.

## 5. Digest, not tag

The `image` job tags the built image as
`<registry>.azurecr.io/azgenai-lab:sha-<commit-sha>`, pushes it, then
immediately reads the digest back with
`docker inspect --format '{{index .RepoDigests 0}}'` and strips the
`name@` prefix. That digest — not the `sha-<commit-sha>` tag that was also
just pushed — is what crosses the job boundary as `needs.image.outputs.digest`,
and it is what `update-container-app.sh --image
<registry>.azurecr.io/azgenai-lab@<digest>` deploys. If the digest cannot
be parsed, the push step fails outright rather than falling back to the
tag it also created.

The reason is the whole point of the pipeline: **the bytes that passed the
gates are the bytes that get deployed.** A digest is a hash of content —
there is exactly one object it can name. A tag is a pointer that can be
repointed. If this pipeline instead deployed by the `sha-<commit-sha>` tag
it also pushes, a *re-run* of the same workflow for the same commit (a
manual re-dispatch, for instance) would rebuild — Docker builds are not
guaranteed bit-for-bit reproducible run to run, given base-image and
package-index drift — and re-push under that identical tag, silently
overwriting the object the first run's boot smoke actually verified. Any
later deploy addressing the image by that tag would then be serving bytes
nobody's gate ever checked, with no error anywhere to say so. Digest
addressing makes that structurally impossible, because the digest handed
to `deploy` is computed fresh, in the very same job run whose boot smoke
just passed, and never read back from anything a later run could have
overwritten.

`update-container-app.sh` was written with this ambiguity already in mind
on the read side: its pre-mutation snapshot of the currently-deployed
image is stored and echoed *verbatim*, "sometimes a tag and sometimes a
digest," because the very first deployment (`deploy-container-app.sh`, Day
24) writes a tag and only a later run through this pipeline writes a
digest. An immutable-tag fallback — deploying by `sha-<commit-sha>`
instead of by digest — would still be far stronger than a mutable `:latest`
style tag, since a given commit SHA is not reused, but it would trade away
exactly the re-run guarantee above: the honest downgrade is "the same
commit" no longer provably means "the same bytes," only "very likely the
same bytes, unless this exact run was re-triggered." This pipeline does
not take that trade.

## 6. Why no `--platform`

Day 24's `az acr build --platform linux/amd64` existed because the
operator runs it from a laptop, and this repo is developed on Apple
Silicon — an arm64 host building for a platform ACR Tasks defaults to
amd64 on. `ubuntu-latest`, the runner this workflow's `image` job runs on,
is itself amd64. `docker build -f docker/Dockerfile -t azgenai-lab:ci .`
therefore builds natively for the architecture Container Apps expects,
with no cross-platform emulation and no `--platform` flag to get wrong.

## 7. Repository variables, not secrets

Every identifier this workflow reads — both client ids, the tenant id, the
subscription id, the ACR name, the resource group, the container app name,
and `DEPLOY_ENABLED` itself — is a **repository variable**
(`gh variable set`), not a secret (`gh secret set`). This is a deliberate,
recorded deviation from Microsoft's own OIDC guidance, which states
plainly, of these same `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` /
`AZURE_SUBSCRIPTION_ID`-shaped identifiers: *"For security reasons, we
recommend using GitHub Secrets rather than passing values directly to the
workflow."* ([Authenticate to Azure from GitHub Actions by OpenID Connect
§ Create GitHub secrets](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect#create-github-secrets),
ms.date 2024-07-01, checked 2026-08-19.)

The reasoning for deviating: the actual credential in this design is the
federated-identity trust relationship itself — the subject match checked
at token-exchange time — and that relationship does not live in the repo
at all; it lives in Entra. A client id, a tenant id, a subscription id, an
ACR name and a resource-group name are not, on their own, secrets that
grant access to anything. Putting them in `secrets` would not protect
anything real; it would only mask them as `***` in every log line that
references them, at the cost of every future debugging session having to
guess what a redacted value was.

The cost is stated here rather than left implicit, because this project's
own discipline treats an unstated trade-off as worse than an admitted one.
This repository is public. Repository *variables*, unlike secrets, are not
masked in workflow run logs — and Actions run logs for a public repository
are themselves publicly readable, by anyone, without a GitHub account.
Every one of these identifiers — the subscription id, the tenant id, the
ACR name, the resource group name, the container app name, both client
ids — appears in plaintext in this repository's public run history the
moment a workflow that references it runs. That is the concrete price of
the deviation, and it is why the values in this document's own examples
are placeholders rather than the project's real ones.

## 8. `DEPLOY_ENABLED` and the ephemeral-resources tension

Every Azure resource this pipeline's `deploy` job depends on — the ACR,
the container app, the two federated identities themselves — is
ephemeral by this series' own standing rule: created for a session,
deleted at the end of it. A workflow that always tries to push an image
and update an app would fail on every push to `main` between sessions, for
a reason that has nothing to do with the code. `DEPLOY_ENABLED`, a
repository variable read in two `if:` conditions in `ci.yml` (the `image`
job's Azure-touching steps, and the `deploy` job's own top-level `if`), is
what lets the workflow tell those two situations apart. `create-github-oidc.sh`
sets it to `true` as its deliberately *last* mutation — reached only after
every earlier verification (role-assignment read-backs, the environment
read-back, the repository-variable read-backs) has already come back
clean — and `delete-github-oidc.sh` sets it to `false` as its deliberately
*first* mutation.

**It is not "the pipeline is stopped."** It is one variable, read once, at
the point GitHub Actions evaluates that `if:` string for a given job. A
run already past that evaluation — already inside the `image` job's Azure
steps, or already inside `deploy` — keeps going even if `DEPLOY_ENABLED`
flips to `false` while it runs; the flag is not polled continuously, and
flipping it revokes nothing already granted. It only prevents a *new*
job evaluation from entering the Azure-touching branches. Contrast that
with `delete-github-oidc.sh`'s later, stronger step — deleting the
federated credentials outright, which does revoke the ability to mint a
*new* token against that subject, though even that step is explicit that
it does not touch a token already issued.

## 9. Teardown runbook, its ordering contract, and no admission lock

`delete-github-oidc.sh` reads every identifier it needs from the record
file `create-github-oidc.sh` wrote — it takes no name knobs from the
caller at all — and tears down in six steps, in an order that is the
contract, the same discipline `delete-container-app.sh`'s own seven steps
follow:

1. `DEPLOY_ENABLED=false` — stops *new* runs from reaching the deploy
   job's gate; does not touch a run already past it.
2. Delete both federated credentials, read back that they are gone. This
   only blocks *future* token exchanges against that subject, which is
   exactly why it is safe to do this early, before checking for anything
   in flight: a run already holding a valid token is untouched by it.
3. **Drain check** — every workflow run in the repository not in a
   terminal state, repo-scoped rather than filtered by workflow name
   (any workflow could in principle declare `environment: production`).
   Anything non-terminal found aborts teardown here, before any role
   assignment or app registration is touched — and nothing is ever
   cancelled by this script; deciding to kill someone else's in-flight
   run is a human decision, not this script's to make. The fetched
   window is also checked against its own size cap
   (`GH_RUN_LIST_LIMIT`, default 1000): a window that came back exactly
   full is treated as possibly truncated and fails closed, rather than
   trusting a non-terminal count that might not have covered the whole
   repository.
4. Delete both role assignments, read back **zero** remaining with
   `--all` (load-bearing, not tidiness — `az role assignment list`
   defaults to subscription scope only, and both assignments here are
   resource-scoped; without it the query silently returns `0` no matter
   what actually remains).
5. Delete the app registrations — only after step 3's drain check and
   step 4's confirmed-empty assignments, and only in that order relative
   to each other, because deleting an app registration also deletes its
   service principal, and Azure evaluates role-assignment authorization
   against a *live* principal at request time: pulling that out from
   under a still-running job would break it immediately, unlike step 2's
   forward-only credential deletion. `az ad app delete` only moves a
   registration into the directory's recycle bin for 30 days, still
   holding its name, so this step also attempts a best-effort purge,
   keyed on the object id `create-github-oidc.sh` records.
6. Delete the GitHub environment and every repository variable this
   script wrote, including `DEPLOY_ENABLED` itself — read back after
   each deletion.

**Before running this script, resolve every pending deployment approval and
cancel any leftover run from a negative test that declares
`environment: production`** (the live session's negative tests, §11, are
exactly such a run) — step 3's drain check aborts on any non-terminal run
by design, including one sitting `waiting` for a reviewer who never
responds. That is the drain check working correctly, not a bug to route
around.

A separate, read-only `--verify-teardown` mode re-runs the same existence
checks against everything the record file names, deletes nothing, and
removes the record file only when every item was both checkable and
confirmed absent; a field missing from the record file makes that one item
*unverifiable*, never silently skipped.

**There is no verified admission lock.** After step 3's drain check reads
empty, nothing in this design stops a fresh `git push` to `main` — or a
`workflow_dispatch` on any workflow this repository might add later, since
`ci.yml` itself has no such trigger today — from starting a run before step
5 finishes — GitHub
Actions concurrency controls exist and this pipeline uses one (§1's
`group: production`), but nothing wires it to the *teardown* process
itself. For a single-operator repository, "do not push during teardown"
is process control — a rule the one operator follows — not a technical
guarantee this script enforces or could enforce without more machinery
than this project built. Nothing here should be read, or written
elsewhere, as a lock.

This is the CI/CD-side half of a teardown that spans two systems, and the
order between the two halves matters as much as the order within each.
`create-github-oidc.sh`'s role assignments are scoped to the container
registry and to the one container app — resources [container-apps.md's own
teardown](container-apps.md#9-teardown-ordering-is-a-contract-not-a-convenience)
deletes. Running `delete-github-oidc.sh` while both still exist is the
clean order: its role-assignment read-backs query those two resources by
their live scope, and doing that before either resource is gone is
simpler than reasoning about a role-assignment query against a scope that
no longer resolves. The full runbook, in order, is documented in
[container-apps.md §8.5](container-apps.md#85-tear-down-in-this-order).

## 10. ACR role-assignment-mode pinning and the ABAC migration path

`create-acr.sh` passes `--role-assignment-mode rbac` **explicitly**, even
though `rbac` also happens to be the CLI's current default. That default
is Microsoft's to change, not this project's to assume — Microsoft has
documented an ABAC-enabled registry mode (`rbac-abac`), on which the
classic `AcrPush` / `AcrPull` / `AcrDelete` roles this series assigns are
not honoured at all, and states its intent to make that mode the default
in the future
([Azure ABAC repository permissions in Azure Container Registry](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-abac-repository-permissions),
ms.date 2025-12-11, checked 2026-08-19). Pinning `rbac` is what keeps the
build identity's `AcrPush` grant (§2) meaningful today.

The migration this project has **documented but not implemented**, should
a future registry move to ABAC — per that same page's own migration table:

- `AcrPush` → `Container Registry Repository Writer` — the role this
  pipeline's `image` job would need for `docker push` under `az acr login`.
- `AcrPull` → `Container Registry Repository Reader` +
  `Container Registry Repository Catalog Lister` — the pair Container
  Apps' own image pull would need.
- `az acr build` would additionally need `--source-acr-auth-id [caller]`
  on an ABAC-enabled registry. That flag matters to Day 24's
  `deploy-container-app.sh`, which builds server-side with `az acr build`
  against this same registry — it does not affect this pipeline's `image`
  job, which builds locally on the runner and pushes with plain `docker
  push` instead.

Neither path has been exercised: this series has never created or tested
an ABAC-enabled registry.

## 11. Open questions, settled only by the live session

`ci.yml` defines `python`, `site` and `image` to trigger on every push and
pull request against this repository, and `deploy` to run after approval on
`main`. **No job in this file has run against Azure state, and `deploy` has
never run at all** — that is what this section leaves open, not whether
`python`, `site` and `image`'s own gates execute on GitHub-hosted runners
(they are configured to, on every push and pull request, and §1 already
states what they check; that is GitHub-hosted-runner and Docker state, not
Azure or GitHub-environment-approval state). Task 7's own report names the
specific mechanisms this leaves unverified, folded into the list below:
OIDC token minting via `azure/login@v2`, `az acr login`'s fallback
behaviour, the environment's required-reviewer gate actually blocking a
run, concurrency behaviour, and the digest round-trip through a real
`docker push`. The following are open until a live run against Azure
happens, and nothing above should be
read as resolving them in advance:

- **Whether the OIDC token exchange itself succeeds end to end** — both
  identities' federated credentials, `azure/login@v2` minting a token
  against each subject, and Entra accepting it. Every mechanism in §2 and
  §3 is a description of the configuration as written, not of an observed
  exchange.
- **Whether the required-reviewer gate actually pauses the `deploy` job**
  as configured, and whether the deployment-branch policy actually blocks
  a non-`main` run from reaching it — both are GitHub-side settings this
  document describes from `create-github-oidc.sh`'s own read-back logic,
  not from watching a run be paused or blocked.
- **Whether the `concurrency` group behaves as specified** under a real
  queued run (§1) — untested, since no two runs have ever competed for it.
- **Role-assignment propagation timing for this pipeline.** The only
  measurement anywhere in this project is Day 20's **14 minutes 44
  seconds**, against Microsoft's documented "up to 5 minutes"
  ([managed-identity.md §2](managed-identity.md#2-keyless-azure-openai-on-this-projects-own-client-shape)).
  That number describes a *different* identity type (a managed identity
  reaching the Azure OpenAI data plane) under Day 20's own conditions —
  cite it here as prior art for how far "up to 5 minutes" has already
  been shown wrong, not as a prediction for these two app-registration
  service principals reaching the ACR and Container Apps control planes.
- **Whether `Container Apps Contributor`'s coverage of
  `containerApps/*/write` extends to the bare `containerApps/write`
  action `az containerapp update --image` needs.** Not verified against a
  live call.
- **Whether ARM/ACA accepts a digest-form `--image`**, and how a
  single-revision-mode app behaves when the revision it produces fails to
  start. `update-container-app.sh` polls the new revision's
  `properties.runningState` as the most specific field `az containerapp
  revision show` exposes for this, but which field authoritatively
  reports a failed start is, in the script's own words, "an open question
  in this project" — this document does not settle it either.
- **What `az acr login` prints under an identity that holds only
  `AcrPush`** (no `registries/read`) — the fallback path §2 documents has
  never been exercised against a real `AcrPush`-only identity in this
  project.
- **The GitHub REST response shapes the environment-protection read-back
  depends on** (`create-github-oidc.sh` step 5's field-by-field
  comparison against `deployment_branch_policy` and
  `protection_rules[].reviewers`) — implemented against the documented
  endpoints and field names, never checked against a live GitHub API
  response in this project.

Task 10's live session is what answers these. Until then, treat every
mechanism in this document as *specified by the code, and verified only by
reading it*, not as *observed running against real Azure state*.
