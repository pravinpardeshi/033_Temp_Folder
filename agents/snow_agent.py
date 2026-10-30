from .rag_agent import RAGAgent
from config import settings

QA_SYSTEM_PROMPT = (
    "You are a ServiceNOW support assistant. Answer questions about closed "
    "incidents using the retrieved incident records. Prefer concrete details "
    "(resolution steps, affected service, error messages) found in the context."
)


class SnowAgent(RAGAgent):
    """Question/answer agent over closed ServiceNOW incidents."""

    allows_table_override = True

    async def invoke(
        self,
        session_id: str,
        prompt: str,
        table_name: str | None = None,
    ) -> str:
        if table_name and table_name not in settings.snow_tables:
            return (
                f"Unknown incident table '{table_name}'. "
                f"Choose one of: {', '.join(settings.snow_tables)}."
            )
        return await super().invoke(session_id, prompt, table_name=table_name)

    def __init__(self):
        super().__init__(
            table_name=settings.RDS_TABLE_NAME,
            top_k=settings.RAG_TOP_K,
            system_prompt=QA_SYSTEM_PROMPT,
            max_tokens=settings.LLM_MAX_TOKENS,
            agent_id=settings.SNOW_AGENT_ID,
            alias_id=settings.SNOW_AGENT_ALIAS_ID,
        )
