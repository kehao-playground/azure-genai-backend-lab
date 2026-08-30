"""Drift test for the frozen comparison query set.

`tools/compare_retrieval.py`'s `QUERIES_BY_TENANT` records each expected
chunk as a `(doc_id, ordinal, heading_path, generations)` ref, authored from a
chunker run against the live corpus. The id itself is *derived*, never
hand-typed, so an id-derivation bug cannot silently drift here — but a re-chunk that keeps
the same ordinal while shifting which section it belongs to (a heading
inserted or removed upstream of it) would still resolve to *a* chunk. This
test is what catches that: it re-chunks the current corpus and asserts every
authored heading_path still matches what that ordinal actually is.

That check covers `base_refs`, the layer this repository's corpus can answer
for. `expansion_refs` names chunks of a distractor corpus built outside the
repository, so it is held to a different assertion — that it is empty, which
is what the 2026-08-30 preregistration freeze concluded.
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
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS, INDEX_NAME
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
Query = compare_retrieval.Query
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


def test_no_expansion_ref_was_pre_registered() -> None:
    """The preregistration freeze concluded the distractor corpus answers nothing.

    This is the reason the heading-path check below is scoped to `base_refs`.
    `SAMPLE_DOCS_DIR` is the base corpus and the only one this test can see —
    the distractor corpus is built into a scratch directory by
    `tools/build_distractor_corpus.py` and is not in the repository — so an
    expansion ref could not be validated here even in principle.

    Scoping alone would leave a silent hole, so the scope is paired with this
    assertion: an expansion ref appearing later fails *here*, loudly, instead
    of slipping past a check that never looked at it. Should a future corpus
    genuinely contain a relevant distractor, this test is where the change is
    made deliberately — teach it the distractor corpus, and re-freeze the
    whole experiment rather than editing one ref into a frozen table.
    """
    for tenant_id, queries in QUERIES_BY_TENANT.items():
        for query in queries:
            assert query.expansion_refs == (), (
                f"Q{query.number} ({tenant_id}, {query.language}) declares "
                f"{len(query.expansion_refs)} expansion ref(s). The 2026-08-30 "
                "freeze recorded none: no admitted distractor document answers "
                "any frozen question. A ref added after that freeze changes the "
                "relevance labels of an experiment that has already been "
                "measured against them"
            )


def test_every_expected_ref_resolves_to_the_authored_heading_path() -> None:
    current = _current_chunks()
    for tenant_id, queries in QUERIES_BY_TENANT.items():
        for query in queries:
            # `base_refs` only: see `test_no_expansion_ref_was_pre_registered`.
            for ref in query.base_refs:
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
    for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "not a digest", "None", "NONE", "non"):
        with pytest.raises(argparse.ArgumentTypeError):
            compare_retrieval._manifest_digest(bad)


def test_manifest_digest_accepts_the_no_manifest_sentinel() -> None:
    # `g1` has no distractor corpus and the builder refuses to produce a
    # zero-document one, so `g1` has no manifest to pin. The literal `none`
    # says that on purpose, rather than forcing the base corpus's digest
    # into a field labelled "corpus manifest".
    assert compare_retrieval._manifest_digest("none") == "none"
    assert compare_retrieval._manifest_digest("  none\n") == "none"


def test_manifest_header_line_renders_digest_and_sentinel_honestly() -> None:
    assert (
        compare_retrieval._manifest_header_line("a" * 64)
        == f"- corpus manifest sha256: `{'a' * 64}`"
    )
    # An empty value or a bare `none` with no explanation would read as a
    # digest computation that silently failed. The absence is by design and
    # the line has to say so.
    line = compare_retrieval._manifest_header_line("none")
    assert "none" in line
    assert "no distractor corpus" in line
    assert "``" not in line


def test_generation_manifest_pairing_contradictions_are_refused() -> None:
    # g1 is definitionally the no-distractor generation in this experiment;
    # a digest for it, or `none` for g2/g3, is the same false-label defect
    # the index-name guard exists to prevent, mirrored onto this argument.
    with pytest.raises(SystemExit, match="g1") as g1_digest:
        compare_retrieval._reject_generation_manifest_mismatch(Generation.G1, "b" * 64)
    assert "b" * 64 in str(g1_digest.value)

    for generation in (Generation.G2, Generation.G3):
        with pytest.raises(SystemExit, match=generation.value) as none_for_distractor:
            compare_retrieval._reject_generation_manifest_mismatch(generation, "none")
        assert "none" in str(none_for_distractor.value)


def test_generation_manifest_pairing_that_agrees_is_left_alone() -> None:
    compare_retrieval._reject_generation_manifest_mismatch(Generation.G1, "none")
    for generation in (Generation.G2, Generation.G3):
        compare_retrieval._reject_generation_manifest_mismatch(generation, "b" * 64)


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
        "some-other-index",
        "azgenai-g2lab",  # `g2` is not a whole segment here
    ):
        compare_retrieval._reject_index_generation_mismatch(index_name, Generation.G2)
    # The default carries no generation token either, but it is the one
    # token-less name with a known content: the base corpus, which *is* g1.
    # See `test_a_defaulted_index_name_is_refused_for_a_distractor_generation`.
    compare_retrieval._reject_index_generation_mismatch(INDEX_NAME, Generation.G1)


def _freeze_vectors(path: Path, queries: Any) -> dict[str, list[float]]:
    """Write a vectors file the read path accepts for exactly this query set.

    Every question gets a *distinct* vector. A shared value would make "this
    query's frozen vector reached the wire" indistinguishable from "some
    query's frozen vector did" — a lookup that returned the same entry for
    every question would satisfy the weaker claim.

    They are `EMBEDDING_DIMENSIONS` wide because the real client rejects any
    other width. A narrower vector would prove the frozen bytes reach the
    wire, but not that they reach it in a shape the wire accepts.
    """
    expected = {
        query.text: [float(n)] * EMBEDDING_DIMENSIONS
        for n, query in enumerate(queries, start=1)
    }
    payload = {
        text: {"vector": vector, "sha256": compare_retrieval._vector_digest(vector)}
        for text, vector in expected.items()
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return expected


def test_main_refuses_a_generation_manifest_contradiction_before_any_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard fires from argv, ahead of the worktree check, the settings
    read, and the embedding client -- nothing here is mocked because nothing
    here should run before the SystemExit does.
    """
    from tools import compare_retrieval as mod

    def refuse_git(*args: str) -> str:
        raise AssertionError("a self-contradictory command line must not reach git")

    monkeypatch.setattr(mod, "_git", refuse_git)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_retrieval.py",
            "--top", "25",
            "--out", "out.md",
            "--tenant-id", "globex",
            "--user-id", "operator",
            "--vectors", "vectors.json",
            "--generation", "g1",
            "--manifest-sha256", "b" * 64,
        ],
    )
    with pytest.raises(SystemExit, match="g1"):
        asyncio.run(mod.main())

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_retrieval.py",
            "--top", "25",
            "--out", "out.md",
            "--tenant-id", "globex",
            "--user-id", "operator",
            "--vectors", "vectors.json",
            "--generation", "g2",
            # Named, so the index guard ahead of this one has nothing to say
            # and the manifest pairing is what refuses the run.
            "--index-name", "azgenai-lab-chunks-g2",
            "--manifest-sha256", "none",
        ],
    )
    with pytest.raises(SystemExit, match="has a manifest to"):
        asyncio.run(mod.main())


