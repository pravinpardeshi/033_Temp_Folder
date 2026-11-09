# Agentic ServiceNOW Chatbot (AWS Bedrock AgentCore)

A ChatGPT-style chatbot for closed ServiceNOW incidents. The backend runs on
**Amazon Bedrock AgentCore Runtime**, uses **Amazon Bedrock** models, and
retrieves context from a **PostgreSQL + pgvector** incident store on **Amazon
RDS** (IAM database authentication, no passwords).

Four agents are registered in the orchestrator:

| Agent | Key | What it does |
| --- | --- | --- |
| SNOW agent | `snow_agent` | Answers questions over closed ServiceNOW incidents. |
| RCA agent | `rca_agent` | Analyzes the root cause of a **new** incident using similar closed incidents. |
| Change QA agent | `change_qa_agent` | Assesses a change request by number (UI form, not chat). |
| Monitoring agent | `monitoring_agent` | Reports application health, token usage, and endpoint response times. |

Legacy keys are accepted as aliases: `chat` → `snow_agent`, `snow` →
`snow_agent`, `rca` → `rca_agent`, `change` / `change_qa` →
`change_qa_agent`; `monitor` / `monitoring` / `metrics` → `monitoring_agent`.

Routing is implemented with **LangGraph**:

```
START -> router -> (snow_agent | rca_agent | change_qa_agent | monitoring_agent) -> END
```

`router` respects an explicit `agent` in the request; otherwise it uses cheap
keyword hints and, if needed, a small Bedrock LLM classifier.

---

## Project structure

```
034_temp/
├── runtime_entrypoint.py     # AgentCore entry point (BedrockAgentCoreApp), /chat + routes
├── main.py                   # Local launcher (same app)
├── config.py                 # All settings (reads .env)
├── orchestrator.py           # Agent registry + routing
├── graph.py                  # LangGraph routing pipeline (router → agent)
├── monitoring.py             # /health, /api/monitor/* endpoints, metrics store
├── demo_monitoring.py        # Synthetic dashboard payload (demo mode)
├── live_monitoring.py        # In-process + CloudWatch + Dynatrace payload (live mode)
├── dynatrace_connector.py    # Dynatrace API v2 client (KPIs for another application)
├── session_memory_store.py   # Session metadata (DynamoDB) + AgentCore Memory transcript
├── create_session_table.py   # One-time: create the ChatSessions DynamoDB table
├── retrieve_agentcore_sessions.py  # Example: list sessions and fetch transcripts
├── create_session_in_agentCore.py  # Example: create a session and invoke the runtime
├── lambda_function.py        # API Gateway → AgentCore proxy
├── agents/
│   ├── base.py               # BaseAgent interface
│   ├── reranker.py           # RRF fusion + Bedrock Rerank (shared utility)
│   ├── chat_agent.py         # Legacy general chat agent (not registered)
│   ├── snow_agent.py         # QA agent — LangGraph embed → retrieve → rerank → answer
│   ├── snow_agent_0.1.py     # Legacy pre-LangGraph backup of the QA agent
│   ├── rca_agent.py          # Root cause analysis (self-contained RAG implementation)
│   ├── change_qa_agent.py    # Change item quality assessment (form-driven)
│   └── monitoring_agent.py   # Health, token usage, and endpoint metrics answers
├── static/                   # Chat UI + Dashboard (HTML/CSS/JS)
├── SESSION_MEMORY_GUIDE.md   # Session storage design, schema, and usage guide
├── AGENTCORE_MEMORY.md       # AgentCore Memory setup (resource, IAM, events)
├── Dynatrace_Observability.md# Dynatrace KPI definitions and alerting strategy
├── AD_SSO_Memory_Implementation_Plan.md  # AD SSO + Memory coding plan
├── build_deploy_package.bat  # Builds dist\agentcore_deployment.zip (Windows)
├── build_deploy_package.sh   # Same packaging for Linux/macOS
├── agentcore.json            # Runtime metadata
├── requirements.txt
└── pyproject.toml
```

---

## Local setup

```bash
cp .env.example .env      # fill in your values
pip install -r requirements.txt
python main.py            # http://localhost:8080
```

Health check: `curl http://localhost:8080/ping`

### Environment variables

