import json

import boto3
import psycopg2
from psycopg2 import sql

from .base import BaseAgent
from config import settings


class SnowAgent(BaseAgent):
    def __init__(self):
        super().__init__(settings.SNOW_AGENT_ID, settings.SNOW_AGENT_ALIAS_ID)
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

    def _vector_search(self, query_vector: list) -> str:
        """Run a pgvector similarity search on the configured RDS table."""
        token = self._get_iam_token()
        conn = psycopg2.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=token,
            dbname=settings.DB_NAME,
            sslmode=settings.DB_SSLMODE,
        )
        try:
            vector_literal = "[" + ",".join(str(v) for v in query_vector) + "]"
            query = sql.SQL(
                "SELECT content FROM {table} ORDER BY embedding <=> %s::vector LIMIT %s"
            ).format(table=sql.Identifier(settings.RDS_TABLE_NAME))

            with conn.cursor() as cur:
                cur.execute(query, (vector_literal, settings.RAG_TOP_K))
                rows = cur.fetchall()
        finally:
            conn.close()

        return "\n".join(str(row[0]) for row in rows)

    def _generate_answer(self, prompt: str, context: str) -> str:
        """Generate the final answer using the configured Bedrock LLM."""
        full_prompt = (
            f"Context from closed ServiceNOW incidents:\n{context}\n\n"
            f"User Question: {prompt}\n\n"
            "Answer based only on the provided context. If the context does not "
            "contain the answer, say so clearly."
        )
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": full_prompt}],
        })
        response = self.bedrock_runtime.invoke_model(
            modelId=settings.LLM_MODEL_ID,
            body=body,
        )
        response_body = json.loads(response.get("body").read())
        return response_body["content"][0]["text"]

    async def invoke(self, session_id: str, prompt: str) -> str:
        try:
            query_vector = self._get_embedding(prompt)
            context = self._vector_search(query_vector)
            return self._generate_answer(prompt, context)
        except Exception as e:
            return f"Error in Direct DB Access: {str(e)}"
