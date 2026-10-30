"""Hybrid-search helpers: reciprocal rank fusion and Bedrock reranking.

These are *retrieval utilities*, not agents — they do not appear in the
orchestrator registry. ``RAGAgent`` uses them after the pgvector/keyword
queries to produce a better top-k context for the LLM.
"""

from typing import Dict, Iterable, List, Sequence

import boto3

from config import settings


def reciprocal_rank_fusion(ranked_lists: Sequence[Sequence[str]], k: int = 60,) -> List[str]:
    """Fuse multiple ranked result lists using RRF.

    score(doc) = sum(1 / (k + rank)) across lists in which the doc appears.
    Documents are de-duplicated by their text.
    """
    scores: Dict[str, float] = {}
    for documents in ranked_lists:
        for rank, document in enumerate(documents, start=1):
            scores[document] = scores.get(document, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=scores.get, reverse=True)


class BedrockReranker:
    """Optional cross-encoder reranker backed by the Bedrock Rerank API.

    If reranking is disabled, unconfigured, or the API call fails, the input
    order is preserved and truncated to ``top_n``.
    """

    def __init__(self, model_arn: str | None = None):
        self.model_arn = model_arn if model_arn is not None else settings.RERANK_MODEL_ARN
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(settings.RERANK_ENABLED and self.model_arn)

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                "bedrock-agent-runtime",
                region_name=settings.AWS_REGION,
            )
        return self._client

    def rerank(self, query: str, documents: Iterable[str], top_n: int) -> List[str]:
        documents = list(documents)
        if not documents:
            return []
        if not self.enabled:
            return documents[:top_n]

        sources = [
            {
                "type": "INLINE",
                "inlineDocumentSource": {
                    "type": "TEXT",
                    "textDocument": {"text": document},
                },
            }
            for document in documents
        ]
        try:
            response = self._get_client().rerank(
                queries=[{"type": "TEXT", "textQuery": {"text": query}}],
                sources=sources,
                rerankingConfiguration={
                    "type": "BEDROCK_RERANKING_MODEL",
                    "bedrockRerankingConfiguration": {
                        "modelConfiguration": {"modelArn": self.model_arn},
                        "numberOfResults": min(top_n, len(documents)),
                    },
                },
            )
            results = response.get("results") or []
            ordered = [documents[item["index"]] for item in results]
            return ordered or documents[:top_n]
        except Exception:
            return documents[:top_n]
