# Infrastructure Evolution: CLI Scripts First, Bicep Later

This repository provisions Azure with 24 bash scripts and one Bicep template.
That ratio is not a to-do list. This page explains how it was arrived at, what
was measured to arrive at it, and what would have to change for it to change.

The short version: **the question was never whether Bicep can express this
lab's infrastructure — it very nearly all can. The question is whether this lab
should hand over the whole lifecycle, and the part it cannot hand over is
exactly the part these scripts spend most of their lines on.**

## Where the lines actually go

| | |
|---|---|
| Scripts / total lines | 24 / 5,214 |
| Call sites that create an ARM resource | 16 |

Sixteen creations, five thousand lines. It is tempting to read that as "look
how much ceremony one `az` call needs", and it is tempting in the other
direction too — "sixteen resources, that is one small template". Both readings
are wrong, and the second one is wrong in a way worth naming.

**Counting by the verb `create` badly understates what a declarative tool could
own.** Role assignments, APIM policies, APIM subscriptions, a container app's
image update, Entra directory objects — all of them are ARM or Graph state, all
of them are declarable, and none of them appear as `az <group> create`. The
APIM `demo-client` subscription is created with `az rest --method put`; no
inventory built on create-verbs sees it at all. Scale and capability are
different measurements; the first table is the scale, and the next one is the
capability.

