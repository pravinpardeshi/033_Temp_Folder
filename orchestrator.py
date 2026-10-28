from typing import Dict
from agents.base import BaseAgent
from agents.snow_agent import SnowAgent

class AgentOrchestrator:
    def __init__(self):
        # Registry of available agents
        self._agents: Dict[str, BaseAgent] = {
            "snow": SnowAgent(),
        }

    async def route_query(self, session_id: str, prompt: str) -> str:
        # V1: Simple routing to SNOW agent.
        # V2: Implement intent classification here to choose agent.
        agent = self._agents.get("snow")
        if not agent:
            return "No active agents available."
        
        return await agent.invoke(session_id, prompt)

orchestrator = AgentOrchestrator()
