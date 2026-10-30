"""Shared RAG implementation for agents that query the pgvector incident store."""

import json

import boto3
import psycopg2
from psycopg2 import sql

from .base import BaseAgent
from .reranker import BedrockReranker, reciprocal_rank_fusion
from config import settings


class RAGAgent(BaseAgent):
    """Embedding -> hybrid retrieval -> optional rerank -> Bedrock LLM answer."""

    # Subclasses that may switch tables per request set this to True.
    allows_table_override = False

    def __init__(
        self,
        table_name: str,
        top_k: int,
        system_prompt: str,
        max_tokens: int = 1024,
        agent_id: str = "",
        alias_id: str = "",
    ):
        super().__init__(agent_id, alias_id)
        self.table_name = table_name
        self.top_k = top_k
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.reranker = BedrockReranker()
        self.bedrock_runtime = boto3.client(
            "bedrock-runtime",
            region_name=settings.AWS_REGION,
        )
        self.rds_client = boto3.client(
            "rds",
            region_name=settings.AWS_REGION,
        )

    def _get_iam_token(self) -> str:
        """Generate a short-lived IAM authentication token for RDS."""
        return self.rds_client.generate_db_auth_token(
            DBHostname=settings.DB_HOST,
            Port=settings.DB_PORT,
            DBUsername=settings.DB_USER,
            Region=settings.AWS_REGION,
        )

    def _get_embedding(self, text: str) -> list:
        """Create an embedding vector using the configured Bedrock model."""
        body = json.dumps({"inputText": text})
        response = self.bedrock_runtime.invoke_model(
            modelId=settings.EMBEDDING_MODEL_ID,
            body=body,
        )
        response_body = json.loads(response.get("body").read())
        return response_body.get("embedding")

    def _vector_candidates(
        self, conn, query_vector: list, limit: int, table_name: str | None = None
    ) -> list:
        """Nearest neighbours by cosine distance (pgvector)."""
        vector_literal = "[" + ",".join(str(v) for v in query_vector) + "]"
        query = sql.SQL(
            "SELECT content FROM {table} ORDER BY embedding <=> %s::vector LIMIT %s"
        ).format(table=sql.Identifier(table_name or self.table_name))
        with conn.cursor() as cur:
            cur.execute(query, (vector_literal, limit))
            return [str(row[0]) for row in cur.fetchall()]

    def _keyword_candidates(
        self, conn, query_text: str, limit: int, table_name: str | None = None
    ) -> list:
        """Lexical matches using PostgreSQL full-text search."""
        query = sql.SQL(
            "SELECT content FROM {table} "
            "WHERE to_tsvector('english', content) @@ plainto_tsquery('english', %s) "
            "ORDER BY ts_rank_cd(to_tsvector('english', content), "
            "plainto_tsquery('english', %s)) DESC "
            "LIMIT %s"
        ).format(table=sql.Identifier(table_name or self.table_name))
        with conn.cursor() as cur:
            cur.execute(query, (query_text, query_text, limit))
            return [str(row[0]) for row in cur.fetchall()]

    def _retrieve(
        self, prompt: str, query_vector: list, table_name: str | None = None
    ) -> list:
        """Hybrid retrieval: vector + keyword, fused, then reranked."""
        candidate_k = settings.HYBRID_CANDIDATE_K
        vector_docs = []
        keyword_docs = []

        token = self._get_iam_token()
        conn = psycopg2.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=token,
            dbname=settings.DB_NAME,
            sslmode=settings.DB_SSLMODE,
            connect_timeout=settings.DB_CONNECT_TIMEOUT,
        )
        try:
            vector_docs = self._vector_candidates(
                conn, query_vector, candidate_k, table_name
            )
            if settings.HYBRID_SEARCH_ENABLED:
                try:
                    keyword_docs = self._keyword_candidates(
                        conn, prompt, candidate_k, table_name
                    )
                except Exception:
                    keyword_docs = []
        finally:
            conn.close()

        if keyword_docs:
            candidates = reciprocal_rank_fusion(
                [vector_docs, keyword_docs], k=settings.RRF_K
            )
        else:
            candidates = vector_docs

        return self.reranker.rerank(prompt, candidates, self.top_k)

    def _build_prompt(self, prompt: str, context: str) -> str:
        return (
            f"{self.system_prompt}\n\n"
            f"Context retrieved from the incident vector store:\n{context}\n\n"
            f"User request:\n{prompt}\n\n"
            "Answer using only the context above. If the context does not contain "
            "the answer, say so clearly."
        )

    def _generate_answer(self, prompt: str, context: str) -> str:
        """Generate the final answer using the configured Bedrock LLM."""
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": settings.LLM_TEMPERATURE,
            "messages": [{"role": "user", "content": self._build_prompt(prompt, context)}],
        })
        response = self.bedrock_runtime.invoke_model(
            modelId=settings.LLM_MODEL_ID,
            body=body,
        )
        response_body = json.loads(response.get("body").read())
        return response_body["content"][0]["text"]

    async def invoke(
        self,
        session_id: str,
        prompt: str,
        table_name: str | None = None,
    ) -> str:
        try:
            selected_table = (
                table_name
                if self.allows_table_override and table_name
                else self.table_name
            )
            query_vector = self._get_embedding(prompt)
            documents = self._retrieve(prompt, query_vector, selected_table)
            context = "\n\n---\n\n".join(documents)
            return self._generate_answer(prompt, context)
        except Exception as e:
            return f"Error querying the incident vector store: {e}"
