"""General-purpose chat agent (no retrieval).

Used by the UI "Chat" mode for questions that are not about closed incidents.
Incident-specific questions should use the `snow` (Q&A) or `rca` agents.
"""

import boto3

from .base import BaseAgent
from config import settings

CHAT_SYSTEM_PROMPT = (
    "You are a helpful, concise assistant embedded in a ServiceNOW operations "
    "copilot. You can explain concepts, help draft text, and reason about "
    "general questions. You do not have access to incident data in this mode. "
    "If the user asks about specific incidents, root causes, or ticket history, "
    "tell them to switch to the SNOW or RCA agent."
)


class ChatAgent(BaseAgent):
    def __init__(self):
        super().__init__("", "")
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=settings.AWS_REGION,
        )

    async def invoke(self, session_id: str, prompt: str) -> str:
        try:
            response = self.client.converse(
                modelId=settings.LLM_MODEL_ID,
                system=[{"text": CHAT_SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={
                    "maxTokens": settings.LLM_MAX_TOKENS,
                    "temperature": 0.4,
                },
            )
            return response["output"]["message"]["content"][0]["text"]
        except Exception as e:
            return f"Error invoking chat agent: {e}"
