from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    content: str
    source: str
    score: float | None = None


class AzureSearchService:
    """Azure AI Search adapter placeholder."""

    async def search(self, query: str) -> list[SearchResult]:
        _ = query
        return []
