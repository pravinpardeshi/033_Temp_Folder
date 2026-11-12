# SSO Chat Session Workflow

This document describes the SSO chat-session flow implemented in `chat_session_flow.py` (standalone, in `034_temp/`). It covers session discovery, historic chat loading, continuation, and new-chat creation using **DynamoDB** for session metadata and **Amazon Bedrock AgentCore Memory** for conversation transcripts.

> **Assumption:** SSO is already implemented upstream. The backend receives a validated identity and derives a stable `actor_id` (Entra object ID / `oid`) from the token claims. All session operations are scoped to this `actor_id`.

## 1. Workflow

| Step | Trigger | Action | Source of truth |
|---|---|---|---|
| 1 | SSO login | Upstream SSO validates the token and derives `actor_id` (`oid`). Requests carry it as `X-Actor-Id` (or `user_id` for local dev). | IdP / token claims |
| 2 | User clicks **Chat** | `GET /api/chat/sessions` → `ChatSessionWorkflow.list_sessions(actor_id)` loads the user's session index. | DynamoDB `ChatSessions` (`PK=USER#<actor_id>`, `SK=SESSION#<session_id>`) |
| 3 | Browser renders history | UI shows title, `updated_at`, `last_message`, `agent` in the left nav. No transcript fetched yet. | Response from step 2 |
| 4 | User selects a session | `GET /api/chat/sessions/{session_id}` → `get_session` returns metadata from DynamoDB and the transcript from AgentCore Memory so the user can continue the historic chat. Falls back to DynamoDB-stored messages if Memory is empty/unavailable. | AgentCore Memory (primary) + DynamoDB metadata |
| 5 | User sends a message | `POST /api/chat/messages` `{session_id, message, agent}` → `continue_session` runs the agent (`answer_fn`), appends the USER+ASSISTANT turn to AgentCore Memory, and touches DynamoDB metadata (`updated_at`, `last_message`, `message_count`, title on first turn). Same `session_id` ⇒ history accumulates. | AgentCore Memory updated; DynamoDB metadata synced |
| 6 | User clicks **New Chat** | `POST /api/chat/sessions` `{agent?, title?}` → `create_session` creates a `SESSION#` row immediately and returns the new `session_id`. UI selects it; the first message (step 5) continues it. | DynamoDB new row |

## 2. Architecture

```
Browser (Chat UI)
   │  X-Actor-Id: <entra_oid>   (injected by SSO layer)
   │
   ├─ GET  /api/chat/sessions  ───────────────┐
   ├─ POST /api/chat/sessions  (New Chat) ────┤  DynamoDB
   ├─ GET  /api/chat/sessions/{id} ── meta ───┤  ChatSessions
   │                                  │       ┘  PK=USER#<actor_id>
   │                                  │          SK=SESSION#<id>
   │                                  └─ transcript ──┐
   └─ POST /api/chat/messages ── save turn ────────────┤ AgentCore Memory
        (agent runs, turn persisted)                   ┘ (memoryId, actorId, sessionId)
```

**Components:**
- `chat_session_flow.py` — `ChatSessionWorkflow`, HTTP routes (`register_routes`), demo server.
- `session_memory_store.py` — `SessionRepository` (DynamoDB metadata + message fallback), `AgentCoreMemoryStore` (AgentCore Memory data plane).
- `sso_user_store.py` — optional SSO user profile store (`SsoUsers`).
- `create_session_table.py` / `create_local_tables.py` — table creation (AWS or local).

## 3. DynamoDB Schema

**Table:** `ChatSessions` (default, configurable via `SESSIONS_TABLE`)

| Key / Attribute | Type | Description |
|---|---|---|
| `PK` | String (HASH) | `USER#<actor_id>` |
| `SK` | String (RANGE) | `SESSION#<session_id>` (metadata) or `MESSAGE#<session_id>#<seq>#<rand>` (transcript fallback) |

**Session metadata item** (`SK = SESSION#<session_id>`): `title`, `created_at`, `updated_at`, `last_message`, `agent`, `message_count`, `ttl`.

**Transcript fallback item** (`SK = MESSAGE#...`): `session_id`, `role` (`user`/`assistant`), `content`, `agent`, `created_at`, `ttl`.

- Billing: `PAY_PER_REQUEST`
- TTL: enabled on `ttl` (controlled by `SESSION_TTL_DAYS`)
- Access pattern (step 2): query `PK = USER#<actor_id>` for all session rows, sorted by `updated_at` desc.

