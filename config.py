from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env next to this file so settings load regardless of the working
# directory (local runs vs. the AgentCore runtime process).
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    # AWS Infrastructure
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_SESSION_TOKEN: str | None = None

    # Chat session storage (DynamoDB metadata/transcript + optional Memory)
    SESSIONS_TABLE: str = "ChatSessions"
    SSO_USERS_TABLE: str = "SsoUsers"
    # Point at DynamoDB Local, e.g. http://localhost:8000. Blank = real AWS.
    DYNAMODB_ENDPOINT_URL: str = ""
    MEMORY_ID: str = ""
    MEMORY_ENABLED: bool = True
    MEMORY_CONTEXT_TURNS: int = 10
    SESSION_TTL_DAYS: int = 30
    SESSION_TITLE_MAX_CHARS: int = 60
    SESSION_TITLE_RENAME_MAX_CHARS: int = 100

    # Monitoring dashboard
    # Default UI mode.
    MONITOR_DEFAULT_MODE: str = "live"
    MONITOR_AWS_ENABLED: bool = True
    MONITOR_LOOKBACK_MINUTES: int = 60
    MONITOR_AWS_CACHE_SECONDS: int = 60
    # CloudWatch dimensions. Leave a name blank to skip that service.
    MONITOR_LAMBDA_FUNCTION_NAME: str = ""
    # API Gateway type: "http" uses ApiId + Stage; "rest" uses ApiName + Stage.
    MONITOR_API_GATEWAY_TYPE: str = "http"
    MONITOR_API_GATEWAY_API_ID: str = ""
    MONITOR_API_GATEWAY_API_NAME: str = ""
    MONITOR_API_GATEWAY_STAGE: str = ""
    MONITOR_BEDROCK_MODEL_ID: str = ""

    # Dynatrace observability (monitor a different application)
    DYNATRACE_ENABLED: bool = True
    # Environment URL, e.g. https://abc12345.live.dynatrace.com
    DYNATRACE_URL: str = ""
    # API token with scopes: metrics.read, entities.read, services.read
    DYNATRACE_API_TOKEN: str = ""
    # Dynatrace entity id of the application to monitor, e.g. APPLICATION-1234567890
    DYNATRACE_APP_ID: str = ""
    # Display name shown on the dashboard
    DYNATRACE_APP_NAME: str = ""
    DYNATRACE_CACHE_SECONDS: int = 60

    # Model Configurations
    LLM_MODEL_ID: str = "anthropic.claude-3-5-sonnet-20240620-v1:0"
    LLM_MAX_TOKENS: int = 1024
    LLM_TEMPERATURE: float = 0.2
    EMBEDDING_MODEL_ID: str = "amazon.titan-embed-text-v2:0"

    # Chat Agent (no retrieval)
    CHAT_MAX_TOKENS: int = 1024
    CHAT_TEMPERATURE: float = 0.4

    # ServiceNOW Agent (v1)
    RDS_INSTANCE_ARN: str = ""
    RDS_TABLE_NAME: str = "snow_embeddings_table"
    # Tables the UI may select for the SNOW agent (comma-separated).
    SNOW_TABLE_OPTIONS: str = "snow_embeddings_table,XYZ123"

    # RCA Agent (v2)
    RCA_TABLE_NAME: str = ""
    RCA_TOP_K: int = 8
    RCA_MAX_TOKENS: int = 1500

    # Change quality assessment agent
    CHANGE_TABLE_NAME: str = ""
    CHANGE_QUALITY_MAX_TOKENS: int = 1200
    CHANGE_QUALITY_TEMPERATURE: float = 0.2

    # Risk assessment service proxied by POST /riskassessment
    RISK_ASSESSMENT_URL: str = ""
    RISK_ASSESSMENT_TOKEN: str = ""
    RISK_ASSESSMENT_TIMEOUT: float = 20.0

    # Intent router (only used when more than one agent is configured)
    AGENT_ROUTER_MODEL_ID: str = ""
    ROUTER_MAX_TOKENS: int = 10
    ROUTER_TEMPERATURE: float = 0.0

    # RDS connection (IAM authentication - no password)
    DB_HOST: str = ""
    DB_PORT: int = 5432
    DB_NAME: str = "postgres"
    DB_USER: str = "postgres"
    DB_SSLMODE: str = "require"
    DB_CONNECT_TIMEOUT: int = 10

    # RAG
    RAG_TOP_K: int = 5

    # Hybrid search (vector + PostgreSQL full-text search, fused with RRF)
    HYBRID_SEARCH_ENABLED: bool = True
    HYBRID_CANDIDATE_K: int = 20
    RRF_K: int = 60

    # Cross-encoder reranking (Bedrock Rerank API)
    RERANK_ENABLED: bool = True
    RERANK_MODEL_ARN: str = (
        "arn:aws:bedrock:us-east-1::foundation-model/cohere.rerank-v3-5:0"
    )

    # Application Settings
    APP_PORT: int = 8080
    APP_HOST: str = "0.0.0.0"

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def rca_table(self) -> str:
        """RCA reuses the SNOW table unless RCA_TABLE_NAME overrides it."""
        return self.RCA_TABLE_NAME or self.RDS_TABLE_NAME

    @property
    def snow_tables(self) -> list[str]:
        """Incident tables available to the SNOW agent (includes the current table)."""
        tables = [
            name.strip()
            for name in self.SNOW_TABLE_OPTIONS.split(",")
            if name.strip()
        ]
        if self.RDS_TABLE_NAME not in tables:
            tables.insert(0, self.RDS_TABLE_NAME)
        return tables


settings = Settings()
