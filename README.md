# Agentic ServiceNOW Chatbot (AWS Bedrock AgentCore)

A ChatGPT-style chatbot for closed ServiceNOW incidents. The backend runs on
**Amazon Bedrock AgentCore Runtime**, uses **Amazon Bedrock** models, and
retrieves context from a **PostgreSQL + pgvector** incident store on **Amazon
RDS** (IAM database authentication, no passwords).

Three agents ship in v1:

| Agent | Key | What it does |
| --- | --- | --- |
| Chat agent | `chat` | General assistant (drafting, explanations) without incident retrieval. |
| QA agent | `snow` | Answers questions over closed ServiceNOW incidents. |
| RCA agent | `rca` | Analyzes the root cause of a **new** incident using similar closed incidents. |

Routing is implemented with **LangGraph**:

```
START -> router -> (snow | chat | rca | solution) -> END
```

`router` respects an explicit `agent` in the request; otherwise it uses cheap
keyword hints and, if needed, a small Bedrock LLM classifier.

---

## Project structure

```
034_temp/
├── runtime_entrypoint.py     # AgentCore entry point (BedrockAgentCoreApp)
├── main.py                   # Local launcher (same app)
├── config.py                 # All settings (reads .env)
├── orchestrator.py           # Agent registry + routing
├── graph.py                  # LangGraph pipeline
├── monitoring.py             # /health and /api/monitor/* endpoints
├── agents/
│   ├── base.py               # BaseAgent interface
│   ├── rag_agent.py          # Embedding -> hybrid retrieval -> rerank -> LLM
│   ├── reranker.py           # RRF fusion + Bedrock Rerank (not an agent)
│   ├── chat_agent.py         # General chat agent
│   ├── snow_agent.py         # QA agent
│   ├── rca_agent.py          # Root cause analysis agent
│   └── bedrock_agent.py      # Wrapper for Agents for Amazon Bedrock
├── static/                   # Chat UI (HTML/CSS/JS)
├── build_deploy_package.bat  # Builds dist\agentcore_deployment.zip
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
| `AWS_REGION` | Region for Bedrock, RDS. |
| `LLM_MODEL_ID` | Bedrock model used for answers (e.g. Claude). |
| `LLM_MAX_TOKENS` | Max output tokens for the QA agent. |
| `EMBEDDING_MODEL_ID` | Bedrock embedding model (Titan). |
| `RDS_TABLE_NAME` | pgvector table for the QA agent. |
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
| `RDS_INSTANCE_ARN` | RDS ARN used in IAM policy scoping. |
| `AGENT_ROUTER_MODEL_ID` | Model for intent classification (blank = `LLM_MODEL_ID`). |
| `SOLUTION_AGENT_ID`, `SOLUTION_AGENT_ALIAS_ID` | Optional Bedrock Agent for solution recommendations. |
| `APP_PORT`, `APP_HOST` | Local server binding (default 8080 / 0.0.0.0). |

---

## Using the agents

Explicit agent selection (recommended for automation):

```bash
# General chat
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"Draft a status update for an active P1","agent":"chat"}'

# QA
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"How do we fix VPN error 809?","agent":"snow"}'

# RCA
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"New P1: payment service returns 502 since 09:00","agent":"rca"}'
```

Auto-routing (omit `agent`):

```bash
curl -X POST http://localhost:8080/chat -H "Content-Type: application/json" \
  -d '{"message":"What is the root cause of the checkout outage?"}'
```

AgentCore invocation contract (`POST /invocations`):

```bash
curl -X POST http://localhost:8080/invocations -H "Content-Type: application/json" \
  -d '{"prompt":"Why did the database fail over?","agent":"rca"}'
