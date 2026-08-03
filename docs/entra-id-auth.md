# Microsoft Entra ID authentication (Day 19)

Day 19 replaces "the gateway told us who this is" with "we verified who this is."
The `Principal` that Day 15 introduced does not change shape; what changes is where
its three fields come from, and whether anything cryptographic stands behind them.

Everything below is true of the code in this repository. Where a limit is real but
uncomfortable, it is stated rather than rounded off — that is the point of the
honest-boundary sections (8a, 8c, 9).

Microsoft Learn pages cited here were checked 2026-08.

---

## 1. Two modes, one dependency

`require_principal` (`src/azgenai_lab/api/principal.py`) is the single identity
boundary for every protected endpoint. Behind it sit two adapters that produce the
same `Principal(tenant_id, user_id, group_ids)`:

| | `AUTH_MODE=headers` | `AUTH_MODE=entra` |
|---|---|---|
| Resolver | `HeaderPrincipalResolver` | `EntraJwtPrincipalResolver` |
| Credential | `X-Tenant-Id`, `X-User-Id`, optional `X-Group-Ids` | `Authorization: Bearer <access token>` |
| Who is trusted | the gateway in front of this backend | Microsoft Entra ID's signing keys |
| `tenant_id` | the header value | the token's `tid` claim |
| `user_id` | the header value | the token's `oid` claim |
| `group_ids` | the `X-Group-Ids` CSV | the token's `groups` claim |
| Outbound dependency at startup | none | OIDC discovery + JWKS for the configured tenant |
| Identifier shape in practice | application-defined strings (`acme`, `oncall`) | GUIDs |

**The mode is selected once, at startup, and never per request.** `create_app()`
calls `build_initial_resolver(settings)`, which returns a working
`HeaderPrincipalResolver` in headers mode and an `UninitializedResolver` sentinel
in Entra mode; the lifespan then replaces the sentinel with the real
`EntraJwtPrincipalResolver` built by `build_entra_resolver()`. Nothing about a
request influences which adapter runs — a request carrying both a Bearer token and
`X-Tenant-Id` gets exactly one of them read, decided by configuration.

The sentinel matters. If the app is served without its lifespan running, Entra mode
raises `RuntimeError` rather than falling back to header trust; a silent fallback
would turn a spoofable header into an accepted identity at exactly the moment
nobody is watching.

**Trust boundary.** In headers mode the backend cannot tell a spoofed identity
header from a real one — a gateway must strip or override all three headers and the
backend must be unreachable except through it. In Entra mode the backend verifies
the credential itself, so the trust boundary moves to the token signature and the
tenant configuration behind it. The two are alternatives, never simultaneous trust
sources: in Entra mode the `X-*` identity headers are read by nothing at all.

`/health` requires no principal in either mode.

---

## 2. Two app registrations

`infra/scripts/create-entra-app.sh` creates exactly two application registrations
and nothing else in the tenant.

**The API app** is the resource. It owns the audience the server validates, and it
is the app whose manifest decides what a token for this API looks like:

- `identifierUris: ["api://<API_APP_ID>"]` — the application ID URI clients name
  when requesting a scope.
- `api.oauth2PermissionScopes` — one delegated scope, `access_as_user` by default.
- `appRoles` — one application role, `Api.Access` by default, with
  `allowedMemberTypes: ["Application"]`.
- `api.requestedAccessTokenVersion: 2`.
- `groupMembershipClaims: "SecurityGroup"`.

