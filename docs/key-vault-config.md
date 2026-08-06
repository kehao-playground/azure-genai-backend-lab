# Key Vault configuration guide (Day 20)

Day 20 is a documentation milestone: no application code changes. This guide records
how this project handles secrets today, what a Key Vault for this backend looks like
when one is warranted, and — the part most guides skip — why the honest answer for
this particular stack is that Key Vault should hold almost nothing.

Microsoft Learn pages cited here were checked 2026-08. Live findings come from a
one-day probe (2026-08-05, japaneast) whose scripts are in this repository
(`infra/scripts/create-keyvault.sh` / `delete-keyvault.sh`).

Companion document: [managed-identity.md](managed-identity.md) — the deployment
note that explains why most of the secrets below are scheduled to stop existing.

---

## 1. The secret surface of this backend, today

An inventory precedes any vault decision — you cannot decide where secrets live
before knowing which ones you have:

| Setting | What it unlocks | Actually a secret? |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | full data-plane access to the Azure OpenAI account | yes — and replaceable by a role assignment (see companion doc) |
| `AZURE_SEARCH_ADMIN_KEY` | full write access to the search service, index management included | yes — and replaceable by role assignments |
| `ENTRA_CLIENT_SECRET` | client credential for the Day 19 smoke tool | yes — but deliberately ephemeral (7-day expiry), never read by the server |
| `AZURE_OPENAI_ENDPOINT`, `INDEX_NAME`, `ENTRA_TENANT_ID`, … | configuration | no — endpoints, names and GUIDs are not credentials |

