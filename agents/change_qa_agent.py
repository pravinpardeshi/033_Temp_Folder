"""Change item quality assessment agent.

Reviews a ServiceNOW change request against standard change-management
quality criteria. If ``CHANGE_TABLE_NAME`` is configured, the change record is
looked up in the pgvector store first; otherwise the assessment is based only
on the change number and the model is instructed not to invent details.
"""

import asyncio
import re

import boto3
import psycopg2
from psycopg2 import sql

import monitoring
from .base import BaseAgent
from config import settings

CHANGE_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

CHANGE_SYSTEM_PROMPT = (
    "You are a ServiceNOW Change Management quality reviewer. Assess a change "
    "request against standard quality criteria: completeness and clarity, "
    "risk and impact analysis, implementation plan, backout/rollback plan, "
    "testing and validation, schedule and maintenance window, and approvals "
    "and communication. Base every finding on the provided change record. If "
    "no record is available, do not invent details: state that the record "
    "could not be retrieved and list the fields required for a complete "
    "assessment. Return markdown with these sections: Summary, Quality score "
    "(0-100 with a one-line rationale), Strengths, Gaps and risks, "
    "Recommendations, and Verdict (Ready / Needs work / Reject)."
)


class ChangeQAAgent(BaseAgent):
    """Quality reviewer for change requests."""

    def __init__(self):
        self.max_tokens = settings.CHANGE_QUALITY_MAX_TOKENS
        self.temperature = settings.CHANGE_QUALITY_TEMPERATURE
        self.bedrock_runtime = boto3.client(
            "bedrock-runtime",
            region_name=settings.AWS_REGION,
        )
        self.rds_client = boto3.client(
            "rds",
            region_name=settings.AWS_REGION,
        )

    def _get_iam_token(self) -> str:
        return self.rds_client.generate_db_auth_token(
            DBHostname=settings.DB_HOST,
            Port=settings.DB_PORT,
            DBUsername=settings.DB_USER,
            Region=settings.AWS_REGION,
        )

    def _fetch_change(self, change_number: str) -> str:
        """Look up the change record when a change table is configured."""
        if not settings.CHANGE_TABLE_NAME:
            return ""

        query = sql.SQL(
            "SELECT content FROM {table} WHERE content ILIKE %s LIMIT 1"
        ).format(table=sql.Identifier(settings.CHANGE_TABLE_NAME))

        try:
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
                with conn.cursor() as cur:
                    cur.execute(query, (f"%{change_number}%",))
                    row = cur.fetchone()
                    return str(row[0]) if row else ""
            finally:
                conn.close()
        except Exception:
            return ""

    def _build_prompt(self, change_number: str, context: str) -> str:
        if context:
            source = (
                "Change record retrieved from the configured data source:\n"
                f"{context}"
            )
        else:
            source = (
                "No change record was retrieved from the configured data "
                "source. Do not invent change details."
            )
        return (
            f"Change request number: {change_number}\n\n"
            f"{source}\n\n"
            "Produce the quality assessment in the required markdown sections."
        )

    async def assess(self, change_number: str) -> str:
        change_number = str(change_number or "").strip()
        if not change_number:
            return "Please enter a change request number."
        if not CHANGE_NUMBER_PATTERN.match(change_number):
            return (
                "Invalid change request number. Use letters, digits, hyphens, "
                "or underscores (maximum 64 characters)."
            )

        context = await asyncio.to_thread(self._fetch_change, change_number)

        try:
            response = await asyncio.to_thread(
                lambda: self.bedrock_runtime.converse(
                    modelId=settings.LLM_MODEL_ID,
                    system=[{"text": CHANGE_SYSTEM_PROMPT}],
                    messages=[
                        {
                            "role": "user",
                            "content": [{"text": self._build_prompt(change_number, context)}],
                        }
                    ],
                    inferenceConfig={
                        "maxTokens": self.max_tokens,
                        "temperature": self.temperature,
                    },
                )
            )
            usage = response.get("usage") or {}
            monitoring.record_token_usage(
                model_id=settings.LLM_MODEL_ID,
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                agent_key="change_qa_agent",
                operation="converse",
            )
            return response["output"]["message"]["content"][0]["text"]
        except Exception as e:
            return f"Error assessing change: {e}"

    async def invoke(
        self,
        session_id: str,
        prompt: str,
        table_name: str | None = None,
    ) -> str:
        return await self.assess(prompt)
