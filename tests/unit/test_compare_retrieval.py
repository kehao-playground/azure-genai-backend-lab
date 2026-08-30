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

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from azgenai_lab.models.rag import Chunk, make_chunk_id, make_parent_id
from azgenai_lab.models.search import SearchHit
from azgenai_lab.models.search_index import INDEX_NAME
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

# CJK Unified Ideographs. Nothing mechanical can check that a translation is
# faithful, but "is the zh member Chinese at all" is one regex, and it
# catches the accident faithfulness never would: a paste error, a
# half-finished edit that leaves English text on both sides of a pair.
_HAN = re.compile(r"[\u4e00-\u9fff]")

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


def make_hit(chunk_id: str, score: float = 1.0) -> SearchHit:
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
    hits = [make_hit(f"filler-{i}", 1.0) for i in range(379)] + [make_hit(target, 0.1)]
    states = dict(rank_states(hits=hits, refs=(ref,), generation=Generation.G1, tenant_id="acme"))
    assert states[target] == "380"


def test_a_retrieved_chunk_declared_outside_the_generation_raises() -> None:
    # The one case where the frozen ref table and the live index contradict
    # each other. Resolving it either way — as `not_in_generation`, which
    # discards a real hit, or as a rank, which keeps a `generations` set just
    # shown to be false — puts the disagreement into the recall numbers.
    only_g3 = ExpectedChunkRef(
        "labdocs-observability", 1, "Observability", frozenset({Generation.G3})
    )
    (target,) = expected_chunk_ids("acme", (only_g3,))
    with pytest.raises(ValueError) as excinfo:
        rank_states(
            hits=[make_hit(target, 1.0)],
            refs=(only_g3,),
            generation=Generation.G1,
            tenant_id="acme",
        )
    message = str(excinfo.value)
    assert target in message
    assert "rank 1" in message
    assert "g1" in message
    assert "g3" in message


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
        assert _HAN.search(chinese.text), f"Q{number} zh member carries no Chinese"
        assert not _HAN.search(english.text), f"Q{number} en member carries Chinese"


def test_the_control_arm_is_not_translated() -> None:
    globex = QUERIES_BY_TENANT["globex"]
    assert len(globex) == 5
    assert {query.language for query in globex} == {"en"}


def test_truncation_keeps_the_rank_and_marks_the_table() -> None:
    from tools.compare_retrieval import _detail_rows

    hits = [make_hit(f"c{i}") for i in range(500)]
    rows, note = _detail_rows(hits, max_recorded_hits=10)
    assert len(rows) == 10
    assert "490" in note and "500" in note


def make_query(number: int, text: str) -> Any:
    """A Query whose only load-bearing field is its text — the vector key."""
    return compare_retrieval.Query(
        number, text, "kind", base_refs=(), expansion_refs=(), language="en"
    )


def test_vectors_file_is_written_then_read_without_embedding(tmp_path: Path) -> None:
    from tools.compare_retrieval import load_or_create_vectors

    calls = []

    class Recorder:
        async def embed(self, texts: Any) -> list[list[float]]:
            calls.append(tuple(texts))
            return [[0.5] * 1536 for _ in texts]

    path = tmp_path / "vectors.json"
    queries = (make_query(1, "alpha"), make_query(2, "beta"))

    first = asyncio.run(load_or_create_vectors(path, queries, Recorder()))
    assert first is None  # written, then the caller must stop
    assert len(calls) == 1

    second = asyncio.run(load_or_create_vectors(path, queries, Recorder()))
    assert set(second) == {"alpha", "beta"}
    assert len(calls) == 1  # the second call embedded nothing


def test_vector_hash_mismatch_aborts(tmp_path: Path) -> None:
    from tools.compare_retrieval import load_or_create_vectors

    path = tmp_path / "vectors.json"
    payload = {"alpha": {"vector": [0.5] * 1536, "sha256": "0" * 64}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit, match="vector hash mismatch"):
        asyncio.run(load_or_create_vectors(path, (make_query(1, "alpha"),), None))


