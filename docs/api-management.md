# API Management as the GenAI Gateway

Day 10 adds the layer the application cannot provide for itself. The Day 9 guardrails (per-call `max_output_tokens`, per-conversation token budget) bound what *this backend* spends — but they only apply to traffic that goes through this backend. A leaked model-endpoint credential bypasses every one of them. Azure API Management (APIM) moves the question "who may spend money on the model" out of key possession and into gateway policy.

## Topology: what sits behind the gateway

Two placements are commonly conflated:

1. **APIM in front of the FastAPI app** — protects *our* API (auth offload, per-client throttling of `/chat`). Relevant from Day 19 (identity) onward.
2. **APIM in front of the model endpoint** — the *AI gateway* pattern: this backend (and any future service) calls Azure OpenAI **through** APIM instead of directly. Central credential custody, per-consumer throttling, and token metrics for every model consumer in the organization.

This milestone implements placement 2, because it is the one that changes the security model: after it, **no application holds a model credential at all**. The gateway authenticates to Azure OpenAI with its own system-assigned managed identity (`authentication-managed-identity` with resource `https://cognitiveservices.azure.com`, role `Cognitive Services OpenAI User` on the account — [policy doc](https://learn.microsoft.com/en-us/azure/api-management/authentication-managed-identity-policy), checked 2026-07). Clients authenticate to the gateway with APIM subscription keys (`Ocp-Apim-Subscription-Key` header); one client = one subscription, revocable and throttled independently. Whatever credential a client sends is stripped and replaced at the gateway (see `infra/apim/genai-api-policy.xml`).

The gateway fronts the **v1 GA endpoint**: the passthrough API maps `https://<apim>.azure-api.net/genai/*` onto `https://<aoai>.openai.azure.com/openai/v1/*`, so `/responses` (this project's API base since Day 5) works unchanged — APIM's AI gateway supports the Responses API schema, and the Foundry import wizard has a dedicated "Azure OpenAI v1" compatibility option ([import doc](https://learn.microsoft.com/en-us/azure/api-management/azure-ai-foundry-api), checked 2026-07). This repo scripts the equivalent setup explicitly (`infra/scripts/configure-apim.sh`) instead of using the wizard, so every moving part is visible and teardown-able (`delete-apim.sh`).

## What the Consumption tier gives up (checked 2026-07)

Cost policy pins this series to the Consumption tier (per-call billing, first 1M calls/month free — [pricing](https://azure.microsoft.com/en-us/pricing/details/api-management/)). That choice has teeth — several marquee AI-gateway policies are not available there:

| Capability | Policy | Consumption tier |
|---|---|---|
| Per-subscription call rate limit | [`rate-limit`](https://learn.microsoft.com/en-us/azure/api-management/rate-limit-policy) | ✅ supported |
| Per-subscription call quota | [`quota`](https://learn.microsoft.com/en-us/azure/api-management/quota-policy) | ✅ supported |
| Token usage metrics | [`llm-emit-token-metric`](https://learn.microsoft.com/en-us/azure/api-management/llm-emit-token-metric-policy) | ✅ supported (needs Application Insights) |
| Managed-identity backend auth | [`authentication-managed-identity`](https://learn.microsoft.com/en-us/azure/api-management/authentication-managed-identity-policy) | ✅ supported |
| **Token** rate limit / token quota | [`llm-token-limit`](https://learn.microsoft.com/en-us/azure/api-management/llm-token-limit-policy) | ❌ not supported |
| Arbitrary-key rate limit (per IP etc.) | [`rate-limit-by-key`](https://learn.microsoft.com/en-us/azure/api-management/rate-limit-by-key-policy) | ❌ not supported |
| Semantic caching | `llm-semantic-cache-lookup`/`-store` | ❌ not supported (needs external Redis anyway) |

Consequences accepted here:

- **Throttling is call-based, not token-based.** One call can be 10 tokens or 100k. The token dimension stays where Day 9 put it: application-level metering of the provider-reported `usage` block. On a dedicated/v2 tier, `llm-token-limit` would add gateway-level TPM per subscription key (returning 429 with `Retry-After` and `remaining-tokens` headers); note that under streaming it *estimates* completion tokens rather than counting them.
- **Per-client granularity comes from subscriptions, not keys-in-policy.** `rate-limit-by-key` (throttle by IP, JWT claim, any expression) needs a dedicated tier; on Consumption the unit of throttling is the subscription — which is exactly the per-client unit this design wants anyway.
- **All APIM throttles are approximate.** Counters are tracked per gateway instance and never aggregated across instances ([rate-limit doc](https://learn.microsoft.com/en-us/azure/api-management/rate-limit-policy), checked 2026-07). Gateway limits are spike protection, not accounting — the authoritative spend record remains Cost Management (Day 9's rule, unchanged).

Note on naming: `llm-token-limit` / `llm-emit-token-metric` are the current, provider-agnostic policy names; `azure-openai-token-limit` / `azure-openai-emit-token-metric` are older provider-specific equivalents. The "AI gateway" is a capability set of APIM, not a separate product or SKU ([AI gateway overview](https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities), checked 2026-07).

## The policy

`infra/apim/genai-api-policy.xml`, applied at API scope by the setup script:

- `rate-limit calls="5" renewal-period="60"` — demo values chosen so a curl loop trips 429 within one minute; production values must come from observed traffic.
- `authentication-managed-identity` + `Authorization` override — the gateway's own Entra token replaces whatever the client sent.
- `set-header name="api-key" exists-action="delete"` — a client-supplied model key never reaches the backend even if someone sends one.

**Verified gotcha (live, 2026-07):** an API created with `az apim api create` has `subscriptionRequired=false` by default — the portal import wizard enables it, the CLI does not. Until `--subscription-required true` was added to the setup script, a keyless request passed straight through to the model (HTTP 200): the gateway existed but was an open proxy. First acceptance test for any gateway deployment: call it **without** credentials and expect 401.

Error-shape caveat: a gateway-produced 429 is APIM's error shape, not this project's error envelope. The application only speaks the Day 3 envelope for errors it produces; documenting gateway-versus-application error shapes for clients is part of the Day 19+ auth milestone, when placement 1 becomes real.

## Layered guardrails after Day 10

| Layer | Bounds | Unit | Timing |
|---|---|---|---|
| App: `max_output_tokens` | one reply | tokens | at the call |
| App: conversation budget | one conversation | tokens | before inference |
| Gateway: `rate-limit` (+ optional `quota`) | one client (subscription) | calls | at the gateway, approximate |
| Model deployment quota | one deployment | TPM | upstream |
| Cost Management budget alert | subscription | currency | delayed notification only |

Each layer bounds a failure mode the others cannot see: the app cannot see other consumers; the gateway cannot see token spend (on this tier); the budget alert cannot stop anything, only report late.