| Variable | Purpose |
| --- | --- |
| `AWS_REGION` | Region for Bedrock, RDS, and CloudWatch. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Optional static credentials for local runs; prefer an IAM role or environment credentials. |
| `AWS_SESSION_TOKEN` | Optional token for temporary STS credentials. |
| `MONITOR_DEFAULT_MODE` | Dashboard mode on first load: `demo` (default) or `live`. |
| `MONITOR_AWS_ENABLED` | Enable AWS CloudWatch metrics in live mode (default true). |
| `MONITOR_LOOKBACK_MINUTES`, `MONITOR_AWS_CACHE_SECONDS` | CloudWatch window and cache duration (defaults 60 / 60). |
| `MONITOR_LAMBDA_FUNCTION_NAME` | Lambda function to query for invocations, errors, duration, and throttles. |
| `MONITOR_API_GATEWAY_TYPE` | API Gateway type: `http` (default) or `rest`. |
| `MONITOR_API_GATEWAY_API_ID` | HTTP API ID (`ApiId`) used for HTTP API CloudWatch metrics. |
| `MONITOR_API_GATEWAY_API_NAME` | REST API name used when `MONITOR_API_GATEWAY_TYPE=rest`. |
| `MONITOR_API_GATEWAY_STAGE` | API Gateway stage name; HTTP APIs commonly use `$default`. |
| `MONITOR_BEDROCK_MODEL_ID` | Bedrock model or inference-profile ID for invocation and token metrics. |
| `DYNATRACE_ENABLED` | Enable the Dynatrace observability feed (default true). |
| `DYNATRACE_URL` | Dynatrace environment URL, e.g. `https://abc12345.live.dynatrace.com`. |
| `DYNATRACE_API_TOKEN` | Dynatrace API token (`metrics.read`, `entities.read`, `services.read`). |
| `DYNATRACE_APP_ID` | Dynatrace entity ID of the application to monitor (`APPLICATION-…`). |
| `DYNATRACE_APP_NAME` | Display name on the dashboard (falls back to the entity ID). |
| `DYNATRACE_CACHE_SECONDS` | Cache duration for Dynatrace metrics (default 60). |
| `LLM_MODEL_ID` | Bedrock model used for answers (e.g. Claude). |
| `LLM_MAX_TOKENS` | Max output tokens for the QA agent. |
| `LLM_TEMPERATURE` | Sampling temperature for RAG answers (default 0.2). |
| `CHAT_MAX_TOKENS`, `CHAT_TEMPERATURE` | Output limit and temperature for the chat agent. |
| `EMBEDDING_MODEL_ID` | Bedrock embedding model (Titan). |
| `RDS_TABLE_NAME` | Default pgvector table for the QA agent. |
| `SNOW_TABLE_OPTIONS` | Comma-separated tables selectable in the UI for the SNOW agent (default `snow_embeddings_table,XYZ123`). |
| `RAG_TOP_K` | Number of incidents retrieved by the QA agent. |
| `HYBRID_SEARCH_ENABLED` | Combine vector search with PostgreSQL full-text search (default true). |
| `HYBRID_CANDIDATE_K` | Candidates fetched from each retriever before fusion (default 20). |
| `RRF_K` | Reciprocal Rank Fusion constant (default 60). |
| `RERANK_ENABLED` | Enable Bedrock cross-encoder reranking (default true). |
| `RERANK_MODEL_ARN` | Reranker model ARN; blank disables reranking (falls back to fused order). |
| `RCA_TABLE_NAME` | Table for the RCA agent (blank = use `RDS_TABLE_NAME`). |
| `RCA_TOP_K` | Incidents retrieved for RCA (default 8). |
| `RCA_MAX_TOKENS` | Max output tokens for RCA. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_SSLMODE` | RDS connection (IAM auth, no password). |
| `DB_CONNECT_TIMEOUT` | Seconds to wait when opening the RDS connection (default 10). |
| `RDS_INSTANCE_ARN` | RDS ARN used in IAM policy scoping. |
| `AGENT_ROUTER_MODEL_ID` | Model for intent classification (blank = `LLM_MODEL_ID`). |
| `ROUTER_MAX_TOKENS`, `ROUTER_TEMPERATURE` | Output limit and temperature for the intent classifier. |
| `CHANGE_TABLE_NAME` | Optional pgvector table with change records; enables lookup by change number. |
| `CHANGE_QUALITY_MAX_TOKENS`, `CHANGE_QUALITY_TEMPERATURE` | Output limit and temperature for change assessments. |
| `SESSIONS_TABLE` | DynamoDB table for chat session metadata (default `ChatSessions`). |
| `MEMORY_ID` | AgentCore Memory resource ID used for chat transcripts. |
| `MEMORY_ENABLED` | Persist transcripts to AgentCore Memory (default true). |
| `SESSION_TTL_DAYS` | Auto-expire session metadata after N days; `0` disables (default 30). |
| `APP_PORT`, `APP_HOST` | Local server binding (default 8080 / 0.0.0.0). |

## Using the agents

Explicit agent selection (recommended for automation):

```bash
# SNOW / QA
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"How do we fix VPN error 809?","agent":"snow_agent"}'

