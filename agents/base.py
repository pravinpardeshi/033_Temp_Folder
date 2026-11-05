from abc import ABC, abstractmethod


class BaseAgent(ABC):
    @abstractmethod
    async def invoke(
        self,
        session_id: str,
        prompt: str,
        table_name: str | None = None,
    ) -> str:
        """Invoke the agent and return the response text.

        ``table_name`` is only honored by agents that query the vector store
        (currently the SNOW agent); other agents ignore it.
        """
        pass
