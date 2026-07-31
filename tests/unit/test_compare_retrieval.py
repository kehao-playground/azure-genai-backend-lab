"""Drift test for the frozen comparison query set.

`tools/compare_retrieval.py`'s `QUERIES_BY_TENANT` records each expected
chunk as a `(doc_id, ordinal, heading_path)` triple, authored from a chunker
run against the live corpus. The id itself is *derived*, never hand-typed, so
an id-derivation bug cannot silently drift here — but a re-chunk that keeps
the same ordinal while shifting which section it belongs to (a heading
inserted or removed upstream of it) would still resolve to *a* chunk. This
test is what catches that: it re-chunks the current corpus and asserts every
authored heading_path still matches what that ordinal actually is.
"""

import importlib.util
import sys
from pathlib import Path

from azgenai_lab.models.rag import Chunk, make_chunk_id, make_parent_id
from azgenai_lab.services.chunking import chunk_markdown
from azgenai_lab.services.document_loader import load_documents

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

CHUNK_MAX_CHARS = 2000
CHUNK_OVERLAP_CHARS = 500


def _current_chunks() -> dict[str, Chunk]:
    """Chunk id -> Chunk, for every document in the live sample corpus."""
    chunks: dict[str, Chunk] = {}
    for document in load_documents():
        for chunk in chunk_markdown(
            document, max_chars=CHUNK_MAX_CHARS, overlap_chars=CHUNK_OVERLAP_CHARS
        ):
            chunks[chunk.chunk_id] = chunk
    return chunks


def test_every_expected_ref_resolves_to_the_authored_heading_path() -> None:
    current = _current_chunks()
    for tenant_id, queries in QUERIES_BY_TENANT.items():
        for query in queries:
            for ref in query.expected_refs:
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
    query = next(q for q in queries if q.number == 1)
    ids = expected_chunk_ids("acme", query.expected_refs)
    assert ids == (make_chunk_id(make_parent_id("acme", "service-sla"), 3),)
