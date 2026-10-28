from abc import ABC, abstractmethod

class BaseAgent(ABC):
    def __init__(self, agent_id: str, alias_id: str):
        self.agent_id = agent_id
        self.alias_id = alias_id

    @abstractmethod
    async def invoke(self, session_id: str, prompt: str) -> str:
        """Invoke the agent and return the response text."""
        pass
