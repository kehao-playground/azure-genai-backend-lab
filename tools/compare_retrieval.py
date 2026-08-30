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
id, its rank, its absence, or the fact that its document is not in this
generation's corpus at all — three states, because collapsing the last two
would record a recall failure that could not have happened.

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

The query vectors are frozen into a JSON file on the first invocation and only
ever read afterwards, so the three generations are queried with the same bytes
rather than with bytes assumed to be the same. Generating and querying are two
separate invocations of the same command: the first writes the file and
queries nothing.

Four conditions stop the run before it can spend anything: fake embeddings, a
worktree that is not clean, a vectors file frozen for a different query set,
and a corpus manifest digest that is not a digest. Each one would otherwise
produce an evidence file that looks complete and cannot be reproduced.

Usage:
    # First: writes the vectors file, queries nothing.
    # Then: the same command again, which reads it and runs.
    uv run python tools/compare_retrieval.py \
        --top 25 --out ../drafts/assets/day-34/g1-acme.md \
        --tenant-id acme --user-id lab-operator \
        --vectors ../drafts/assets/day-34/vectors-acme.json \
        --generation g1 --index-name azgenai-lab-chunks-g1 \
        --manifest-sha256 "$(cat corpora/g1/manifest.json.sha256)"

`--baseline-only` records the four-mode survey and neither experiment: the
control arm, whose corpus does not grow, is surveyed rather than experimented
on, at four calls per query instead of nine.

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
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from azgenai_lab.core.config import Settings, get_settings
from azgenai_lab.core.errors import ConfigurationError, UpstreamError
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.rag import make_chunk_id, make_parent_id
from azgenai_lab.models.search import DEFAULT_VECTOR_K, SearchHit, SearchMode
from azgenai_lab.models.search_index import INDEX_NAME, SEARCH_API_VERSION
from azgenai_lab.services.azure_search import AzureSearchClient
from azgenai_lab.services.embeddings import EmbeddingClient, build_embedding_client