Its service principal is created *after* the `appRoles` PATCH (a service principal
copies the application's roles at creation time) and is explicitly set to
`appRoleAssignmentRequired: false`, which is what lets the deferred smoke phase
obtain a valid-audience token carrying no `roles` claim and watch **this API**
refuse it.

**The client app** is the caller. It is `isFallbackPublicClient: true` (the device
code flow is a public-client grant) and also holds a client secret for the app-only
leg, so one registration serves both flows in the smoke test. Its
`requiredResourceAccess` names the API app's scope (`type: "Scope"`) and role
(`type: "Role"`). Delegated admin consent is written directly as an
`oauth2PermissionGrant` rather than through `az ad app permission admin-consent`,
because that command consents to *every* entry in `requiredResourceAccess` —
including the application role, which `--defer-app-role-assignment` exists to
withhold. The generated secret is given a seven-day expiry.

### Why `requestedAccessTokenVersion: 2`, specifically

This is the setting most likely to cost someone an afternoon, so here is the reason
rather than the value.

`requestedAccessTokenVersion` is nullable and **defaults to `1`**. The manifest
reference is explicit that this is not something the client can influence: the
setting "changes the version and format of the JWT produced independent of the
endpoint or client used to request the access token", and "The endpoint used, v1.0
or v2.0, is chosen by the client and only impacts the version of id_tokens.
Resources need to explicitly configure `requestedAccessTokenVersion` to indicate
the supported access token format."
([app manifest reference](https://learn.microsoft.com/en-us/entra/identity-platform/reference-app-manifest#requestedaccesstokenversion-attribute),
checked 2026-08.)

Leave it unset and you get a v1.0 access token whose `iss` is
`https://sts.windows.net/{tid}/`. The verifier compares `iss` against
`https://login.microsoftonline.com/{tid}/v2.0` — the value it built from the
configured tenant GUID — so every request fails with a 401 that names no reason
(by design; see §7). Calling the v2.0 token endpoint does not fix it, because the
endpoint choice does not affect access tokens. The v1.0 audience is also looser:
`aud` in a v1.0 token "can be the client ID or the resource URI used in the
request", where a v2.0 token's `aud` "is always the client ID of the API"
([access token claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference),
checked 2026-08) — which is what makes the audience configuration in §3 a plain
GUID rather than a URI.

### Why `groupMembershipClaims: "SecurityGroup"`, specifically

Without it, no `groups` claim is issued at all. The claim is not a default: the
`groupMembershipClaims` property "Configures the `groups` claim issued in a user or
OAuth 2.0 access token that the app expects"
([app manifest reference](https://learn.microsoft.com/en-us/entra/identity-platform/reference-app-manifest#groupmembershipclaims-attribute),
checked 2026-08), and the value this lab sets, `SecurityGroup`, "Emits security
groups and Microsoft Entra roles that the user is a member of in the group claim"
([configure group claims](https://learn.microsoft.com/en-us/entra/identity/hybrid/connect/how-to-connect-fed-group-claims),
checked 2026-08).

The failure mode if you omit it is quiet, not loud. `EntraJwtPrincipalResolver`
reads an absent `groups` claim as `group_ids=()` — which is a legitimate state, and
indistinguishable from "this user really is in no groups". Every Entra-mode caller
would then silently degrade to tenant-wide-only visibility: Day 15's ACL filter
keeps working, keeps returning documents with `allowed_groups: []`, and returns
nothing that needed a group. There is no error anywhere to explain it.

---

## 3. Configuration

| Environment variable | Required | Meaning |
|---|---|---|
| `AUTH_MODE` | no (default `headers`) | `headers` or `entra`. Anything else fails startup validation. |
| `ENTRA_TENANT_ID` | in `entra` mode | The tenant GUID. Used to build both the issuer and the discovery URL. |
| `ENTRA_AUDIENCE` | in `entra` mode | The **API application's client ID GUID** — not the `api://…` URI. |
| `ENTRA_REQUIRED_SCOPE` | at least one of these two | Delegated scope required in `scp` (e.g. `access_as_user`). |
| `ENTRA_REQUIRED_APP_ROLE` | at least one of these two | Application role required in `roles` (e.g. `Api.Access`). |

Startup validation (`core/config.py`):

- `ENTRA_TENANT_ID` and `ENTRA_AUDIENCE` are normalized to canonical lower-case
  GUID form *in either mode*, so a value left over in a shared `.env` is still
  validated as a GUID rather than silently accepted as free text. A non-GUID fails
  startup with `must be a GUID`.
- In `entra` mode, both are required, and at least one of the two permission
  settings must be present. Blank strings normalize to `None`, so a `.env`
  placeholder and an unset variable behave identically.
- A permission setting that is `None` **never matches anything**. "Not configured"
  means "this credential type is not accepted", not "this credential type is not
  checked" — an app-only token cannot slip through because
  `ENTRA_REQUIRED_APP_ROLE` was left blank.

**Audience is the GUID, requested scope is the URI.** This asymmetry is not a
mistake and it trips people up:

```
ENTRA_AUDIENCE=<API_APP_ID>                      # what the server compares aud against
scope=api://<API_APP_ID>/access_as_user          # what the client asks for
scope=api://<API_APP_ID>/.default                # app-only variant
```

With `requestedAccessTokenVersion: 2`, `aud` in the issued token is the API's
client ID GUID, which is why the server is configured with the GUID even though no
client ever types one.

`create-entra-app.sh` prints the whole server-side block at the end of a
successful run.

---

## 4. Delegated flow: OAuth 2.0 device code

`tools/entra_smoke.py` programs directly against the protocol — raw `POST` forms,
no MSAL — so that what reaches the server is visible rather than library-shaped.

**Device authorization request**
([device authorization grant](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code),
checked 2026-08):

```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode
Content-Type: application/x-www-form-urlencoded

client_id=<CLIENT_APP_ID>
&scope=openid profile api://<API_APP_ID>/access_as_user
```

The response carries `device_code`, `user_code`, `verification_uri`, `expires_in`
and `interval`. The user opens `verification_uri` on another device and enters
`user_code`; the client polls meanwhile.

**Token request**

```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:device_code
&client_id=<CLIENT_APP_ID>
&device_code=<device_code>
```

Learn documents four polling errors: `authorization_pending` (keep polling),
`authorization_declined`, `bad_verification_code` and `expired_token` (all stop).
The smoke tool additionally handles RFC 8628 §3.5's `slow_down`, which Learn's
table does not list, by raising the interval **permanently** for the rest of the
session — the RFC says the increase applies "for this and all subsequent requests",
not to one poll.

### Why `openid profile` is in the scope string

Not decoration, and not for the API. Three separate things depend on it:

- `id_token` is "Issued if the original `scope` parameter included the `openid`
  scope." The smoke test needs one, because presenting an ID token to the API is
  its cleanest wrong-audience 401 — a real, correctly signed Microsoft token that
  this API must still refuse.
- `oid` is what becomes `Principal.user_id`, and Learn is explicit: "Because the
  `oid` allows multiple applications to correlate principals, to receive this claim
  for users use the `profile` scope."
- `tid` is what becomes `Principal.tenant_id`: "To receive this claim, the
  application must request the `profile` scope."
  (Both quotes: [access token claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference),
  checked 2026-08.)

So without `profile`, an otherwise valid delegated token can arrive with no `tid`
or no `oid`, and the resolver rejects it — correctly, and with no way to tell the
caller why.

### The `scp` path

A delegated token carries `scp`, "a space separated list of scopes … Only included
for user tokens." `_require_permission` sees `scp`, splits it on whitespace, and
requires `ENTRA_REQUIRED_SCOPE` to be an exact member of the resulting set. Set
membership rather than substring matching is deliberate: `access_as_user_extended`
is a different scope and must not satisfy a requirement for `access_as_user`.

---

## 5. Application-only flow: client credentials

```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=<CLIENT_APP_ID>
&client_secret=<secret>
&scope=api://<API_APP_ID>/.default
```

`.default` is required by the flow's shape, not by this lab: the `scope` value
"should be the resource identifier (application ID URI) of the resource you want,
suffixed with `.default`", and it "tells the Microsoft identity platform that of
all the direct application permissions you have configured for your app, the
endpoint should issue a token for the ones associated with the resource you want to
use"
([client credentials flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow),
checked 2026-08). There is no user to consent, so there are no incremental scopes
to name — "you can't use *delegated permissions* because there is no user for your
app to act on behalf of."

The permission arrives as `roles`, not `scp`. `_require_permission` finds no `scp`,
falls through to the application-role gate, and requires `ENTRA_REQUIRED_APP_ROLE`
to be a member of the `roles` array — again by membership, not substring
(`Api.Access.Extended` is a different role).

`Principal.user_id` for such a caller is the **client service principal's `oid`**.
That is a stable identifier for the calling application, and it is what appears in
the `identity resolved` log line; it is not a person, and nothing downstream
pretends otherwise.

### Role-less app-only tokens are a real state, not an error

This is the part worth internalizing before you conclude the API is broken. Entra
will happily issue an app-only token with **no** `roles` claim: "In order to enable
this ACL-based authorization pattern, Microsoft Entra ID doesn't require that
applications be authorized to get tokens for another application. Thus, app-only
tokens can be issued without a `roles` claim. Applications that expose APIs must
implement permission checks in order to accept tokens."

That is exactly why the API's own gate is load-bearing, and why
`create-entra-app.sh` sets `appRoleAssignmentRequired: false` explicitly rather
than leaving it to a default: with that setting,
`tools/entra_smoke.py --phase no-role` gets a valid-audience, role-less token and
proves the **server** answers 403. Flip it to `true` — Learn's recommended
hardening if you want Entra to refuse instead — and the token endpoint refuses
first, and that phase proves nothing about this API.

---

## 6. The validation chain

Two modules, one boundary each. `services/entra_jwt.py` answers *was this token
signed by a key this tenant publishes, and are its registered claims the ones we
require?* — nothing more. `api/principal.py` answers *what identity is this, and
does it carry the permission this API requires?* The verifier imports no FastAPI,
raises no `HTTPException`, and knows nothing about `Principal`.

**At startup** (`build_entra_resolver` → `EntraTokenVerifier.initialize()`):

1. `GET https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`.
   The URL is built from the configured tenant GUID, never discovered.
2. The document's `issuer` must **exactly** match
   `https://login.microsoftonline.com/{tenant}/v2.0`. No normalization: this is the
   value every token's `iss` is later compared against, so a "close enough" issuer
   here would widen every later comparison.
3. `jwks_uri` is taken from the document but not followed blindly — it must be
   `https`, on host `login.microsoftonline.com`, with no port, no username and no
   password. Redirects are not followed. A compromised or spoofed metadata document
   therefore cannot point key retrieval outside Microsoft's identity endpoint.
4. `GET <jwks_uri>`, parsed into a `kid → PyJWK` map. Structural problems with the
   key *set* are fatal (an entry that is not an object, a missing `kid`, duplicate
   `kid` values — picking one of two keys sharing a `kid` would be a coin toss per
   request). Individual unusable keys are skipped with a warning rather than fatal,
   so the day Entra publishes one key of a type PyJWT will not build as RS256, an
   additive provider change does not take this service's authentication offline.
5. Failure raises `TokenVerifierStartupError` and the process does not start. A
   process that cannot fetch signing keys cannot authenticate anybody; starting
   anyway would mean serving requests guaranteed to fail — or, worse, inviting a
   later "temporarily skip verification" workaround.

**Per request** (`EntraJwtPrincipalResolver.resolve`):

1. Exactly one `Authorization` header. The **raw** value, `Bearer ` prefix
   included, is capped at 16 KiB *before* any splitting — bounding the token body
   instead would mean splitting unbounded attacker input first. A token carrying
   group claims is commonly 2–4 KB, so this is a ceiling, not a tuning knob.
2. Split on a single ASCII space, exactly two parts, scheme compared
   case-insensitively per RFC 7235. No leading, trailing, doubled or tab separators
   — those are the shapes a permissive parser accepts and a proxy downstream might
   read differently.
3. `jwt.get_unverified_header` → `alg` must be `RS256`, `kid` must be a non-empty
   string. These screens run **before** the key cache is touched, so algorithm
   confusion (`none`, or HS256 keyed on the public key) never reaches the
   verification path, and a junk token can never trigger a network fetch (§9).
4. Key lookup by `kid`, with the two refresh triggers described in §9.
5. `jwt.decode(..., algorithms=["RS256"], audience=ENTRA_AUDIENCE,
   issuer=<built issuer>, leeway=60, options={"require": ["exp", "iss", "aud"]})`.
   The `require` list matters because PyJWT only validates claims that exist — a
   token missing `aud` would otherwise pass audience validation by having nothing
   to check. `leeway=60` is clock skew between Entra and this host, not a grace
   period for expired tokens.
6. Claims → identity: `tid` and `oid` must both be strings, and `tid` must equal
   `ENTRA_TENANT_ID`. Groups per §8. `Principal` construction applies Day 15's own
   validation (charset, length, at most 100 groups, deduplicated and sorted).
7. Identity first, permission second — `_principal_from` runs before
   `_require_permission`. Claims we do not trust are not claims we evaluate
   permissions on: a group overage or a malformed `tid` is 401 even when the scope
   would also have been refused.
8. `require_principal` sets the `tenant_id` / `user_id` `ContextVar`s, logs
   `identity resolved`, and yields. It is an async-generator dependency with
   `scope="request"` on the callers' `Depends(...)`, so the context stays set for
   the *entire* response — including a streamed body — and is reset in `finally`.

### Where `tid` is checked, and why only there

The verifier module deliberately does **not** check `tid`. The tenant comparison
happens exactly once, in the resolver, which is the only layer that needs `tid`
anyway (it builds `Principal.tenant_id` from it). A duplicated check would merely
be redundant; two layers each assuming the other did it would be a tenant bypass.
The tenant is still pinned at the verifier layer by the exact `iss` match, because
the tenant-specific issuer URL embeds the tenant GUID.

### Why the signing-key issuer is deliberately not separately validated

Microsoft documents a third validation step beyond signature and issuer — checking
the `issuer` property attached to each key in the keys document — and scopes it
precisely: "Applications using the v2.0 tenant-independent metadata need to
validate the signing key issuer."

This lab uses the **tenant-specific** metadata endpoint. For that case Learn says
the exact `iss` comparison is sufficient: "OpenID Connect Core says 'The Issuer
Identifier […] MUST exactly match the value of the iss (issuer) Claim.' For
applications which use a tenant-specific metadata endpoint (like
`https://login.microsoftonline.com/{tenant-id}/v2.0/.well-known/openid-configuration`
…), **this is all that is needed**."
([access tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#validate-the-issuer),
checked 2026-08.)

The signing-key issuer check exists because the tenant-independent (`common` /
`organizations`) keys document returns a *templated* issuer,
`https://login.microsoftonline.com/{tenantid}/v2.0`, and the application must bind
the key it used back to the tenant it is serving. With a tenant-specific endpoint
there is no template to substitute and no other tenant's keys in the document.

**To accept multi-tenant tokens you would have to change all of this together**,
not just relax the `iss` comparison:

1. Point discovery at `common` or `organizations` instead of the tenant GUID.
2. Stop comparing `iss` to a single configured string; substitute the token's `tid`
   into the metadata issuer template and require an exact match.
3. Validate the signing key issuer: read the `issuer` property of the key selected
   by `kid`, substitute `tid` if it is templated, and require it to match the
   token's `iss`.
4. Validate that `tid` is a GUID and that `iss` has the form
   `https://login.microsoftonline.com/{tid}/v2.0` for that exact `tid` — the step
   that ties tenant, issuer and key scope into one chain.
5. Replace the resolver's single `tid != ENTRA_TENANT_ID` comparison with a real
   tenant allow-list, and re-examine every place `tenant_id` is used as a key
   (Day 15's store keys and ACL filter), because `tid` then becomes a
   caller-supplied partition selector rather than a constant.

Until all five are done, the exact-issuer check is the thing keeping this API
single-tenant, and relaxing it alone would be a tenant bypass.

---

## 7. Failure contract

Both rejections travel through the standard error envelope with a
`correlation_id`, and both carry an RFC-conformant challenge.

| Status | `error.code` | `WWW-Authenticate` | Meaning |
|---|---|---|---|
| 401 | `unauthorized` | `Bearer` | Missing, malformed, or unverifiable credential; verified but wrong tenant; unreadable `tid`/`oid`; group overage. |
| 403 | `insufficient_scope` | `Bearer error="insufficient_scope"` | Verified credential, authenticated caller, but the required scope or application role is absent. |

The split is the whole point. A 401 means "retrying with this credential is
pointless because we could not establish who you are"; a 403 means "we know exactly
who you are, and this token will never work — get a different one", which is what
RFC 6750's `insufficient_scope` challenge says.

**The 401 message never names a mechanism or a reason.** "Missing or invalid
credentials." is all a caller gets, whether the failure was a bad signature, an
expired token, the wrong audience, or a header with two spaces in it. Telling an
unauthenticated caller *which* check failed is free reconnaissance. The detail
goes to the server log: one INFO line naming only the exception class
(`bearer token rejected exception=TokenInvalidError`), never the token, never the
provider payload.

**Never 422.** Every parsing violation on this path is 401. Header syntax is not
request-body validation, and a 422 here would leak the distinction between "who are
you" and "what did you ask for".

**Only `TokenInvalidError` becomes a 401.** `verify()` also raises a bare
`RuntimeError` for an uninitialized verifier and `TokenVerifierStartupError` for a
failed startup. Both are our faults, not the caller's, and both must reach the 500
handler loudly — an `except Exception` here would turn a misconfigured deployment
into a 401 storm that reads as a fleet of bad clients.

**On `/api/v1/chat/stream`**, both 401 and 403 are pre-stream JSON responses (the
Day 6 two-stage error boundary), never SSE `error` events. `require_principal` runs
before `StreamingResponse` is constructed, so the status line has not been sent yet.

**ACL denial is not in this table.** Day 15's contract is unchanged: a document
outside the caller's scope is filtered at query time and is indistinguishable from
a document that does not exist. `/rag` answers `status: "no_answer"`, `/chat`
answers `404 conversation_not_found` for another tenant's `conversation_id`.
Silent filtering *is* the absence contract — there is no authorization-denied
signal anywhere in the retrieval pipeline, and Day 19 does not add one.

### What the generated OpenAPI says, and where it deviates

`docs/openapi/openapi.yaml` declares a `bearerAuth` HTTP bearer security scheme and
attaches `security: [{bearerAuth: []}]` to all four protected operations —
`/api/v1/chat`, `/api/v1/chat/stream`, `/api/v1/rag`, `/api/v1/agent`.

**It does so unconditionally, including under `AUTH_MODE=headers`.** The scheme
comes from a module-level `HTTPBearer` instance that `require_principal` depends on
regardless of mode; the exported contract has no way to express "this credential
applies in one deployment configuration". In headers mode the real credentials are
`X-Tenant-Id` / `X-User-Id` / `X-Group-Ids`, which appear **nowhere** in the
document as parameters — they are mentioned only in the security scheme's prose
`description`. A reader generating a client from this file in headers mode will
produce one that sends a Bearer token the server ignores, and omits the headers the
server actually requires. Read the `description`, and read `AUTH_MODE`.

The `HTTPBearer` is configured `auto_error=False`, so a missing or malformed header
produces `None` rather than FastAPI's own error response, and the parsed credential
is deliberately unused — every rejection still comes from the resolver, through the
shared envelope, and the resolver re-reads the raw header because the size cap and
the exactly-one-header rule are ours to enforce.

One more inconsistency, stated rather than papered over: the 401 and 403 response
descriptions in the generated document read "Missing or invalid credentials" and
"Authenticated credential lacks required API permission" — they describe the
condition but do not name the envelope `code`, while sibling entries in the same
response dictionaries do name theirs (`content_filtered`,
`rag_context_overflow`, …). The codes are `unauthorized` and `insufficient_scope`,
as in the table above; this document is where that mapping is written down.

---

## 8. Groups and overage

Entra stops emitting `groups` past a limit: "The number of groups emitted in a
token is limited to 150 for SAML assertions and 200 for JWT, including nested
groups… Exceeding this limit will cause Microsoft Entra ID completely omit sending
group claims in the token."
([configure group claims](https://learn.microsoft.com/en-us/entra/identity/hybrid/connect/how-to-connect-fed-group-claims),
checked 2026-08.) In its place comes an overage signal pointing at Microsoft Graph,
in one of two shapes:

```json
{ "_claim_names": { "groups": "src1" },
  "_claim_sources": { "src1": { "endpoint": "…/getMemberObjects" } } }
```

or a `hasgroups` claim — "If present, always `true`, indicates whether the user is
in at least one group. Indicates that the client should use the Microsoft Graph API
to determine the groups
(`https://graph.microsoft.com/v1.0/users/{userID}/getMemberObjects`) of the user."
([access token claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference),
checked 2026-08.)

**Current behavior: fail closed with 401.** Reading an overage as "this user has no
groups" would silently demote them — Day 15's ACL filter would hide documents they
are entitled to, with no error anywhere to explain it. Resolving the overage
properly needs a Graph call, which is out of scope for Day 19, so the honest answer
is to refuse.

The two signals are read with deliberately different null semantics:

- `hasgroups` is a **flag**, so only one value means "no overage": exactly `False`.
  Present and anything else — `true`, `"true"`, `1`, `null` — is either the signal
  itself or a shape we do not recognize, and guessing at what an unrecognized
  overage signal meant is the one thing this must not do. (Learn says the claim is
  always `true` when present; the code is stricter than the documentation on
  purpose, because an unexpected shape is not evidence of absence.)
- `_claim_names` is a **pointer**, so its null is read the other way: a null carries
  no pointer, so it names no claim as living elsewhere. Only an entry for `groups`
  is an overage signal — Entra also uses `_claim_names` for unrelated distributed
  claims.

An absent `groups` claim with no overage signal is `group_ids=()`, which is a
legitimate identity, not a failure.

A present `groups` claim must be a JSON array of strings with at most 100 entries;
all three violations are 401. `Principal` re-checks all three, so this is a second
layer rather than the only one — stated at the adapter because "what a `groups`
claim may look like" is the adapter's question, not the model's.

**Production direction.** Implement the Graph `getMemberObjects` lookup before
deploying to a tenant where any user is in more than 200 groups. Note that Learn
warns the URL returned in `_claim_sources` may be an Azure AD Graph URL
(`graph.windows.net`) and that services "should instead use the `idtyp` optional
claim … to construct a Microsoft Graph URL for querying the full list of groups".
Two cheaper mitigations exist and are worth considering first: restrict emission to
`ApplicationGroup` (groups explicitly assigned to the application), or configure a
group filter — though filtering itself stops applying above 1,000 group
memberships, at which point an overage claim is sent anyway.

### 8a. Identity namespaces differ between modes — flipping `AUTH_MODE` is not a drop-in change for RAG

This is an operational break, and it is silent.

Headers mode carries **application-defined strings**. This repository's sample
corpus (`data/sample-docs/`) ships three tenants — `acme`, `globex`, `opsdemo` —
and one group, `oncall`, written into each document's front matter as `tenant_id`
and `allowed_groups`. Those exact strings are indexed into the `tenant_id` and
`allowed_groups` fields of every search document.

Entra mode delivers **GUIDs**: `tid` is a tenant GUID and `groups` entries are
group object IDs.

Point an Entra-mode server at an index built for headers mode and every `/rag`
question returns **zero hits** — not degraded results, zero, for every question and
every user. The tenant filter alone excludes everything, because no indexed
document has `tenant_id` equal to a GUID. And because Day 15's contract has no
authorization-denied signal, the endpoint answers `status: "no_answer"`: exactly
what it would say about an empty corpus. Nothing in the logs distinguishes the two.

**This is not a bug and there is no code change to make.** `IDENTIFIER_RE` is
`[A-Za-z0-9_-]{1,64}`, and a canonical GUID is 36 characters of hex and hyphens, so
GUIDs validate as `Principal` identifiers without modification. Switching the
identity source means **re-indexing the corpus with Entra identifiers** — tenant
GUIDs in `tenant_id`, group object IDs in `allowed_groups`. If you flip
`AUTH_MODE` and your RAG answers go quiet, this is the first thing to check.

### 8b. Application-only callers have no groups

`groups` is a user concept. A client-credentials token is issued for an application
with no user behind it, so it carries no `groups` claim, and the resolver builds
`group_ids=()` for it. Every app-only caller therefore sees only tenant-wide
documents — those with `allowed_groups: []` — and never a group-restricted one.

The two flows have structurally different visibility, and that is expected rather
than a misconfiguration. A daemon that needs to read group-restricted content needs
a different design (its own tenant-wide documents, or a delegated flow on behalf of
a user), not a wider `groups` claim.

### 8c. The app-only gate depends on tenant configuration the code cannot see

`_require_permission` branches on the **presence of `scp`**: present means the
delegated gate, absent means the application-role gate. The two are never allowed
to satisfy each other, because Entra can assign app roles to *users* — "For user
tokens, this set of values contains the assigned roles of the user on the target
application" — so a delegated token may carry `roles`. If a missing or wrong `scp`
could fall through to the role gate, a user token would be admitted on a permission
it was never granted as a delegated scope.

**The residual, stated because the rule above reads stronger than it is.** The
presence of `scp` is a proxy for "delegated", not proof of it. A user token for an
app exposing **no** delegated scopes carries `roles` and no `scp` — and would be
admitted through the app-only gate.

What actually bounds this is tenant configuration the code cannot inspect: the API
app declares `allowedMemberTypes: ["Application"]` on its `Api.Access` role, and a
role restricted to applications cannot be assigned to a user. **Change the role to
allow `User` members and the app-only gate weakens accordingly.** The bound is not
in this repository's source; it is in the manifest that
`infra/scripts/create-entra-app.sh` writes.

Two things keep the blast radius small. Both branches produce the same `Principal`,
so what a credential *becomes* does not change — only which gate it passed. And the
role still had to be granted by a tenant administrator; this is not a path an
unprivileged caller can walk on their own.

**Production hardening direction: the `idtyp` optional claim.** Learn calls it "the
most accurate way for an API to determine if a token is an app token or an app+user
token", with "The value is `app` when the token is an app-only token."
([optional claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims-reference),
checked 2026-08.) Requesting it is a manifest change on the API app, and adopting it
needs one more decision than it first appears: by default `idtyp` "is only emitted
for app-only tokens", and emitting it for user tokens requires the
`include_user_token` additional property. So an implementation must decide what an
*absent* `idtyp` means — a user token, or an API whose optional claim was never
configured — and that fallback is the part that makes it more than a one-line
change. `api/principal.py` carries a comment naming `idtyp` at the branch point so
the next person finds this from the code.

---

## 9. JWKS lifecycle

One cache, two refresh triggers, one serialized path.

| Constant | Value | Role |
|---|---|---|
| `JWKS_MAX_AGE_SECONDS` | 24 h | Age past which a cached key set triggers a refresh. |
| `JWKS_REFRESH_COOLDOWN_SECONDS` | 60 s | Shortest interval between two refresh *attempts*, successful or not. |
| `HTTP_TIMEOUT_SECONDS` | 10.0 | Per-operation httpx timeout on each leg. |

**Startup is fail-fast** (§6): no keys, no process.

**Trigger 1 — unknown `kid` (cache miss).** The request has nothing to serve, so it
blocks on the fetch; that is the only way it can ever get an answer.

**Trigger 2 — cache older than 24 h (cache hit).** The request *can* be answered
now, so the refresh is opportunistic: it is taken only if nobody else is already
fetching (`self._refresh_lock.locked()` is False). Refreshing on age *before* the
lookup would put every request behind one round trip on a dead provider — including
requests holding a key the cache can serve — turning a provider outage into the
authentication outage this module promises it is not.

**Single-flight and cooldown.** `kid` is attacker-controlled, so a refresh per
unknown `kid` would turn any unauthenticated client into an outbound request
amplifier aimed at Microsoft. The cooldown bounds that on its own: the attempt is
stamped **before** the await, so a simultaneous burst finds the window already
closed, and a *failed* attempt is rate-limited exactly like a successful one. The
lock is not a second copy of that guarantee — it is what makes waiters actually
wait, so a real key rotation does not answer 401 to every legitimate request that
arrived alongside the first one while the call count stays reassuringly at 1.

**Every refresh is two outbound requests, not one** — discovery, then keys. A
rotation can move `jwks_uri`, and a cached one would be the stale half of a
rotation. So the bound is two requests per cooldown window per verifier.

**A failed refresh is swallowed.** The existing cache is untouched until a
fully-built replacement is ready, published in a single rebind, so no request is
ever served from a partially-parsed key set. A known key keeps verifying; an
unknown one stays 401 because the lookup after the refresh simply misses again.

### Four residuals worth knowing before you rely on this

Stated in the code's own terms, because each is a real behavior under a dead
provider or a live rotation.

1. **Dead provider, stale cache: one request per cooldown window blocks; unknown-`kid`
   requests block unboundedly in number.** On the cache-**hit** path, one request per
   60 s cooldown window pays for the failed refresh — blocking up to roughly 20 s —
   and is then answered normally from the retained cache. The **miss** path has no
   such guard: it is called unconditionally, so every request naming an uncached
   `kid` that arrives during an in-flight fetch queues for the remainder of it and
   is then rejected. The consequence worth stating plainly: someone flooding forged
   `kid`s during a dead-provider window can park many inbound requests for ~20 s
   each while still costing only two outbound requests **per 60 s cooldown window
   per verifier** — the cooldown bounds the outbound side no matter how many
   requests arrive. Inbound concurrency, not outbound traffic, is what that costs
   you.

2. **The same token at the same instant can get two different verdicts.** A request
   that triggers an age-based refresh re-reads the key map afterwards, so it is
   judged against the *post*-refresh key set — and is rejected if its key was just
   withdrawn. Its concurrent peers, served from cache while the fetch was in
   flight, are accepted on the *pre*-refresh key set. This lasts only as long as
   the fetch. It is the deliberate cost of not parking cache hits behind a refresh:
   the request that paid for the fetch is not asked to honor a key we have just
   learned the tenant withdrew.

3. **Max age is a trigger, not a serving bound.** "A request against a cache this
   old causes a refresh attempt" — not "no request is answered from a cache this
   old". Because a failed refresh retains the cache, a verifier facing a dead
   provider can keep serving from a cache well past 24 h. If you need the second
   property, the refresh failure would have to invalidate rather than retain, which
   is the trade this module explicitly refuses.

4. **The `locked()` fast path can still block briefly.** `locked()` reads the lock
   flag alone, while `Lock.acquire`'s fast path also demands an empty waiter queue,
   and `release()` clears the flag *before* the woken waiter sets it again — so
   there is a real window where `locked()` reads False and the acquire below still
   queues behind a draining waiter list. That cost is event-loop ticks, not a round
   trip: waiters only ever queue through the miss path, and by the time any is woken
   the holder has already stamped the attempt timestamp, so each returns at a guard
   without touching the network.

**And the ~20 s figure is a practical bound, not a guaranteed one.**
`HTTP_TIMEOUT_SECONDS = 10.0` is passed to httpx as a float, which sets connect,
read, write and pool timeouts to 10 s **each** — per-operation, not a total
deadline. Two legs at 10 s puts a failing refresh near 20 s against a 60 s cooldown
window, comfortably inside it. A provider trickling bytes could hold a read open
past that. The cost if it ever happens is bounded anyway: that one request blocks
on a real fetch, for itself rather than for everyone.

---

## 10. Production checklist

Day 19 makes the backend able to verify a caller. It does not make the surrounding
deployment safe on its own.

- [ ] **Strip all three `X-*` identity headers at the gateway**, in Entra mode too.
      They are ignored by the Entra resolver today, but a future change or a
      misread `AUTH_MODE` should not be one line away from accepting them. Strip
      `X-Tenant-Id`, `X-User-Id` and `X-Group-Ids` unconditionally.
- [ ] **Make the headers-mode backend unreachable.** If any deployment still runs
      `AUTH_MODE=headers`, it must be reachable only through the gateway that sets
      those headers. Anyone who can reach it directly can claim any tenant.
- [ ] **Rotate the client secret, or stop using one.** The lab's secret is issued
      with a seven-day expiry on purpose. Outside a lab, prefer a certificate or a
      federated credential — Learn documents both as client-credentials variants —
      and never put the secret on a command line (`ps` shows another user the whole
      argv of a running process, and shell history keeps it afterwards).
- [ ] **Treat the identity log fields as pseudonymous personal data.** Every
      `LogRecord` carries `tenant_id` and `user_id`; in Entra mode `user_id` is a
      user's directory object ID, which is stable across applications within a
      tenant. Apply the retention and access controls you would apply to any user
      identifier. Group IDs are never logged, and that should stay true.
- [ ] **Plan for claims challenges and Continuous Access Evaluation.** This API
      answers 401 with a bare `Bearer` challenge and never emits a claims
      challenge, so a revoked session or a Conditional Access policy change is
      visible to it only when the token expires. A production API that wants
      near-real-time revocation has to advertise CAE capability and return the
      provider's claims challenge to the caller.
- [ ] **Implement Graph `getMemberObjects` overage resolution** before deploying to
      a tenant where any user can exceed 200 group memberships (§8), or restrict
      emission with `ApplicationGroup` / a group filter.
- [ ] **Consider `idtyp`** to close the §8c residual, together with a decision about
      what an absent `idtyp` means.
- [ ] **Re-index with Entra identifiers** before serving RAG traffic in Entra mode
      (§8a).
- [ ] **Decide whether role-less app-only tokens should be refusable at Entra.**
      Setting `appRoleAssignmentRequired: true` on the API service principal blocks
      them at the token endpoint. The lab keeps it `false` so the 403 path is
      provable end to end; production has no such need.

---

## 11. Teardown

The registrations are ephemeral, like every other Azure resource in this lab. One
command removes both — deleting an application also removes its service principal,
its secret, its app-role assignments and its permission grants:

```bash
ENTRA_TENANT_ID=<tenant> \
ENTRA_API_APP_ID=<api-app-id> \
ENTRA_CLIENT_APP_ID=<client-app-id> \
  infra/scripts/delete-entra-app.sh
```

It targets applications by ID rather than by display name (a name match would
happily delete somebody else's registration that happens to share the name),
verifies the active `az` tenant first, and succeeds if either registration was
already removed. `create-entra-app.sh` prints this command, with the ids filled in,
on success — and also on an abort partway through, with `none` in place of
whichever registration it never got as far as creating.

---

## See also

- [Authentication sequence diagram](diagrams/entra-auth-sequence.md)
- [API conventions — identity and tenancy](api-conventions.md#identity-and-tenancy)
- [RAG retrieval — access control is a query-time filter](rag-retrieval.md#access-control-is-a-query-time-filter-not-a-separate-check)
- [RAG indexing — tenant-scoped keys](rag-indexing.md#tenant-scoped-keys)
