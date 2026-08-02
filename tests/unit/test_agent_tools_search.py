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


async def test_search_docs_result_is_byte_capped() -> None:
    huge = tuple(_hit(i, content="x" * 5000) for i in range(3))
    tool = make_search_docs(RecordingRetriever(huge), OPS)
    result = await tool("q")
    assert len(result.encode("utf-8")) <= MAX_TOOL_RESULT_BYTES