def test_vectors_frozen_for_a_different_query_set_abort(tmp_path: Path) -> None:
    # The freeze is only worth having if it covers *this* run's questions. A
    # file written before a question was reworded still answers for every
    # other one, so a partial read would query with frozen vectors for most
    # of the set and freshly-diverged ones for the rest — the exact
    # unrecorded variable the freeze exists to remove.
    from tools.compare_retrieval import load_or_create_vectors

    digest = compare_retrieval._vector_digest([0.5] * 4)
    path = tmp_path / "vectors.json"
    path.write_text(
        json.dumps(
            {
                "alpha": {"vector": [0.5] * 4, "sha256": digest},
                "beta": {"vector": [0.5] * 4, "sha256": digest},
            }
        ),
        encoding="utf-8",
    )

    # An entry the run never asks for: the query set shrank, or a question was
    # reworded, and this file predates it.
    with pytest.raises(SystemExit, match="beta"):
        asyncio.run(load_or_create_vectors(path, (make_query(1, "alpha"),), None))

    # A question the file has no entry for.
    with pytest.raises(SystemExit, match="gamma"):
        asyncio.run(
            load_or_create_vectors(
                path, (make_query(1, "alpha"), make_query(2, "gamma")), None
            )
        )


def test_dirty_worktree_refuses_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import compare_retrieval as mod

    monkeypatch.setattr(mod, "_git", lambda *a: " M src/azgenai_lab/main.py")
    with pytest.raises(SystemExit, match="worktree is not clean"):
        mod.pre_run_lab_sha()


def test_clean_worktree_returns_the_head_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import compare_retrieval as mod

    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        calls.append(args)
        return "" if args[0] == "status" else "a" * 40 + "\n"

    monkeypatch.setattr(mod, "_git", fake_git)
    assert mod.pre_run_lab_sha() == "a" * 40
    # Plain `--porcelain`: untracked files count as dirty too. An untracked
    # file changes what runs (a stray corpus document, a shadowing module),
    # so a check that declared such a tree clean would certify a commit that
    # does not describe the code that ran.
    assert ("status", "--porcelain") in calls


def test_no_answer_expected_still_renders_a_reason() -> None:
    # Q6's whole point is that the corpus has no answer. An empty state list
    # rendered as an empty string would leave that row blank in paid evidence.
    assert compare_retrieval._render_rank_states([]) == "n/a (no answer expected)"


def test_all_three_rank_states_reach_the_row() -> None:
    rendered = compare_retrieval._render_rank_states(
        [("a", "3"), ("b", "absent"), ("c", "not_in_generation")]
    )
    assert rendered == "`a`=3; `b`=absent; `c`=not_in_generation"


def test_the_generation_blind_renderer_is_gone() -> None:
    # `_positions` was extracted so two renderers could not drift on ranking;
    # the durable fix is that there is only one renderer left to drift.
    assert not hasattr(compare_retrieval, "_ranks")


def test_baseline_only_drops_both_experiments() -> None:
    # The globex control arm runs baseline modes only: 4 calls per query, not
    # 9. The difference is 75 semantic-tier queries the budget was never
    # cleared for.
    assert compare_retrieval._experiment_sweeps(baseline_only=True) == ((), ())
    vector_ks, reranking_modes = compare_retrieval._experiment_sweeps(baseline_only=False)
    assert len(compare_retrieval.BASELINE_MODES) == 4
    assert len(compare_retrieval.BASELINE_MODES) + len(vector_ks) + len(reranking_modes) == 9


def test_manifest_digest_strips_whitespace_and_rejects_anything_else() -> None:
    # `manifest.json.sha256` is written with a trailing newline, per
    # sha256sum convention. Handing the 65-character string straight through
    # would record a digest that matches nothing.
    assert compare_retrieval._manifest_digest("  " + "a" * 64 + "\n") == "a" * 64
    for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "not a digest"):
        with pytest.raises(argparse.ArgumentTypeError):
            compare_retrieval._manifest_digest(bad)


def _run_arguments(**overrides: str) -> list[str]:
    required = {
        "--top": "25",
        "--out": "out.md",
        "--tenant-id": "acme",
        "--user-id": "u",
        "--vectors": "vectors.json",
        "--generation": "g2",
        "--manifest-sha256": "b" * 64,
    }
    required.update(overrides)
    return [token for pair in required.items() for token in pair]