```

Reuse a conversation by passing the same `session_id`.

---

## Chat UI

The UI in `static/` is a single-page app served by the runtime:

- **Agent switcher** — Chat, SNOW, and RCA, with per-agent suggestions.
  Every request sends the selected agent key explicitly.
- **Light / dark theme** toggle in the sidebar. The choice is persisted in
  `localStorage` and defaults to the OS preference.
- **Conversation history** stored in `localStorage` with per-conversation
  delete and "clear all". The backend `session_id` is preserved per
  conversation.
- **Markdown rendering** for assistant answers (headings, lists, bold,
  inline code, fenced code blocks) and a copy button per answer.
- **Typing indicator**, runtime status (`/ping`), responsive sidebar for
  mobile, `Enter` to send and `Shift+Enter` for a newline.

The browser only calls `POST /chat` (or API Gateway in AWS). No AWS
credentials are exposed to the browser.

---

## Retrieval: hybrid search and reranking

Both `snow` and `rca` use the same retrieval pipeline in
`agents/rag_agent.py`, implemented as retrieval utilities (not agents):

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
   - `bedrock:InvokeAgent` if using `SOLUTION_AGENT_*`
   - `rds-db:connect` on `arn:aws:rds-db:<region>:<acct>:dbuser:<resource-id>/<db-user>`
   - `s3:GetObject` for the deployment ZIP
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

All three agents live in **one Runtime**. The Lambda chooses the agent with the
`agent` field (`"chat"`, `"snow"`, or `"rca"`), or omits it to use auto-routing.
One Runtime means one deployment package and one set of environment variables;
the SNOW and RCA agents share the vector store but use different prompts,
`top_k`, and (max) tokens, while the chat agent uses no retrieval.

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

In the browser, replace the relative `/chat` call with your API Gateway URL:

```javascript
const API_BASE = "https://<api-id>.execute-api.<region>.amazonaws.com";
fetch(`${API_BASE}/chat`, { method: "POST", /* ... */ });
```

Enable CORS on API Gateway for your UI origin, and add `OPTIONS` handling (HTTP
APIs provide CORS configuration without a Lambda change).

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

1. The request carries an optional `agent` key (`{"message": "...", "agent": "rca"}`).
2. `graph.py` routes to that key when it is registered.
3. Otherwise the router uses a keyword hint, then an LLM classifier that is
   given each agent's **description** (the second argument to `register`).
4. If nothing matches, the default agent (`snow`) handles it.

### Backend checklist

1. **Settings** — add any new IDs/prompts to `config.py` and `.env`
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
       async def invoke(self, session_id: str, prompt: str) -> str: ...
   ```

   Pick the closest starting point:

   **a) RAG over the vector store** (like SNOW / RCA): subclass `RAGAgent`
   and supply a table, `top_k`, and system prompt. Embedding, hybrid search,
   RRF fusion, and reranking are inherited.

   ```python
   # agents/kb_agent.py
   from .rag_agent import RAGAgent
   from config import settings

   KB_PROMPT = "You are a knowledge-base assistant. Answer using the retrieved records."

   class KBAgent(RAGAgent):
       def __init__(self):
           super().__init__(
               table_name=settings.KB_AGENT_TABLE_NAME,
               top_k=settings.KB_AGENT_TOP_K,
               system_prompt=KB_PROMPT,
               max_tokens=settings.KB_AGENT_MAX_TOKENS,
           )
   ```

   **b) Plain LLM agent** (like Chat): subclass `BaseAgent` and call
   `bedrock-runtime` `converse` directly. See `agents/chat_agent.py`.

   **c) Bedrock Agent** — reuse `agents/bedrock_agent.py`:
   ```python
   self.register(
       "solution",
       BedrockAgent(settings.SOLUTION_AGENT_ID, settings.SOLUTION_AGENT_ALIAS_ID),
       "Recommends remediation steps.",
   )
   ```
   The `solution` agent is already wired this way and only registers when
   both env vars are set.

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
   - `default=True` is only for the fallback agent (currently `snow`); leave
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
| `GET /api/monitor/metrics` | Requests, tokens, latency, error rate, uptime. |
| `GET /api/monitor/invocations?limit=50` | Recent invocation log. |

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
