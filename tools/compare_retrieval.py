"""Run the frozen query set and write the live evidence.

Two experiments, each with exactly one variable:

  1. Candidate generation — VECTOR mode, vector_k in {1, 3, 50}.
  2. Reranking — HYBRID vs HYBRID_SEMANTIC, vector_k fixed at 50.

A four-mode baseline is also recorded, but it is a *survey*, not an
experiment: it varies more than one thing and is labelled accordingly. Folding
it into the reranking experiment would mean the section titled "mode is the
only variable" was varying the candidate generator too.

Everything else (query, vector, filter, top, index generation) is held fixed,
and the observable is declared before the run: for each pre-registered chunk
id, its rank or its absence.

Evidence is checkpointed to disk after every call. A live session that dies on
query four must not lose queries one to three, and the failing call is usually
the one worth keeping.

Request bodies are written **redacted** to a JSON sidecar — the raw OData
``filter`` (which spells out the querying principal's tenant id and group
ids) is stripped from every recorded request and replaced with
``filter_present``/``filter_sha256``/``vector_filter_mode`` evidence fields;
the Markdown shows a further readable summary with the 1536-float vector
elided. Neither is replayable on its own: the vector and every non-ACL field
are reusable as-is, but a full replay of a recorded call requires rebuilding
the filter from a `Principal` (`services/acl.py`) — the sidecar deliberately
does not carry enough to skip that step. The sidecar's SHA-256 is recorded so
the pair cannot silently drift. Anything written from an upstream error has
the search service's name and host redacted first, because those bodies name
the resource and this evidence is published.

Two conditions stop the run before it can spend anything: fake embeddings, and
pre-registered chunk ids left as placeholders. Either one produces an evidence
file that looks complete and measures nothing.

Usage:
    uv run python tools/compare_retrieval.py \
        --top 25 --out ../drafts/assets/day-13/comparison-free.md

`top` must be at least the corpus chunk count. Below it, a chunk that was
generated as a candidate but truncated out of the response is indistinguishable
from one that was never a candidate — which is the distinction the candidate
generation experiment exists to measure. The output filename names the tier,
because nothing inside the file records which one produced it.
"""

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from azgenai_lab.core.config import Settings, get_settings
from azgenai_lab.core.errors import ConfigurationError, UpstreamError
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.rag import make_chunk_id, make_parent_id
from azgenai_lab.models.search import DEFAULT_VECTOR_K, SearchHit, SearchMode
from azgenai_lab.models.search_index import SEARCH_API_VERSION
from azgenai_lab.services.azure_search import AzureSearchClient
from azgenai_lab.services.embeddings import EmbeddingClient, build_embedding_client


def _scrub(text: str, endpoint: str | None, placeholder: str) -> str:
    """Redact a configured endpoint's host and bare service name from ``text``.

    Azure error bodies routinely echo the resource name back verbatim (a 403
    naming "the service 'azgenai-lab-search-7f3a'"), and an SSL hostname
    mismatch echoes the full host. Both are resource names this project's
    rules require masking before anything is written to evidence, so this
    runs on every detail before it is added to a table row.

    The bare label is matched on token boundaries rather than as a substring.
    Service names may be as short as two characters, and a plain ``replace``
    of a short one would mangle ordinary words in published evidence — while
    a length floor would leave short names unredacted, which is worse than a
    uniform failure because the surrounding output still looks scrubbed.
    Azure Search names are lowercase letters, digits and dashes, so a match
    bounded by that character class replaces the name and nothing else.
    """
    if not endpoint:
        return text
    host = urlparse(endpoint).hostname or endpoint
    scrubbed = text.replace(host, placeholder)
    bare_name = host.split(".")[0]
    if bare_name and bare_name != host:
        pattern = rf"(?<![0-9a-z-]){re.escape(bare_name)}(?![0-9a-z-])"
        scrubbed = re.sub(pattern, placeholder, scrubbed)
    return scrubbed


