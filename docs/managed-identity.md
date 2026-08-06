# Managed Identity deployment note (Day 20)

The best secret is the one that does not exist. This note records how this backend
authenticates to Azure services without API keys, what `DefaultAzureCredential`
actually does (and why Microsoft now tells you to replace it in production), and
the concrete identity plan for the Day 24 Container Apps deployment.

Day 20 is a documentation milestone: the application still reads
`AZURE_OPENAI_API_KEY` from the environment, on purpose (§5). What changed is that
the keyless path is now verified against this project's own resources, and the
deployment posture is decided before the deployment exists.

Microsoft Learn pages cited here were checked 2026-08. Live findings: one-day
probe, 2026-08-05, japaneast, against the series' standing Azure OpenAI account
(kind `OpenAI`, deployment `chat-mini`).

Companion document: [key-vault-config.md](key-vault-config.md).

---

## 1. Control plane is not data plane — measured

The probe's negative control, before any role assignment: the caller **owns the
subscription**, and the v1 Responses API answered

```
401 PermissionDenied: The principal `<oid>` lacks the required data action
`Microsoft.CognitiveServices/accounts/OpenAI/responses/write`
to perform `POST /openai/v1/responses` operation.
```

Owner can create and delete the account; Owner cannot call the model. Inference is
a **data action**, granted by data-plane roles only — the Foundry troubleshooting
table states it flatly: "Owner or Contributor don't provide access either"
([configure-entra-id](https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-models/how-to/configure-entra-id),
checked 2026-08). If your mental model of Azure RBAC is "Owner can do everything,"
data actions are the correction.

## 2. Keyless Azure OpenAI, on this project's own client shape

One role assignment — `Cognitive Services OpenAI User`, scoped to the account —
and the exact client shape this backend has used since Day 4 works with a bearer
token in place of the key:

```python
from azure.identity import AzureCliCredential, get_bearer_token_provider
from openai import OpenAI

token_provider = get_bearer_token_provider(
    AzureCliCredential(), "https://cognitiveservices.azure.com/.default"
)
client = OpenAI(
    base_url="https://<account>.openai.azure.com/openai/v1/",
    api_key=token_provider,
)
```

Note what is passed: **the callable itself, not its result.** The pinned
`openai` 2.45.0 client accepts `str | Callable[[], str]` for `api_key`; given
the callable it re-invokes the provider on every request, which is the refresh
seam a long-running service needs when the first bearer token expires.
`api_key=token_provider()` would freeze a single token as a static string and
the service would start failing at that token's expiry.
`tests/unit/test_openai_callable_api_key.py` pins both behaviors against the
locked SDK, so an upgrade that changes either fails in CI before this document
goes stale.

Verified live (2026-08-05): `responses.create` with `store=False` returned 200 with
a normal `usage` block. No API key was involved anywhere in the call. The probe's
calls were short-lived, well inside one token's lifetime — token expiry and
refresh were **not** exercised live; the refresh claim rests on the pinned SDK
source and the regression test above.

Two findings worth their own paragraphs:

