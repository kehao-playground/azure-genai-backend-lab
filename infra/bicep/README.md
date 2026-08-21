# Bicep

`main.bicep` declares the Azure OpenAI account and its two model deployments —
a translation of [`../scripts/create-openai.sh`](../scripts/create-openai.sh).
It is the only Bicep in this repository, and that is a decision rather than a
backlog item. [`docs/infra-evolution.md`](../../docs/infra-evolution.md) has the
full reasoning; this file is the operational part.

## Using it

```bash
az deployment group what-if \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --template-file infra/bicep/main.bicep \
  --parameters openAiName="$AZ_OPENAI_NAME" location="$AZ_LOCATION"

az deployment group create \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --template-file infra/bicep/main.bicep \
  --parameters openAiName="$AZ_OPENAI_NAME" location="$AZ_LOCATION"
```

To check the template without an Azure call:

```bash
az bicep build --file infra/bicep/main.bicep --stdout > /dev/null
```

Verified with azure-cli 2.89.0 and Bicep CLI 0.46.1 (2026-08-21): zero
diagnostics.

Deleting is still `../scripts/delete-openai.sh`. See "The two axes" below.

## The two axes

Handing infrastructure to a template is not one decision. It is two, and this
repository has crossed one of them.

**Axis 1 — provisioning ownership.** Create and update move to Bicep; teardown
stays in the scripts. A resource qualifies when:

1. a Bicep type can express it,
2. the properties you care about are not resource functions (`listKeys`,
   `listSecrets`) or generated secrets, and
3. changes can be previewed — `what-if` supports the resource type.
   Documented coverage is the bar; a live `what-if` run raises the evidence
   from documentation to observation but is not the entry condition.

**Axis 2 — full lifecycle ownership.** Teardown moves too, via deployment
stacks. Axis 1 plus:

4. the teardown semantics fit `actionOnUnmanage` — no purge step, no ordering
   that spans systems, and
5. ownership is traceable.

Axis 1 and axis 2 are not the same question, and answering them together is how
you end up concluding that a repository which needs `purge` cannot use Bicep at
all. It can. It just cannot hand over the ending.

## Where this repository stands

| | Axis 1 | Axis 2 |
|---|---|---|
| AOAI account + deployments | **crossed** — `main.bicep`, backed by a live `what-if` | inferred no: deletion requires a separate purge |
| Budget alert, resource group | conditions met (types + docs only; no live probe run) | not verified |
| Managed identity, role assignments | conditions met (types + docs only) | **not verified** — see below |
| Key Vault, Content Safety, APIM | conditions met | inferred no: each needs a purge |
| Entra app / SP / federated credential | **not met** — condition (a) holds via the Graph Bicep extension (docs only, never exercised here), but `what-if` does not support Graph resources, so (c) fails; declaring them buys creation without preview | **no** — deployment stacks do not support Graph resources |
| Client secrets, search index schema, GitHub environment and secrets | **no** — generated secrets, a data-plane schema, and a non-Azure API | no |

Axis 2 has no ✅ anywhere in this table, and the honest reason is not that
deployment stacks fail here. It is that **this repository has never run one**.
"Not verified" is not "does not work" — it is a statement about what may be
used as grounds for a decision, and teardown is the part of this lab least
suited to being guessed at. Day 24 spent a live session discovering that
`env delete` returns while the environment is still listed; Day 25 discovered
that `az role assignment list` rejects `--all` together with `--scope` only by
running it.

The managed identity and role assignment rows are the ones most likely to move.
The scripts must delete role assignments before the identity, because a script
can only find the assignments by querying the principal id, and that handle
disappears with the identity. A stack tracks resource ids and does not depend
on that lookup, so it may not need the same ordering — which is a hypothesis, and
the reason a bounded live probe for exactly those two rows is on this roadmap.

## What stays in the scripts even after axis 1

Converting more resources shrinks the scripts far less than it looks. These do
not become declarative at any point on this roadmap:

- **Purges.** Cognitive Services accounts, Key Vaults, Content Safety accounts
  and APIM services are all soft-deleted first. Purge is an operation, not a
  desired state, and Content Safety additionally holds the name for 48 hours.
- **Secret generation.** The Graph Bicep extension does not support
  `passwordCredentials`; the documented workaround is a deployment script,
  which is a shell script wearing a template as a coat.
- **Value-retrieval chains.** Reading a search admin key with `listAdminKeys`
  and writing it into a vault is expressible, but `what-if` cannot evaluate
  those functions, so precisely the step you would want previewed is the step
  that is not.
- **The search index schema**, which is a data-plane object.
- **The GitHub side** — environments, secrets, variables. Not ARM at all.
- **The fail-closed read-backs that guard what the scripts still own.** A
  conversion does retire some read-backs — the create/update verification for
  a resource that moves into a template is absorbed by ARM's own deployment
  outcome. What stays is every read-back around purges, ordering and
  cross-system steps: the cost of noticing when something did not happen, in
  exactly the operations that never become declarative.

## Deliberately not in CI

The [CI pipeline](../../.github/workflows/ci.yml) does not build this template.
It has no deployment consumer — nothing in CI or in `deploy` reads it — so a
gate would mean installing Bicep on the runner to protect an artifact that
nothing consumes. The build command above is in this file instead, with the
toolchain versions it was verified against.

Revisit when the template gains a consumer, not before.

## Roadmap

Trigger conditions, not dates:

1. **A live deployment-stack probe** covering the managed identity plus its
   role assignments: a dedicated resource group, per-run unique names, a
   least-privilege role, a resource-id inventory taken before and after, and a
   cleanup path for failure. It closes those two rows and nothing else — no
   extrapolation to purge-bearing resources or to Graph. The probe should also
   run the deployment-stack-specific `what-if` Microsoft published on
   2026-08-14 as a pre-mutation check — noting that the stacks known-issues
   page still said stack `what-if` was unavailable on the same date (checked
   2026-08): record the conflict, do not silently trust either page.
2. **Budget and resource group to axis 1.** Their conditions already hold —
   types exist and `what-if` coverage is documented — so crossing is a matter
   of writing the declaration. A live `what-if` would upgrade the evidence
   from documentation to observation; it is not the entry condition.
3. **Reassess when a resource stops being ephemeral.** The whole shape of this
   decision comes from resources that exist for the length of a test session
   under a US$20/month ceiling. For a team whose resources are permanently on,
   axis 1 should have been crossed long ago.