# tools/ sits at the repository root, so the tree this run is anchored to is
# resolved from the module's own location rather than from the working
# directory a run happens to be launched from.
_LAB_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    """Run one git command against the lab tree and return its stdout.

    ``check=True`` would raise a ``CalledProcessError`` whose message is an
    exit code and whose captured stderr is never printed — git's own
    explanation would be swallowed at the one moment it is needed.
    """
    completed = subprocess.run(
        ("git", *args), cwd=_LAB_ROOT, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise SystemExit(f"`git {' '.join(args)}` failed: {completed.stderr.strip()}")
    return completed.stdout


def pre_run_lab_sha() -> str:
    """The commit the evidence anchors to, refusing an unreproducible tree.

    A paid run is worth what a reader can redo. The header's commit is the
    only thing that says which code produced these numbers, and a SHA read
    from a dirty tree names a commit that does not describe that code — there
    is no way to write down "HEAD plus these uncommitted edits" that anyone
    can check out. The failure is otherwise silent: nothing in the finished
    artifact would reveal it, and by then the money is spent. Committing
    first costs seconds.

    ``--porcelain`` with no ``--untracked-files`` override, so untracked
    files count as dirty as well. An untracked file changes what runs — a
    stray document under the sample corpus, a module that shadows an import
    — and a check that called such a tree clean would certify the commit
    anyway. Evidence artifacts belong outside this repo (``--out`` already
    points into the planning repo's ``drafts/assets/``); the vectors file
    should go there too rather than be left untracked here.
    """
    dirty = _git("status", "--porcelain")
    if dirty.strip():
        entries = dirty.splitlines()
        listed = "\n".join(entries[:10])
        if len(entries) > 10:
            listed += f"\n... and {len(entries) - 10} more"
        raise SystemExit(
            "worktree is not clean -- a paid run whose evidence anchors a tree "
            "that never existed cannot be replayed. Commit, stash, or move "
            "these out of the repo first:\n" + listed
        )
    return _git("rev-parse", "HEAD").strip()


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


def _principal_header_line(principal: Principal) -> str:
    """Render the evidence header's principal line without leaking group ids.

    Group values are exactly the ACL contents the sidecar redaction exists to
    keep out of persisted evidence — the header line must not reintroduce
    them via a different path. Only a count is safe to write.
    """
    return f"- principal: tenant_id={principal.tenant_id} group_count={len(principal.group_ids)}"


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


class Generation(StrEnum):
    """Which index generation a corpus, and therefore a chunk, belongs to.

    The same base corpus is indexed three times, each time with more
    distractor documents alongside it. A generation names one of those three
    indexes, not a version of this code.
    """

    G1 = "g1"
    G2 = "g2"
    G3 = "g3"


ALL_GENERATIONS = frozenset(Generation)


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

    ``generations`` is which of the three indexes actually contain the
    document. It defaults to all three because the base corpus is in all
    three; a ref that names a document only some generations carry has to say
    so, or a generation that never held it would be scored as having missed
    it.
    """

    doc_id: str
    ordinal: int
    heading_path: str
    generations: frozenset[Generation] = ALL_GENERATIONS


@dataclass(frozen=True)
class Query:
    """Frozen before the run, from a local chunker run against the live corpus.

    ``base_refs`` and ``expansion_refs`` hold structured references, not raw
    chunk ids: the id is *derived* from ``(tenant_id, doc_id, ordinal)`` via
    ``expected_chunk_ids()`` at run time, never hand-typed and never stored.
    A hand-typed id copy could drift from the id-derivation scheme silently;
    a derived one cannot.

    The two layers are kept apart because they answer to different corpora.
    ``base_refs`` names chunks of the base corpus, which every generation
    indexes; ``expansion_refs`` names chunks that only arrive with a later
    generation's added documents. Merging them would lose the one fact that
    tells a genuine recall failure apart from a document that was not there
    to be found.

    An empty pair of tuples means the corpus genuinely has no answer. Two or
    more entries mean several chunks are legitimately relevant, and every one
    of them is reported separately — stopping at the first would hide whether
    the second was retrieved at all.

    ``language`` is ``"en"`` or ``"zh"``. A Chinese counterpart carries the
    same ``kind`` and the same refs as its English pair, so the pair differs
    in exactly one thing: the language the question is asked in.
    """

    number: int
    text: str
    kind: str
    base_refs: tuple[ExpectedChunkRef, ...]
    expansion_refs: tuple[ExpectedChunkRef, ...]
    language: str


def expected_chunk_ids(tenant_id: str, refs: Sequence[ExpectedChunkRef]) -> tuple[str, ...]:
    """Derive chunk ids for ``refs`` under ``tenant_id`` — never stored, always computed."""
    return tuple(make_chunk_id(make_parent_id(tenant_id, ref.doc_id), ref.ordinal) for ref in refs)


def _vector_digest(vector: Sequence[float]) -> str:
    """A digest of the vector as Python renders its floats.

    ``repr`` of a float round-trips exactly through ``json``, which is what
    makes this survive the write/read cycle: the digest computed before the
    file is written equals the one computed after it is parsed back. Anything
    that reformatted the numbers -- rounding for readability, a different
    serializer -- would make every stored digest disagree with its own vector.
    """
    return hashlib.sha256(repr([float(value) for value in vector]).encode("utf-8")).hexdigest()


async def load_or_create_vectors(
    path: Path, queries: Sequence[Query], embedding_client: EmbeddingClient | None
) -> dict[str, list[float]] | None:
    """Generate once, then only ever read.

    Three index generations must be queried with the same vector bytes.
    Re-embedding per run leaves an unrecorded variable beside the treatment:
    the vectors are probably identical, and probably is not a measurement.
    Returning None means the file was just written and the caller must exit
    without querying, so that generating and spending are never one step.

    The stored keys must be exactly this run's questions. A file written
    before a question was reworded still answers for every other one, so a
    partial read would query most of the set with frozen vectors and the rest
    with freshly diverged ones -- the precise unrecorded variable the freeze
    exists to remove, and invisible in the finished evidence. Both directions
    of the mismatch abort.
    """
    if not path.exists():
        if embedding_client is None:
            raise SystemExit(
                f"{path} does not exist and no embedding client was supplied to write it"
            )
        texts = [query.text for query in queries]
        vectors = await embedding_client.embed(texts)
        payload = {
            text: {"vector": list(vector), "sha256": _vector_digest(vector)}
            for text, vector in zip(texts, vectors, strict=True)
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return None
    stored = json.loads(path.read_text(encoding="utf-8"))
    wanted = {query.text for query in queries}
    if set(stored) != wanted:
        missing = sorted(wanted - set(stored))
        extra = sorted(set(stored) - wanted)
        raise SystemExit(
            f"{path} was frozen for a different query set -- "
            f"no entry for {missing!r}; entries this run never asks for {extra!r}. "
            "Delete it and re-freeze, or point --vectors at the file this "
            "query set was frozen into."
        )
    result: dict[str, list[float]] = {}
    for query in queries:
        entry = stored[query.text]
        vector = [float(value) for value in entry["vector"]]
        if _vector_digest(vector) != entry["sha256"]:
            raise SystemExit(f"vector hash mismatch for: {query.text!r}")
        result[query.text] = vector
    return result


def _positions(hits: Sequence[SearchHit]) -> dict[str, int]:
    """Chunk id -> 1-based rank in the returned order.

    One definition, because two renderers read it. A second copy of this
    comprehension could drift from the first — an off-by-one in one of them
    would make two tables built from the same response disagree about where a
    chunk landed, with nothing to say which was right.
    """
    return {hit.chunk_id: rank for rank, hit in enumerate(hits, start=1)}


def rank_states(
    hits: Sequence[SearchHit],
    refs: Sequence[ExpectedChunkRef],
    *,
    generation: Generation,
    tenant_id: str,
) -> list[tuple[str, str]]:
    """Three states, because two would lie about one of them.

    ``absent`` is a recall failure: the chunk was in this generation's corpus
    and did not come back. ``not_in_generation`` is not an observation at all
    — the document was not there to be found. Reporting them with one token
    would put a failure that could not have happened into paid evidence, and
    then into a recall count.

    A chunk that comes back while its ref says its document is not in this
    generation is neither state: the frozen ref table and the live index
    contradict each other, and one of them is wrong. That raises ``ValueError``
    rather than resolving in either direction. Recording it as
    ``not_in_generation`` would silently discard a real observation, and
    recording it as a rank would keep a ``generations`` set that has just been
    shown to be false. Every recall number from the run would rest on the
    disagreement either way. Evidence is checkpointed after every call, so
    aborting loses nothing already paid for.

    Callers must invoke this *outside* the try/except that wraps a search
    call. This ``ValueError`` is a bug in the frozen table, not a rejected
    request, and recording it as a failed call would file it under the one
    thing it is not.
    """
    positions = _positions(hits)
    states: list[tuple[str, str]] = []
    for ref in refs:
        (chunk_id,) = expected_chunk_ids(tenant_id, (ref,))
        rank = positions.get(chunk_id)
        if generation not in ref.generations:
            if rank is not None:
                declared = ", ".join(sorted(g.value for g in ref.generations)) or "none"
                raise ValueError(
                    f"{chunk_id} came back at rank {rank} from generation "
                    f"{generation.value}, but its pre-registered ref declares it "
                    f"present only in [{declared}]. The frozen ref table and the "
                    "index this run queried disagree; re-freeze the refs against "
                    "the corpus this generation was actually built from before "
                    "spending another run."
                )
            states.append((chunk_id, "not_in_generation"))
            continue
        states.append((chunk_id, str(rank) if rank is not None else "absent"))
    return states


_NO_REFS: tuple[ExpectedChunkRef, ...] = ()

# One refs tuple and one `kind` string per question, referenced by both
# members of an English/Chinese pair. Sharing the objects rather than
# repeating the literals is what makes "the pair differs only in language"
# true by construction: there is no second copy for a later hand-edit to
# change on one side only.
_ACME_Q1_REFS = (
    ExpectedChunkRef("service-sla", 3, "Service SLA > Availability targets > Premium tier"),
)
_ACME_Q2_REFS = (
    ExpectedChunkRef("returns-policy", 2, "Returns Policy > Refund window > Promotional purchases"),
)
_ACME_Q3_REFS = (
    ExpectedChunkRef("service-sla", 2, "Service SLA > Availability targets > Standard tier"),
    ExpectedChunkRef("returns-policy", 2, "Returns Policy > Refund window > Promotional purchases"),
)
_ACME_Q4_REFS = (ExpectedChunkRef("service-sla", 5, "Service SLA > Exclusions"),)
_ACME_Q5_REFS = (ExpectedChunkRef("service-sla", 4, "Service SLA > Response times"),)

_ACME_Q1_KIND = "exact literal"
_ACME_Q2_KIND = "paraphrase"
_ACME_Q3_KIND = "cross-document ambiguity (both relevant)"
_ACME_Q4_KIND = "lexical decoy"
_ACME_Q5_KIND = "acme has no runbook — only its own SLA response-time section is relevant"
_ACME_Q6_KIND = "absent from corpus"

# Filled in from a local chunker run against the live corpus, before any
# query is issued. Choosing an expected ordinal/heading after seeing rankings
# would make the whole comparison unfalsifiable — freeze first, run second.
# Split by tenant: a query issued with tenant A's principal can only ever see
# tenant A's chunks, so a query authored against tenant B's corpus belongs in
# tenant B's tuple, not in a shared one.
#
# The acme set is paired: every question is asked once in English and once in
# Traditional Chinese, with the same `kind` and the same refs, so the pair is
# meant to isolate the language. The Chinese strings are experimental data —
# the cross-language arm cannot exist without them — and are the only
# non-English text in this module.
#
# The pairing is not clean, and these four declared asymmetries say where. The
# index's `content` field is the only analyzed one and pins
# `analyzer: "en.microsoft"`, so every Chinese question is tokenised by an
# English analyser; three of the four follow from that. An undeclared
# asymmetry is what would make a result unreadable; a declared one is a
# property of the experiment.
#
#   1. Q1's `kind` is "exact literal", and the literal is English source text.
#      `99.9%` survives as a numeral and nothing else does, so the zh side
#      matches no literal in any other token. Q1 compares a literal match
#      against a numeral match plus whatever the vector side contributes.
#   2. Q4's `kind` is "lexical decoy", and the decoy is English vocabulary
#      ("customer", "system", "configur*") occurring outside the Exclusions
#      section. A Chinese query shares no surface tokens with any of it, so on
#      the lexical side there is nothing left to be decoyed by; Q4-zh is a
#      vector-side question wearing a lexical-decoy label.
#   3. Q5-zh keeps `Sev 1` as a Latin token, which is how Taiwanese ops write
#      it — so it is the one Chinese query carrying a lexical anchor the
#      English analyser can tokenise. Five of the six have essentially no
#      lexical contribution and one has some, so any aggregate over "the
#      Chinese arm" mixes two regimes.
#   4. Q3's `kind` is cross-document ambiguity. In English one polysemous noun
#      ("credit") reaches both the SLA and the returns policy. Traditional
#      Chinese has no equally polysemous noun, and the phrasing chosen for the
#      zh side asks when the customer gets money back — which is what a refund
#      is, whereas an SLA service credit is applied to the account rather than
#      returned. The zh side therefore leans toward the returns policy: this
#      pair varies ambiguity strength as well as language, and a Q3 difference
#      must not be read as a pure language effect.
#
# globex is the control arm and is deliberately not translated: if the paired
# arm moves and the untranslated one does not, the pairing is the difference.
QUERIES_BY_TENANT: dict[str, tuple[Query, ...]] = {
    "acme": (
        Query(
            1,
            "99.9% monthly uptime",
            _ACME_Q1_KIND,
            base_refs=_ACME_Q1_REFS,
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            1,
            "99.9% 的每月可用率",
            _ACME_Q1_KIND,
            base_refs=_ACME_Q1_REFS,
            expansion_refs=_NO_REFS,
            language="zh",
        ),
        Query(
            2,
            "How long do I have to send something back if I bought it on sale?",
            _ACME_Q2_KIND,
            base_refs=_ACME_Q2_REFS,
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            2,
            "特價買的東西還有多久可以退回去？",
            _ACME_Q2_KIND,
            base_refs=_ACME_Q2_REFS,
            expansion_refs=_NO_REFS,
            language="zh",
        ),
        Query(
            3,
            "when do customers get credit?",
            _ACME_Q3_KIND,
            base_refs=_ACME_Q3_REFS,
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            3,
            "客戶什麼時候可以拿回錢？",
            _ACME_Q3_KIND,
            base_refs=_ACME_Q3_REFS,
            expansion_refs=_NO_REFS,
            language="zh",
        ),
        Query(
            4,
            "what happens if the customer misconfigured their own system?",
            _ACME_Q4_KIND,
            base_refs=_ACME_Q4_REFS,
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            4,
            "如果是客戶自己把系統設定弄錯了會怎樣？",
            _ACME_Q4_KIND,
            base_refs=_ACME_Q4_REFS,
            expansion_refs=_NO_REFS,
            language="zh",
        ),
        Query(
            5,
            "how do I escalate a Sev 1 outage at 3am?",
            _ACME_Q5_KIND,
            base_refs=_ACME_Q5_REFS,
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            5,
            "凌晨三點發生 Sev 1 中斷要怎麼往上升級？",
            _ACME_Q5_KIND,
            base_refs=_ACME_Q5_REFS,
            expansion_refs=_NO_REFS,
            language="zh",
        ),
        Query(
            6,
            "What is the parental leave policy?",
            _ACME_Q6_KIND,
            base_refs=_NO_REFS,
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            6,
            "育嬰假的規定是什麼？",
            _ACME_Q6_KIND,
            base_refs=_NO_REFS,
            expansion_refs=_NO_REFS,
            language="zh",
        ),
    ),
    "globex": (
        Query(
            1,
            "how are invoices delivered?",
            "exact literal",
            base_refs=(ExpectedChunkRef("billing-faq", 1, "Billing FAQ > Invoices"),),
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            2,
            "what cards can I pay with?",
            "paraphrase",
            base_refs=(ExpectedChunkRef("billing-faq", 2, "Billing FAQ > Payment methods"),),
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            3,
            "how do I dispute a charge?",
            "lexical decoy",
            base_refs=(ExpectedChunkRef("billing-faq", 3, "Billing FAQ > Disputes"),),
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            5,
            "how do I escalate a Sev 1 outage at 3am?",
            "requires the oncall group — run with --group-id oncall",
            base_refs=(ExpectedChunkRef("oncall-runbook", 3, "On-Call Runbook > Escalation path"),),
            expansion_refs=_NO_REFS,
            language="en",
        ),
        Query(
            6,
            "What is the parental leave policy?",
            "absent from corpus",
            base_refs=_NO_REFS,
            expansion_refs=_NO_REFS,
            language="en",
        ),
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


def _experiment_sweeps(*, baseline_only: bool) -> tuple[tuple[int, ...], tuple[SearchMode, ...]]:
    """Which sweeps run beyond the four-mode baseline survey.

    The control arm is surveyed, not experimented on: it exists to show what
    the treated arm's corpus growth did *not* do to it, and both experiments
    would answer a question nobody asked of it. Nine calls per query instead
    of four is not free: the extra five are semantic-tier queries against a
    capped budget, which is why this is a flag the run has to be given rather
    than a step in a runbook someone has to remember.
    """
    if baseline_only:
        return (), ()
    return VECTOR_K_SWEEP, RERANKING_MODES


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


def _render_rank_states(states: Sequence[tuple[str, str]]) -> str:
    """One cell per pre-registered chunk: its rank, `absent`, or `not_in_generation`.

    ``rank_states`` expresses "this query has no expected answer" as an empty
    list. Joining that would render an empty string, leaving Q6's row — the
    query whose whole point is that the corpus cannot answer it — blank in
    paid evidence, indistinguishable from a bug in this renderer.
    """
    if not states:
        return "n/a (no answer expected)"
    return "; ".join(f"`{chunk_id}`={state}" for chunk_id, state in states)


def _hit_row(rank: int, hit: SearchHit) -> str:
    """One detail-table row for a single hit, rank already 1-based."""
    reranker = "-" if hit.reranker_score is None else f"{hit.reranker_score:.3f}"
    return f"| {rank} | `{hit.chunk_id}` | {hit.score:.6f} | {reranker} |"


def _detail_rows(
    hits: Sequence[SearchHit], *, max_recorded_hits: int
) -> tuple[list[str], str]:
    """Render at most N hit rows, and say so when there were more.

    Only the rendering is capped. Every pre-registered chunk's rank is
    computed by `rank_states` from the complete hit list before this runs, so
    a gold chunk at rank 380 is recorded as 380 even when ten rows are shown.
    A file that quietly showed ten rows of five hundred would be
    indistinguishable from a run that only returned ten.
    """
    shown = list(hits[:max_recorded_hits])
    rows = [_hit_row(rank, hit) for rank, hit in enumerate(shown, start=1)]
    if len(hits) <= max_recorded_hits:
        return rows, ""
    return rows, (
        f"_{len(hits) - max_recorded_hits} of {len(hits)} hit rows omitted; "
        "pre-registered ranks above are computed from the full list._"
    )


async def _run(
    client: AzureSearchClient,
    evidence: Evidence,
    label: str,
    query: Query,
    refs: Sequence[ExpectedChunkRef],
    vector: list[float],
    principal: Principal,
    *,
    generation: Generation,
    mode: SearchMode,
    top: int,
    vector_k: int,
    search_endpoint: str | None,
    max_recorded_hits: int,
) -> list[str]:
    """One call, one set of table rows.

    A failed call — including argument validation that rejects the request
    before it is ever sent — is evidence, not an error, and is returned as a
    row rather than raised. Two things do propagate.

    ``ConfigurationError`` (a bad key, a missing index): our own deployment is
    broken, every remaining call in this run would fail identically, so this
    still records the one row that call produced — aborting the run must not
    discard the diagnostic it is aborting because of — flushes it, and then
    re-raises so the caller can abort instead of spending a paid run
    collecting N copies of the same error.

    ``ValueError`` from ``rank_states``: the frozen ref table and the index
    this run queried contradict each other. It is raised *after* the
    try/except below, deliberately — inside it, a bug in the frozen table
    would be caught by the ``ValueError`` arm meant for a rejected request and
    filed as a failed search call, which is the one thing it is not.
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
            f"| {label} | **{status}** | {request_id} | {latency} | — | — | — | "
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

    try:
        states = rank_states(
            result.hits, refs, generation=generation, tenant_id=principal.tenant_id
        )
    except ValueError:
        # This call was paid for and its request is recorded but not yet
        # written. Flush before the abort propagates, for the same reason the
        # ConfigurationError arm above does: aborting must not discard the
        # evidence of the call it is aborting on. The exception is re-raised
        # unchanged — it is a bug in the frozen ref table, and nothing here
        # turns it into a table row.
        evidence.flush()
        raise
    found = _render_rank_states(states)
    shared = (
        f"| {label} | {diagnostics.status} | {diagnostics.request_id} "
        f"| {diagnostics.latency_ms:.1f} | {len(result.hits)} | {found} "
    )
    if not result.hits:
        return [shared + "| — | (no results) | — | — |"]
    detail_rows, note = _detail_rows(result.hits, max_recorded_hits=max_recorded_hits)
    rows = [shared + detail_row for detail_row in detail_rows]
    if note:
        rows.append(note)
    return rows


HEADER = (
    "| run | status | request id | ms | hits_total | expected chunk ranks "
    "| rank | chunk_id | score | reranker |"
)
DIVIDER = "|---|---|---|---|---|---|---|---|---|---|"


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _manifest_digest(raw: str) -> str:
    """Accept a corpus manifest digest, or reject it here rather than in the evidence.

    `build_distractor_corpus.py` writes `manifest.json.sha256` in sha256sum's
    convention, digest plus a trailing newline. A caller that reads that file
    without stripping hands this a 65-character string, which would be
    recorded verbatim and match nothing a reader later computes — a
    discrepancy with no visible cause, in a file that is otherwise correct.
    Strip, then insist on exactly the 64 lowercase hex characters, so a
    whitespace slip is a loud argparse error at the boundary instead.
    """
    candidate = raw.strip()
    if not _SHA256.fullmatch(candidate):
        raise argparse.ArgumentTypeError(
            f"expected 64 lowercase hex characters, got {raw!r} -- pass the "
            "contents of the corpus's manifest.json.sha256 with surrounding "
            "whitespace stripped"
        )
    return candidate


def _build_parser() -> argparse.ArgumentParser:
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
        "--user-id",
        required=True,
        help="the principal's user id; identity is required from Day 19 onward",
    )
    parser.add_argument(
        "--group-id",
        action="append",
        default=[],
        help="repeatable; a query gated behind allowed_groups needs its group here",
    )
    parser.add_argument(
        "--max-recorded-hits",
        type=int,
        default=10,
        help=(
            "caps rendered hit rows per table; does not affect --top or the "
            "recorded rank of a pre-registered chunk, which is always read "
            "from the full response"
        ),
    )
    parser.add_argument(
        "--vectors",
        type=Path,
        required=True,
        help=(
            "the frozen query vectors. Written and nothing else on the first "
            "run; read and nothing else on every run after, so all three "
            "generations are queried with the same bytes"
        ),
    )
    parser.add_argument(
        "--generation",
        type=Generation,
        choices=list(Generation),
        required=True,
        help=(
            "which index generation this run queries; recorded in the header "
            "and used to tell a recall failure from a document that was never "
            "in this corpus"
        ),
    )
    parser.add_argument(
        "--index-name",
        default=INDEX_NAME,
        help=(
            "the index this generation lives in; the three generations "
            "coexist on one service, so a run against any index other than "
            "the default has to name it"
        ),
    )
    parser.add_argument(
        "--manifest-sha256",
        type=_manifest_digest,
        required=True,
        help=(
            "digest of the corpus manifest this generation was built from, "
            "pinning at query time which corpus produced these numbers"
        ),
    )
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help=(
            "run the four-mode baseline survey and neither experiment -- the "
            "control arm, whose corpus does not grow, is surveyed rather than "
            "experimented on (4 calls per query instead of 9)"
        ),
    )
    return parser


