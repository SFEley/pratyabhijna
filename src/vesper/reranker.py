"""Voyage AI cross-encoder (reranker) for Graphiti.

Graphiti's only built-in reranker uses OpenAI. This implements the
CrossEncoderClient interface using Voyage AI's rerank endpoint,
keeping the entire provider stack OpenAI-free.
"""

import voyageai
from graphiti_core.cross_encoder.client import CrossEncoderClient


class VoyageRerankerClient(CrossEncoderClient):
    """Reranker using Voyage AI's rerank API."""

    def __init__(self, api_key: str | None = None, model: str = "rerank-2"):
        self.client = voyageai.AsyncClient(api_key=api_key)
        self.model = model

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        """Rank passages by relevance to the query.

        Returns (passage, score) tuples sorted by descending relevance.
        """
        if not passages:
            return []

        result = await self.client.rerank(
            query=query,
            documents=passages,
            model=self.model,
        )
        # Voyage returns RerankingResult with .results, each having .document and .relevance_score
        return [
            (r.document, r.relevance_score)
            for r in sorted(result.results, key=lambda r: r.relevance_score, reverse=True)
        ]