# RCA
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"New P1: payment service returns 502 since 09:00","agent":"rca_agent"}'

# Change QA
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"CHG0030001","agent":"change_qa_agent"}'
```

Auto-routing (omit `agent`):

```bash
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"What is the root cause of the checkout outage?"}'
```

AgentCore invocation contract (`POST /invocations`):

```bash
curl -X POST http://localhost:8080/invocations -H "Content-Type: application/json" \
  -d '{"prompt":"Why did the database fail over?","agent":"rca_agent"}'
```

Reuse a conversation by passing the same `session_id` (see
[Chat sessions and history](#chat-sessions-and-history) for how session IDs
are resolved and stored).

---

## Chat UI

The UI in `static/` is a single-page app served by the runtime:

- **Agent switcher** — Chat, SNOW, RCA, and Monitoring, with per-agent
  suggestions. Every request sends the selected agent key explicitly.
- **Dashboard** — the landing page (auto-selected on first load) with
  stat cards, endpoint/invocation tables, an AWS service feed, and the
  Dynatrace pane. A **Demo / Live** toggle persists in `localStorage`
  and refreshes every 10 seconds while the view is open.
- **Tools → Observability** — a dedicated view of the Dynatrace
  golden-signal KPIs (availability, throughput, response time, error rate)
  for the monitored application. It fetches
  `GET /api/monitor/dynatrace?mode=…` directly, has its own **Demo / Live**
  toggle (kept in sync with the Dashboard's setting), and auto-refreshes
  every 10 seconds while open.
- **Data source dropdown** — when the SNOW agent is active, a table selector
  lists the pgvector tables from `SNOW_TABLE_OPTIONS` (default
  `snow_embeddings_table`, plus `XYZ123`). The choice is persisted per
  conversation and sent as `table_name`; the backend validates it against the
  allow-list before querying.
- **Light / dark theme** toggle in the sidebar. The choice is persisted in
  `localStorage` and defaults to the OS preference.
- **Conversation history** stored in `localStorage` with per-conversation
  delete and "clear all". The backend `session_id` is preserved per
  conversation and persisted server-side (see
  [Chat sessions and history](#chat-sessions-and-history)).
- **Markdown rendering** for assistant answers (headings, lists, bold,
  inline code, fenced code blocks) and a copy button per answer.
- **Typing indicator**, runtime status (`/ping`), responsive sidebar for
  mobile, `Enter` to send and `Shift+Enter` for a newline.

The browser only calls the single `POST /chat` route (or its API Gateway
equivalent). Change QA uses the same route with `agent: "change"`. No AWS
credentials are exposed to the browser.

---

## Chat sessions and history

Chat history is persisted server-side in two stores with distinct roles:

| Store | Holds | Key |
| --- | --- | --- |
| **AgentCore Memory** | The full message transcript (assistant/user turns) | `actor_id` + `session_id` |
| **DynamoDB `ChatSessions`** | Session metadata: title, timestamps, user mapping | `PK = USER#<actor_id>`, `SK = SESSION#<session_id>` |

DynamoDB is the index — AgentCore has no "list sessions for user" API, so the
metadata table answers "which sessions does this user have and which is the
newest", while AgentCore Memory answers "what was said". Session IDs are
generated by the application (UUID) at creation time.

Full details: [`SESSION_MEMORY_GUIDE.md`](SESSION_MEMORY_GUIDE.md) (schema,
usage, request flow) and [`AGENTCORE_MEMORY.md`](AGENTCORE_MEMORY.md)
(resource + IAM setup).

### Session resolution in `POST /chat`

The handler in `runtime_entrypoint.py` resolves the session before running
the agent, in this order:

1. **Requested session exists** — if the request carries `session_id` and it
   exists (DynamoDB metadata present), reuse it.
2. **Otherwise, the user's latest session** — look up the most recent session
   for `actor_id` in DynamoDB and reuse that.
3. **Otherwise, a new session** — use the request's `session_id` as the new
   session (generating a UUID only if the request omitted one).

Whatever is resolved is returned in the response as `session_id` and reused on
every subsequent turn of that conversation.

Failures are never fatal: if session lookup fails, the handler falls back to
the request's `session_id` (or a fresh UUID) and proceeds; if `save_turn`
fails, the error is logged and the chat response is still returned. Session
storage cannot break an answer.

