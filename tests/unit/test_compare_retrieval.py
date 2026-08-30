"""Drift test for the frozen comparison query set.

`tools/compare_retrieval.py`'s `QUERIES_BY_TENANT` records each expected
chunk as a `(doc_id, ordinal, heading_path, generations)` ref, authored from a
chunker run against the live corpus. The id itself is *derived*, never
hand-typed, so an id-derivation bug cannot silently drift here — but a re-chunk that keeps
the same ordinal while shifting which section it belongs to (a heading
inserted or removed upstream of it) would still resolve to *a* chunk. This
test is what catches that: it re-chunks the current corpus and asserts every
authored heading_path still matches what that ordinal actually is.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

from azgenai_lab.models.rag import Chunk, make_chunk_id, make_parent_id
from azgenai_lab.models.search import SearchHit
from azgenai_lab.services.chunking import chunk_markdown
from azgenai_lab.services.document_loader import SAMPLE_DOCS_DIR, load_documents

# tools/ is not a package (no __init__.py, not installed) — it is a
# directory of standalone scripts, so this is a plain file import rather
# than `from tools.compare_retrieval import ...`.
_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "compare_retrieval.py"
_SPEC = importlib.util.spec_from_file_location("compare_retrieval", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
compare_retrieval = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = compare_retrieval
_SPEC.loader.exec_module(compare_retrieval)

QUERIES_BY_TENANT = compare_retrieval.QUERIES_BY_TENANT
expected_chunk_ids = compare_retrieval.expected_chunk_ids
ExpectedChunkRef = compare_retrieval.ExpectedChunkRef
Generation = compare_retrieval.Generation
rank_states = compare_retrieval.rank_states

CHUNK_MAX_CHARS = 2000
CHUNK_OVERLAP_CHARS = 500


def _current_chunks() -> dict[str, Chunk]:
    """Chunk id -> Chunk, for every document in the live sample corpus."""
    chunks: dict[str, Chunk] = {}
    for document in load_documents(SAMPLE_DOCS_DIR):
        for chunk in chunk_markdown(
            document, max_chars=CHUNK_MAX_CHARS, overlap_chars=CHUNK_OVERLAP_CHARS
        ):
            chunks[chunk.chunk_id] = chunk
    return chunks


def test_every_expected_ref_resolves_to_the_authored_heading_path() -> None:
    current = _current_chunks()
    for tenant_id, queries in QUERIES_BY_TENANT.items():
        for query in queries:
            for ref in query.base_refs + query.expansion_refs:
                derived_id = make_chunk_id(make_parent_id(tenant_id, ref.doc_id), ref.ordinal)
                resolved = current.get(derived_id)
                assert resolved is not None, (
                    f"Q{query.number} ({tenant_id}): expected chunk {derived_id!r} "
                    f"(doc_id={ref.doc_id!r}, ordinal={ref.ordinal}) does not exist in "
                    "the current corpus — the chunker output has drifted, re-freeze "
                    "the expected refs from a fresh chunker run"
                )
                assert resolved.chunk_id == derived_id
                assert resolved.heading_path == ref.heading_path, (
                    f"Q{query.number} ({tenant_id}): chunk {derived_id!r} now has "
                    f"heading_path {resolved.heading_path!r}, authored as "
                    f"{ref.heading_path!r} — the ordinal still resolves but the "
                    "section it points at has shifted"
                )


def test_expected_chunk_ids_derives_rather_than_stores() -> None:
    # A direct check on the helper itself: given a tenant and refs, it must
    # apply the same two-step derivation the write path uses.
    queries = QUERIES_BY_TENANT["acme"]
    query = next(q for q in queries if q.number == 1 and q.language == "en")
    ids = expected_chunk_ids("acme", query.base_refs + query.expansion_refs)
    assert ids == (make_chunk_id(make_parent_id("acme", "service-sla"), 3),)


def _hit(chunk_id: str, score: float) -> SearchHit:
    """A hit whose only load-bearing field is its id — rank comes from position."""
    return SearchHit(
        chunk_id=chunk_id,
        parent_id="unused",
        title="unused",
        heading_path="unused",
        content="unused",
        score=score,
        reranker_score=None,
    )


def test_not_in_generation_is_not_absent() -> None:
    only_g3 = ExpectedChunkRef(
        "labdocs-observability", 1, "Observability", frozenset({Generation.G3})
    )
    states = dict(rank_states(hits=[], refs=(only_g3,), generation=Generation.G1, tenant_id="acme"))
    # The document was not in G1's corpus. Recording that as `absent` would
    # write a recall failure that could not have happened.
    assert set(states.values()) == {"not_in_generation"}


def test_absent_means_present_and_not_retrieved() -> None:
    ref = ExpectedChunkRef("service-sla", 3, "Service SLA", frozenset(Generation))
    states = dict(rank_states(hits=[], refs=(ref,), generation=Generation.G1, tenant_id="acme"))
    assert set(states.values()) == {"absent"}


def test_rank_is_reported_from_the_full_hit_list() -> None:
    ref = ExpectedChunkRef("service-sla", 3, "Service SLA", frozenset(Generation))
    (target,) = expected_chunk_ids("acme", (ref,))
    hits = [_hit(f"filler-{i}", 1.0) for i in range(379)] + [_hit(target, 0.1)]
    states = dict(rank_states(hits=hits, refs=(ref,), generation=Generation.G1, tenant_id="acme"))
    assert states[target] == "380"


def test_chinese_and_english_counterparts_share_one_label_set() -> None:
    by_number: dict[int, list[Any]] = {}
    for query in QUERIES_BY_TENANT["acme"]:
        by_number.setdefault(query.number, []).append(query)
    assert len(by_number) == 6
    for number, pair in by_number.items():
        assert len(pair) == 2, f"Q{number} has no counterpart"
        english, chinese = sorted(pair, key=lambda q: q.language)
        assert (english.language, chinese.language) == ("en", "zh")
        assert english.base_refs == chinese.base_refs, f"Q{number} base_refs differ"
        assert english.expansion_refs == chinese.expansion_refs, f"Q{number} expansion differ"
        # `kind` is the experimental label: it names what the query does to the
        # retriever. A counterpart that quietly turns a lexical decoy into a
        # plain paraphrase would still share its refs, so refs alone do not pin
        # the pair.
        assert english.kind == chinese.kind, f"Q{number} kind differs"
        assert english.text != chinese.text, f"Q{number} was not translated"
        assert expected_chunk_ids("acme", english.base_refs) == expected_chunk_ids(
            "acme", chinese.base_refs
        )


def test_the_control_arm_is_not_translated() -> None:
    globex = QUERIES_BY_TENANT["globex"]
    assert len(globex) == 5
    assert {query.language for query in globex} == {"en"}