def test_the_index_name_reaches_the_client_not_only_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name that reaches the header but not the query is the failure --
    a green run and a paid evidence file labelled with an index it never hit.
    """
    from tools import compare_retrieval as mod

    from azgenai_lab.core.config import Settings
    from azgenai_lab.models.search import (
        SearchMode,
        SearchResult,
        validate_search_arguments,
    )

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
            # The real adapter routes every call through this validator, so
            # a double that skips it accepts calls the service rejects --
            # the fake-fidelity gap this repository tracks as a watchpoint.
            validate_search_arguments(
                query_text, query_vector, mode=mode, top=top, vector_k=vector_k
            )
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
            return [[0.25] * EMBEDDING_DIMENSIONS for _ in texts]

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


def test_the_header_announces_only_this_generation_s_pre_registered_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header is filtered by generation; the refs passed to the table are not.

    An unfiltered header would name a chunk as pre-registered for a run whose
    own table says `not_in_generation` on the same page, and a reader would
    have to guess which of the two lines was lying.
    """
    from tools import compare_retrieval as mod

    from azgenai_lab.core.config import Settings
    from azgenai_lab.models.search import SearchResult, validate_search_arguments

    class FakeClient:
        def __init__(
            self, settings: Any, *, client: Any = None, index_name: str = INDEX_NAME
        ) -> None:
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
            # Same contract as the real adapter; see the fake above.
            validate_search_arguments(
                query_text, query_vector, mode=mode, top=top, vector_k=vector_k
            )
            return SearchResult(hits=(), mode=mode, vector_k=vector_k)

    class StubEmbeddings:
        async def embed(self, texts: Any) -> list[list[float]]:
            raise AssertionError("the read path must not embed anything")

    later = ExpectedChunkRef("late-doc", 0, "Late Doc", generations=frozenset({Generation.G3}))
    present = ExpectedChunkRef(
        "billing-faq", 1, "Billing FAQ > Invoices",
        generations=frozenset({Generation.G2, Generation.G3}),
    )
    queries = (
        Query(1, "arrives later", "k", base_refs=(), expansion_refs=(later,), language="en"),
        Query(2, "here already", "k", base_refs=(present,), expansion_refs=(), language="en"),
        Query(3, "no answer at all", "k", base_refs=(), expansion_refs=(), language="en"),
    )
    monkeypatch.setattr(mod, "QUERIES_BY_TENANT", {"globex": queries})

    vectors = tmp_path / "vectors.json"
    _freeze_vectors(vectors, queries)
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

    written = out.read_text(encoding="utf-8")
    late_id = make_chunk_id(make_parent_id("globex", "late-doc"), 0)
    present_id = make_chunk_id(make_parent_id("globex", "billing-faq"), 1)

    # Q1's only ref arrives with g3. The header must not claim it for this run
    # -- and the table on the same page must still carry it, as the third
    # state, so the run records that the chunk was never there to be found.
    assert (
        "- pre-registered chunk(s): none in this generation "
        "(1 pre-registered for another one)"
    ) in written
    assert f"pre-registered chunk(s): `{late_id}`" not in written
    assert f"`{late_id}`=not_in_generation" in written

    # Unfiltered rendering is still exactly right for a ref this generation has,
    # and "no answer" stays distinguishable from "answer, but not yet".
    assert f"- pre-registered chunk(s): `{present_id}`" in written
    assert "- pre-registered chunk(s): none (no answer)" in written


