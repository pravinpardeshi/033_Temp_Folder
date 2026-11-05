"""Root Cause Analysis agent with its own RAG implementation.

Given a new ServiceNOW incident, retrieves similar *closed* incidents from the
PostgreSQL + pgvector store and asks the LLM to identify the most likely root
cause, citing the historical evidence.
"""

import json

import boto3
import psycopg2
from psycopg2 import sql

from .base import BaseAgent
from .reranker import BedrockReranker, reciprocal_rank_fusion
from config import settings

RCA_SYSTEM_PROMPT = (
    "You are a senior ServiceNOW root cause analysis (RCA) expert. "
    "You are given a new incident and a set of similar closed incidents "
    "retrieved from the incident knowledge base.\n\n"
    "Produce a structured root cause analysis with these sections:\n"
    "1. Incident summary - restate the new incident in one or two sentences.\n"
    "2. Most likely root cause - the single most probable cause.\n"
    "3. Supporting evidence - cite the similar closed incidents that support "
    "the conclusion (include incident identifiers when present in the context).\n"
    "4. Alternative causes - other plausible causes, ranked, with why they are "
    "less likely.\n"
    "5. Confidence - High, Medium, or Low, with a one-line justification.\n"
    "6. Recommended next steps - concrete diagnostic or remediation actions.\n\n"
    "Do not invent incidents or facts that are not present in the retrieved "
    "context. If the evidence is insufficient, say so and explain what "
    "additional data would be needed."
)


class RCAAgent(BaseAgent):
    """Analyzes the root cause of a new incident using closed-incident history."""

    def __init__(self):
        self.table_name = settings.rca_table
        self.top_k = settings.RCA_TOP_K
        self.max_tokens = settings.RCA_MAX_TOKENS
        self.system_prompt = RCA_SYSTEM_PROMPT
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

    def _vector_candidates(self, conn, query_vector: list, limit: int) -> list:
        """Nearest neighbours by cosine distance (pgvector)."""
        vector_literal = "[" + ",".join(str(v) for v in query_vector) + "]"
        query = sql.SQL(
            "SELECT content FROM {table} ORDER BY embedding <=> %s::vector LIMIT %s"
        ).format(table=sql.Identifier(self.table_name))
        with conn.cursor() as cur:
            cur.execute(query, (vector_literal, limit))
            return [str(row[0]) for row in cur.fetchall()]

    def _keyword_candidates(self, conn, query_text: str, limit: int) -> list:
        """Lexical matches using PostgreSQL full-text search."""
        query = sql.SQL(
            "SELECT content FROM {table} "
            "WHERE to_tsvector('english', content) @@ plainto_tsquery('english', %s) "
            "ORDER BY ts_rank_cd(to_tsvector('english', content), "
            "plainto_tsquery('english', %s)) DESC "
            "LIMIT %s"
        ).format(table=sql.Identifier(self.table_name))
        with conn.cursor() as cur:
            cur.execute(query, (query_text, query_text, limit))
            return [str(row[0]) for row in cur.fetchall()]

    def _retrieve(self, prompt: str, query_vector: list) -> list:
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
            vector_docs = self._vector_candidates(conn, query_vector, candidate_k)
            if settings.HYBRID_SEARCH_ENABLED:
                try:
                    keyword_docs = self._keyword_candidates(conn, prompt, candidate_k)
                except Exception:
                    keyword_docs = []
        finally:
            conn.close()

        if keyword_docs:
            candidates = reciprocal_rank_fusion([vector_docs, keyword_docs], k=settings.RRF_K)
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

    async def invoke(self, session_id: str, prompt: str, table_name: str | None = None,) -> str:
        try:
            query_vector = self._get_embedding(prompt)
            documents = self._retrieve(prompt, query_vector)
            context = "\n\n---\n\n".join(documents)
            return self._generate_answer(prompt, context)
        except Exception as e:
            return f"Error querying the incident vector store: {e}"