def test_a_run_must_name_its_vectors_generation_and_manifest() -> None:
    parser = compare_retrieval._build_parser()
    arguments = parser.parse_args(_run_arguments(**{"--manifest-sha256": "b" * 64 + "\n"}))
    assert arguments.generation is Generation.G2
    assert arguments.manifest_sha256 == "b" * 64
    assert arguments.baseline_only is False
    assert arguments.index_name == INDEX_NAME

    for omitted in ("--vectors", "--generation", "--manifest-sha256"):
        tokens = _run_arguments()
        position = tokens.index(omitted)
        del tokens[position : position + 2]
        with pytest.raises(SystemExit):
            parser.parse_args(tokens)


def test_an_index_named_for_another_generation_is_refused() -> None:
    # Not an enforcement of the naming convention -- INDEX_NAME itself carries
    # no suffix, so demanding one would reject the tool's own default. What is
    # refused is the pair that contradicts itself, which no operator meant.
    for index_name in (
        "azgenai-lab-chunks-g3",
        # A generation token anywhere as a whole segment, not only at the end.
        # This project has been forced onto a Free-tier retry twice, so a
        # `-retry` suffix on a mislabelled index is not hypothetical.
        "azgenai-lab-chunks-g3-retry",
        "g3-chunks",
        # Every token is checked: agreeing with the first must not buy a pass
        # for the rest.
        "azgenai-lab-chunks-g2-g3",
    ):
        with pytest.raises(SystemExit, match="disagree about which generation"):
            compare_retrieval._reject_index_generation_mismatch(index_name, Generation.G2)


def test_an_index_name_that_agrees_or_says_nothing_is_left_alone() -> None:
    for index_name in (
        "azgenai-lab-chunks-g2",  # agrees
        "azgenai-lab-chunks-g2-retry",  # agrees, and not in the last segment
        INDEX_NAME,  # the default, which carries no generation token
        "azgenai-lab-chunks",
        "some-other-index",
        "azgenai-g2lab",  # `g2` is not a whole segment here
    ):
        compare_retrieval._reject_index_generation_mismatch(index_name, Generation.G2)


