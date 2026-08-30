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

Seven things stop the run before it can spend anything, in the order they
fire: a ``--top`` outside the service's own page-size bounds and a
``--manifest-sha256`` value that is neither a digest nor the literal ``none``
(both rejected while parsing), an index name carrying a generation token that
contradicts ``--generation`` or left at the base index for a generation that
does not live there, a ``--generation``/``--manifest-sha256`` pair that
contradicts itself (``g1`` paired with a digest, or ``g2``/``g3`` paired with
``none``), a worktree that is not clean, fake embeddings, and a vectors file
that is not the frozen one this query set needs — either its keys are not
exactly these questions, or a stored vector does not match its own digest.
Each one would otherwise produce an evidence file that looks complete and
either cannot be reproduced or is labelled with something it did not measure.

Usage:
    # First: writes the vectors file, queries nothing.
    # Then: the same command again, which reads it and runs.
    uv run python tools/compare_retrieval.py \
        --top 14 --out ../drafts/assets/day-34/g1-acme.md \
        --tenant-id acme --user-id lab-operator \
        --vectors ../drafts/assets/day-34/vectors-acme.json \
        --generation g1 --index-name azgenai-lab-chunks-g1 \
        --manifest-sha256 none

    # Four arguments change between generations, not two. g2:
    #     --top 156 \
    #     --generation g2 --index-name azgenai-lab-chunks-g2 \
    #     --manifest-sha256 "$(cat /tmp/bonus7/g2/manifest.json.sha256)"
    # and g3:
    #     --top 774 \
    #     --generation g3 --index-name azgenai-lab-chunks-g3 \
    #     --manifest-sha256 "$(cat /tmp/bonus7/g3/manifest.json.sha256)"
    # The corpora live outside this repository on purpose: building them in
    # the worktree makes `git status --porcelain` non-empty, which the
    # pre-run cleanliness check below refuses.

`--baseline-only` records the four-mode survey and neither experiment: the
control arm, whose corpus does not grow, is surveyed rather than experimented
on, at four calls per query instead of nine.

``top`` must be **strictly greater** than the visible corpus's chunk count —
14 / 156 / 774 for the measured g1 / g2 / g3 counts of 13 / 155 / 773. Below
the count, a chunk that was generated as a candidate but truncated out of the
response is indistinguishable from one that was never a candidate, which is
the distinction the candidate generation experiment exists to measure. *At*
the count the ambiguity survives in a subtler form: a query matching every
chunk fills the page legitimately, so ``len(hits) == top`` no longer means
anything. One above it, a full page cannot be legitimate, which is what makes
the refusal in ``_run`` sound — it fires on the first call rather than after
all 108 are spent. The output filename names the tier, because nothing inside
the file records which one produced it.
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
from azgenai_lab.models.search import (
    DEFAULT_VECTOR_K,
    MAX_TOP,
    MIN_BOUND,
    SearchHit,
    SearchMode,
)
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

# Every `expansion_refs` below is `_NO_REFS`, and that is a finding, not an
# omission. Before the first query was issued, the author read all 77
# admitted distractor documents (773 acme-visible chunks at g3, 155 at g2)
# against all twelve acme questions and found no chunk that answers one. The
# distractor corpus is Azure/GenAI engineering prose; the questions ask about
# uptime targets, refund windows, service credits, SLA exclusions and Sev 1
# escalation. Four near-misses were read in full and ruled out on the
# record — see section 8.4 ("preregistration record") of
# `drafts/research/bonus-7-retrieval-mode-selection.md` in the planning repo
# — the strongest being a *fabricated* return window inside a chunking
# tutorial, which contradicts the policy it resembles and would have scored a
# wrong answer as a hit.
#
# Q6 therefore stays "absent from corpus" at all three generations, in both
# languages. This table is frozen: adding or removing a ref after the run
# would make the comparison unfalsifiable, because nothing afterwards could
# tell "froze the labels, then measured" from "measured, then chose labels".
# `tests/unit/test_compare_retrieval.py` holds the emptiness as an assertion.

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