async def main() -> None:
    arguments = _build_parser().parse_args()

    # Before the settings are read, before a client exists, before anything
    # can be spent: the tree this evidence will name has to be one a reader
    # can check out.
    lab_sha = pre_run_lab_sha()

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
    principal = Principal(
        tenant_id=arguments.tenant_id,
        user_id=arguments.user_id,
        group_ids=tuple(arguments.group_id),
    )
    configure_logging(settings.log_level)
    embedding_client = build_embedding_client(settings)

    queries = QUERIES_BY_TENANT[arguments.tenant_id]
    vectors = await load_or_create_vectors(arguments.vectors, queries, embedding_client)
    if vectors is None:
        # Generating and spending are never one step. Nothing was queried and
        # no evidence file exists yet; the same command run again reads what
        # was just written.
        print(
            f"wrote {arguments.vectors} — nothing was queried. Review it, then "
            "run the same command again to query with those frozen vectors."
        )
        return

    # The client owns its connection pool here, so it is closed on the way out
    # — including when a ConfigurationError below aborts the run partway.
    async with AzureSearchClient(settings, index_name=arguments.index_name) as client:
        await _compare(
            client,
            embedding_client,
            settings,
            arguments,
            principal,
            queries=queries,
            vectors=vectors,
            lab_sha=lab_sha,
        )