> The transcript is **primarily** read/written via AgentCore Memory (step 4/5). DynamoDB message items serve as a durable fallback when Memory is unavailable.

## 4. API Reference

Base path examples below assume the demo app; integrate into your app via `register_routes(routes, workflow)`.

Actor resolution: `X-Actor-Id` or `X-User-Id` header → `user_id`/`actor_id` in body/query → `default_user`.

### 4.1 List sessions (step 2)
```http
GET /api/chat/sessions
X-Actor-Id: <entra_oid>
```
**200**
```json
{
  "sessions": [
    {
      "session_id": "c7f8...",
      "title": "VPN error 809",
      "created_at": "2026-05-12T10:00:00Z",
      "updated_at": "2026-05-12T10:05:00Z",
      "last_message": "answer to: VPN error 809",
      "agent": "snow",
      "message_count": 1
    }
  ]
}
```

### 4.2 Create session / New Chat (step 6)
```http
POST /api/chat/sessions
X-Actor-Id: <entra_oid>
Content-Type: application/json

{ "agent": "snow", "title": "New chat" }
```
**201**
```json
{
  "session_id": "a1b2...",
  "title": "New chat",
  "created_at": "2026-05-12T10:10:00Z",
  "updated_at": "2026-05-12T10:10:00Z",
  "last_message": "",
  "agent": "snow",
  "message_count": 0
}
```

### 4.3 Get session + transcript (step 4)
```http
GET /api/chat/sessions/{session_id}
X-Actor-Id: <entra_oid>
```
**200** (transcript from AgentCore Memory; falls back to DynamoDB messages)
```json
{
  "session_id": "c7f8...",
  "title": "VPN error 809",
  "messages": [
    { "role": "user", "content": "VPN error 809", "timestamp": "2026-05-12T10:00:01Z" },
    { "role": "assistant", "content": "answer to: VPN error 809", "timestamp": "2026-05-12T10:00:02Z" }
  ]
}
```
**404** if the session does not exist for this actor.

### 4.4 Send message / continue session (step 5)
```http
POST /api/chat/messages
X-Actor-Id: <entra_oid>
Content-Type: application/json

{ "session_id": "c7f8...", "message": "any other fix?", "agent": "snow" }
```
**200**
```json
{ "session_id": "c7f8...", "response": "..." }
```
**400** if `session_id` or `message` is missing. The turn is saved to AgentCore Memory and DynamoDB metadata is updated (title set from the first user message if still "New chat").

## 5. Code Usage

```python
from chat_session_flow import ChatSessionWorkflow

workflow = ChatSessionWorkflow()  # reads env config

# Step 2: load history on Chat click
sessions = workflow.list_sessions(actor_id)

# Step 6: New Chat -> DynamoDB entry
new_session = workflow.create_session(actor_id, agent="snow", title="New chat")

# Step 4: open historic chat -> transcript from AgentCore
session = workflow.get_session(actor_id, session_id)
messages = session["messages"]

# Step 5: continue chat -> AgentCore updated (async)
result = await workflow.continue_session(
    actor_id=actor_id,
    session_id=session_id,
    user_message="VPN error 809",
    agent="snow",
    answer_fn=lambda sid, text, agent: orchestrator.route_query(sid, text, agent_key=agent),
)
```

**Integration with the existing app:** replace the demo `answer_fn` in `register_routes` / `continue_session` with your agent call, e.g. `orchestrator.route_query(sid, text, agent_key=agent)` or `_run_agent(...)` from `runtime_entrypoint.py`. Wire the UI to call step 2 on Chat-open, step 4 on session click, step 5 on send, and step 6 on New Chat.

## 6. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AWS_REGION` | `us-east-1` | AWS region for DynamoDB and AgentCore Memory |
| `SESSIONS_TABLE` | `ChatSessions` | DynamoDB table name |
| `MEMORY_ID` | *(empty)* | AgentCore Memory resource id (required for real transcripts) |
| `MEMORY_ENABLED` | `true` | Enable AgentCore Memory reads/writes |
| `DYNAMODB_ENDPOINT_URL` | *(empty)* | Set to `http://localhost:8000` for DynamoDB Local |
| `SESSION_TTL_DAYS` | `30` | TTL window written to DynamoDB (`0` disables) |
| `SESSION_TITLE_MAX_CHARS` | `60` | Title length derived from first message |

**Local dev:** DynamoDB Local ignores credentials but boto3 requires them — set `AWS_ACCESS_KEY_ID=local` and `AWS_SECRET_ACCESS_KEY=local`.