### Saving a turn

After each answer the handler calls `SessionMemoryService.save_turn`, which:

- appends the user message and assistant answer to the **AgentCore Memory**
  transcript for `actor_id`/`session_id`, and
- upserts **DynamoDB metadata** (creating the row on first turn, touching
  `updated_at` and the TTL thereafter).

### Create the metadata table (one-time)

```bash
python create_session_table.py
```

The script is idempotent: it creates `ChatSessions` with the PK/SK schema and
on-demand billing, warns if a table exists with the wrong schema, and does
nothing if the table is already correct. The AWS CLI equivalent:

```bash
aws dynamodb create-table \
  --table-name ChatSessions \
  --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST
```

### Retention

`SESSION_TTL_DAYS` (default 30) writes a DynamoDB `ttl` attribute (epoch
seconds) on session creation and refreshes it on every turn — active chats
never expire; abandoned ones are cleaned up automatically. Set `SESSION_TTL_DAYS=0`
to disable expiry.

### Example scripts

| Script | Purpose |
| --- | --- |
| `create_session_in_agentCore.py` | Create a session, link it to a user, invoke the runtime. |
| `retrieve_agentcore_sessions.py` | List a user's sessions from DynamoDB, then fetch transcripts from AgentCore Memory (the two-step fetch). |

```bash
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"What is VPN error 809?","session_id":"<optional-uuid>"}'
```

---

## Change item quality assessment

The sidebar **Tools → Change QA** page is a standalone form, not a chat. It
takes a **Change Request Number** and a submit button, calls the backend, and
renders the returned assessment below the form. The chat composer is hidden
on this page.

The UI sends the change number through the same route as chat:

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"CHG0030001","agent":"change"}'
```

Response:

```json
{
  "response": "## Summary\n...",
  "session_id": "…"
}
```

The standalone runtime route `POST /api/change/assess` remains available for
direct integrations, but the UI does not use it so everything can go through
one Lambda.

The agent (`agents/change_qa_agent.py`) reviews completeness, risk/impact,
implementation and backout plans, testing, scheduling, and approvals. If
`CHANGE_TABLE_NAME` is set, it looks up the change record by number in that
pgvector table and grounds the assessment in it. When no record is available
it does **not** invent details; it states what could not be retrieved and
lists the fields needed for a complete review.

---

## Retrieval: hybrid search and reranking

Both `snow_agent` and `rca_agent` retrieve context from the same pgvector
store using the shared utilities in `agents/reranker.py` (not as agents). The
shared pieces are:

1. **Vector search** — pgvector cosine distance (`<=>`) over the incident
   embeddings, top `HYBRID_CANDIDATE_K`.
2. **Keyword search** — PostgreSQL full-text search
   (`to_tsvector` / `plainto_tsquery` / `ts_rank_cd`), top
   `HYBRID_CANDIDATE_K`. This catches incident IDs, error codes, and exact
   phrases that embeddings can miss.
3. **Fusion** — Reciprocal Rank Fusion (`agents/reranker.py`) merges both
   ranked lists; a document in both lists ranks higher.
4. **Reranking** — if `RERANK_MODEL_ARN` is set and `RERANK_ENABLED=true`, the
   fused candidates go through the Bedrock Rerank API and the best
   `top_k` are kept. Any failure falls back to the fused order.

Reranking is deliberately **not** an agent: it does not converse, route, or
produce answers, so adding it to the agent registry would complicate routing
without benefit. It is a retrieval step shared by all RAG agents.

### SNOW agent pipeline (LangGraph)

`agents/snow_agent.py` runs its retrieval as an explicit **LangGraph**
`StateGraph` (`self.graph`, compiled in `__init__`); `rca_agent` keeps the
same steps as a linear method:

```
START → embed_query → retrieve → rerank ─┬→ generate_answer → END   (documents found)
                                         └→ no_context_reply → END  (nothing found)
