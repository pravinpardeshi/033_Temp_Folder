from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # AWS Infrastructure
    AWS_REGION: str = "ca-central-1"
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None

    # Model Configurations
    LLM_MODEL_ID: str = "anthropic.claude-3-5-sonnet-20240620-v1:0"
    LLM_MAX_TOKENS: int = 1024
    EMBEDDING_MODEL_ID: str = "amazon.titan-embed-text-v2:0"

    # ServiceNOW Agent (v1)
    SNOW_AGENT_ID: str = ""
    SNOW_AGENT_ALIAS_ID: str = ""
    RDS_INSTANCE_ARN: str = ""
    RDS_TABLE_NAME: str = "snow_embeddings_table"

    # RCA Agent (v2)
    RCA_TABLE_NAME: str = ""
    RCA_TOP_K: int = 8
    RCA_MAX_TOKENS: int = 1500

    # Solution Recommender Agent (v2)
    SOLUTION_AGENT_ID: str = ""
    SOLUTION_AGENT_ALIAS_ID: str = ""

    # Intent router (only used when more than one agent is configured)
    AGENT_ROUTER_MODEL_ID: str = ""


    # RDS connection (IAM authentication - no password)
    DB_HOST: str = ""
    DB_PORT: int = 5432
    DB_NAME: str = "postgres"
    DB_USER: str = "postgres"
    DB_SSLMODE: str = "require"

    # RAG
    RAG_TOP_K: int = 5

    # Hybrid search (vector + PostgreSQL full-text search, fused with RRF)
    HYBRID_SEARCH_ENABLED: bool = True
    HYBRID_CANDIDATE_K: int = 20
    RRF_K: int = 60

    # Cross-encoder reranking (Bedrock Rerank API)
    RERANK_ENABLED: bool = True
    RERANK_MODEL_ARN: str = ""

    # Application Settings
    APP_PORT: int = 8080
    APP_HOST: str = "0.0.0.0"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