async def _compare(
    client: AzureSearchClient,
    embedding_client: EmbeddingClient,
    settings: Settings,
    arguments: argparse.Namespace,
    principal: Principal,
    *,
    queries: Sequence[Query],
    vectors: dict[str, list[float]],
    lab_sha: str,
) -> None:
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(arguments.out, total_queries=len(queries))
    evidence.add(
        "# Retrieval comparison — live evidence",
        "",
        f"- checked: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        _principal_header_line(principal),
        f"- embedding client: {embedding_client.__class__.__name__} "
        f"deployment={settings.azure_openai_embedding_deployment}",
        f"- data-plane API version: `{SEARCH_API_VERSION}`",
        f"- top (frozen for every run): {arguments.top}",
        f"- pre-run lab commit: `{lab_sha}`",
        f"- generation: `{arguments.generation.value}` (index `{arguments.index_name}`)",
        f"- corpus manifest sha256: `{arguments.manifest_sha256}`",
        f"- query vectors: `{arguments.vectors.name}` "
        f"sha256=`{hashlib.sha256(arguments.vectors.read_bytes()).hexdigest()}` "
        "(frozen once; every generation queries these same bytes)",
        "- region / SKU / semanticSearch plan: paste the projected fields "
        "`infra/scripts/create-search.sh` prints (`sku`/`location`/"
        "`semanticSearch` only) — never the unprojected `az search service "
        "show` output, which includes the subscription id",
        "",
    )
    evidence.flush()

    vector_k_sweep, reranking_modes = _experiment_sweeps(
        baseline_only=arguments.baseline_only
    )

    for query in queries:
        refs = query.base_refs + query.expansion_refs
        expected_ids = expected_chunk_ids(principal.tenant_id, refs)
        # The query set now holds two questions per number, one per
        # language. Headings and run labels carry the language so a pair's
        # two halves stay distinguishable in the evidence and the sidecar.
        label_prefix = f"Q{query.number} {query.language}"
        evidence.start_query()
        # Read, never computed: `load_or_create_vectors` has already checked
        # that every question in this set has an entry and that the entry
        # matches its own digest, so there is no embedding call here to time
        # or to fail.
        vector = vectors[query.text]

        expected = ", ".join(f"`{c}`" for c in expected_ids) or "none (no answer)"
        evidence.add(
            f"## Q{query.number} ({query.language}) — {query.kind}",
            "",
            f"> {query.text}",
            "",
            f"- pre-registered chunk(s): {expected}",
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
                f"{label_prefix} baseline {mode.value}",
                query,
                refs,
                vector,
                principal,
                generation=arguments.generation,
                mode=mode,
                top=arguments.top,
                vector_k=DEFAULT_VECTOR_K,
                search_endpoint=settings.azure_search_endpoint,
                max_recorded_hits=arguments.max_recorded_hits,
            )
            evidence.add(*rows)
            evidence.flush()

        if vector_k_sweep:
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
        for vector_k in vector_k_sweep:
            rows = await _run(
                client,
                evidence,
                f"{label_prefix} k={vector_k}",
                query,
                refs,
                vector,
                principal,
                generation=arguments.generation,
                mode=SearchMode.VECTOR,
                top=arguments.top,
                vector_k=vector_k,
                search_endpoint=settings.azure_search_endpoint,
                max_recorded_hits=arguments.max_recorded_hits,
            )
            evidence.add(*rows)
            evidence.flush()

        if reranking_modes:
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
        for mode in reranking_modes:
            rows = await _run(
                client,
                evidence,
                f"{label_prefix} rerank {mode.value}",
                query,
                refs,
                vector,
                principal,
                generation=arguments.generation,
                mode=mode,
                top=arguments.top,
                vector_k=DEFAULT_VECTOR_K,
                search_endpoint=settings.azure_search_endpoint,
                max_recorded_hits=arguments.max_recorded_hits,
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