What most of the other five thousand lines do: fail-closed read-back after
nearly every mutation (an empty answer aborts), ordering contracts (role
assignments before the identity, federated credentials before the app
registration), per-run unique naming (a cleanup step that identifies its
resources by a fixed name will happily purge a concurrent run's), three
different soft-delete purge semantics, and recovery paths for half-finished
state. `delete-github-oidc.sh` is 852 lines — 167 longer than the script that
creates what it destroys.

Those categories do not disappear automatically by adopting Bicep. The
provisioning commands and their create-side read-backs are the part a template
does absorb — [`main.bicep`](../infra/bicep/main.bicep) already carries
`create-openai.sh`'s provisioning shape — but purge, ordering, secret
generation and the verification around them are the cost of tearing down
cleanly and of noticing when something did not happen, not the cost of
standing things up.

## What is declarable, and what is not

Two separate columns, because they answer two separate questions. The last
column keeps two kinds of value apart: an **evidence grade** for the stack
verdict — *inferred no* (reasoned from documented `actionOnUnmanage`
behaviour), *documented no* (official limitation), *not verified* (no grounds
either way), or *n/a* (a stack has no jurisdiction: not ARM, or not state) —
and a **dependency**, written *follows &lt;parent&gt;*, meaning the row's
lifecycle rides on its parent and the stack verdict is the parent's.

| Object | Plane | Declarable | Secret output | Teardown needs | Teardown via stack? |
|---|---|---|---|---|---|
| Resource group | ARM | yes | — | delete | not verified |
| Budget alert | ARM | yes | — | delete | not verified |
| AOAI account + 2 model deployments | ARM | yes — [`main.bicep`](../infra/bicep/main.bicep) | account key | delete + **purge** | inferred no |
| Role assignments | ARM | yes | — | before the principal | **not verified** |
| User-assigned managed identity | ARM | yes | — | delete (after the row above) | **not verified** |
| AI Search service | ARM | yes | admin key (`listAdminKeys`) | delete | not verified |
| Container registry | ARM | yes | — (OIDC) | delete | not verified |
| Log Analytics workspace | ARM | yes | — | delete — **and it gets created implicitly if you do not declare it** | not verified |
| Container Apps environment + app | ARM | yes | — | delete | not verified |
| Container app image update (by digest) | ARM | yes | — | with the app | follows the app |
| Key Vault | ARM | yes | — | delete + **purge** | inferred no |
| Key Vault secret and its value | ARM child | yes | yes | with the vault | **documented no** — stacks cannot delete Key Vault secrets; removal needs detach handling (see below) |
| Content Safety account | ARM | yes | account key | delete + **purge** (48h name lock) | inferred no |
| APIM service | ARM | yes | — | delete + purge | inferred no |
| APIM API + policy XML | ARM child | yes (`loadTextContent()`) | — | with APIM | follows APIM |
| APIM `demo-client` subscription | ARM child | yes | yes (`listSecrets`) | with APIM | follows APIM |
| Entra app / SP / federated credential / app role / grant | Graph | yes (Graph extension) | — | delete + recycle bin purge | **documented no** |
| Entra client secret | Graph | **no** | yes | with the app | follows the app (Graph as a class: documented no) |
| Search index schema | **data plane** | no | — | with the service | n/a — data plane |
| Container image build | build action | no | — | with the registry | n/a — an action, not state |
| GitHub environment / secrets / variable | GitHub API | **no** | yes | manual | n/a — not Azure |

Three things fall out of this table.

**The declarable column is almost entirely yes.** "Bicep cannot handle this
lab" is not true. That includes the row people assume is a no: Entra
applications, service principals and federated identity credentials all have
Graph Bicep types.

**What is genuinely not declarable clusters in three kinds of state** — a
data-plane schema, the *generation* of secrets, and systems that are not
Azure — plus one row that is not state at all: the container image build is an
action, and the declarative question does not apply to it.

**The dividing line is the last column, and no cell in it carries first-hand
evidence — but the grades differ.** Purge-bearing resources are an *inferred*
no, Graph objects and Key Vault secrets are a *documented* no, and the rest is
*unverified*: three different strengths, none of them observation, because
this repository has never run a deployment stack. See
[The two axes](#the-two-axes).

Two rows are worth reading closely, because both were misclassified on the way
here. **Key Vault secret values are not data-plane-only**:
`Microsoft.KeyVault/vaults/secrets` is a control-plane child resource and
`properties.value` is declarable. The real constraints are different ones —
retrieving a key with `listAdminKeys` to put it there is a resource function
`what-if` cannot evaluate, a literal value in a template lands in the
deployment history, and the teardown side has its own documented limitation:
**deployment stacks cannot delete Key Vault secrets** (checked 2026-08,
[stacks known issues](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deployment-stacks-known-issues));
removing an explicitly managed secret requires detach handling. The three CLI
`actionOnUnmanage` presets are not the whole schema — the
`Microsoft.Resources/deploymentStacks` type separately exposes
`resourcesWithoutDeleteSupport: detach | fail` for exactly this class of
resource. None of that implies the parent vault cannot be deleted. And the
**APIM subscription** row was missing entirely from the first pass, for the
create-verb reason above.

## The two axes

Handing infrastructure to a template is two decisions, not one.

**Axis 1 — provisioning ownership.** Create and update move to Bicep; teardown
stays in the scripts. Conditions: (a) a type can express it, (b) the properties
you care about are not resource functions or generated secrets, (c) changes can
be previewed — `what-if` supports the resource type, where documented coverage
is the bar and a live run only raises the evidence strength.

**Axis 2 — full lifecycle ownership.** Teardown moves too, via deployment
stacks. Axis 1 plus: (d) the teardown semantics fit `actionOnUnmanage` — no
purge, no ordering that spans systems — and (e) ownership is traceable.

Collapsing these into one switch produces a conclusion that sounds decisive and
is wrong: *this lab needs `purge`, therefore Bicep is not for this lab.* Purge
is an axis-2 problem. It says nothing about whether a template should create
the account.

Against the table above: **AOAI, the budget and the resource group clear axis
1**, and AOAI is now across it. The evidence is not uniform, so it is worth
separating — AOAI has a live `what-if` behind it (below); the budget and the
resource group have types and documentation and no live probe. **Graph objects
do not clear it**: they pass (a) through the Graph extension, but `what-if`
does not support Graph resources, so (c) fails — declaring them buys creation
without preview.

**Axis 2 has nothing on it.** Purge-bearing resources are an inferred no; Graph
objects are a documented no — deployment stacks do not support them, and
neither does `what-if` — as are Key Vault secrets; and everything else is
unverified. Unverified is not
the same as refuted; it means there are no grounds to decide yet, and teardown
is the wrong place to guess. Deployment stacks default to `detachAll`: get
`actionOnUnmanage` wrong and the thing you meant to delete quietly keeps
running and keeps billing.

## What a live `what-if` actually shows you

Measured 2026-08-20 in japaneast against the running lab resource group, with
[`main.bicep`](../infra/bicep/main.bicep)'s predecessor — a template declaring
exactly what `create-openai.sh` declares, nothing else. Nothing was deployed;
no resource changed.

**`what-if` reported 3 resources as `Modify` and 12 property removals, against
a resource group nobody had touched.** The usual explanation is "template
noise — those are server-side properties the template does not set", and that
explanation is partly true and partly a trap. Sorting the 12 gave three
different lines:

- **Properties the template omitted but the API genuinely accepts.**
  `deploymentState` is documented as controlling "whether the deployment is
  accepting inference requests", values `Paused` and `Running`. That is not
  cosmetic drift.
- **Fields no current type or export covers** (`a365LoggingEnabled`,
  `a365Status`) — high-confidence noise.
- **Effect at actual deployment: untested.** Finding out means deploying, on a
  live resource, against properties governing model behaviour.

The distinction between the first two lines was settled with a control test,
not by argument. Bicep raises `BCP073` for a genuinely read-only property —
`endpoint` and `provisioningState` produce three of them. Declaring
`currentCapacity` and `deploymentState` produces **zero diagnostics**. They are
writable. The template just does not say anything about them, and `what-if`
reports silence as removal.

`what-if` is worth having. Read the property-level output as a claim to check,
not as a verdict.

## Three more things measured the same day

**`az group export` fails open.** It emitted a `WARNING` and two `ERROR` lines
and exited **0**. Any script that trusts the exit code gets a partial template
and no indication. Microsoft's own documentation says export "is not a reliable
way to turn pre-existing resources into templates that are usable in
production" — take that at face value.

**The export returned 6 resources for 3 declared ones.** The extras are
`defenderForAISettings` and two `raiPolicies`, created implicitly by Azure,
carrying full content-filter configuration. Same lesson as Day 24's implicit
Log Analytics workspace: things exist that you did not declare, and a teardown
that only knows about your declarations does not know about them.

**Types have holes, and they are not simply "the newest".** Bicep CLI 0.46.1
has no types for the two newest versions the provider advertises — expected
lag — and also none for `2023-06-01-preview`, an isolated gap with typed
versions on both sides. An API version without types still compiles: `BCP081`,
and no property checking at all. Pin a version with types, deliberately, and
record the date you checked. `main.bicep` does.

One counter-example, in the interest of not stacking the deck: `az bicep
decompile` on that export produced **one** boilerplate disclaimer and **zero**
Bicep diagnostics. "Export then decompile gives you a mess" did not hold here.

There is also what looks like a **read-direction twin of a Day 24 bug**.
`az cognitiveservices account show` does not return `a365LoggingEnabled` or
`a365Status`, but a raw ARM `GET` at the same api-version the CLI itself uses
does — so the drop happens somewhere in the CLI's own pipeline, and the
likeliest seam is the SDK's typed response model, though the capture does not
pin the exact drop site. Day 24 was the same seam facing the other way, with
source-level evidence: an omitted YAML field materialised as an explicit
`null` on the way in. In both directions something typed sits between you and
the API, and in both directions what you see is not what ARM said.

## What crossing axis 1 does not buy you

`main.bicep` now owns creating and updating the Azure OpenAI account.
`delete-openai.sh` is untouched, and everything below stays in bash regardless
of how much else converts:

- **Purges** — Cognitive Services, Key Vault, Content Safety, APIM. Purge is an
  operation, not a desired state.
- **Secret generation** — the Graph extension does not support
  `passwordCredentials`; the documented workaround is a deployment script.
- **Value-retrieval chains** — `listKeys` / `listSecrets` into a vault work,
  but `what-if` cannot evaluate them, so the step you most want previewed is
  the one that is not.
- **The search index schema** — data plane.
- **GitHub** — not ARM.
- **The fail-closed read-backs that guard what the scripts still own.** The
  create/update read-back for a converted resource is absorbed by ARM's own
  deployment outcome; the read-backs around purges, ordering and cross-system
  steps stay.

## When to revisit

Conditions, not dates. Axis 1: a type exists, the properties you care about do
not come from resource functions, and `what-if` covers the type — convert, and
do not wait for the teardown question to have an answer. Axis 2: someone has to
run a deployment stack against these resource types first, and then (d) and (e)
have to hold.

A [bounded live probe](../infra/bicep/README.md#roadmap) for the managed
identity and its role assignments is the next step on axis 2 — dedicated
resource group, per-run unique names, least-privilege role, resource-id
inventory before and after, cleanup on failure, plus the stack-specific
`what-if` Microsoft published on 2026-08-14 as a pre-mutation check (the
stacks known-issues page still said it was unavailable on the same date —
checked 2026-08; the probe records that conflict instead of trusting either
page). It closes two rows. It does not generalise to purge-bearing resources
or to Graph.

And the honest caveat about all of this: **this lab is unusual.** Its
infrastructure is ephemeral by design, because it runs under a US$20/month
ceiling and most of it exists only for the length of a test session. That is
what puts so much weight on teardown. If your resources are permanently on,
your teardown is a rare event and your drift is a daily one — axis 1 should
have been crossed a long time ago, and axis 2 is a much easier question than it
is here.

## References

- [`infra/bicep/README.md`](../infra/bicep/README.md) — the template, how to
  run it, the roadmap
- [`infra/scripts/`](../infra/scripts/) — everything else
- [Container Apps](container-apps.md) — the Day 24 deployment and its teardown
  ordering
- [CI/CD Pipeline](ci-cd.md) — why deployment is digest-pinned and gated
- [Managed Identity](managed-identity.md) — the keyless path this template
  leaves room for
