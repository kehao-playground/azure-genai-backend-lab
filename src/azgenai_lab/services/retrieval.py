"""Retrieve step of the RAG pipeline: question -> ranked chunks.

Composes the Day 12 embedding adapter and the Day 13 search adapter. Mode is
fixed to hybrid: Day 13 measured that keyword-only and vector-only each miss
queries the other catches, and the semantic ranker stays out of the default
path (single-probe result; documentation conflict unresolved).
"""

from azgenai_lab.core.config import Settings
from azgenai_lab.models.search import SearchMode, SearchResult
from azgenai_lab.services.azure_search import SearchClient, build_search_client
from azgenai_lab.services.embeddings import EmbeddingClient, build_embedding_client


class Retriever:
    def __init__(
        self, embedding_client: EmbeddingClient, search_client: SearchClient, *, top: int
    ) -> None:
        self._embedding_client = embedding_client
        self._search_client = search_client
        self._top = top

    async def retrieve(self, question: str) -> SearchResult:
        vector = (await self._embedding_client.embed([question]))[0]
        return await self._search_client.search(
            question, vector, mode=SearchMode.HYBRID, top=self._top
        )

    async def aclose(self) -> None:
        """Close both composed clients. Each adapter's own aclose() is idempotent."""
        await self._embedding_client.aclose()
        await self._search_client.aclose()


def build_retriever(settings: Settings) -> Retriever:
    return Retriever(
        build_embedding_client(settings), build_search_client(settings), top=settings.rag_top
    )


__all__ = [
    "Retriever",
    "build_retriever",
]