def _freeze_vectors(path: Path, queries: Any) -> dict[str, list[float]]:
    """Write a vectors file the read path accepts for exactly this query set.

    Every question gets a *distinct* vector. A shared value would make "this
    query's frozen vector reached the wire" indistinguishable from "some
    query's frozen vector did" — a lookup that returned the same entry for
    every question would satisfy the weaker claim.
    """
    expected = {query.text: [float(n)] * 4 for n, query in enumerate(queries, start=1)}
    payload = {
        text: {"vector": vector, "sha256": compare_retrieval._vector_digest(vector)}
        for text, vector in expected.items()
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return expected


def test_the_index_name_reaches_the_client_not_only_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name that reaches the header but not the query is the failure --
    a green run and a paid evidence file labelled with an index it never hit.
    """
    from tools import compare_retrieval as mod

    from azgenai_lab.core.config import Settings
    from azgenai_lab.models.search import SearchMode, SearchResult

    constructed: list[str] = []
    searched: list[Any] = []

    class FakeClient:
        def __init__(
            self, settings: Any, *, client: Any = None, index_name: str = INDEX_NAME
        ) -> None:
            constructed.append(index_name)
            self.last_diagnostics = SimpleNamespace(
                request_body={"search": "x"}, status=200, request_id="rid", latency_ms=1.5
            )

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exception: object) -> None:
            return None

        async def search(
            self,
            query_text: str,
            query_vector: Any = None,
            *,
            mode: Any,
            top: int,
            principal: Any,
            vector_k: int,
        ) -> Any:
            searched.append((query_text, mode, query_vector))
            return SearchResult(hits=(), mode=mode, vector_k=vector_k)

    class StubEmbeddings:
        async def embed(self, texts: Any) -> list[list[float]]:
            raise AssertionError("the read path must not embed anything")

    vectors = tmp_path / "vectors.json"
    expected_vectors = _freeze_vectors(vectors, QUERIES_BY_TENANT["globex"])
    out = tmp_path / "evidence.md"

    monkeypatch.setattr(mod, "_git", lambda *a: "" if a[0] == "status" else "c" * 40 + "\n")
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: Settings(
            azure_search_endpoint="https://example.search.windows.net",
            azure_search_admin_key="k",
            use_fake_search=False,
            use_fake_embeddings=False,
        ),
    )
    monkeypatch.setattr(mod, "configure_logging", lambda level: None)
    monkeypatch.setattr(mod, "build_embedding_client", lambda settings: StubEmbeddings())
    monkeypatch.setattr(mod, "AzureSearchClient", FakeClient)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_retrieval.py",
            "--top", "25",
            "--out", str(out),
            "--tenant-id", "globex",
            "--user-id", "operator",
            "--vectors", str(vectors),
            "--generation", "g2",
            "--index-name", "azgenai-lab-chunks-g2",
            "--manifest-sha256", "d" * 64,
            "--baseline-only",
        ],
    )

    asyncio.run(mod.main())

    # The point of the test: the parsed name reached the constructor, not just
    # the header line below.
    assert constructed == ["azgenai-lab-chunks-g2"]
    # Four baseline modes per query, and neither experiment.
    assert len(searched) == 4 * len(QUERIES_BY_TENANT["globex"])
    # The vector that reached the wire is the frozen one for *that* question,
    # byte for byte. "nothing was embedded" is a weaker claim: a run sending
    # zeros, or sending question one's vector for every question, would also
    # embed nothing. KEYWORD alone sends no vector.
    for query_text, mode, query_vector in searched:
        if mode is SearchMode.KEYWORD:
            assert query_vector is None
        else:
            assert query_vector == expected_vectors[query_text]
    assert len(set(map(tuple, expected_vectors.values()))) == len(expected_vectors)

    written = out.read_text(encoding="utf-8")
    assert f"- pre-run lab commit: `{'c' * 40}`" in written
    assert "- generation: `g2` (index `azgenai-lab-chunks-g2`)" in written
    assert f"- corpus manifest sha256: `{'d' * 64}`" in written
    assert "### Experiment 1" not in written and "### Experiment 2" not in written
    # Q6 has no expected chunk in any generation; its row must still say why.
    assert "n/a (no answer expected)" in written
    assert "**Run complete — all 5 queries finished.**" in written


def test_the_vectors_file_is_written_and_nothing_is_queried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generating and spending are never one step: the write run builds no client."""
    from tools import compare_retrieval as mod

    from azgenai_lab.core.config import Settings

    class Recorder:
        async def embed(self, texts: Any) -> list[list[float]]:
            return [[0.25] * 4 for _ in texts]

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no search client may be built on the write path")

    vectors = tmp_path / "nested" / "vectors.json"
    out = tmp_path / "evidence.md"

    monkeypatch.setattr(mod, "_git", lambda *a: "" if a[0] == "status" else "c" * 40 + "\n")
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: Settings(
            azure_search_endpoint="https://example.search.windows.net",
            azure_search_admin_key="k",
            use_fake_search=False,
            use_fake_embeddings=False,
        ),
    )
    monkeypatch.setattr(mod, "configure_logging", lambda level: None)
    monkeypatch.setattr(mod, "build_embedding_client", lambda settings: Recorder())
    monkeypatch.setattr(mod, "AzureSearchClient", refuse)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_retrieval.py",
            "--top", "25",
            "--out", str(out),
            "--tenant-id", "globex",
            "--user-id", "operator",
            "--vectors", str(vectors),
            "--generation", "g2",
            "--index-name", "azgenai-lab-chunks-g2",
            "--manifest-sha256", "d" * 64,
        ],
    )

    asyncio.run(mod.main())

    assert not out.exists(), "no evidence file may exist for a run that queried nothing"
    stored = json.loads(vectors.read_text(encoding="utf-8"))
    assert set(stored) == {query.text for query in QUERIES_BY_TENANT["globex"]}
