import re
from typing import Dict, Optional, Tuple

import boto3

import monitoring
from agents.base import BaseAgent
from agents.change_qa_agent import ChangeQAAgent
from agents.monitoring_agent import MonitoringAgent
from agents.snow_agent import SnowAgent
from config import settings

class AgentOrchestrator:
    """Direct agent orchestrator (LangGraph removed)."""

    # Backwards-compatible names accepted from the API/UI.
    _AGENT_ALIASES = {
        "chat": "snow_agent",
        "snow": "snow_agent",
        "change": "change_qa_agent",
        "change_qa": "change_qa_agent",
        "ci_qa": "change_qa_agent",
        "monitor": "monitoring_agent",
        "monitoring": "monitoring_agent",
        "metrics": "monitoring_agent",
    }

    def __init__(self):
        # key -> (agent, description used by the intent router)
        self._agents: Dict[str, Tuple[BaseAgent, str]] = {}
        self._default_key: Optional[str] = None
        self._router_client = None
        self._register_agents()

    def _register_agents(self) -> None:
        self.register("snow_agent", SnowAgent(), "Question/answer over closed ServiceNOW incidents.", default=True,)
        self.register("change_qa_agent", ChangeQAAgent(), "Quality assessment for a ServiceNOW change request by change number.",)
        self.register("monitoring_agent", MonitoringAgent(), "Application health, token usage, and API endpoint response times.",)

    def register(self, key: str, agent: BaseAgent, description: str, default: bool = False) -> None:
        self._agents[key] = (agent, description)
        if default or self._default_key is None:
            self._default_key = key

    @property
    def agent_keys(self) -> list:
        return list(self._agents.keys())

    def _get_router_client(self):
        if self._router_client is None:
            self._router_client = boto3.client(
                "bedrock-runtime",
                region_name=settings.AWS_REGION,
            )
        return self._router_client

    def _resolve_agent_key(self, key: Optional[str]) -> Optional[str]:
        """Normalize an incoming agent name and reject unknown keys."""
        if not key:
            return None
        normalized = str(key).strip().lower()
        normalized = self._AGENT_ALIASES.get(normalized, normalized)
        return normalized if normalized in self._agents else None

    def _keyword_route(self, prompt: str) -> Optional[str]:
        """Cheap deterministic hints before falling back to the LLM router."""
        text = (prompt or "").lower()
        if "change_qa_agent" in self._agents and re.search(r"\bchg\d+\b", text):
            return "change_qa_agent"
        monitoring_terms = (
            "monitor", "dashboard", "token usage", "api latency",
            "endpoint latency", "response time", "uptime", "error rate",
            "app health", "application health", "system health",
        )
        if "monitoring_agent" in self._agents and any(term in text for term in monitoring_terms):
            return "monitoring_agent"
        return None

    def _classify_intent(self, prompt: str) -> str:
        """Pick an agent: explicit keyword hint, then a small LLM classifier."""
        hinted = self._keyword_route(prompt)
        if hinted:
            return hinted

        choices = "\n".join(f"- {key}: {description}" for key, (_, description) in self._agents.items())

        classification_prompt = (
            "Choose the single best agent for the user's question.\n"
            f"Agents:\n{choices}\n\n"
            f"Question: {prompt}\n\n"
            "Reply with only the agent name."
        )
        try:
            response = self._get_router_client().converse(
                modelId=settings.AGENT_ROUTER_MODEL_ID or settings.LLM_MODEL_ID,
                messages=[{"role": "user", "content": [{"text": classification_prompt}]}],
                inferenceConfig={
                    "maxTokens": settings.ROUTER_MAX_TOKENS,
                    "temperature": settings.ROUTER_TEMPERATURE,
                },
            )
            usage = response.get("usage") or {}
            monitoring.record_token_usage(
                model_id=settings.AGENT_ROUTER_MODEL_ID or settings.LLM_MODEL_ID,
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                agent_key="router",
                operation="converse",
            )
            answer = response["output"]["message"]["content"][0]["text"].strip().lower()
            for key in self._agents:
                if key in answer:
                    return key
        except Exception:
            pass
        return self._default_key

    async def route_query(self, session_id: str, prompt: str, agent_key: Optional[str] = None, table_name: Optional[str] = None,) -> str:
        if not self._agents:
            return "No active agents available."
        
        # Resolve agent key (explicit or routed)
        resolved_key = self._resolve_agent_key(agent_key) or self._classify_intent(prompt)
        
        # Get the agent instance
        agent, _ = self._agents[resolved_key]
        
        # Execute agent directly (bypassing LangGraph)
        return await agent.run(prompt, session_id=session_id, table_name=table_name)

orchestrator = AgentOrchestrator()
