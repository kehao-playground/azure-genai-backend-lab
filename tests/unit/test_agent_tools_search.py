import json

from azgenai_lab.models.principal import Principal
from azgenai_lab.models.search import SearchHit, SearchMode, SearchResult
from azgenai_lab.services.agent_tools import (
    MAX_SEARCH_HITS,
    MAX_TOOL_RESULT_BYTES,
    make_search_docs,
    truncate_utf8,
)

OPS = Principal(tenant_id="opsdemo", group_ids=())


def _hit(i: int, content: str = "budget text") -> SearchHit:
    return SearchHit(
        chunk_id=f"c{i}", parent_id="token-budget", title="Conversation Token Budget",
        heading_path="Conversation Token Budget > What a 429 means",
        content=content, score=1.0,
    )


class RecordingRetriever:
    def __init__(self, hits: tuple[SearchHit, ...]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, Principal]] = []

    async def retrieve(self, question: str, principal: Principal) -> SearchResult:
        self.calls.append((question, principal))
        return SearchResult(hits=self.hits, mode=SearchMode.HYBRID, vector_k=None)


def test_truncate_utf8_is_byte_measured_and_valid() -> None:
    text = "預算" * 100  # 3 bytes per char in UTF-8
    out = truncate_utf8(text, 10)
    assert len(out.encode("utf-8")) <= 10
    out.encode("utf-8")  # still valid UTF-8 (no split surrogate)
    assert truncate_utf8("short", 4800) == "short"


def test_truncate_utf8_boundary_exact_cap_is_unchanged() -> None:
    text = "ab文"  # 2 ASCII bytes + one 3-byte code point = 5 bytes exactly
    assert len(text.encode("utf-8")) == 5
    assert truncate_utf8(text, 5) == text


def test_truncate_utf8_boundary_one_byte_over_drops_one_code_point() -> None:
    text = "ab文"  # 5 bytes; cap one byte short of the full string
    out = truncate_utf8(text, 4)
    # The trailing 3-byte code point is dropped whole (its first two bytes
    # alone are not valid UTF-8), not split into a partial/invalid sequence.
    assert out == "ab"
    out.encode("utf-8")  # still decodable
    assert len(out.encode("utf-8")) == 2


async def test_search_docs_envelope_and_fixed_principal() -> None:
    retriever = RecordingRetriever(tuple(_hit(i) for i in range(5)))
    tool = make_search_docs(retriever, OPS)
    payload = json.loads(await tool("token budget"))
    assert set(payload) == {"hits"}
    assert len(payload["hits"]) == MAX_SEARCH_HITS  # 5 retrieved, 3 returned
    assert set(payload["hits"][0]) == {"source", "heading_path", "snippet"}
    # fail-closed principal: the tool passed exactly the closed-over principal
    assert retriever.calls == [("token budget", OPS)]


async def test_search_docs_no_hit_is_returned_not_raised() -> None:
    tool = make_search_docs(RecordingRetriever(()), OPS)
    assert json.loads(await tool("nothing")) == {"hits": []}


def _wide_hit(rank: int) -> SearchHit:
    # "字" is a 3-byte UTF-8 code point; 2000 of them (well past
    # MAX_SNIPPET_CHARS) forces the char-clamp to bite before the byte-clamp
    # does, and their 3x byte/char ratio forces the byte-clamp to bite too —
    # exercising the byte-vs-char distinction the envelope clamp depends on.
    content = f"h{rank}-" + "字" * 2000
    return SearchHit(
        chunk_id=f"c{rank}",
        parent_id=f"doc-{rank}",
        title="Wide",
        heading_path="Wide > Section",
        content=content,
        score=1.0,
    )


async def test_search_docs_result_is_byte_capped() -> None:
    wide_hits = tuple(_wide_hit(i) for i in range(3))
    tool = make_search_docs(RecordingRetriever(wide_hits), OPS)
    result = await tool("q")

    # 1. Always under the cap, in UTF-8 bytes.
    assert len(result.encode("utf-8")) <= MAX_TOOL_RESULT_BYTES

    # 2. Always parseable JSON — the clamp must not cut mid-structure.
    payload = json.loads(result)
    hits = payload["hits"]
    assert 1 <= len(hits) <= MAX_SEARCH_HITS

    # 3. Surviving hits are the rank-ordered prefix; rank-1 is present intact.
    assert hits[0]["source"] == "doc-0"
    full_char_clamped_snippet_0 = wide_hits[0].content[:1200]
    assert hits[0]["snippet"] == full_char_clamped_snippet_0
    for i, hit in enumerate(hits):
        assert hit["source"] == f"doc-{i}"

    # Prove the clamp actually fired: the unclamped envelope (all three hits
    # at their full char-clamped snippet) is larger than both the cap and the
    # clamped result actually returned.
    unclamped = json.dumps(
        {
            "hits": [
                {
                    "source": hit.parent_id,
                    "heading_path": hit.heading_path,
                    "snippet": hit.content[:1200],
                }
                for hit in wide_hits
            ]
        },
        ensure_ascii=False,
    )
    assert len(unclamped.encode("utf-8")) > MAX_TOOL_RESULT_BYTES
    assert len(result.encode("utf-8")) < len(unclamped.encode("utf-8"))
    # At least one hit was dropped or shortened relative to its full form.
    assert len(hits) < MAX_SEARCH_HITS or any(
        len(hits[i]["snippet"]) < len(wide_hits[i].content[:1200]) for i in range(len(hits))
    )
