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

This pipeline has been built and run. `ci.yml` triggers `python`, `site` and
`image` on every push to `main` and on every pull request, and `deploy` after
approval on `main`. **On 2026-08-20 the whole pipeline ran end to end against
real Azure and GitHub state** (japaneast): both OIDC token exchanges
succeeded, an image was pushed to a live registry, the required-reviewer gate
paused `deploy` until a human approved, and `az containerapp update` deployed
by digest to a live container app that then served the exact `/health` body.
That run also produced the two live bugs recorded in
[§11](#11-what-the-live-session-settled-and-what-is-still-open) and the
evidence file it points to.

What remains genuinely unverified is now a short list, not a blanket
disclaimer: `concurrency` behaviour under two competing runs, single-revision
behaviour when a revision fails to start, and role-assignment propagation
timing for these two service principals. Those three, and nothing broader,
are what [§11](#11-what-the-live-session-settled-and-what-is-still-open)
leaves open. Claims elsewhere in this document that were written before that
run and phrased as "not yet observed" have been reconciled against it; where
a mechanism is described from the code rather than from an observation, the
sentence says so locally.

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
`image`, because it now does more than build. This workflow's triggers are
`pull_request:` and `push:` restricted to `branches: [main]`, so all three of
those jobs run on every push to `main` and on every pull request — including a
pull request from a branch that will never touch Azure, which is the point of a
gate: the correctness bar does not depend on `DEPLOY_ENABLED` or on which branch
opened the pull request. A push to a side branch with no pull request open
triggers nothing at all; that is a property of these triggers, not a gap the
gates cover.

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
against real Azure and GitHub state — a class this pipeline then shipped two
more of, both caught by the 2026-08-20 run and neither reachable by any
static check (§11). What it does catch is narrower and
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

**The residual this buys is real, and it is not a corner case — by design, an
ordinary armed push to `main` reaches it with no approval anywhere in
front.** The `image` job's Azure-touching steps
(`az acr login`, tag, push) are gated only on `github.ref == 'refs/heads/main'
&& vars.DEPLOY_ENABLED == 'true'`, with no approval requirement at all —
approval belongs to the `deploy` job, three jobs downstream. So on a run where
the pipeline is armed, the ref is `main`, and the build, boot smoke, ACR login
and push all succeed, the build identity's `AcrPush` grant is exercised and an
image really is pushed to the registry before any human has approved anything.
Those conditions are what make the write routine rather than exceptional, and
not one of them is an approval — which is the whole point. A run that is
unarmed, on another ref, or that fails anywhere ahead of the push writes
nothing at all. What that write is
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
tidiness" would break the push. What the CLI prints while taking that
fallback path was observed on 2026-08-20 under a real `AcrPush`-only
identity: `Login Succeeded`, and nothing else — no warning, no note that a
control-plane lookup failed. The fallback is invisible in the log, which is
why the warning above has to live in a comment in `ci.yml` instead
([§11](#11-what-the-live-session-settled-and-what-is-still-open)).

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

**"Only `main` deploys" is enforced by two independent layers, neither of
them the subject string.** The first is this workflow's own job-level
condition, `if: github.ref == 'refs/heads/main' && …` on the `deploy` job
(§4): a run on any other ref never even queues the job, so no environment
protection is evaluated at all. The second is a GitHub-side setting that does
not depend on this workflow being written correctly: the `production`
environment's deployment-branch policy
(`custom_branch_policies: true`, `protected_branches: false`, one policy
named `main` — set by `create-github-oidc.sh` step 5, then **read back and
compared**, because GitHub silently auto-creates an *unprotected*
environment with no restriction at all the first time any workflow
references a name that does not yet exist; the name existing proves
nothing about whether it is gated).

The second layer is the one worth understanding, precisely because it holds
where the first does not: it stops a run whose workflow declares
`environment: production` with **no ref condition at all** — a workflow whose
job-level `if` was never written, was removed, or is a `workflow_dispatch`
entry point this repository might add later — from ever reaching the point of
requesting an `environment: production` token. That is exactly the shape the
live session's negative test A used, and why that test needed a separate
throwaway workflow rather than `ci.yml`: under `ci.yml`'s own job-level `if`,
a side-branch run is skipped before the branch policy is ever consulted, so
the layer under test would never be reached. Both layers are enforced upstream
of the subject, not encoded inside it. If that policy
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
| Deployment branch policy | Environment setting, one custom policy named `main` | Only a run triggered from `main` can request a token whose claims match the deploy identity's subject | The second of the two layers that enforce "only `main` deploys" (the job `if:` row above is the first), and the one that survives a workflow which omits the ref condition — see §3. It says nothing about the *content* of that commit, only its ref |
| Freshness guard | `scripts/check_freshness.sh` — the `deploy` job's first check, immediately after `actions/checkout` and before any Azure login | This run's commit is still `main`'s current HEAD, queried live from the GitHub API | Fails closed on any query failure or empty read, but it answers "is this commit still current," not "was it reviewed" — approval already happened by the time this check runs |
| Federated subject | Deploy identity's federated credential, subject `repo:<owner>/<repo>:environment:production` (§3) | Entra will not exchange the OIDC token `azure/login@v2` presents for this identity's own token unless the claimed subject matches exactly | Carries no ref of its own (§3) — branch restriction comes from the job `if:` and deployment-branch-policy layers above, not from anything in this string |

The live session's negative tests (§11) produced evidence for two of these
rows directly: the side-branch job that declared `environment: production`
with no ref condition targeted the deployment branch policy layer, and was
refused by it; the job that presented the deploy identity without declaring
`environment:` at all targeted the federated subject layer, and was refused
at the Entra token exchange.

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
that no longer belong to the run whose gates and whose approval were the
reason to deploy at all — with no error anywhere to say so. The re-pushed
bytes are not ungated: the re-run's own `image` job builds and boot-smokes
before it can push, so whatever a tag now resolves to has passed *some* run's
gate. What tag addressing cannot do is prove *which* run's — and the
approval a human gave was given for one specific run's artefact, not for
whatever later occupies the same name. Digest
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

**Stripping to the bare digest is also what keeps this job output from
disappearing, now that the ACR name is a secret (§7).** GitHub's runner
masks a job output by comparing its evaluated string against a
secret-masked copy of itself and, on any difference, drops the output
entirely rather than passing through `***` — `context.Warning($"Skip
output '{output.Key}' since it may contain secret.")` immediately followed
by `continue`, in `FinalizeJob`
([`src/Runner.Worker/JobExtension.cs`](https://github.com/actions/runner/blob/258d6c857db3519913f7deb6004b60172f8043ae/src/Runner.Worker/JobExtension.cs),
checked 2026-08-20). The full digest string
(`<acr-name>.azurecr.io/azgenai-lab@sha256:…`) would trip that check, since
it contains the now-secret ACR name; `DIGEST="${FULL_DIGEST#*@}"` above
removes exactly that prefix before the value ever reaches
`echo "digest=$DIGEST" >> "$GITHUB_OUTPUT"`, so `sha256:` plus 64 hex
characters is what actually crosses the job boundary — content none of
these seven values can occur inside: every one of them either contains a
`-`, which a digest never does, or — for the ACR name, the only
alphanumeric-only one — would have to be composed entirely of hex
characters. This strip predates the secrets migration and was
written for the reproducibility reason above; that it also keeps this
particular output alive is a side effect of that design, not something the
migration itself reasoned about. Had this pipeline instead published the
full `<acr-name>.azurecr.io/...@sha256:...` reference as a job output, the
masker would empty it — that warning in the `image` job's log being the
only clue on that side — and `deploy`'s own `[ -z "$DIGEST" ]` guard would
then refuse to deploy, a failure this design avoids entirely by never
putting the secret-shaped substring in an output in the first place.

## 6. Why no `--platform`

Day 24's `az acr build --platform linux/amd64` existed because the
operator runs it from a laptop, and this repo is developed on Apple
Silicon — an arm64 host building for a platform ACR Tasks defaults to
amd64 on. `ubuntu-latest`, the runner this workflow's `image` job runs on,
is itself amd64. `docker build -f docker/Dockerfile -t azgenai-lab:ci .`
therefore builds natively for the architecture Container Apps expects,
with no cross-platform emulation and no `--platform` flag to get wrong.

## 7. Repository secrets, and the one variable that stays a variable

Every identifier this workflow reads — both client ids, the tenant id, the
subscription id, the ACR name, the resource group and the container app
name — is a **repository secret** (`gh secret set`). `DEPLOY_ENABLED` is the
one exception, and it stays a **repository variable** (`gh variable set`),
for a reason that has nothing to do with secrecy: `ci.yml` reads it in four
`if:`s (§1, §4) — one job-level (`deploy`'s own top-level `if`) and three
step-level (the `image` job's Azure-touching steps) — and GitHub documents
the `secrets` context as unusable in `if:` conditionals generally:
*"Secrets cannot be directly referenced in `if:` conditionals. Instead,
consider setting secrets as job-level environment variables, then
referencing the environment variables to conditionally run steps in the
job."* ([Using secrets in GitHub Actions § Using secrets in a
workflow](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets),
checked 2026-08-19.) That suggested workaround would actually cover the
three step-level reads — the `env` context is available in
`jobs.<job_id>.steps.if` — but not the one job-level read: `jobs.<job_id>.if`
only has `github`, `needs`, `vars` and `inputs` in scope, no `env` and no
`secrets` ([Contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts),
checked 2026-08-20). So the job-level `if:` on `deploy` is the one read
that actually forces `DEPLOY_ENABLED` to stay a variable; the three
step-level reads are along for the ride, kept on the same variable for one
consistent name rather than split across two mechanisms. `DEPLOY_ENABLED`
is not itself a secret-shaped value —
it is a boolean gate — so this is not a workaround forced on a value that
belongs in `secrets`; it is the one identifier here that was never a
candidate for `secrets` in the first place, now placed correctly next to
the constraint that would have broken it if it were.

**This project changed its mind about the other seven.** An earlier version
of this pipeline put all eight of these in repository variables, arguing —
correctly, and the argument still holds — that Microsoft's own OIDC
tutorial's reason for putting them in secrets does not survive contact with
what a secret actually protects: *"For security reasons, we recommend using
GitHub Secrets rather than passing values directly to the workflow."*
([Authenticate to Azure from GitHub Actions by OpenID Connect § Create
GitHub secrets](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect#create-github-secrets),
ms.date 2024-07-01, checked 2026-08-19.) The actual credential in this
design is the federated-identity trust relationship itself — the subject
match checked at token-exchange time (§2, §3) — and that relationship does
not live in this repository at all; it lives in Entra. A client id, a
tenant id, a subscription id, an ACR name and a resource-group name are
not, on their own, secrets that grant access to anything: knowing
`AZURE_CLIENT_ID_DEPLOY` without also holding a token whose subject matches
`repo:<owner>/<repo>:environment:production` gets an attacker nothing.
**That classification argument has not changed, and this document still
makes it.**

**What changed is the exposure surface, not the classification.** This
repository is public, and Actions run logs for a public repository are
readable by anyone, without a GitHub account, for as long as GitHub
retains them — 90 days by default, configurable 1–90 days for a public
repository ([Managing GitHub Actions settings for a
repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository),
checked 2026-08-20). "90 days" bounds what GitHub stores, not what a
reader keeps: whatever anyone reads inside that window is copyable and
outside this project's control **from then on, permanently** — the
permanence is a property of what got read, not of the storage window —
and this project has no way to know who read what before a run's logs
expire. Repository *variables*, unlike secrets, are never masked in a log
line that references them. Independently of this pipeline, this
project's own discipline already masks subscription ids, tenant ids,
endpoints and resource names in every screenshot and every evidence file it
publishes; that discipline exists precisely because these values are the
project's own, and the project has chosen not to publish them even though
none of them is, on its own, a credential. Publishing the identical values
through a workflow log while masking them in a screenshot would be
incoherent — the same value, under the same threat model, handled two
different ways depending on which surface happened to carry it. Moving
these seven identifiers into `secrets` is what makes the handling match the
threat model consistently across every surface this project controls, not
a reversal of the classification argument above. Both things are true at
once: these values are not secrets in the OIDC threat model, and this
project masks them anyway, because *where* a value becomes visible is a
fact about this repository (public, readable by anyone without an account
for the retention window, and anything read there is out of this
project's control for good), not a fact this document is willing to leave
to which command happened to write the value.

**The cost, stated plainly rather than left implicit:** `gh` has no command
that reads a secret's value back once set — `gh secret list` and the REST
endpoint behind it return a name and timestamps only, never a value, by
GitHub's own design. `create-github-oidc.sh`'s previous read-back discipline
for these seven values — write, then read the value back, then compare it
against what was just sent — is not reproducible for secrets and is not
faked here. Step 6 of that script now confirms only that each secret
**exists** under its expected name; it cannot confirm that its **value**
is the one the script just sent. If a value is wrong at write time — a
stale `$AZ_ACR_NAME`, a typo carried in from the caller's environment — the
first symptom is a failure at the first workflow run that reads it, not
here. This is a real weakening of this branch's read-back discipline
relative to the repository-variable design it replaces, not a gap this
document is silently carrying forward: `DEPLOY_ENABLED`, the one value that
stayed a variable, keeps the full write-then-compare read-back exactly as
before (§8), because nothing about that value's own verifiability changed.

## 8. `DEPLOY_ENABLED` and the ephemeral-resources tension

Every Azure resource this pipeline's `deploy` job depends on — the ACR,
the container app, the two federated identities themselves — is
ephemeral by this series' own standing rule: created for a session,
deleted at the end of it. A workflow that always tries to push an image
and update an app would fail on every push to `main` between sessions, for
a reason that has nothing to do with the code. `DEPLOY_ENABLED`, a
repository variable read in one job-level `if:` (`deploy`'s own top-level
`if`) plus three step-level `if:`s in the `image` job's Azure-touching
steps, is what lets the workflow tell those two situations apart.
`create-github-oidc.sh` sets it to `true` as its deliberately *last*
mutation — reached only after every earlier verification (role-assignment
read-backs, the environment read-back, the seven identifier secrets'
presence read-backs — §7's own value-verification limit, not repeated
here) has already come back clean — and `delete-github-oidc.sh` sets it to
`false` as its deliberately *first* mutation.

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
6. Delete the GitHub environment, every repository secret this script
   wrote (the seven identifiers, §7), and `DEPLOY_ENABLED` itself, the one
   repository variable — read back after each deletion. For the secrets,
   the read-back can only prove presence/absence, the same limit §7 states
   for creation; that is a complete check for what deletion needs, since a
   secret has no ambiguous soft-delete state the way an app registration
   does (step 5 above).

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

## 11. What the live session settled, and what is still open

The live session ran on **2026-08-20, japaneast**, against real Azure and real
GitHub state, and its redacted record is
`reviews/evidence/day25/2026-08-20-cicd-live-session.md` in the planning
repository. It is what turned this section from a list of open questions into
a settled/open split. Everything in the settled column below is an
**observation from that one session on that date**, not a general guarantee
about GitHub, Entra or Azure; single-session scoping is the standing rule in
this series, and it applies to these rows as much as to any measurement.

### Settled by that run

| Question | Observed |
|---|---|
| Does the OIDC token exchange succeed end to end, for both identities? | Yes. `azure/login@v2` obtained an Azure token for the build identity in `image` and for the deploy identity in `deploy`; Entra accepted both subjects. |
| Does the required-reviewer gate actually pause `deploy`? | Yes. An unapproved `deploy` job sat at `status: waiting`, `conclusion: null`, `steps: []` — no step had run, yet `started_at` was already populated — and `pending_deployments` reported `current_user_can_approve: true` for the same account that opened and merged the PR. Single-operator self-approval, with an API field to prove it. |
| Does the deployment-branch policy block a non-`main` run? | Yes, and automatically. A throwaway workflow on a side branch declaring `environment: production` went `waiting` → `failure` with `steps: []`, annotated `Branch "…" is not allowed to deploy to production due to environment protection rules`. Its `pending_deployments` was **empty**: nothing was ever routed to a human. |
| What happens if a job presents the deploy identity without declaring `environment:`? | GitHub mints a **ref**-form subject; Entra refuses that assertion with `AADSTS700213: No matching federated identity record found`. The refusal happens at the token exchange — the workflow holds a GitHub OIDC assertion but never obtains an Azure access token, so no Azure authorization is even attempted. |
| Does `Microsoft.App/containerApps/*/write` cover the bare `containerApps/write` that `az containerapp update --image` needs? | Yes. The update was accepted under `Container Apps Contributor` alone, and the app read back the requested image. |
| Does ARM/ACA accept a digest-form `--image`? | Yes. The update landed with a `@sha256:…` reference and the app ran it. |
| What does `az acr login` print under an `AcrPush`-only identity? | `Login Succeeded`, with no warning at all (§2). The control-plane fallback leaves no trace in the log. |
| Does the digest round-trip through a real `docker push` work? | Yes. `docker inspect`'s `RepoDigests[0]` was parsed, stripped to a bare `sha256:…`, crossed the job boundary intact under secret masking, and was what `deploy` addressed. |
| Do the GitHub REST response shapes the environment read-back depends on match what the API returns? | Yes. `create-github-oidc.sh` step 5's field-by-field comparison against `deployment_branch_policy` and `protection_rules[].reviewers` executed against the live API and passed. |

Two live bugs also came out of that run and are fixed in the code this
document describes: `az role assignment list` rejects `--all` together with
`--scope` (the flag is for the case where no scope is named), and the
revision poll's success allow-list was unsound — see the next row and
`update-container-app.sh`.

**The `runningState` vocabulary, and why the poll is failure-shaped.**
`update-container-app.sh` polls `properties.runningState`, and the field
choice was right — it is still the most specific signal
`az containerapp revision show` exposes. The *vocabulary* assumed for it was
not. A healthy, correctly-deployed revision reported `RunningAtMaxScale`, and
a second deployment in the same session reported `Activating`; neither value
appears in the `RevisionRunningState` enum shipped by the containerapp CLI
extension installed at the time (`azext_containerapp/_sdk_enums.py`, which
lists only `Running`/`Processing`/`Stopped`/`Degraded`/`Failed`/`Unknown`).
Two observations against that installed enum are enough to establish that it
could not be used as a complete success allow-list, which is why the poll is
now failure-shaped: it aborts on the enum's named failure states, keeps
waiting through `Processing`, and treats every other value — including
vocabulary this project has not seen — as not evidence of failure. It does
not establish what the service's full vocabulary is, nor that any particular
new value will appear next. What proves a deployment actually succeeded is
therefore not the poll: it is the combination the script performs around it —
the pre-mutation snapshot, the read-back that the app now carries the exact
requested digest, the revision reading active/provisioned, and step 4's
exact-body `/health` probe. See the comment above the poll in
`infra/scripts/update-container-app.sh` for the full account.

### Still open

- **`concurrency` behaviour under two competing runs** (§1) — untested; no
  two runs ever competed for the `production` group, and in particular
  whether a run waiting on approval counts as the queued `pending` slot was
  not exercised.
- **Single-revision behaviour when a revision fails to start.** This
  session's revisions both came up healthy. Because the app runs in single
  revision mode, this leaves a specific gap worth naming: it has not been
  observed whether a new revision that fails to pull its image would be
  caught by the checks above, or whether the previous revision would keep
  serving `/health` while the new one fails — which would make the probe
  return the expected body for the wrong reason. The digest read-back is a
  control-plane check and would still report what was requested. This is the
  one place where "the deployment succeeded" rests on an untested
  assumption.
- **Role-assignment propagation timing for this pipeline** — **not measured
  this run.** The first workflow run authenticated and pushed without an
  authorization failure, which is a single non-failure, not a measurement.
  The only propagation figure anywhere in this project remains Day 20's
  **14 minutes 44 seconds** against Microsoft's documented "up to 5 minutes"
  ([managed-identity.md §2](managed-identity.md#2-keyless-azure-openai-on-this-projects-own-client-shape)),
  and that number describes a *different* identity type (a managed identity
  reaching the Azure OpenAI data plane) under Day 20's own conditions — prior
  art for how far "up to 5 minutes" has been shown wrong, not a prediction
  for these two app-registration service principals.
- **The ABAC migration path** (§10) — neither `rbac-abac` registry mode nor
  the replacement roles have ever been created or tested in this series.

### One recorded, not fixed

`az containerapp env delete`'s completion signal failed for the second time,
in a second distinct way: Day 24 saw the command return while the environment
was still listed (`ScheduledForDelete`); this session saw the CLI's own
long-poll `GET` raise a client-side `ReadTimeout` (with `read timeout=None`)
while the server had in fact accepted the delete. Teardown aborted at step 2
and never reached the workspace, the role assignments or the identity. No
script change was made: what covers it today is an operator re-running the
script, which is idempotent. Recorded here because teardown order is a
contract, and an abort partway through it leaves more behind than an outright
failure would.
