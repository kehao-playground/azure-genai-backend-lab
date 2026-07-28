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

Request bodies are written verbatim to a JSON sidecar — the Markdown shows a
readable summary with the 1536-float vector elided, which is not replayable on
its own. The sidecar's SHA-256 is recorded so the pair cannot silently drift.

Usage:
    uv run python tools/compare_retrieval.py \
        --top 10 --out ../drafts/assets/day-13/comparison.md
"""

import argparse
import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from azgenai_lab.core.config import get_settings
from azgenai_lab.core.errors import ConfigurationError, UpstreamError
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.models.search import DEFAULT_VECTOR_K, SearchHit, SearchMode
from azgenai_lab.models.search_index import SEARCH_API_VERSION
from azgenai_lab.services.azure_search import AzureSearchClient
from azgenai_lab.services.embeddings import build_embedding_client


@dataclass(frozen=True)
class Query:
    """Frozen before the run — see Task 17 Step 2.

    ``expected_chunks`` holds **chunk ids**, not document ids. Matching on the
    document would score "right document, wrong section" as a hit, which is
    exactly the failure the reranking experiment is supposed to expose.

    An empty tuple means the corpus genuinely has no answer. Two or more
    entries mean several chunks are legitimately relevant, and every one of
    them is reported separately — stopping at the first would hide whether the
    second was retrieved at all.
    """

    number: int
    text: str
    kind: str
    expected_chunks: tuple[str, ...]


# Filled in from the local chunker run in Task 17 Step 2, BEFORE any query is
# issued. Leaving a placeholder here and choosing after seeing rankings would
# make the whole comparison unfalsifiable.
QUERIES = (
    Query(1, "99.9% monthly uptime", "exact literal", ("<service-sla premium tier>",)),
    Query(
        2,
        "How long do I have to send something back if I bought it on sale?",
        "paraphrase",
        ("<returns-policy promotional purchases>",),
    ),
    Query(
        3,
        "when do customers get credit?",
        "cross-document ambiguity (both relevant)",
        ("<service-sla standard tier>", "<returns-policy promotional purchases>"),
    ),
    Query(
        4,
        "what happens if the customer misconfigured their own system?",
        "lexical decoy",
        ("<service-sla exclusions>",),
    ),
    Query(
        5,
        "how do I escalate a Sev 1 outage at 3am?",
        "scattered answer",
        ("<oncall-runbook escalation path>", "<service-sla response times>"),
    ),
    Query(6, "What is the parental leave policy?", "absent from corpus", ()),
)

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
    lines: list[str] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sidecar(self) -> Path:
        return self.out.with_suffix(".requests.json")

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def record_request(self, label: str, body: dict[str, Any] | None) -> None:
        self.requests.append({"label": label, "body": body})

    def flush(self) -> None:
        self.sidecar.write_text(
            json.dumps(self.requests, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        digest = hashlib.sha256(self.sidecar.read_bytes()).hexdigest()
        footer = [
            "",
            "---",
            "",
            f"Verbatim request bodies: `{self.sidecar.name}`",
            f"SHA-256: `{digest}`",
            "",
            "The tables above elide the 1536-float query vector for readability.",
            "Replay from the sidecar, not from the tables.",
        ]
        self.out.write_text("\n".join(self.lines + footer) + "\n", encoding="utf-8")


def _ranks(hits: Sequence[SearchHit], expected: tuple[str, ...]) -> str:
    """Report every pre-registered chunk separately: its rank, or 'absent'."""
    if not expected:
        return "n/a (no answer expected)"
    positions = {hit.chunk_id: rank for rank, hit in enumerate(hits, start=1)}
    return "; ".join(
        f"`{chunk_id}`={positions.get(chunk_id, 'absent')}" for chunk_id in expected
    )


async def _run(
    client: AzureSearchClient,
    evidence: Evidence,
    label: str,
    query: Query,
    vector: list[float],
    *,
    mode: SearchMode,
    top: int,
    vector_k: int,
) -> list[str]:
    """One call, one set of table rows.

    Never raises: a failed call — including argument validation that rejects
    the request before it is ever sent — is evidence, not an error.
    """
    try:
        result = await client.search(
            query.text,
            vector if mode is not SearchMode.KEYWORD else None,
            mode=mode,
            top=top,
            vector_k=vector_k,
        )
    except (UpstreamError, ValueError) as exc:
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
            detail = (exc.upstream_detail or exc.message)[:160].replace("|", "\\|")
        else:
            detail = (str(exc) or exc.__class__.__name__)[:160].replace("|", "\\|")
        return [
            f"| {label} | **{status}** | {request_id} | {latency} | — | — | "
            f"FAILED: {detail} | — | — |"
        ]

    diagnostics = client.last_diagnostics
    assert diagnostics is not None  # set on every completed round trip
    evidence.record_request(label, diagnostics.request_body)

    found = _ranks(result.hits, query.expected_chunks)
    shared = (
        f"| {label} | {diagnostics.status} | {diagnostics.request_id} "
        f"| {diagnostics.latency_ms:.1f} | {found} "
    )
    if not result.hits:
        return [shared + "| — | (no results) | — | — |"]
    rows = []
    for rank, hit in enumerate(result.hits, start=1):
        reranker = "-" if hit.reranker_score is None else f"{hit.reranker_score:.3f}"
        rows.append(
            shared + f"| {rank} | `{hit.chunk_id}` | {hit.score:.6f} | {reranker} |"
        )
    return rows


HEADER = (
    "| run | status | request id | ms | expected chunk ranks | rank "
    "| chunk_id | score | reranker |"
)
DIVIDER = "|---|---|---|---|---|---|---|---|---|"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, required=True, help="frozen for every run")
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    client = AzureSearchClient(settings)
    embedding_client = build_embedding_client(settings)

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(arguments.out)
    evidence.add(
        "# Retrieval comparison — live evidence",
        "",
        f"- checked: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"- data-plane API version: `{SEARCH_API_VERSION}`",
        f"- top (frozen for every run): {arguments.top}",
        "- region / SKU / semanticSearch plan: paste from `az search service show`",
        "",
    )
    evidence.flush()

    for query in QUERIES:
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
            detail = (exc.upstream_detail or exc.message)[:160].replace("|", "\\|")
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

        expected = ", ".join(f"`{c}`" for c in query.expected_chunks) or "none (no answer)"
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
                vector,
                mode=mode,
                top=arguments.top,
                vector_k=DEFAULT_VECTOR_K,
            )
            evidence.add(*rows)
            evidence.flush()

        evidence.add(
            "",
            "### Experiment 1 — candidate generation (vector_k is the only variable)",
            "",
            "Fixed: query, vector, filter=None, top, index generation. Mode = VECTOR.",
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
                vector,
                mode=SearchMode.VECTOR,
                top=arguments.top,
                vector_k=vector_k,
            )
            evidence.add(*rows)
            evidence.flush()

        evidence.add(
            "",
            "### Experiment 2 — reranking (mode is the only variable)",
            "",
            "Fixed: query, vector, filter=None, top, vector_k=50, index generation.",
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
                vector,
                mode=mode,
                top=arguments.top,
                vector_k=DEFAULT_VECTOR_K,
            )
            evidence.add(*rows)
            evidence.flush()
        evidence.add("")
        evidence.flush()

    print(f"wrote {arguments.out} and {evidence.sidecar}")


if __name__ == "__main__":
    asyncio.run(main())