def _diagnostics() -> SimpleNamespace:
    return SimpleNamespace(
        request_body={"search": "x"}, status=200, request_id="rid", latency_ms=1.5
    )


class PageClient:
    """A search client that returns a fixed page, and validates like the real one.

    `search()` runs `validate_search_arguments` for the reason that
    validator's own docstring gives: a double that accepts a call the service
    would reject turns a green suite into a live-run failure.
    """

    def __init__(self, hits: Any) -> None:
        self._hits = tuple(hits)
        self.last_diagnostics = _diagnostics()

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
        from azgenai_lab.models.search import SearchResult, validate_search_arguments

        validate_search_arguments(
            query_text, query_vector, mode=mode, top=top, vector_k=vector_k
        )
        return SearchResult(hits=self._hits, mode=mode, vector_k=vector_k)


def _run_one(
    client: Any,
    evidence: Any,
    *,
    top: int,
    refs: Any = (),
    generation: Any = None,
    text: str = "alpha",
) -> list[str]:
    from azgenai_lab.models.principal import Principal
    from azgenai_lab.models.search import SearchMode
    from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS

    return asyncio.run(
        compare_retrieval._run(
            client,
            evidence,
            "Q1 en baseline vector",
            make_query(1, text),
            refs,
            [0.5] * EMBEDDING_DIMENSIONS,
            Principal(tenant_id="acme", user_id="u", group_ids=()),
            generation=generation or Generation.G1,
            mode=SearchMode.VECTOR,
            top=top,
            vector_k=50,
            search_endpoint=None,
            max_recorded_hits=10,
        )
    )


def test_a_response_that_exactly_fills_the_page_aborts_the_run(tmp_path: Path) -> None:
    """`hits == top` cannot be told from a truncated page, and that is the
    distinction the candidate-generation experiment exists to measure. It is
    available on the first call, before the other 107 are spent."""
    evidence = compare_retrieval.Evidence(tmp_path / "evidence.md", total_queries=1)
    client = PageClient(make_hit(f"c{i}") for i in range(5))

    with pytest.raises(SystemExit) as excinfo:
        _run_one(client, evidence, top=5)

    message = str(excinfo.value)
    assert "vector" in message, "the refusal must name the mode"
    assert "alpha" in message, "the refusal must name the query"
    assert "5" in message, "the refusal must name --top"
    assert "--top" in message, "the refusal must say what to do about it"

    written = (tmp_path / "evidence.md").read_text(encoding="utf-8")
    assert "**Aborted:" in written, "the artifact must explain its own abort"
    assert "alpha" in written