```

- **State** — `SnowState` (`TypedDict`): `prompt`, `table_name`,
  `query_vector`, `candidates`, `documents`, `context`, `answer`.
- **Nodes** — `embed_query`, `retrieve`, `rerank`, `generate_answer`,
  `no_context_reply` are synchronous nodes; LangGraph runs them in a worker
  thread during `ainvoke`.
- **Conditional edge** — `_route_after_rerank` sends the run to the
  deterministic `no_context_reply` (skips the LLM entirely) when reranking
  returns no documents, otherwise to `generate_answer`.
- **Public contract unchanged** — `invoke()` still accepts the `BaseAgent`
  arguments and returns the answer string; `generate_answer` records exact
  Bedrock `usage` tokens for the dashboard.
- **Errors** propagate to `invoke()` and return
  `Error querying the incident vector store: …`.


For scale, add a GIN index for keyword search:

```sql
CREATE INDEX IF NOT EXISTS snow_content_fts
ON snow_embeddings_table
USING gin (to_tsvector('english', content));
```

---

## Deployment to AgentCore Runtime

1. Fill `.env` with real values.
2. Build the package (downloads Linux **arm64** wheels):
   ```bat
   build_deploy_package.bat
   ```
3. Upload `dist\agentcore_deployment.zip` to S3.
4. Create the AgentCore Runtime:
   - Runtime: `PYTHON_3_12`
   - Entry point: `runtime_entrypoint.py`
   - Health check: `/ping`
   - Port: `8080`
5. Set the environment variables from the table above in the Runtime
   configuration (do **not** ship `.env`).
6. Ensure the execution role has:
   - `bedrock:InvokeModel` (LLM + embeddings)
   - `bedrock:Rerank` (optional reranking)
   - `rds-db:connect` on `arn:aws:rds-db:<region>:<acct>:dbuser:<resource-id>/<db-user>`
   - `s3:GetObject` for the deployment ZIP
   - DynamoDB read/write on the `ChatSessions` table
     (`GetItem`, `PutItem`, `Query`, `UpdateItem`, `DeleteItem`) for session
     metadata
   - `bedrock-agentcore` memory data-plane actions scoped to the memory ARN
     (`CreateEvent`, `ListEvents`, `RetrieveMemoryRecords`, …) — full policy
     in [`AGENTCORE_MEMORY.md`](AGENTCORE_MEMORY.md#4-iam-permissions)
7. Attach the Runtime to the **VPC** with the RDS security group reachable
   (RDS is private). Allow the Runtime security group on port 5432 and enable
   IAM database authentication on the RDS instance.

---

## Browser → API Gateway → Lambda → AgentCore Runtime → Vector DB

The chat UI cannot call a private runtime directly, so use a thin proxy:

```
Browser (static UI on S3/CloudFront, or any web host)
   │  POST /chat  {message, agent?, session_id?}
   ▼
Amazon API Gateway (HTTP API, CORS enabled)
   ▼
AWS Lambda (proxy, IAM invoke)
   │  bedrock-agentcore:InvokeAgentRuntime
   ▼
Amazon Bedrock AgentCore Runtime  (runtime_entrypoint.py, /invocations)
   │  pgvector similarity search (IAM auth)
   ▼
Amazon RDS for PostgreSQL + pgvector (snow_embeddings_table)
```

All agents live in **one Runtime**, behind **one Lambda**. The Lambda chooses
the agent with the `agent` field (`"snow_agent"`, `"rca_agent"`, or
`"change_qa_agent"`), or omits it to use auto-routing. One Runtime means one deployment package and
one set of environment variables; the SNOW and RCA agents share the vector
store but use different prompts, `top_k`, and (max) tokens, while the chat
agent uses no retrieval.

### Lambda handler (proxy)

```python
import json
import os
import uuid

import boto3

client = boto3.client("bedrock-agentcore")

def handler(event, context):
    body = json.loads(event.get("body") or "{}")

    payload = {
        "prompt": body.get("message") or body.get("prompt"),
        "agent": body.get("agent"),  # "snow", "rca", or None for auto-route
        "session_id": body.get("session_id") or str(uuid.uuid4()),
    }

    response = client.invoke_agent_runtime(
        agentRuntimeArn=os.environ["AGENT_RUNTIME_ARN"],
        runtimeSessionId=payload["session_id"],
        qualifier=os.environ.get("AGENT_QUALIFIER", "DEFAULT"),
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )

    data = json.loads(response["response"].read())

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({
            "response": data.get("result", data),
            "session_id": payload["session_id"],
        }),
    }
```

Lambda environment variables: `AGENT_RUNTIME_ARN`, optional `AGENT_QUALIFIER`.
The Lambda execution role needs `bedrock-agentcore:InvokeAgentRuntime` on the
Runtime ARN.

### Pointing the UI at API Gateway

Set `window.AGENTCORE_API_BASE` in `static/index.html` to your API Gateway URL:

```html
<script>
    window.AGENTCORE_API_BASE = "https://<api-id>.execute-api.<region>.amazonaws.com";