def _abort(evidence: Evidence, reason: str) -> None:
    """Write why the run stops, then flush, before the abort propagates.

    The published artifact is the Markdown; the exception text is stderr,
    which nobody reads afterwards. A file that stops mid-table with a footer
    counter below total and no stated cause tells a reader that something
    went wrong and nothing about what, so every abort leaves this line. The
    blank line first is not cosmetic: a bare paragraph glued to the last row
    of a Markdown table is parsed as part of the table.
    """
    evidence.add("", f"**Aborted: {reason}**")
    evidence.flush()


def _reject_full_page(
    evidence: Evidence,
    label: str,
    query: Query,
    *,
    mode: SearchMode,
    top: int,
    hits: int,
) -> None:
    """Refuse a response that exactly fills the page.

    ``top`` is prescribed strictly above the visible corpus's chunk count, so
    a full page cannot be a corpus that merely ended on the boundary — it is
    the service saying there was more and it stopped. Which is precisely the
    reading the three-state rank vocabulary must never have to guess at: past
    the cut every pre-registered chunk records ``absent``, defined as a recall
    failure, and nothing in the finished file would reveal that no such
    failure happened.

    It fires on the first call of the run, before the other 107 are spent, and
    the operator's fix is a run parameter rather than a re-freeze — so this
    costs one call and no correction to any frozen artifact.
    """
    if hits != top:
        return
    _abort(
        evidence,
        f"{label} returned exactly --top ({top}) hits for {query.text!r} in "
        f"mode {mode.value}. A response that fills the page cannot be told "
        "apart from one the page truncated, and that distinction is what "
        "this experiment measures.",
    )
    raise SystemExit(
        f"{label}: the search returned exactly --top ({top}) hits for "
        f"{query.text!r} in mode {mode.value}. A full page is either the whole "
        "corpus or a truncated one, and this run cannot tell which — every "
        "pre-registered chunk past the cut would be recorded as `absent`, "
        "which the rank vocabulary defines as a recall failure. Re-run with "
        "--top strictly greater than this generation's visible chunk count "
        "(14 / 156 / 774 for g1 / g2 / g3) and discard this evidence file."
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

    A third: a response holding exactly ``top`` hits. See ``_reject_full_page``.
    Every one of the three writes its own explanation into the Markdown before
    it propagates. Aborting must not leave a reader a half-written table, a
    footer counter below total, and no stated cause.
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

    _reject_full_page(evidence, label, query, mode=mode, top=top, hits=len(result.hits))

    try:
        states = rank_states(
            result.hits, refs, generation=generation, tenant_id=principal.tenant_id
        )
    except ValueError as exc:
        # This call was paid for and its request is recorded but not yet
        # written. Write the reason and flush before the abort propagates, for
        # the same reason the ConfigurationError arm above does: aborting must
        # not discard the evidence of the call it is aborting on — and here
        # the evidence *is* the reason, since no row can be rendered from a
        # contradiction. Without this line the artifact stops mid-table with
        # nothing saying why, because the exception text goes to stderr, which
        # is not published. The exception is re-raised unchanged: it is a bug
        # in the frozen ref table, and nothing here turns it into a table row.
        _abort(evidence, _scrub(str(exc), search_endpoint, "[search-service]"))
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


# `g1` is the unchanged sample corpus: no distractor documents, and
# `build_distractor_corpus.py` refuses to produce a zero-document manifest
# for it. So `g1` has no manifest to pin, by design rather than by omission.
# This is the one value `_manifest_digest` accepts besides a digest, and the
# one value `_reject_generation_manifest_mismatch` accepts for `g1`.
NO_MANIFEST = "none"


def _manifest_digest(raw: str) -> str:
    """Accept a corpus manifest digest or the no-manifest sentinel.

    `build_distractor_corpus.py` writes `manifest.json.sha256` in sha256sum's
    convention, digest plus a trailing newline. A caller that reads that file
    without stripping hands this a 65-character string, which would be
    recorded verbatim and match nothing a reader later computes — a
    discrepancy with no visible cause, in a file that is otherwise correct.
    Strip, then insist on exactly the 64 lowercase hex characters or the
    literal ``none``, so a whitespace slip -- or a base-corpus digest smuggled
    in for a generation that has no manifest at all -- is a loud argparse
    error at the boundary instead of a false label in paid evidence.
    """
    candidate = raw.strip()
    if candidate == NO_MANIFEST:
        return candidate
    if not _SHA256.fullmatch(candidate):
        raise argparse.ArgumentTypeError(
            f"expected 64 lowercase hex characters or the literal "
            f"{NO_MANIFEST!r}, got {raw!r} -- pass the contents of the "
            "corpus's manifest.json.sha256 with surrounding whitespace "
            f"stripped, or {NO_MANIFEST!r} for a generation with no "
            "distractor corpus"
        )
    return candidate


def _top(raw: str) -> int:
    """Bound ``--top`` by the service's own page-size limits while parsing.

    ``validate_search_arguments`` enforces the same bounds, but it enforces
    them per call: an out-of-range ``top`` is caught there as a rejected
    request, recorded as a ``FAILED`` row, and the loop carries on to write
    108 of them under a ``**Run complete**`` footer. Nothing is spent, so the
    cost is not money — it is an evidence file that looks like a measurement
    and is a typo. Both bounds are knowable from argv alone, so this is where
    they belong; an exit 2 says so in one line.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from None
    if not MIN_BOUND <= value <= MAX_TOP:
        raise argparse.ArgumentTypeError(
            f"--top must be between {MIN_BOUND} and {MAX_TOP} (the service's "
            f"documented maximum page size), got {value}. A larger value is "
            "not rejected by the service, it is silently honoured as "
            f"{MAX_TOP} -- which is why this experiment cannot measure a "
            f"visible corpus above {MAX_TOP} chunks at all."
        )
    return value


def _manifest_header_line(manifest_sha256: str) -> str:
    """Render the corpus-manifest evidence line, honest about an absent one.

    Printing the sentinel verbatim, or an empty value, would read as a digest
    computation that silently failed. The absence is by design -- `g1` has no
    distractor corpus to have a manifest for -- so the line says that instead
    of leaving a reader to guess why the field looks empty.
    """
    if manifest_sha256 == NO_MANIFEST:
        return "- corpus manifest sha256: none -- this generation has no distractor corpus"
    return f"- corpus manifest sha256: `{manifest_sha256}`"


# A generation token occupying a whole dash-delimited segment, anywhere in the
# name. Anchored with ``\A``/``\Z`` rather than ``^``/``$``, for the reason
# ``_manifest_digest`` gives: ``$`` also matches before a trailing newline,
# and a module should not contain its own counterexample.
_GENERATION_SEGMENT = re.compile(
    rf"(?:\A|-)({'|'.join(g.value for g in Generation)})(?=-|\Z)"
)


def _reject_index_generation_mismatch(index_name: str, generation: Generation) -> None:
    """Refuse a command line that contradicts itself, and stay quiet otherwise.

    The run's convention is ``azgenai-lab-chunks-<generation>``, but this does
    not enforce that convention: ``INDEX_NAME`` itself carries no generation
    token, so a tool that demanded one would reject its own default. What it
    rejects is narrower and not a matter of taste — a name carrying a
    generation token *other* than the one ``--generation`` names. There is no
    reading of ``--generation g2 --index-name ...-g3`` that anyone meant: one
    of the two is a typo, and either way the run would label a paid evidence
    file with a generation it did not query. That is the same false-label
    failure the three-state rank vocabulary exists to prevent, and it is
    cheaper to catch here than to notice afterwards.

    The token is matched as a whole ``-gN-`` segment anywhere in the name, not
    only at the end. ``azgenai-lab-chunks-g3-retry`` is exactly as
    self-contradictory under ``--generation g2`` as ``...-g3`` is, and this
    project has already been forced onto Free-tier retries twice, so a name
    that is not the last segment is not a hypothetical. Every token is
    checked, so a name carrying two of them cannot pass by agreeing with the
    first.

    One name is refused despite carrying no token at all: ``INDEX_NAME``
    itself, under ``g2`` or ``g3``. That is the index every earlier day's
    runbook creates, from the unmodified sample corpus and with no
    ``--index-name`` argument anywhere in the series — so it is provably not
    where a distractor generation lives, and it is also what omitting
    ``--index-name`` here silently selects. Omission, not contradiction, is
    the hole: a session that ran any smoke check first leaves that index
    populated, and 108 paid calls then return g1-shaped numbers under a g2 or
    g3 label, with the correct manifest digest printed beside them. By the
    time this guard runs the tool already knows ``--generation`` is not
    ``g1``, so nothing has to be guessed.

    Every other name with no generation token passes silently, and so does
    one where ``gN`` is not a whole segment (``azgenai-g2lab``). The tool has
    no basis for an opinion about those: for ``g2``/``g3``, which corpus an
    index really holds is pinned by ``--manifest-sha256``, not by a string,
    and guessing from the string would turn a rename into an outage.

    That pin does not exist for ``g1``, whose manifest is ``none`` by design
    (``_reject_generation_manifest_mismatch``) because it has no distractor
    corpus to have a manifest for. On a ``g1`` run neither argument pins the
    corpus: what anchors it is the base-corpus digest recorded in the
    research doc alongside the evidence, which this tool neither takes nor
    checks. ``g1`` is the one generation whose target index is legitimately
    the default, which is why the refusal above cannot cover it.
    """
    if generation is not Generation.G1 and index_name == INDEX_NAME:
        raise SystemExit(
            f"--generation {generation.value} with --index-name "
            f"{INDEX_NAME!r} (the default, so this is also what omitting "
            "--index-name selects): that is the base index every earlier "
            "day's runbook builds from the unmodified sample corpus, and it "
            f"is provably not where {generation.value}'s distractor corpus "
            "lives. If it happens to be populated, this run completes and "
            "labels g1-shaped numbers as "
            f"{generation.value}. Name the index this generation was built "
            f"into (the convention is azgenai-lab-chunks-{generation.value})."
        )
    contradicting = [
        token for token in _GENERATION_SEGMENT.findall(index_name) if token != generation.value
    ]
    if not contradicting:
        return
    named = ", ".join(f"-{token}" for token in contradicting)
    raise SystemExit(
        f"--generation {generation.value} but --index-name {index_name!r} "
        f"carries {named}: the two disagree about which generation this run "
        "queries, and the evidence header would carry whichever one is wrong. "
        "Fix the command line before spending a run."
    )


def _reject_generation_manifest_mismatch(generation: Generation, manifest_sha256: str) -> None:
    """Refuse a ``--generation``/``--manifest-sha256`` pair that contradicts itself.

    ``g1`` is definitionally the no-distractor generation in this experiment
    -- the base corpus alone, with `build_distractor_corpus.py` refusing to
    build it a manifest for zero documents. So there are exactly two illegal
    pairings: ``g1`` with an actual digest, and ``g2``/``g3`` -- which do have
    a distractor corpus, and therefore a manifest -- with the ``none``
    sentinel. Either one would make ``_manifest_header_line`` print a label
    that does not match what the run actually queried: a manifest digest
    claimed for a generation that has none, or "no distractor corpus" claimed
    for one that has one. That is the same false-label failure
    ``_reject_index_generation_mismatch`` exists to catch on the other
    argument, so it is refused here on the same terms and before the same
    point -- a command line an operator did not mean, caught before it can
    spend anything.
    """
    is_none = manifest_sha256 == NO_MANIFEST
    if generation is Generation.G1 and not is_none:
        raise SystemExit(
            f"--generation g1 --manifest-sha256 {manifest_sha256!r}: g1 is the "
            "baseline generation and has no distractor corpus, so it has no "
            f"manifest to pin. Pass --manifest-sha256 {NO_MANIFEST!r} for g1, "
            "or fix --generation if this run is meant to query a generation "
            "that does have a distractor corpus."
        )
    if generation is not Generation.G1 and is_none:
        raise SystemExit(
            f"--generation {generation.value} --manifest-sha256 "
            f"{NO_MANIFEST!r}: {generation.value} has a distractor corpus "
            "built by build_distractor_corpus.py, so it has a manifest to "
            "pin. Pass the contents of that corpus's manifest.json.sha256, "
            "or fix --generation if this run is meant to query the baseline "
            "generation."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--top",
        type=_top,
        required=True,
        help=(
            "frozen for every run of one generation, and strictly greater "
            "than that generation's visible chunk count (14 / 156 / 774)"
        ),
    )
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
            "pinning at query time which corpus produced these numbers; "
            "'none' for g1, which has no distractor corpus and therefore no "
            "manifest"
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

    # Cheapest and most local first: a command line that contradicts itself is
    # the operator's typo, and reporting it before telling them to go commit
    # their work saves a round trip. Same reasoning, same moment, on the
    # other argument that can disagree with --generation.
    _reject_index_generation_mismatch(arguments.index_name, arguments.generation)
    _reject_generation_manifest_mismatch(arguments.generation, arguments.manifest_sha256)

    # Then, before the settings are read, before a client exists, before
    # anything can be spent: the tree this evidence will name has to be one a
    # reader can check out.
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
        f"- embedding client configured for this run: "
        f"{embedding_client.__class__.__name__} "
        f"deployment={settings.azure_openai_embedding_deployment} "
        "— this run embedded nothing; the query vectors were frozen by an "
        "earlier invocation and are identified by the digest below",
        f"- data-plane API version: `{SEARCH_API_VERSION}`",
        f"- top (frozen for every run): {arguments.top}",
        f"- pre-run lab commit: `{lab_sha}`",
        f"- generation: `{arguments.generation.value}` (index `{arguments.index_name}`)",
        _manifest_header_line(arguments.manifest_sha256),
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
        # The header announces what *this* generation pre-registered, so it
        # is filtered by generation while `refs` is not. `rank_states` still
        # receives the full tuple below — it needs the narrowed refs to print
        # `not_in_generation` — but announcing one of them here would put a
        # chunk in the header of a run whose own table correctly says the
        # document was never in that corpus, and a reader comparing the two
        # would have to guess which line was lying.
        expected_ids = expected_chunk_ids(
            principal.tenant_id,
            tuple(ref for ref in refs if arguments.generation in ref.generations),
        )
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

        # Two ways to have nothing to announce, and they are not the same
        # claim: the corpus has no answer at all, or it has one that lives in
        # a generation other than this run's. "another" rather than "a later
        # one" because nothing here enforces that a narrowed ref points
        # forward — under a different corpus design a ref could be narrowed to
        # {G1, G2}, and the sentence would then be backwards.
        if expected_ids:
            expected = ", ".join(f"`{c}`" for c in expected_ids)
        elif refs:
            expected = (
                f"none in this generation ({len(refs)} pre-registered for another one)"
            )
        else:
            expected = "none (no answer)"
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
                "The semantic ranker reorders the **top 50** of the merged set and "
                "nothing below it (`docs/rag-retrieval.md`). Wherever `top` exceeds "
                "50, a rank past 50 in the `hybrid_semantic` column is a position "
                "the ranker never touched, identical in form to one it did. "
                "Comparing two such ranks compares two pre-rerank orderings, so a "
                "difference between them is not a reranking effect.",
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