**The documented token scope conflict is real, and on this resource both answers
work.** Two current Microsoft pages disagree about the audience:
[keyless-connections](https://learn.microsoft.com/en-us/azure/developer/ai/keyless-connections)
says `https://cognitiveservices.azure.com/.default` (with the older `AzureOpenAI`
client), while the Foundry page above insists — three times — on
`https://ai.azure.com/.default` for exactly the v1 `OpenAI`-client shape used here.
Measured against this account (kind `OpenAI`): **both scopes returned 200.** The
conflict does not bite on this resource kind; no claim is made here about Foundry
(`AIServices`) resources, which is what that page is actually about. One near-miss
is instructive: the first `ai.azure.com` attempt returned 401 and only the retry
35 seconds later succeeded — a single probe would have "proven" the wrong
conclusion. Measurements adjudicate docs only when repeated.

**Role propagation took 14–15 minutes, against a documented "up to 5".** The
assignment was verified correct immediately (right principal, right role, right
scope); the data plane kept answering 401 — with a *different* message than the
no-role case — for 14 minutes 44 seconds before the first 200. The two token
audiences flipped to 200 about 30 seconds apart. One observation, one account,
one afternoon: a counterexample to "up to 5 minutes", not a new bound.

What follows from that is a **bounded readiness procedure**, not open-ended
patience. First verify every input once — principal object id, role, scope,
tenant, token audience, resource kind — because a control-plane listing proves
the assignment object exists, not that every data-plane input is right. Then
retry the live call with backoff against a deadline (this probe would have
needed ~15 minutes; pick a deadline you can defend, and stop churning
known-good configuration while it runs). A 401 inside the window *may* be
propagation; it is never proof the configuration is correct. Past the
deadline, stop waiting and diagnose — at that point the odds have shifted from
propagation to one of the inputs being wrong.

The same follows for revocation, with the sign flipped and worse: managed identity
tokens are cached "for around 24 hours" per resource URI and "Forcing a token
refresh isn't supported"
([managed-identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity),
checked 2026-08). Removing a role does not promptly remove access a live token
already grants.

## 3. `DefaultAzureCredential`: what the convenience costs

`DefaultAzureCredential` tries a chain of credential sources and uses the first
that yields a token. The Python chain, in order
([credential-chains](https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication/credential-chains),
checked 2026-08; the .NET chain differs — do not transplant):

Environment → WorkloadIdentity → ManagedIdentity → SharedTokenCache (Windows) →
VisualStudioCode → **AzureCli** → AzurePowerShell → AzureDeveloperCli →
(InteractiveBrowser, off by default) → Broker.

On a developer laptop the chain almost always resolves to `AzureCliCredential` —
which means the effective identity is *whatever `az login` last left behind*. The
probe captured the failure mode this produces. Same code, same machine, one change:
the az default context pointed at a different tenant. The result was not a clean
"wrong tenant" error but a `ClientAuthenticationError` wrapping **every chain
member's failure** — seven credential reports, of which the relevant one
(`AzureCliCredential: AADSTS50020`, user unknown in the vault's tenant) is buried
mid-wall. Microsoft's own list of tradeoffs names exactly this: debugging
challenges, performance overhead, unpredictable behavior — and its production
guidance is now explicit: "replace `DefaultAzureCredential` with a specific
`TokenCredential` implementation, such as `ManagedIdentityCredential`."

The production-side warning story comes from Microsoft's **.NET** best-practices
guidance (the same guidance family as the credential-chain pages, checked
2026-08): managed identity briefly fails on a production host where someone once
ran `az login`, the chain silently falls through, and the app now runs as a
human. Treat it here as a cross-SDK caution about first-token-wins, not as a
reproduced Python fact — this project has not demonstrated that specific
continuation on the pinned `azure-identity`, whose chain differs from .NET's
(the wall-of-failures capture above is the same *mechanism*, observed from the
opposite side: no credential succeeded, so every member reported). The Python
guidance page independently supports the conclusion that matters — explicit
credentials in production — on its own three tradeoffs.

This project already has the structural answer: adapters are chosen at **one
composition point**, from configuration, at startup (Day 4 for fake/real, Day 19
for headers/entra). Credentials get the same treatment — deliberate selection per
environment, not runtime discovery:

- **Local dev**: `AzureCliCredential`, explicitly. The identity is the developer's,
  and saying so in code makes the wrong-tenant failure a one-line error.
- **Production**: `ManagedIdentityCredential`, explicitly (for user-assigned:
  `ManagedIdentityCredential(client_id=...)` — the client ID must be passed, it is
  not discovered).
- **If `DefaultAzureCredential` must stay** (e.g. a tool that genuinely runs in
  many environments): pin it with the `AZURE_TOKEN_CREDENTIALS` environment
  variable — `prod`/`dev` categories need azure-identity ≥ 1.23.0, single
  credential names (e.g. `ManagedIdentityCredential`) need ≥ 1.24.0, and
  `DefaultAzureCredential(require_envvar=True)` turns "env var forgotten" into a
  startup failure instead of a silent full chain. Fail-fast at the composition
  point, same as every other Day.

## 4. The Day 24 identity plan (this is the deployment note)

Decided now, executed when `deploy-container-app.sh` stops being a placeholder:

- **User-assigned managed identity**, created by script *before* the app. The
  documented constraint decides this: "System assigned identity can't be used with
  the create command because it's not available until after the container app is
  created" ([manage-secrets](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets),
  checked 2026-08). A user-assigned identity makes create-assign-deploy one
  idempotent sequence; system-assigned forces two phases with a role-assignment
  step wedged between.
- **Role assignments on that identity**: `Cognitive Services OpenAI User` on the
  Azure OpenAI account (verified sufficient for `responses.create`, §2), and the
  Search data-plane roles once Search RBAC is measured (§6). Assignments happen at
  script time, and the script ends with the §2 readiness gate — verify the
  inputs once, then probe the data plane with backoff to a deadline — so the
  propagation window closes before any revision boots, and a deadline overrun
  surfaces as a deploy-script failure instead of a mystery 401 in production.
- **Secrets, if any remain**, arrive as Key Vault references
  (`keyvaultref:<secret-uri>,identityref:<identity-id>`) resolved by the same
  identity holding `Key Vault Secrets User` — with the rotation/restart semantics
  described in the companion doc §4.
- **If the identity ends up used only for Key Vault / registry pulls** and the
  app code itself goes fully keyless, `identitySettings` lifecycle `None` removes
  the identity's tokens from the app container entirely — the platform reads the
  secret, the code cannot mint tokens. Least privilege applies to identities, not
  just roles.

What this deletes, per the companion doc: key regeneration ceremony, secret
versions, near-expiry plumbing, the 30-minute versionless re-fetch and the
env-var-scoped rotation restart that rides on it. The keyless path is not
(only) a security posture — it is the removal of an entire operational
category.

## 5. Why the lab still runs on keys today

Honesty about the gap between posture and practice:

- Local development has no managed identity, and this series' daily loop is local.
  `.env` + key is the low-friction local path; the alternative (every developer
  action minting Entra tokens) buys little on a laptop that already holds `az login`
  tokens for the same resources.
- `disableLocalAuth: true` — the switch that makes keyless the *only* path — is
  deliberately not flipped on the standing account. Microsoft's own caveat: the
  change "doesn't take effect immediately … it can take up to several hours"
  ([disable-local-auth](https://learn.microsoft.com/en-us/azure/ai-services/disable-local-auth),
  checked 2026-08). An eventually-consistent kill switch on the resource every
  smoke test depends on is a bad trade for a lab; for production it is the right
  final step *after* clients are verified keyless.
- The application code path for bearer tokens (token provider at the composition
  point) is a Day 24 change, landing with the deployment that needs it — not
  before, per this series' rule against speculative abstraction.

## 6. Honest boundaries

- **No managed identity was live-tested** — there is no deployed compute yet. The
  probe's identity was a user via `AzureCliCredential`; token acquisition and RBAC
  evaluation follow the same path, but IMDS behavior, the 24-hour token cache, and
  user-assigned client-ID wiring are documented claims here, measured claims only
  after Day 24.
- **Search keyless is asserted from docs, not measured.** Current pages even
  disagree on whether Free tier supports RBAC ("any tier, including free" vs
  "must be a billable tier (basic or higher)", both checked 2026-08). The series'
  Day 13 discipline applies: conflicting docs get a measurement, not a coin flip —
  deferred until a Search milestone needs the service live anyway.
- **Propagation numbers are single observations.** 14–15 minutes happened once, on
  one account, one region, one afternoon; the vault role the same day propagated
  in under a minute. The transferable lesson is the verification loop, not the
  numbers.
