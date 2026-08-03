"""Live cross-tenant smoke probes: does the live index enforce the ACL filter it claims to?

Three probes, same fixed query, same ``mode=hybrid``, same ``top``, same
target — the globex on-call runbook's escalation-path chunk, which only a
globex principal carrying the ``oncall`` group may see:

  1. globex + ``["oncall"]`` -> target chunk id **in** the hits (authorized baseline)
  2. globex + ``[]``         -> target chunk id **not in** the hits (missing group)
  3. acme + ``["oncall"]``   -> target chunk id **not in** the hits (cross-tenant)

Every assertion is about the target's *visibility*, never about an empty
result set: a forbidden principal may legitimately retrieve other, public
documents that land in the same top-N. Only the one gated chunk is asserted
present or absent.

Probe 1 is the precondition the other two depend on for meaning anything. If
the authorized baseline never surfaces the target in the top-N — a stale
index, a corpus that has drifted, a query that no longer matches — then an
"absent" result from probes 2-3 is not evidence that the ACL filter is
working, it is evidence that nothing is being exercised. Probes 2 and 3
report **INCONCLUSIVE** in that case, never PASS, and the process still
exits non-zero.

This tool refuses to run against fakes (there is no ACL enforcement to
observe) and treats missing or invalid Azure credentials as a distinct,
non-FAIL, non-INCONCLUSIVE outcome: contract-level verification, not a code
blocker. The article cites that outcome by its documented wording, not by
running this tool.

Usage:
    uv run python tools/tenant_smoke.py
"""

import asyncio

from azgenai_lab.core.config import get_settings
from azgenai_lab.core.errors import ConfigurationError
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.rag import make_chunk_id, make_parent_id
from azgenai_lab.models.search import SearchMode
from azgenai_lab.services.azure_search import AzureSearchClient
from azgenai_lab.services.embeddings import EmbeddingClient, build_embedding_client

# The same fixed question `tools/compare_retrieval.py` freezes for globex
# query 5 ("requires the oncall group"), and the same chunk it expects —
# reused rather than re-picked, so this tool and that evidence agree on what
# "the gated chunk" means.
QUERY_TEXT = "how do I escalate a Sev 1 outage at 3am?"
TOP = 5
TARGET_CHUNK_ID = make_chunk_id(make_parent_id("globex", "oncall-runbook"), 3)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2
EXIT_MISSING_CREDENTIALS = 3


async def _probe_has_target(
    client: AzureSearchClient, vector: list[float], principal: Principal
) -> bool:
    result = await client.search(
        QUERY_TEXT,
        vector,
        mode=SearchMode.HYBRID,
        top=TOP,
        principal=principal,
    )
    return any(hit.chunk_id == TARGET_CHUNK_ID for hit in result.hits)


async def main() -> None:
    settings = get_settings()
    if settings.use_fake_search or settings.use_fake_embeddings:
        print(
            "SKIPPED: USE_FAKE_SEARCH/USE_FAKE_EMBEDDINGS is true — this "
            "tool only probes a live index's real ACL enforcement; a fake "
            "carries no filter semantics to observe. Use the documented "
            "contract-level verification instead of this tool here."
        )
        raise SystemExit(EXIT_MISSING_CREDENTIALS)
    configure_logging(settings.log_level)

    try:
        embedding_client = build_embedding_client(settings)
        async with AzureSearchClient(settings) as client:
            await _run(client, embedding_client)
    except ConfigurationError as exc:
        print(
            "SKIPPED: missing or invalid Azure credentials "
            f"({exc.upstream_detail or exc.message}) — this is a "
            "contract-level-verification gap, not a probe result."
        )
        raise SystemExit(EXIT_MISSING_CREDENTIALS) from exc


async def _run(client: AzureSearchClient, embedding_client: EmbeddingClient) -> None:
    vector = (await embedding_client.embed([QUERY_TEXT]))[0]

    print(f"query: {QUERY_TEXT!r}")
    print(f"target chunk: {TARGET_CHUNK_ID}")
    print()

    baseline = Principal(tenant_id="globex", user_id="smoke-user", group_ids=("oncall",))
    baseline_hit = await _probe_has_target(client, vector, baseline)
    print(
        "probe 1 (globex+[oncall], authorized baseline): "
        f"{'PASS' if baseline_hit else 'FAIL'} — target "
        f"{'in' if baseline_hit else 'NOT in'} top-{TOP}"
    )

    if not baseline_hit:
        print(
            "probe 2 (globex+[], missing group): INCONCLUSIVE — the "
            "authorized baseline (probe 1) never surfaced the target, so an "
            "absent result here would not be evidence of enforcement"
        )
        print("probe 3 (acme+[oncall], cross-tenant): INCONCLUSIVE — same reason")
        raise SystemExit(EXIT_INCONCLUSIVE)

    missing_group = Principal(tenant_id="globex", user_id="smoke-user", group_ids=())
    missing_group_hit = await _probe_has_target(client, vector, missing_group)
    missing_group_pass = not missing_group_hit
    print(
        "probe 2 (globex+[], missing group): "
        f"{'PASS' if missing_group_pass else 'FAIL'} — target "
        f"{'NOT in' if missing_group_pass else 'unexpectedly in'} top-{TOP}"
    )

    cross_tenant = Principal(tenant_id="acme", user_id="smoke-user", group_ids=("oncall",))
    cross_tenant_hit = await _probe_has_target(client, vector, cross_tenant)
    cross_tenant_pass = not cross_tenant_hit
    print(
        "probe 3 (acme+[oncall], cross-tenant): "
        f"{'PASS' if cross_tenant_pass else 'FAIL'} — target "
        f"{'NOT in' if cross_tenant_pass else 'unexpectedly in'} top-{TOP}"
    )

    if not (missing_group_pass and cross_tenant_pass):
        raise SystemExit(EXIT_FAIL)

    print()
    print("all probes PASS")


if __name__ == "__main__":
    asyncio.run(main())