</script>
```

Every UI request then goes to `POST {AGENTCORE_API_BASE}/chat`, including
change quality assessment (`agent: "change"`). Enable CORS on API Gateway for
your UI origin, and add `OPTIONS` handling (HTTP APIs provide CORS
configuration without a Lambda change).

### Network notes

- Lambda → AgentCore is an AWS API call, so Lambda does **not** need VPC access.
- AgentCore Runtime → RDS requires the Runtime to be VPC-attached with a route
  and security-group rules to the database.
- The browser never talks to RDS or Bedrock directly.

---

## Adding another agent

The backend is registry-driven: agents are objects with an `invoke()` method,
registered under a short key. The LangGraph pipeline, the intent router, and
the `/chat` / `/invocations` endpoints all pick a new agent up automatically.
Adding UI controls is a separate (optional) step.

### How requests reach an agent

1. The request carries an optional `agent` key (`{"message": "...", "agent": "rca_agent"}`).
2. `graph.py` routes to that key when it is registered.
3. Otherwise the router uses a keyword hint, then an LLM classifier that is
   given each agent's **description** (the second argument to `register`).
4. If nothing matches, the default agent (`snow_agent`) handles it.

### Backend checklist

1. **Settings** — add any new settings/prompts to `config.py` and `.env`
   (and `.env.example`):
   ```python
   # config.py
   KB_AGENT_TABLE_NAME: str = "kb_embeddings_table"
   KB_AGENT_TOP_K: int = 6
   KB_AGENT_MAX_TOKENS: int = 1200
   ```
2. **Agent class** — create `agents/<name>_agent.py`. The contract is one
   async method:

   ```python
   class BaseAgent(ABC):
       async def invoke(
           self,
           session_id: str,
           prompt: str,
           table_name: str | None = None,
       ) -> str: ...
   ```

   Pick the closest starting point:

   **a) RAG over the vector store** (like SNOW / RCA): copy the retrieval
   methods from `agents/snow_agent.py` (or `rca_agent.py`) into the new agent
   and adjust the table, `top_k`, and system prompt. Each agent owns its full
   implementation — embedding, hybrid search, RRF fusion, reranking, and the
   LLM call. Only `agents/reranker.py` is shared.

   ```python
   # agents/kb_agent.py
   from .base import BaseAgent
   from .reranker import BedrockReranker, reciprocal_rank_fusion
   from config import settings

   KB_PROMPT = "You are a knowledge-base assistant. Answer using the retrieved records."

   class KBAgent(BaseAgent):
       def __init__(self):
           self.table_name = settings.KB_AGENT_TABLE_NAME
           self.top_k = settings.KB_AGENT_TOP_K
           self.max_tokens = settings.KB_AGENT_MAX_TOKENS
           self.system_prompt = KB_PROMPT
           # ... copy the RAG methods from snow_agent.py
   ```

   **b) Plain LLM agent** (like Chat): subclass `BaseAgent` and call
   `bedrock-runtime` `converse` directly. See `agents/chat_agent.py`.

3. **Register it** in `orchestrator.py:_register_agents()`:

   ```python
   self.register(
       "kb",
       KBAgent(),
       "Questions about the internal knowledge base and runbooks.",
   )
   ```

   - The key (`"kb"`) is what clients send as `agent` and what appears in
     logs and monitor metrics.
   - The description is what the LLM router sees — make it specific and
     distinct from the other agents.
   - `default=True` is only for the fallback agent (currently `snow_agent`); leave
     it off for new agents.
   - `register()` rebuilds the compiled graph, so `graph.py` needs no edits.

4. **Optional keyword routing** — add deterministic hints to
   `AgentOrchestrator._keyword_route()` before the LLM classifier:
   ```python
   if "kb" in self._agents and any(t in text for t in ("runbook", "knowledge base")):
       return "kb"
   ```

5. **AWS/IAM** — grant the Runtime role permissions for any new service the
   agent calls (e.g. `bedrock:Retrieve` for a Knowledge Base). New tables must
   live in the same RDS instance and be reachable by the existing IAM user.

6. **Deploy** — add the new env vars to the AgentCore Runtime configuration,
   rebuild `dist\agentcore_deployment.zip` with `build_deploy_package.bat`,
   and redeploy. New modules under `agents/` are included by the script.

No changes are required in `graph.py`, `runtime_entrypoint.py`, or
`monitoring.py`.

### UI checklist (optional)

The web UI keeps its own agent metadata, so a new agent does not appear until
you add it there:

1. **Sidebar + welcome card** in `static/index.html` (copy an existing block;
   `data-agent` must equal the backend key):
   ```html
   <button class="agent-item" data-agent="kb" role="radio" title="KB — Knowledge base">
       <span class="agent-icon kb">K</span>
       <span class="agent-copy">
           <strong>KB</strong>
           <small>Internal runbooks</small>
       </span>
   </button>
   ```
2. **Metadata map** in `static/script.js` (`AGENTS`): label, avatar letter,
   composer placeholder, and sample prompts. Requests send `state.agent`, so
   the key must match the backend registration:
   ```js
   kb: {
       key: "kb",
       label: "KB",
       letter: "K",
       placeholder: "Ask about runbooks…",
       samples: ["What is the runbook for a database failover?"],
   },
   ```
3. **Icon colour** in `static/style.css`:
   ```css
   .agent-icon.kb { background: linear-gradient(135deg, #7c3aed, #a78bfa); }
   ```
   Use a key that does not collide with existing CSS class names (the icon
   modifier is `.agent-icon.<key>` and the avatar gets `.avatar.<key>`).
   Unknown stored values fall back to `chat` in the UI.

### Verify

```bash
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"What is the failover runbook?","agent":"kb"}'

curl "http://localhost:8080/api/monitor/invocations?limit=5"
```

Auto-routing test: omit `agent` and ask something that only matches the new
description, then confirm the response and the invocation log show the new key.

---

## Monitoring

| Endpoint | Purpose |
| --- | --- |
| `GET /ping` | AgentCore health contract. |
| `GET /health` | Dashboard health probe. |
| `GET /api/monitor/dashboard?mode=demo\|live` | Unified dashboard payload; defaults to `MONITOR_DEFAULT_MODE` (`demo`). |
| `GET /api/monitor/metrics` | Requests, tokens, latency, error rate, uptime, and per-endpoint response times. |
| `GET /api/monitor/endpoints` | Per-endpoint request counts, average/P95 latency, and errors. |
| `GET /api/monitor/invocations?limit=50` | Recent invocation log. |
| `GET /api/monitor/dynatrace?mode=demo\|live` | Dynatrace KPI feed for the monitored (other) application; `mode=demo` returns synthetic data, default is live. |
| `GET /api/tables` | Tables available to the SNOW agent and the default table. |

The UI dashboard defaults to **Demo** mode and can be switched to **Live** with
the toggle. Live mode combines in-process application metrics with AWS
CloudWatch metrics. Configure credentials and resource names in `config.py`:
`MONITOR_LAMBDA_FUNCTION_NAME`, `MONITOR_API_GATEWAY_TYPE`,
`MONITOR_API_GATEWAY_API_ID` (HTTP), `MONITOR_API_GATEWAY_API_NAME` (REST),
`MONITOR_API_GATEWAY_STAGE`, and `MONITOR_BEDROCK_MODEL_ID`. AWS metrics are
cached for `MONITOR_AWS_CACHE_SECONDS`.

For an **HTTP API**, use the API ID and stage:

```python
MONITOR_API_GATEWAY_TYPE = "http"
MONITOR_API_GATEWAY_API_ID = "abc123def4"   # API Gateway HTTP API ID
MONITOR_API_GATEWAY_STAGE = "$default"      # or your named stage
```

HTTP API metrics use the `ApiId` + `Stage` dimensions and the `5xx` metric.
REST APIs use `ApiName` + `Stage` and the `5XXError` metric.

### Connecting live mode to AWS

Live mode works with either an attached IAM role or credentials supplied in
`config.py`:

```python
# config.py (inside Settings, or as environment variables)
AWS_REGION = "us-east-1"
AWS_ACCESS_KEY_ID = "..."          # optional if using an IAM role
AWS_SECRET_ACCESS_KEY = "..."      # optional if using an IAM role
AWS_SESSION_TOKEN = ""             # optional for temporary credentials

MONITOR_DEFAULT_MODE = "demo"      # "demo" or "live"
MONITOR_AWS_ENABLED = True
MONITOR_LOOKBACK_MINUTES = 60
MONITOR_AWS_CACHE_SECONDS = 60

MONITOR_LAMBDA_FUNCTION_NAME = "my-agent-lambda"

# HTTP API Gateway
MONITOR_API_GATEWAY_TYPE = "http"
MONITOR_API_GATEWAY_API_ID = "abc123def4"
MONITOR_API_GATEWAY_STAGE = "$default"

MONITOR_BEDROCK_MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
```

Leave any resource value blank to skip that service. The identity running the
application needs CloudWatch read access:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["cloudwatch:GetMetricData"],
      "Resource": "*"
    }
  ]
}
```

The AWS service feed reads these CloudWatch metrics:

| AWS service | Metrics |
| --- | --- |
| AWS Lambda | `Invocations`, `Errors`, `Duration`, `Throttles` |
| Amazon API Gateway (HTTP) | `Count`, `5xx`, `Latency`, `IntegrationLatency` |
| Amazon API Gateway (REST) | `Count`, `5XXError`, `Latency`, `IntegrationLatency` |
| Amazon Bedrock | `Invocations`, `InputTokenCount`, `OutputTokenCount`, `InvocationLatency` |

If the UI is hosted separately from the AgentCore runtime, route
`GET /api/monitor/dashboard` through API Gateway to the runtime; the existing
proxy Lambda only handles agent chat invocations.

### AWS Bedrock token and call metrics

Live mode can report Bedrock LLM call counts and token usage from CloudWatch.
Set the exact model or inference-profile ID used by the application:

```python
MONITOR_BEDROCK_MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
```

The dashboard’s **AWS service feed** then queries the `AWS/Bedrock` namespace:

| CloudWatch metric | Dashboard meaning |
| --- | --- |
| `Invocations` | Bedrock LLM call count |
| `InputTokenCount` (Sum) | Total input tokens |
| `OutputTokenCount` (Sum) | Total output tokens |
| `InvocationLatency` (Average) | Model response latency |

Details and limitations:

- Leave `MONITOR_BEDROCK_MODEL_ID` blank to skip Bedrock CloudWatch metrics.
- Counts are scoped to the **AWS account, region, and model**, not this
  application or a single chat session. Usage from other workloads on the same
  model is included.
- For cross-Region inference, use the inference-profile identifier as the
  `ModelId` value.
- CloudWatch metrics can lag by a few minutes. The dashboard caches AWS metrics
  for `MONITOR_AWS_CACHE_SECONDS` (default `60`).
- The AWS identity needs `cloudwatch:GetMetricData`.
- The application records exact `usage` metadata returned by Bedrock for each
  LLM call. The dashboard shows the live Bedrock LLM call count and actual
  input/output tokens for this application, separate from the account-wide
  CloudWatch aggregate.
- If a response does not include usage metadata, the application falls back to
  an estimate based on request/response length; the card label and recent
  invocation rows indicate whether usage is exact or estimated.

### Monitoring another application with Dynatrace

The Dynatrace feed appears in two places: the **"Another application ·
Dynatrace"** pane at the bottom of the Dashboard, and its own **Tools →
Observability** view in the sidebar (full-page KPI display). Both fetch
golden-signal KPIs and extended metrics for a different application from the
Dynatrace API v2 (see `Dynatrace_Observability.md` for the KPI definitions).
Configure it in `.env`:

```bash
DYNATRACE_ENABLED=true
DYNATRACE_URL=https://abc12345.live.dynatrace.com
DYNATRACE_API_TOKEN=dt0c01.XXXX   # scopes: metrics.read, entities.read, services.read
DYNATRACE_APP_ID=APPLICATION-1234567890
DYNATRACE_APP_NAME=checkout-web
DYNATRACE_CACHE_SECONDS=60
```

Behavior:

- **Demo mode** shows synthetic Dynatrace metrics (no API calls).
- **Live mode** queries `GET /api/v2/metrics/query` once per cache window,
  scoped with `entitySelector=entityId("…")`, and reads availability,
  throughput, response time, and errors.
- The feed always renders: unconfigured, disabled, and API-failure states
  appear as a status pill with an explanatory message instead of breaking the
  dashboard. Failures are negative-cached for up to 30 seconds.
- `GET /api/monitor/dynatrace?mode=demo|live` returns the feed directly
  (`mode` defaults to `live`; both UI surfaces pass the shared Demo/Live
  setting). Useful for the monitoring agent or external checks.

The KPI metric IDs live in `dynatrace_connector.py` (`_KPI_METRICS`); adjust
them there if the monitored entity is a SERVICE rather than an APPLICATION.

---

## Troubleshooting

- **`No credentials` / `AccessDenied`** — the Runtime execution role is missing
  `bedrock:InvokeModel` or the local AWS profile is not configured.
- **RDS connection timeout** — the Runtime is not VPC-attached, or the security
  group does not allow port 5432 from it.
- **`password authentication failed`** — IAM database authentication must be
  enabled on the RDS instance and the DB user must be an IAM-mapped user.
- **RCA answers lack evidence** — check `RCA_TABLE_NAME`, `RCA_TOP_K`, and that
  embeddings use the same model used to populate the table.
