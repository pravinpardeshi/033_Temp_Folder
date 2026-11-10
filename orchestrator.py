"""Manual agent orchestrator.

Routing is explicit: the UI sends one of ``snow``, ``ci_qa`` or ``monitor``
and we dispatch to the matching agent. There is no LLM intent classifier.
"""

from typing import Dict, Optional, Tuple

from agents.base import BaseAgent
from agents.change_qa_agent import ChangeQAAgent
from agents.monitoring_agent import MonitoringAgent
from agents.snow_agent import SnowAgent


class AgentOrchestrator:
    """Route each request to the agent named by the caller."""

    # Names accepted from the API/UI -> registered agent key.
    _AGENT_ALIASES = {
        "chat": "snow_agent",
        "snow": "snow_agent",
        "snow_agent": "snow_agent",
        "change": "change_qa_agent",
        "change_qa": "change_qa_agent",
        "ci_qa": "change_qa_agent",
        "change_qa_agent": "change_qa_agent",
        "monitor": "monitoring_agent",
        "monitoring": "monitoring_agent",
        "monitoring_agent": "monitoring_agent",
        "metrics": "monitoring_agent",
    }

    def __init__(self) -> None:
        self._agents: Dict[str, Tuple[BaseAgent, str]] = {}
        self._default_key: Optional[str] = None
        self._register_agents()

    def _register_agents(self) -> None:
        self.register(
            "snow_agent",
            SnowAgent(),
            "Question/answer over closed ServiceNOW incidents.",
            default=True,
        )
        self.register(
            "change_qa_agent",
            ChangeQAAgent(),
            "Quality assessment for a ServiceNOW change request by change number.",
        )
        self.register(
            "monitoring_agent",
            MonitoringAgent(),
            "Application health, token usage, and API endpoint response times.",
        )

    def register(
        self,
        key: str,
        agent: BaseAgent,
        description: str,
        default: bool = False,
    ) -> None:
        self._agents[key] = (agent, description)
        if default or self._default_key is None:
            self._default_key = key

    @property
    def agent_keys(self) -> list:
        return list(self._agents.keys())

    def resolve_agent_key(self, key: Optional[str]) -> Optional[str]:
        """Normalize an incoming agent name; return None when unknown."""
        if not key:
            return None
        normalized = str(key).strip().lower()
        normalized = self._AGENT_ALIASES.get(normalized, normalized)
        return normalized if normalized in self._agents else None

    async def route_query(
        self,
        session_id: str,
        prompt: str,
        agent_key: Optional[str] = None,
        table_name: Optional[str] = None,
    ) -> str:
        """Dispatch to the caller-selected agent (no LLM routing).

        Falls back to the default agent only when the caller sends no agent
        name. An unknown name raises so bad routes are visible in the logs.
        """
        if not self._agents:
            return "No active agents available."

        if agent_key:
            resolved_key = self.resolve_agent_key(agent_key)
            if resolved_key is None:
                raise ValueError(f"Unknown agent: {agent_key}")
        else:
            resolved_key = self._default_key

        agent, _ = self._agents[resolved_key]
        return await agent.invoke(
            session_id=session_id,
            prompt=prompt,
            table_name=table_name,
        )


orchestrator = AgentOrchestrator()