def test_a_response_short_of_the_page_is_not_refused(tmp_path: Path) -> None:
    # One hit short of `top` is the ordinary case: the corpus ended before
    # the page did, which is exactly what the prescribed `top` buys.
    evidence = compare_retrieval.Evidence(tmp_path / "evidence.md", total_queries=1)
    client = PageClient(make_hit(f"c{i}") for i in range(5))
    rows = _run_one(client, evidence, top=6)
    assert rows and all("Aborted" not in row for row in rows)


def test_the_frozen_ref_abort_says_why_in_the_artifact(tmp_path: Path) -> None:
    """The one abort meaning "the frozen ref table is wrong". Its cause must
    survive into the published artifact, not only into stderr."""
    only_g3 = ExpectedChunkRef(
        "labdocs-observability", 1, "Observability", frozenset({Generation.G3})
    )
    (target,) = expected_chunk_ids("acme", (only_g3,))
    evidence = compare_retrieval.Evidence(tmp_path / "evidence.md", total_queries=1)
    client = PageClient([make_hit(target)])

    with pytest.raises(ValueError):
        _run_one(client, evidence, top=25, refs=(only_g3,), generation=Generation.G1)

    written = (tmp_path / "evidence.md").read_text(encoding="utf-8")
    assert "**Aborted:" in written
    assert target in written
    assert "g3" in written


def test_top_is_bounded_by_the_service_ceiling_while_parsing() -> None:
    # `MAX_TOP` is parse-time knowable. Left to the request validator it
    # costs a complete-looking evidence file of FAILED rows instead of an
    # exit 2.
    from azgenai_lab.models.search import MAX_TOP

    parser = compare_retrieval._build_parser()
    assert parser.parse_args(_run_arguments(**{"--top": str(MAX_TOP)})).top == MAX_TOP
    for bad in (str(MAX_TOP + 1), "0", "-1", "seven"):
        with pytest.raises(SystemExit):
            parser.parse_args(_run_arguments(**{"--top": bad}))


def test_a_defaulted_index_name_is_refused_for_a_distractor_generation() -> None:
    # Omitting --index-name leaves the base index every earlier day's runbook
    # creates. It carries no generation token, so the token scan says nothing
    # about it -- and a populated base index returns g1-shaped numbers under
    # a g2/g3 label, at full price.
    for generation in (Generation.G2, Generation.G3):
        with pytest.raises(SystemExit, match="provably not"):
            compare_retrieval._reject_index_generation_mismatch(INDEX_NAME, generation)
    # g1 *is* the base corpus, so the default index is the right target there.
    compare_retrieval._reject_index_generation_mismatch(INDEX_NAME, Generation.G1)


def test_experiment_two_states_the_reranking_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At g2/g3 `top` exceeds 50, so a rank past 50 is a position the
    semantic ranker never touched. Two such ranks compared against each other
    are two pre-rerank orderings, and the artifact has to say so."""
    from tools import compare_retrieval as mod

    from azgenai_lab.core.config import Settings
    from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS

    class StubEmbeddings:
        async def embed(self, texts: Any) -> list[list[float]]:
            raise AssertionError("the read path must not embed anything")

    queries = (Query(1, "only question", "k", base_refs=(), expansion_refs=(), language="en"),)
    monkeypatch.setattr(mod, "QUERIES_BY_TENANT", {"globex": queries})

    vectors = tmp_path / "vectors.json"
    _freeze_vectors(vectors, queries)
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
    monkeypatch.setattr(
        mod,
        "AzureSearchClient",
        lambda settings, index_name=INDEX_NAME: _AsyncPageClient([]),
    )
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
    assert EMBEDDING_DIMENSIONS  # the frozen vectors above are this wide

    asyncio.run(mod.main())

    written = out.read_text(encoding="utf-8")
    assert "### Experiment 2" in written
    assert "top 50" in written and "never touched" in written


class _AsyncPageClient(PageClient):
    """`PageClient` as an async context manager, for the `main()` path."""

    async def __aenter__(self) -> "_AsyncPageClient":
        return self

    async def __aexit__(self, *exception: object) -> None:
        return None
