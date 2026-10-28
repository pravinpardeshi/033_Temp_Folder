from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # AWS Infrastructure
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None

    # Model Configurations
    LLM_MODEL_ID: str = "anthropic.claude-3-5-sonnet-20240620-v1:0"
    EMBEDDING_MODEL_ID: str = "amazon.titan-embed-text-v2:0"

    # ServiceNOW Agent (v1)
    SNOW_AGENT_ID: str = ""
    SNOW_AGENT_ALIAS_ID: str = ""
    RDS_INSTANCE_ARN: str = ""
    RDS_TABLE_NAME: str = "snow_embeddings_table"

    # RDS connection (IAM authentication - no password)
    DB_HOST: str = ""
    DB_PORT: int = 5432
    DB_NAME: str = "postgres"
    DB_USER: str = ""
    DB_SSLMODE: str = "require"

    # RAG
    RAG_TOP_K: int = 5

    # Application Settings
    APP_PORT: int = 8080
    APP_HOST: str = "0.0.0.0"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
