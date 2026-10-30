"""Generic wrapper for an AWS Bedrock Agent.

Use this for agents that are provisioned as Bedrock Agents (with their own
agent ID and alias ID). It is the simplest way to add RCA or Solution
Recommender agents in v2: create the Bedrock Agent in AWS, add its IDs to
``.env``, and register it in ``orchestrator.py``.
"""

import boto3

from .base import BaseAgent
from config import settings


class BedrockAgent(BaseAgent):
    def __init__(self, agent_id: str, alias_id: str):
        super().__init__(agent_id, alias_id)
        self.client = boto3.client(
            "bedrock-agent-runtime",
            region_name=settings.AWS_REGION,
        )

    async def invoke(self, session_id: str, prompt: str) -> str:
        try:
            response = self.client.invoke_agent(
                agentId=self.agent_id,
                agentAliasId=self.alias_id,
                sessionId=session_id,
                inputText=prompt,
            )

            completion = ""
            for event in response.get("completion", []):
                chunk = event.get("chunk")
                if chunk:
                    completion += chunk.get("bytes", b"").decode("utf-8")

            return completion or "No response received from the agent."
        except Exception as e:
            return f"Error invoking BedrockAgent: {str(e)}"