The last row matters as much as the first two. Key Vault's own guidance draws this
line explicitly: configuration belongs in configuration systems, and "IP addresses,
service names, feature flags, and other configuration settings should be stored in
Azure App Configuration rather than in Key Vault"
([secure-secrets](https://learn.microsoft.com/en-us/azure/key-vault/secrets/secure-secrets),
checked 2026-08). A vault that accumulates non-secrets stops being an inventory of
what is dangerous.

**Local development** keeps these in `.env` — gitignored, per-machine, per-developer.
That is a deliberate posture with a precisely stated boundary: the `.env` shrinks
the **exposure surface** (one machine, one gitignored file, one person), not the
**authorization radius** — a key that does leak replays from any host with the
full data-plane capability the inventory above describes. Local key auth is
accepted debt (see the managed-identity doc's rejected-alternatives discussion),
not a contained blast radius. The failure this guide must prevent outright is
the third place a secret can live: **code and container images.**
`Settings` (`core/config.py`) reads secrets from the environment as `SecretStr`;
nothing in the repository or image ever contains a value.

**Cloud runtime** is where Key Vault enters: the deployed app (Day 24, Azure
Container Apps) should receive whatever secrets still exist as Key Vault references
resolved by a managed identity — never as plaintext values pasted into deployment
configuration. The mechanics live in the companion doc, §4.

## 2. The vault this lab creates

`create-keyvault.sh` makes the decisions explicit rather than inheriting defaults
silently:

| Decision | Value | Why |
|---|---|---|
| Authorization model | **Azure RBAC** (explicit) | The default flipped: since control-plane API `2026-02-01`, new vaults default to `enableRbacAuthorization = true` ([access-control-default](https://learn.microsoft.com/en-us/azure/key-vault/general/access-control-default), checked 2026-08). Access policies are legacy and not recommended — anyone with `Microsoft.KeyVault/vaults/write` can grant themselves data access — but both models remain supported; the separate 2027-02-27 retirement applies to **pre-2026-02-01 control-plane API versions**, not to the access-policy model itself ([rbac-access-policy](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-access-policy), checked 2026-08). The script passes the flag explicitly and reads it back rather than inheriting any default. |
| Soft delete retention | 7 days (minimum) | Soft delete cannot be disabled; the retention window is also how long a deleted vault's globally-unique name stays reserved unless purged. Short retention keeps the lab's teardown honest. |
| Purge protection | **off** | Enabling it is irreversible and blocks early purge. Production wants it on; an ephemeral lab that promises "every create script has a teardown" cannot have it. This tension is real — state it, don't paper over it. |
| Data-plane access | explicit role assignment | Under RBAC, creating a vault grants **no** secret access, not even to the creator. The script assigns `Key Vault Secrets Officer` to the signed-in user; production runtimes get `Key Vault Secrets User` (read-only) instead. |

Two live findings from validating the script (2026-08-05):

- A subscription that has never held a vault fails with `MissingSubscriptionRegistration` —
  the `Microsoft.KeyVault` resource provider is not registered on fresh
  subscriptions. The script now registers it first.
- The topology rule from the docs is one vault **per application, per environment,
  per region** — blast-radius reasoning ("Grouping secrets into the same vault
  increases the blast radius of a security event",
  [secure-key-vault](https://learn.microsoft.com/en-us/azure/key-vault/general/secure-key-vault),
  checked 2026-08). For a multitenant product the same page adds: one vault per
  tenant. This lab needs exactly one vault, in one environment, in one region —
  which is why the script takes a name rather than inventing a naming scheme.

## 3. What actually belongs in this vault

Work the inventory from §1 against the keyless capabilities in the companion doc
and the result is uncomfortable for a guide named "Key Vault configuration":

- `AZURE_OPENAI_API_KEY` — replaceable by `Cognitive Services OpenAI User` on a
  managed identity. Verified live against this project's account (2026-08-05).
- `AZURE_SEARCH_ADMIN_KEY` — replaceable by Search RBAC roles, with a caveat: the
  admin key bundles index management and querying, and the RBAC split
  (`Search Service Contributor` / `Search Index Data Contributor` /
  `Search Index Data Reader`) is precisely the least-privilege improvement the
  single key cannot express. Not yet live-verified here (see companion doc §6).
- `ENTRA_CLIENT_SECRET` — belongs to the smoke *client*, not the server; it expires
  in 7 days by design and is never persisted.

So the steady-state contents of this lab's vault, once Day 24 deploys with a
managed identity, is **approximately nothing**. Key Vault, for this stack, is a
transition mechanism (the place a key lives between "pasted into deployment config"
and "no longer exists") and a home for the secrets that genuinely cannot be
replaced by an identity — a third-party SaaS API key, a signing key for something
Azure does not manage. The series keeps the vault scripts because that migration
period is real, and because "we have Key Vault" is the wrong reason to keep keys.

## 4. Rotation: what Key Vault does and does not solve

Rotation is where "put the key in Key Vault" quietly stops being a solution and
becomes plumbing you own. The pieces, each verified against current docs:

**The key side.** Both services issue two keys so one can be regenerated while
clients ride the other, but each half of that claim has its own source. For
Azure OpenAI / AI Services resources, regeneration is immediate and
unforgiving: "once a key is regenerated, the older version of that key stops
working immediately", and clients on the old key get 401
([rotate-keys](https://learn.microsoft.com/en-us/azure/ai-services/rotate-keys),
checked 2026-08). For Azure AI Search, two admin keys exist "so that you can
rotate a primary key while using the secondary key for business continuity",
only one can be regenerated at a time, and regenerating both at once leaves
clients failing with 403
([search-security-api-keys](https://learn.microsoft.com/en-us/azure/search/search-security-api-keys),
checked 2026-08). Regeneration semantics do not transfer across products —
each statement above stays inside its own page. What both share: storing "the
key" as a single vault secret makes zero-downtime rotation structurally
impossible; the dual-credential pattern needs both keys represented.

**The vault side.** Every `secret set` creates a new **version**; the old version
stays readable at its versioned URI (verified live: two enabled versions after one
rotation, 2026-08-05). A versionless URI resolves to latest. Nothing about that
updates a running client: the docs tell you to cache secrets in memory and to
"refresh them when secrets are rotated" — and provide no mechanism. The refresh
loop is yours.

**The notification side.** Key rotation *policies* exist for vault **keys**, not
secrets. Secrets get Event Grid events: `SecretNewVersionCreated`,
`SecretNearExpiry`, `SecretExpired`
([event-schema-key-vault](https://learn.microsoft.com/en-us/azure/event-grid/event-schema-key-vault),
checked 2026-08). Two traps: near-expiry fires a fixed **30 days** before expiry
(configurable for keys, not for secrets), and it fires only if the secret has an
expiration date set at all. A secret without `EXP` never warns.

**The runtime side.** Azure Container Apps closes the loop for its own secrets,
in two distinct layers ([manage-secrets](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets),
checked 2026-08). Layer one, the re-fetch: a Key Vault reference without a
pinned version retrieves "the latest version within 30 minutes" — that is
general to versionless references. Layer two, the restart: active revisions are
automatically restarted **specifically when they reference the secret in an
environment variable**; the cited sentence covers that consumption shape only,
and says nothing about volume-mounted secrets or scale-rule references. For
env-var consumption, rotation is therefore also an availability event — the
restart is the feature. Pinning a version opts out of the re-fetch entirely.

**The exit.** Every paragraph above is a cost that exists only because a key
exists. A managed identity has no key to regenerate, no version to pin, no
near-expiry event to miss. That is the strongest argument in the companion doc.

## 5. Cost

What the Azure Retail Prices API currently lists for japaneast (checked
2026-08; the public pricing page renders placeholders, so the API is the
citable source): Standard vault operations at **USD 0.03 per 10,000**, and no
standing per-vault meter in that query's results. Automated **key** rotation is
a metered event at USD 1.00 per rotation — secrets have no such meter because
they have no rotation policy to bill.

Scoped the way Day 9 taught: that is the current meter, not a bill. An idle
Standard vault with no operations has no listed standing meter to accrue on,
so its incremental vault-operation cost approaches zero — which is a statement
about today's price list, not a timeless free-tier guarantee, and it says
nothing about surrounding features (diagnostics, networking) a production
setup might attach. The billing authority remains Azure Cost Management and
the invoice. The lab keeps the vault ephemeral anyway — `delete-keyvault.sh`
deletes *and purges* — because the series' teardown discipline is about
reproducibility ("a reader can rebuild everything from scripts") as much as
spend, and because a standing vault with no secrets in it guards nothing.

## 6. Honest boundaries

- The probe behind this guide is one day, one subscription, one region. RBAC
  propagation happened to be near-instant for the vault role and took 14–15
  minutes for the Azure OpenAI role the same afternoon — treat propagation time
  as unbounded-ish, not as either measurement.
- This guide covers **secrets**. Key Vault keys (cryptographic material,
  HSM-backed options) and certificates have different lifecycle machinery —
  rotation policies exist for keys, renewal for certificates — and nothing here
  transfers to them without re-checking.
- The claim "this vault ends up nearly empty" is an architecture-level conclusion
  for *this* stack, where every Azure dependency is Entra-capable. A stack with
  one non-Entra dependency keeps a vault with real contents, and everything in §4
  applies to it in earnest.
