"""ServiceNOW Q&A agent built as a LangGraph pipeline.

Answers questions about closed incidents stored in the pgvector table.

Graph pipeline:

    START -> embed_query -> retrieve -> rerank -> [conditional]
        |                                    |-- documents found -> generate_answer -> END
        |                                    |-- no documents    -> no_context_reply -> END

State flows through a TypedDict (``SnowState``); each node is a sync
function that LangGraph runs in a thread during ``ainvoke``.
"""

import asyncio
import json

import boto3
import psycopg2
from psycopg2 import sql
from langgraph.graph import END, START, StateGraph
from typing import TypedDict

import monitoring
from .base import BaseAgent
from .reranker import BedrockReranker, reciprocal_rank_fusion
from config import settings

QA_SYSTEM_PROMPT = (
    "You are a ServiceNOW support assistant. Answer questions about closed "
    "incidents using the retrieved incident records. Prefer concrete details "
    "(resolution steps, affected service, error messages) found in the context."
)


class SnowState(TypedDict):
    """Shared state for the snow_agent LangGraph pipeline."""

    prompt: str
    table_name: str
    query_vector: list
    candidates: list
    documents: list
    context: str
    answer: str


class SnowAgent(BaseAgent):
    """Question/answer agent over closed ServiceNOW incidents (LangGraph)."""

    def __init__(self):
        self.table_name = settings.RDS_TABLE_NAME
        self.top_k = settings.RAG_TOP_K
        self.max_tokens = settings.LLM_MAX_TOKENS
        self.system_prompt = QA_SYSTEM_PROMPT
        self.reranker = BedrockReranker()
        self.bedrock_runtime = boto3.client(
            "bedrock-runtime",
            region_name=settings.AWS_REGION,
        )
        self.rds_client = boto3.client(
            "rds",
            region_name=settings.AWS_REGION,
        )
        self.graph = self._build_graph()

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self):
        """Compile the embed -> retrieve -> rerank -> answer pipeline."""
        builder = StateGraph(SnowState)

        builder.add_node("embed_query", self._node_embed_query)
        builder.add_node("retrieve", self._node_retrieve)
        builder.add_node("rerank", self._node_rerank)
        builder.add_node("generate_answer", self._node_generate_answer)
        builder.add_node("no_context_reply", self._node_no_context_reply)

        builder.add_edge(START, "embed_query")
        builder.add_edge("embed_query", "retrieve")
        builder.add_edge("retrieve", "rerank")

        builder.add_conditional_edges(
            "rerank",
            self._route_after_rerank,
            {"generate": "generate_answer", "no_context": "no_context_reply"},
        )

        builder.add_edge("generate_answer", END)
        builder.add_edge("no_context_reply", END)

        return builder.compile()

    def _route_after_rerank(self, state: SnowState) -> str:
        """Send the flow to the LLM only when retrieval found documents."""
        if state.get("documents"):
            return "generate"
        return "no_context"

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    def _node_embed_query(self, state: SnowState) -> dict:
        """Embed the user prompt with the configured Bedrock model."""
        return {"query_vector": self._get_embedding(state["prompt"])}

    def _node_retrieve(self, state: SnowState) -> dict:
        """Hybrid retrieval: vector + keyword candidates fused with RRF."""
        candidates = self._retrieve_candidates(
            state["prompt"],
            state["query_vector"],
            state["table_name"],
        )
        return {"candidates": candidates}

    def _node_rerank(self, state: SnowState) -> dict:
        """Rerank fused candidates down to top_k and build the LLM context."""
        documents = self.reranker.rerank(
            state["prompt"],
            state.get("candidates") or [],
            self.top_k,
        )
        return {
            "documents": documents,
            "context": "\n\n---\n\n".join(documents),
        }

    def _node_generate_answer(self, state: SnowState) -> dict:
        """Generate the final answer from the retrieved context."""
        return {
            "answer": self._generate_answer(
                state["prompt"],
                state.get("context") or "",
            )
        }

    def _node_no_context_reply(self, state: SnowState) -> dict:
        """Deterministic reply when retrieval found nothing (no LLM call)."""
        return {
            "answer": (
                "I could not find any relevant closed incidents in the "
                f"vector store to answer: {state['prompt']}. Try rephrasing "
                "the question or selecting a different incident table."
            )
        }

    # ------------------------------------------------------------------
    # Helpers used by nodes
    # ------------------------------------------------------------------

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

    def _vector_candidates(self, conn, query_vector: list, limit: int, table_name: str) -> list:
        """Nearest neighbours by cosine distance (pgvector)."""
        vector_literal = "[" + ",".join(str(v) for v in query_vector) + "]"
        query = sql.SQL(
            "SELECT content FROM {table} ORDER BY embedding <=> %s::vector LIMIT %s"
        ).format(table=sql.Identifier(table_name))
        with conn.cursor() as cur:
            cur.execute(query, (vector_literal, limit))
            return [str(row[0]) for row in cur.fetchall()]

    def _keyword_candidates(self, conn, query_text: str, limit: int, table_name: str) -> list:
        """Lexical matches using PostgreSQL full-text search."""
        query = sql.SQL(
            "SELECT content FROM {table} "
            "WHERE to_tsvector('english', content) @@ plainto_tsquery('english', %s) "
            "ORDER BY ts_rank_cd(to_tsvector('english', content), "
            "plainto_tsquery('english', %s)) DESC "
            "LIMIT %s"
        ).format(table=sql.Identifier(table_name))
        with conn.cursor() as cur:
            cur.execute(query, (query_text, query_text, limit))
            return [str(row[0]) for row in cur.fetchall()]

    def _retrieve_candidates(self, prompt: str, query_vector: list, table_name: str) -> list:
        """Hybrid retrieval: vector + keyword, fused with reciprocal rank fusion."""
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
            vector_docs = self._vector_candidates(conn, query_vector, candidate_k, table_name)
            if settings.HYBRID_SEARCH_ENABLED:
                try:
                    keyword_docs = self._keyword_candidates(conn, prompt, candidate_k, table_name)
                except Exception:
                    keyword_docs = []
        finally:
            conn.close()

        if keyword_docs:
            return reciprocal_rank_fusion([vector_docs, keyword_docs], k=settings.RRF_K)
        return vector_docs

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
        usage = response_body.get("usage") or {}
        monitoring.record_token_usage(
            model_id=settings.LLM_MODEL_ID,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            agent_key="snow_agent",
            operation="invoke_model",
        )
        return response_body["content"][0]["text"]

    # ------------------------------------------------------------------
    # Public interface (BaseAgent)
    # ------------------------------------------------------------------

    async def invoke(self, session_id: str, prompt: str, table_name: str | None = None,) -> str:
        if table_name and table_name not in settings.snow_tables:
            return (
                f"Unknown incident table '{table_name}'. "
                f"Choose one of: {', '.join(settings.snow_tables)}."
            )

        selected_table = table_name or self.table_name
        initial_state: SnowState = {
            "prompt": str(prompt),
            "table_name": selected_table,
            "query_vector": [],
            "candidates": [],
            "documents": [],
            "context": "",
            "answer": "",
        }
        try:
            # Graph nodes are synchronous (psycopg2 + boto3), so run the whole
            # pipeline in a worker thread to keep the event loop responsive.
            final_state = await asyncio.to_thread(self.graph.invoke, initial_state)
            return final_state["answer"]
        except Exception as e:
            return f"Error querying the incident vector store: {e}"
