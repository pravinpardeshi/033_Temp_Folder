import os
import json
import re
import logging

import boto3
import psycopg
import sqlglot

from sqlglot import exp

from bedrock_agentcore import BedrockAgentCoreApp


# ============================================================
# 1. CONFIGURATION
# ============================================================
class Config:

    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
    CHAT_MODEL_ID = os.getenv("BEDROCK_CHAT_MODEL_ID", "YOUR_SUPPORTED_CLAUDE_MODEL_ID")
    DATABASE_URL = os.environ["DATABASE_URL"]
    ALLOWED_TABLES = {"sales"}
    MAX_ROWS = 500
    QUERY_TIMEOUT_MS = 10000


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# 2. LLM SERVICE
# ============================================================
class LLMService:
    def __init__(self, client):
        self.client = client

    def generate(self, system_prompt, user_prompt):
        response = self.client.converse(
            modelId=Config.CHAT_MODEL_ID,
            system=[
                {"text": system_prompt}
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": user_prompt}
                    ]
                }
            ],

            inferenceConfig={
                "temperature": 0,
                "maxTokens": 2000
            }
        )

        return "".join(
            item.get("text", "")
            for item in response["output"]["message"]["content"]
            if "text" in item
        )

# ============================================================
# 3. SQL GENERATOR
# ============================================================
class SQLGenerator:
    def __init__(self, llm_service):
        self.llm = llm_service

    def generate_sql(self, question):
        system_prompt = """
        You are a PostgreSQL SQL generation assistant.

        Your only available table is:

        sales(
            id BIGINT,
            order_date DATE,
            department TEXT,
            revenue NUMERIC,
            quantity INTEGER
        )

        Rules:

        1. Generate PostgreSQL SELECT queries only.
        2. Never generate INSERT, UPDATE, DELETE, DROP,
           ALTER, CREATE, or other modification statements.
        3. Only use the sales table.
        4. Never access system catalogs or other tables.
        5. Use explicit column names.
        6. Do not use SELECT *.
        7. Include a LIMIT of at most 500.
        8. Do not invent columns.
        9. Return ONLY SQL, without Markdown fences.
        """

        sql = self.llm.generate(system_prompt=system_prompt, user_prompt=question)

        # Remove accidental Markdown fences
        sql = re.sub(
            r"^```(?:sql)?\s*|\s*```$",
            "",
            sql.strip(),
            flags=re.IGNORECASE
        )

        return sql.strip()

# ============================================================
# 4. SQL VALIDATOR
# ============================================================

class SQLValidator:
    def validate(self, sql):
        logger.info("Validating generated SQL")

        statements = sqlglot.parse(sql, read="postgres")

        if len(statements) != 1 or statements[0] is None:
            raise ValueError("Only one SQL statement is allowed")

        statement = statements[0]

        # Only SELECT statements are permitted
        if not isinstance(statement, exp.Select):
            raise ValueError("Only SELECT queries are permitted")

        # Reject SELECT INTO
        if statement.args.get("into") is not None:
            raise ValueError("SELECT INTO is not permitted")

        # Check all referenced tables
        tables = statement.find_all(exp.Table)

        if not tables:
            raise ValueError("Query must reference an approved table")

        for table in tables:
            if table.name.lower() not in Config.ALLOWED_TABLES:
                raise ValueError(f"Unauthorized table: {table.name}")

            # Reject schema-qualified table access
            if table.db or table.catalog:
                raise ValueError("Schema-qualified table access is not permitted")

        # Reject SELECT *
        if any(isinstance(node, exp.Star) for node in statement.find_all(exp.Star)):
            raise ValueError("SELECT * is not permitted")

        # Enforce a maximum result size
        limit = statement.args.get("limit")

        if limit is None:
            statement = statement.limit(Config.MAX_ROWS)
        else:
            limit_expression = limit.expression
            if not isinstance(limit_expression, exp.Literal):
                raise ValueError("LIMIT must be a numeric literal")

            if limit_expression.is_string:
                raise ValueError("Invalid LIMIT")

            limit_value = int(limit_expression.this)

            if not 1 <= limit_value <= Config.MAX_ROWS:
                raise ValueError("LIMIT exceeds allowed range")

        return statement.sql(dialect="postgres")

# ============================================================
# 5. DATABASE SERVICE
# ============================================================
class DatabaseService:
    def __init__(self, database_url):
        self.database_url = database_url

    def execute_query(self, sql):
        logger.info("Executing read-only SQL query")

        with psycopg.connect(self.database_url, connect_timeout=10) as conn:
            # Read-only transaction
            with conn.transaction():
                conn.execute( "SET TRANSACTION READ ONLY")
                conn.execute( f"SET LOCAL statement_timeout = " f"'{Config.QUERY_TIMEOUT_MS}ms'")

                with conn.cursor() as cursor:
                    cursor.execute(sql)
                    columns = [ column.name for column in cursor.description ]
                    rows = cursor.fetchall()

        results = []

        for row in rows:
            results.append( dict(zip(columns, row)))

        return results

# ============================================================
# 6. DATA ANALYSIS SERVICE
# ============================================================
class DataAnalysisService:
    def __init__(self, llm_service):
        self.llm = llm_service

    def analyze(self, question, sql, data):
        system_prompt = """
        You are a professional business data analyst.

        Analyze the SQL results and answer the user's question.

        Rules:

        1. Use only the supplied query results.
        2. Do not invent or extrapolate missing values.
        3. Explain important trends and comparisons.
        4. If there is insufficient data, say so.
        5. Format numerical values clearly.
        6. Keep the answer understandable for business users.
        """

        user_prompt = f"""
        USER QUESTION:

        {question}

        SQL EXECUTED:

        {sql}

        QUERY RESULTS:

        {json.dumps(data, default=str)}

        Provide your analysis.
        """

        return self.llm.generate(system_prompt, user_prompt)

# ============================================================
# 7. DATA ANALYSIS AGENT
# ============================================================
class DataAnalysisAgent:
    def __init__(self):
        logger.info("Initializing Data Analysis Agent")
        bedrock_client = boto3.client("bedrock-runtime", region_name=Config.AWS_REGION)

        llm_service = LLMService(bedrock_client)
        self.sql_generator = SQLGenerator(llm_service)
        self.sql_validator = SQLValidator()
        self.database = DatabaseService(Config.DATABASE_URL)
        self.analysis_service = DataAnalysisService(llm_service)

    def execute(self, question):
        logger.info("Starting data analysis")

        # Step 1: Generate SQL
        sql = self.sql_generator.generate_sql(question)

        # Step 2: Validate SQL
        validated_sql = self.sql_validator.validate(sql)

        # Step 3: Execute SQL
        data = self.database.execute_query(validated_sql)

        # Step 4: Analyze results
        analysis = self.analysis_service.analyze(question=question, sql=validated_sql, data=data)

        # Step 5: Return structured response
        return {
            "agent": "data_analysis",
            "status": "success",
            "question": question,
            "answer": analysis,
            "sql": validated_sql,
            "row_count": len(data),
            "data": data
        }

# ============================================================
# 8. AGENTCORE ENTRY POINT
# ============================================================
app = BedrockAgentCoreApp()
data_agent = DataAnalysisAgent()

@app.entrypoint
def invoke(payload):
    question = payload.get("question")

    if not isinstance(question, str) or not question.strip():
        raise ValueError("A valid question is required")

    return data_agent.execute(question)

if __name__ == "__main__":

    app.run()