## 7. Setup & Running

### Create tables (local DynamoDB)
```powershell
# start DynamoDB Local (port 8000), then:
$env:AWS_REGION="us-east-1"
$env:AWS_ACCESS_KEY_ID="local"
$env:AWS_SECRET_ACCESS_KEY="local"
$env:DYNAMODB_ENDPOINT_URL="http://localhost:8000"
$env:SESSIONS_TABLE="ChatSessions"

python create_session_table.py --local
# or create ChatSessions + SsoUsers:
python create_local_tables.py
```

### Run the demo API
```powershell
python chat_session_flow.py
# listens on http://localhost:8090
#   GET  /api/chat/sessions
#   POST /api/chat/sessions
#   GET  /api/chat/sessions/{session_id}
#   POST /api/chat/messages
```

> The demo uses a stub `answer_fn` (`"(demo) received: ..."`) and requires `MEMORY_ID` + AWS credentials for real AgentCore Memory; without them, transcripts fall back to DynamoDB-stored messages.

## 8. Verification (tested)

`chat_session_flow.py` was syntax-checked and exercised with in-memory fakes implementing the `SessionRepository` and `AgentCoreMemoryStore` interfaces (no DynamoDB Local/`moto`/AgentCore services available in the test environment). This validates the workflow logic and HTTP contract.

### 8.1 Workflow logic tests — **ALL PASSED**
| Check | Result |
|---|---|
| Step 2: `list_sessions` empty initially | PASS |
| Step 6: `create_session` returns `session_id`, `title="New chat"` | PASS |
| Step 2: new session appears in list | PASS |
| Step 4: `get_session` returns metadata, empty transcript | PASS |
| Step 5: `continue_session` returns response, preserves `session_id` | PASS |
| Step 5: turn saved to AgentCore Memory (2 messages, user + assistant) | PASS |
| Step 5: DynamoDB metadata touched (`message_count==1`, title from first message) | PASS |
| Step 4: transcript loaded from AgentCore Memory after interaction | PASS |
| Step 5: history accumulates in same session (4 messages, `message_count==2`) | PASS |
| Step 6: second New Chat independent, listed separately | PASS |
| New chat transcript empty; original transcript intact (4 messages) | PASS |
| Actor isolation (other user sees no sessions) | PASS |
| Missing session returns `None` | PASS |

### 8.2 HTTP route tests (Starlette `TestClient`) — **ALL PASSED**
| Request | Expected | Result |
|---|---|---|
| `GET /api/chat/sessions` | 200, `sessions=[]` | PASS |
| `POST /api/chat/sessions` `{"agent":"snow"}` | 201, `session_id` returned | PASS |
| `GET /api/chat/sessions` | 200, 1 session | PASS |
| `GET /api/chat/sessions/{id}` | 200, `messages=[]` | PASS |
| `POST /api/chat/messages` `{session_id,message,agent}` | 200, response present | PASS |
| `GET /api/chat/sessions/{id}` (after send) | 200, 2 messages (`user` → `assistant`) | PASS |
| 2nd `POST /api/chat/messages` | transcript accumulates to 4 | PASS |
| `GET /api/chat/sessions/nope` | 404 | PASS |
| `POST /api/chat/messages` (missing `session_id`) | 400 | PASS |
| 2nd `POST /api/chat/sessions` | independent session; old transcript intact | PASS |

### 8.3 End-to-end (real backends)
To validate against real services: run DynamoDB Local (or AWS) with tables created, set `MEMORY_ID`/credentials, replace the demo `answer_fn` with `orchestrator.route_query`, and exercise the same HTTP sequence — step 4/5 will then read/write AgentCore Memory events.

## 9. Notes & Constraints

- `actor_id` is validated (`session_memory_store.validate_actor_id`): required, no `@`/`.` characters (use Entra object ID, not UPN/email), max 255 chars.
- **New Chat** creates the DynamoDB row immediately (step 6) so the session is addressable before the first message; `continue_session` will also create the row defensively if a client skips step 6.
- AgentCore Memory is the authoritative transcript for step 4/5; DynamoDB message items are a fallback for local/dev resilience.
- The demo `answer_fn` is a stub — wire in `orchestrator.route_query(sid, text, agent_key=agent)` for real agent responses.
- In production, trust `actor_id` only from the SSO/validated token (`X-Actor-Id` injected by the trusted layer), not from client-supplied body fields.