def _redact_request_body(body: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a redacted **copy** of a request body — the original is never mutated.

    The raw ``filter`` string names the querying principal's tenant id and
    (when present) its group ids, and this project's rule is that identifiers
    like those do not go into published evidence. What the evidence needs
    from the filter is not its text but three provable facts about it: that
    one was sent at all, a fingerprint of it (so two runs can be compared
    without either one disclosing the value), and whether the vector query
    that accompanied it asked for pre-filtering or post-filtering. Those
    travel as top-level fields instead.
    """
    if body is None:
        return None
    redacted = dict(body)
    raw_filter = redacted.pop("filter", None)
    redacted["filter_present"] = isinstance(raw_filter, str) and raw_filter != ""
    redacted["filter_sha256"] = (
        hashlib.sha256(raw_filter.encode("utf-8")).hexdigest()
        if isinstance(raw_filter, str) and raw_filter
        else None
    )
    redacted["vector_filter_mode"] = redacted.pop("vectorFilterMode", None)
    return redacted


@dataclass(frozen=True)
class ExpectedChunkRef:
    """One author-recorded expectation: which document, which ordinal, which
    section, as of the last time a human looked at the chunker's output.

    ``heading_path`` is not consumed to build the chunk id — ``ordinal`` and
    ``doc_id`` alone determine that, via ``make_chunk_id``/``make_parent_id``.
    It exists so ``tests/unit/test_compare_retrieval.py`` can catch the
    failure mode the id alone cannot: a re-chunk that keeps the same ordinal
    but shifts which section it belongs to (a heading inserted or removed
    upstream of it). The id would still resolve; the heading path would not
    match, and that mismatch is the drift signal.
    """

    doc_id: str
    ordinal: int
    heading_path: str


@dataclass(frozen=True)
class Query:
    """Frozen before the run, from a local chunker run against the live corpus.

    ``expected_refs`` holds structured references, not raw chunk ids: the id
    is *derived* from ``(tenant_id, doc_id, ordinal)`` via
    ``expected_chunk_ids()`` at run time, never hand-typed and never stored.
    A hand-typed id copy could drift from the id-derivation scheme silently;
    a derived one cannot.

    An empty tuple means the corpus genuinely has no answer. Two or more
    entries mean several chunks are legitimately relevant, and every one of
    them is reported separately — stopping at the first would hide whether the
    second was retrieved at all.
    """

    number: int
    text: str
    kind: str
    expected_refs: tuple[ExpectedChunkRef, ...]


def expected_chunk_ids(tenant_id: str, refs: Sequence[ExpectedChunkRef]) -> tuple[str, ...]:
    """Derive chunk ids for ``refs`` under ``tenant_id`` — never stored, always computed."""
    return tuple(make_chunk_id(make_parent_id(tenant_id, ref.doc_id), ref.ordinal) for ref in refs)


# Filled in from a local chunker run against the live corpus, before any
# query is issued. Choosing an expected ordinal/heading after seeing rankings
# would make the whole comparison unfalsifiable — freeze first, run second.
# Split by tenant: a query issued with tenant A's principal can only ever see
# tenant A's chunks, so a query authored against tenant B's corpus belongs in
# tenant B's tuple, not in a shared one.
QUERIES_BY_TENANT: dict[str, tuple[Query, ...]] = {
    "acme": (
        Query(
            1,
            "99.9% monthly uptime",
            "exact literal",
            (
                ExpectedChunkRef(
                    "service-sla", 3, "Service SLA > Availability targets > Premium tier"
                ),
            ),
        ),
        Query(
            2,
            "How long do I have to send something back if I bought it on sale?",
            "paraphrase",
            (
                ExpectedChunkRef(
                    "returns-policy", 2, "Returns Policy > Refund window > Promotional purchases"
                ),
            ),
        ),
        Query(
            3,
            "when do customers get credit?",
            "cross-document ambiguity (both relevant)",
            (
                ExpectedChunkRef(
                    "service-sla", 2, "Service SLA > Availability targets > Standard tier"
                ),
                ExpectedChunkRef(
                    "returns-policy", 2, "Returns Policy > Refund window > Promotional purchases"
                ),
            ),
        ),
        Query(
            4,
            "what happens if the customer misconfigured their own system?",
            "lexical decoy",
            (ExpectedChunkRef("service-sla", 5, "Service SLA > Exclusions"),),
        ),
        Query(
            5,
            "how do I escalate a Sev 1 outage at 3am?",
            "acme has no runbook — only its own SLA response-time section is relevant",
            (ExpectedChunkRef("service-sla", 4, "Service SLA > Response times"),),
        ),
        Query(6, "What is the parental leave policy?", "absent from corpus", ()),
    ),
    "globex": (
        Query(
            1,
            "how are invoices delivered?",
            "exact literal",
            (ExpectedChunkRef("billing-faq", 1, "Billing FAQ > Invoices"),),
        ),
        Query(
            2,
            "what cards can I pay with?",
            "paraphrase",
            (ExpectedChunkRef("billing-faq", 2, "Billing FAQ > Payment methods"),),
        ),
        Query(
            3,
            "how do I dispute a charge?",
            "lexical decoy",
            (ExpectedChunkRef("billing-faq", 3, "Billing FAQ > Disputes"),),
        ),
        Query(
            5,
            "how do I escalate a Sev 1 outage at 3am?",
            "requires the oncall group — run with --group-id oncall",
            (ExpectedChunkRef("oncall-runbook", 3, "On-Call Runbook > Escalation path"),),
        ),
        Query(6, "What is the parental leave policy?", "absent from corpus", ()),
    ),
}


VECTOR_K_SWEEP = (1, 3, DEFAULT_VECTOR_K)

BASELINE_MODES = (
    SearchMode.KEYWORD,
    SearchMode.VECTOR,
    SearchMode.HYBRID,
    SearchMode.HYBRID_SEMANTIC,
)
RERANKING_MODES = (SearchMode.HYBRID, SearchMode.HYBRID_SEMANTIC)


@dataclass
class Evidence:
    """Accumulates Markdown and raw request bodies, flushing after every call."""

    out: Path
    total_queries: int
    lines: list[str] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    attempted_queries: int = 0

    @property
    def sidecar(self) -> Path:
        return self.out.with_suffix(".requests.json")

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def start_query(self) -> None:
        self.attempted_queries += 1

    def record_request(self, label: str, body: dict[str, Any] | None) -> None:
        # `_redact_request_body` returns a new dict; `body` (and, upstream of
        # it, `SearchDiagnostics.request_body`) is never touched.
        self.requests.append({"label": label, "body": _redact_request_body(body)})

    def flush(self) -> None:
        self.sidecar.write_text(
            json.dumps(self.requests, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        digest = hashlib.sha256(self.sidecar.read_bytes()).hexdigest()
        # A file truncated mid-run (the process died, the terminal was
        # closed) is otherwise structurally identical to a finished one —
        # same footer, same closing line. This count is the only signal
        # inside the file itself: a value below `total_queries` means the
        # run never reached the end, no matter how complete the last table
        # looks.
        footer = [
            "",
            "---",
            "",
            f"Redacted request bodies: `{self.sidecar.name}`",
            f"SHA-256: `{digest}`",
            "",
            "The ACL `filter` this project sends is replaced in the sidecar by "
            "`filter_present`/`filter_sha256`/`vector_filter_mode` evidence "
            "fields — the filter text itself, which names the querying "
            "principal's tenant id and group ids, is never written here.",
            "The tables above additionally elide the 1536-float query vector "
            "for readability.",
            "The vector and every other, non-ACL field in the sidecar are "
            "reusable as recorded; a full replay of a call also requires "
            "rebuilding its filter from a `Principal`, which the sidecar "
            "deliberately does not carry enough to skip.",
            "",
            f"Queries attempted: {self.attempted_queries}/{self.total_queries} "
            "(below total means this file is truncated)",
        ]
        self.out.write_text("\n".join(self.lines + footer) + "\n", encoding="utf-8")


def _ranks(hits: Sequence[SearchHit], expected: tuple[str, ...]) -> str:
    """Report every pre-registered chunk separately: its rank, or 'absent'."""
    if not expected:
        return "n/a (no answer expected)"
    positions = {hit.chunk_id: rank for rank, hit in enumerate(hits, start=1)}
    return "; ".join(f"`{chunk_id}`={positions.get(chunk_id, 'absent')}" for chunk_id in expected)


async def _run(
    client: AzureSearchClient,
    evidence: Evidence,
    label: str,
    query: Query,
    expected_ids: tuple[str, ...],
    vector: list[float],
    principal: Principal,
    *,
    mode: SearchMode,
    top: int,
    vector_k: int,
    search_endpoint: str | None,
) -> list[str]:
    """One call, one set of table rows.

    Never raises: a failed call — including argument validation that rejects
    the request before it is ever sent — is evidence, not an error. The one
    exception is ``ConfigurationError`` (a bad key, a missing index): our own
    deployment is broken, every remaining call in this run would fail
    identically, so this still records the one row that call produced —
    aborting the run must not discard the diagnostic it is aborting because
    of — flushes it, and then re-raises so the caller can abort instead of
    spending a paid run collecting N copies of the same error — the same rule
    the query-embedding failure path in ``main()`` applies.
    """
    try:
        result = await client.search(
            query.text,
            vector if mode is not SearchMode.KEYWORD else None,
            mode=mode,
            top=top,
            principal=principal,
            vector_k=vector_k,
        )
    except (ConfigurationError, UpstreamError, ValueError) as exc:
        # ValueError is `validate_search_arguments()` rejecting the call
        # before any request is sent (empty query text, wrong vector width).
        # It carries none of UpstreamError's diagnostic fields, so its detail
        # is read straight off the exception instead.
        diagnostics = client.last_diagnostics
        evidence.record_request(label, diagnostics.request_body if diagnostics else None)
        status: int | str = "no response"
        if diagnostics is not None and diagnostics.status is not None:
            status = diagnostics.status
        request_id = diagnostics.request_id if diagnostics and diagnostics.request_id else "—"
        latency = f"{diagnostics.latency_ms:.1f}" if diagnostics else "—"
        if isinstance(exc, UpstreamError):
            raw_detail = exc.upstream_detail or exc.message
        else:
            raw_detail = str(exc) or exc.__class__.__name__
        detail = _scrub(raw_detail, search_endpoint, "[search-service]")[:160].replace("|", "\\|")
        row = (
            f"| {label} | **{status}** | {request_id} | {latency} | — | — | "
            f"FAILED: {detail} | — | — |"
        )
        if isinstance(exc, ConfigurationError):
            # Our own deployment is broken; every remaining call in this run
            # would fail identically. Write and flush this one row before
            # aborting — the caller's `evidence.add(*rows)` is never reached
            # once this propagates, so this is the only chance to keep it.
            evidence.add(row)
            evidence.flush()
            raise
        return [row]

    diagnostics = client.last_diagnostics
    assert diagnostics is not None  # set on every completed round trip
    evidence.record_request(label, diagnostics.request_body)

    found = _ranks(result.hits, expected_ids)
    shared = (
        f"| {label} | {diagnostics.status} | {diagnostics.request_id} "
        f"| {diagnostics.latency_ms:.1f} | {found} "
    )
    if not result.hits:
        return [shared + "| — | (no results) | — | — |"]
    rows = []
    for rank, hit in enumerate(result.hits, start=1):
        reranker = "-" if hit.reranker_score is None else f"{hit.reranker_score:.3f}"
        rows.append(shared + f"| {rank} | `{hit.chunk_id}` | {hit.score:.6f} | {reranker} |")
    return rows


HEADER = (
    "| run | status | request id | ms | expected chunk ranks | rank | chunk_id | score | reranker |"
)
DIVIDER = "|---|---|---|---|---|---|---|---|---|"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, required=True, help="frozen for every run")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--tenant-id",
        required=True,
        choices=sorted(QUERIES_BY_TENANT),
        help="selects both the principal's tenant and the frozen query set",
    )
    parser.add_argument(
        "--group-id",
        action="append",
        default=[],
        help="repeatable; a query gated behind allowed_groups needs its group here",
    )
    arguments = parser.parse_args()

    settings = get_settings()
    if settings.use_fake_embeddings:
        raise SystemExit(
            "USE_FAKE_EMBEDDINGS is true — refusing to run a live comparison "
            "session with fake vectors. The fake's vectors carry no "
            "semantics; a paid run against real search using them would "
            "produce evidence that looks real and means nothing. Set "
            "USE_FAKE_EMBEDDINGS=false and provide real Azure OpenAI "
            "embedding credentials before running this tool."
        )
    principal = Principal(tenant_id=arguments.tenant_id, group_ids=tuple(arguments.group_id))
    configure_logging(settings.log_level)
    embedding_client = build_embedding_client(settings)

    # The client owns its connection pool here, so it is closed on the way out
    # — including when a ConfigurationError below aborts the run partway.
    async with AzureSearchClient(settings) as client:
        await _compare(client, embedding_client, settings, arguments, principal)


async def _compare(
    client: AzureSearchClient,
    embedding_client: EmbeddingClient,
    settings: Settings,
    arguments: argparse.Namespace,
    principal: Principal,
) -> None:
    queries = QUERIES_BY_TENANT[arguments.tenant_id]
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(arguments.out, total_queries=len(queries))
    evidence.add(
        "# Retrieval comparison — live evidence",
        "",
        f"- checked: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"- principal: tenant_id={principal.tenant_id} group_ids={list(principal.group_ids)}",
        f"- embedding client: {embedding_client.__class__.__name__} "
        f"deployment={settings.azure_openai_embedding_deployment}",
        f"- data-plane API version: `{SEARCH_API_VERSION}`",
        f"- top (frozen for every run): {arguments.top}",
        "- region / SKU / semanticSearch plan: paste the projected fields "
        "`infra/scripts/create-search.sh` prints (`sku`/`location`/"
        "`semanticSearch` only) — never the unprojected `az search service "
        "show` output, which includes the subscription id",
        "",
    )
    evidence.flush()

    for query in queries:
        expected_ids = expected_chunk_ids(principal.tenant_id, query.expected_refs)
        evidence.start_query()
        started = time.perf_counter()
        try:
            vector = (await embedding_client.embed([query.text]))[0]
        except ConfigurationError:
            # A broken key or deployment name fails identically on every
            # remaining query; continuing would just spend a paid run
            # collecting N copies of the same error instead of one.
            raise
        except UpstreamError as exc:
            embed_ms = (time.perf_counter() - started) * 1000
            detail = _scrub(
                exc.upstream_detail or exc.message,
                settings.azure_openai_endpoint,
                "[openai-service]",
            )[:160].replace("|", "\\|")
            evidence.add(
                f"## Q{query.number} — {query.kind}",
                "",
                f"> {query.text}",
                "",
                f"**embedding FAILED** after {embed_ms:.1f} ms "
                f"({exc.__class__.__name__}): {detail}",
                "",
                "No search calls were attempted for this query — every mode "
                "compared here needs the query vector. This row means the "
                "embedding call failed, not that retrieval found nothing.",
                "",
            )
            evidence.flush()
            continue
        embed_ms = (time.perf_counter() - started) * 1000

        expected = ", ".join(f"`{c}`" for c in expected_ids) or "none (no answer)"
        evidence.add(
            f"## Q{query.number} — {query.kind}",
            "",
            f"> {query.text}",
            "",
            f"- pre-registered chunk(s): {expected}",
            f"- query embedding latency: {embed_ms:.1f} ms "
            "(measured separately, never folded into search latency)",
            "",
            "### Baseline survey — all four modes (varies more than one thing)",
            "",
            HEADER,
            DIVIDER,
        )
        evidence.flush()
        for mode in BASELINE_MODES:
            rows = await _run(
                client,
                evidence,
                f"Q{query.number} baseline {mode.value}",
                query,
                expected_ids,
                vector,
                principal,
                mode=mode,
                top=arguments.top,
                vector_k=DEFAULT_VECTOR_K,
                search_endpoint=settings.azure_search_endpoint,
            )
            evidence.add(*rows)
            evidence.flush()

        evidence.add(
            "",
            "### Experiment 1 — candidate generation (vector_k is the only variable)",
            "",
            "Fixed: query, vector, principal (filter derived from it), top, "
            "index generation. Mode = VECTOR.",
            "",
            HEADER,
            DIVIDER,
        )
        evidence.flush()
        for vector_k in VECTOR_K_SWEEP:
            rows = await _run(
                client,
                evidence,
                f"Q{query.number} k={vector_k}",
                query,
                expected_ids,
                vector,
                principal,
                mode=SearchMode.VECTOR,
                top=arguments.top,
                vector_k=vector_k,
                search_endpoint=settings.azure_search_endpoint,
            )
            evidence.add(*rows)
            evidence.flush()

        evidence.add(
            "",
            "### Experiment 2 — reranking (mode is the only variable)",
            "",
            "Fixed: query, vector, principal (filter derived from it), top, "
            "vector_k=50, index generation.",
            "",
            HEADER,
            DIVIDER,
        )
        evidence.flush()
        for mode in RERANKING_MODES:
            rows = await _run(
                client,
                evidence,
                f"Q{query.number} rerank {mode.value}",
                query,
                expected_ids,
                vector,
                principal,
                mode=mode,
                top=arguments.top,
                vector_k=DEFAULT_VECTOR_K,
                search_endpoint=settings.azure_search_endpoint,
            )
            evidence.add(*rows)
            evidence.flush()
        evidence.add("")
        evidence.flush()

    # Added only once the loop above runs to completion — a death partway
    # through the last query's tables still increments the per-query counter
    # to the total, so this line, not that counter, is what a finished
    # artifact needs to say so positively.
    evidence.add(f"**Run complete — all {len(queries)} queries finished.**")
    evidence.flush()

    print(f"wrote {arguments.out} and {evidence.sidecar}")


if __name__ == "__main__":
    asyncio.run(main())
