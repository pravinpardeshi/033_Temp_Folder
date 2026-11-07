# User Session and Chat History Storage

This guide explains how to store and retrieve per-user chat sessions and
history **after SSO is in place**. The companion code is standalone:

```text
session_memory_store.py
```

It does not import or modify `lambda_function.py`, `runtime_entrypoint.py`,
`orchestrator.py`, or the agent files.

---

## 1. Design summary

Two stores are used because they solve different problems:

| Store | Data | Why |
| --- | --- | --- |
| DynamoDB `ChatSessions` | Session metadata: title, timestamps, last message, agent, count | Fast list, rename, delete, ownership checks |
| AgentCore Memory | Actual user/assistant transcript | Conversation replay and agent context |

Relationship:

```text
SSO token
  │ actor_id = Entra object ID
  ▼
DynamoDB
  PK = USER#<actor_id>
  SK = SESSION#<session_id>
  │
  └── one session ──► AgentCore Memory
                       actorId   = <actor_id>
                       sessionId = alphanumeric(session_id)
                       events    = user + assistant turns
```

`session_id` is a server-generated UUID. AgentCore Memory requires an
alphanumeric session ID, so the stored UUID is sanitized only when talking to
Memory. The API/UI always uses the original UUID.

---

## 2. Actor identity

The SSO token is the only source of identity:

- Entra ID: `actor_id = claims["oid"]` (fallback `sub`)
- Cognito federated to AD/ADFS: `actor_id = claims["custom:entra_oid"]`

Rules:

- Never use email/UPN; AgentCore actor IDs reject `@` and `.`.
- Never accept `actor_id` or `user_id` from the request body.
- Use the same `actor_id` for DynamoDB keys and Memory `actorId`.
- Enforce ownership by reading DynamoDB under `USER#<actor_id>`.

---

## 3. Configuration

`SessionMemoryConfig` reads environment variables:

```env
AWS_REGION=us-east-1
MEMORY_ID=<agentcore-memory-id>
MEMORY_ENABLED=true
SESSIONS_TABLE=ChatSessions
MEMORY_CONTEXT_TURNS=10
SESSION_TITLE_MAX_CHARS=60
SESSION_TITLE_RENAME_MAX_CHARS=100
```

| Variable | Meaning |
| --- | --- |
| `MEMORY_ID` | AgentCore Memory resource ID. |
| `MEMORY_ENABLED` | `false` disables Memory reads/writes but keeps metadata. |
| `SESSIONS_TABLE` | DynamoDB table name. |
| `MEMORY_CONTEXT_TURNS` | Number of recent turns loaded for prompt context. |
| `SESSION_TITLE_MAX_CHARS` | First-question title length. |
| `SESSION_TITLE_RENAME_MAX_CHARS` | Maximum renamed title length. |

---

## 4. DynamoDB schema

```text
Table: ChatSessions
Billing: on-demand
Encryption: enabled
PITR: recommended

PK: USER#<actor_id>
SK: SESSION#<session_id>

Attributes:
  title          string
  created_at     string  ISO-8601 UTC
  updated_at     string  ISO-8601 UTC
  last_message   string
  agent          string
  message_count  number
```

Access patterns:

| Operation | DynamoDB call |
| --- | --- |
| List a user's sessions | `Query(PK = USER#<actor_id>)` |
| Get one session | `GetItem(PK, SK)` |
| Rename | `UpdateItem` title + updated_at |
| Save turn | `UpdateItem` updated_at, last_message, agent, `ADD message_count 1` |
| Delete | `DeleteItem` |

Cross-user access is impossible because `PK` is derived from the token.

### Create the table (one-time)

```bash
aws dynamodb create-table \
  --table-name ChatSessions \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions \
      AttributeName=PK,AttributeType=S \
      AttributeName=SK,AttributeType=S \
  --key-schema \
      AttributeName=PK,KeyType=HASH \
      AttributeName=SK,KeyType=RANGE
```

Optional TTL:

```bash
aws dynamodb update-time-to-live \
  --table-name ChatSessions \
  --time-to-live-specification "Enabled=true,AttributeName=expires_at"
```

If TTL is enabled, the application must write an `expires_at` epoch value.

---

## 5. AgentCore Memory event format

Each successful turn is a single event containing both messages:

```python
create_event(
    memoryId=MEMORY_ID,
    actorId=actor_id,
    sessionId=sanitize_session_id(session_id),
    eventTimestamp=datetime.now(timezone.utc),
    payload=[
        {"conversational": {"role": "USER", "content": {"text": user_text}}},
        {"conversational": {"role": "ASSISTANT", "content": {"text": answer}}},
    ],
    metadata={"agent": {"stringValue": agent_key}},
)
```

Notes:

- `sanitize_session_id` keeps only alphanumeric characters and caps at 100.
- Event expiry is configured on the Memory resource (`eventExpiryDuration`).
- Deleting an event does **not** automatically delete derived long-term memory
  records.
- Long-term memory strategies are optional; this code works with short-term
  memory only.

---

## 6. Code overview

`session_memory_store.py` exposes:

### `SessionMemoryConfig`

Configuration dataclass, populated from environment variables.

### `SessionRepository`

DynamoDB metadata operations.

```python
repository = SessionRepository(table_name="ChatSessions", region="us-east-1")

repository.create(actor_id, session_id, title, agent="")
repository.get(actor_id, session_id)
repository.list(actor_id)
repository.touch(actor_id, session_id, last_message, agent)
repository.rename(actor_id, session_id, title, max_chars=100)
repository.delete(actor_id, session_id)
```

### `AgentCoreMemoryStore`

AgentCore Memory transcript operations.

```python
memory = AgentCoreMemoryStore(memory_id, region, enabled=True)

memory.save_turn(actor_id, session_id, user_text, assistant_text, agent_key)
memory.list_messages(actor_id, session_id)
memory.recent_messages(actor_id, session_id, max_turns=10)
memory.delete_session(actor_id, session_id)
memory.list_session_ids(actor_id)
```

### `SessionMemoryService`

High-level operations intended for the API layer.

```python
service = SessionMemoryService(SessionMemoryConfig())

service.save_turn(actor_id, session_id, user_text, assistant_text, agent_key)
service.list_sessions(actor_id)
service.get_session(actor_id, session_id)
service.rename_session(actor_id, session_id, title)
service.delete_session(actor_id, session_id)
service.get_context(actor_id, session_id, max_turns=10)
service.get_context_text(actor_id, session_id, max_turns=10)
```

---

## 7. Recommended request flow

### 7.1 New chat / first question

```text
1. UI sends POST /chat without session_id.
2. Lambda derives actor_id from the JWT.
3. Lambda generates session_id = new_session_id().
4. Runtime loads no history (new session).
5. Agent produces answer.
6. Lambda/service saves the turn:
   - Memory event with user + assistant text
   - DynamoDB item with title = first question[:60]
7. Lambda returns { response, session_id, title }.
```

### 7.2 Existing chat

```text
1. UI sends POST /chat with session_id.
2. Lambda derives actor_id.
3. Lambda verifies the session exists under USER#<actor_id>.
4. Runtime loads recent Memory events (last N turns).
5. Agent produces answer.
6. Service appends Memory event and touches DynamoDB metadata.
```

### 7.3 Important ordering

Save the turn **after** the agent returns successfully. Do not create an event
for a failed request.

For a new session, create the DynamoDB item when saving the first successful
turn, not before the model runs.

---

## 8. Usage examples

### 8.1 Save a completed chat turn

```python
from session_memory_store import SessionMemoryConfig, SessionMemoryService

service = SessionMemoryService(SessionMemoryConfig())

service.save_turn(
    actor_id=claims["oid"],
    session_id=session_id,
    user_text=user_message,
    assistant_text=answer,
    agent_key="snow_agent",
)
```

`save_turn` does three things:

1. Writes the user/assistant event to AgentCore Memory.
2. Creates the DynamoDB session on the first turn.
3. Updates `updated_at`, `last_message`, `agent`, and `message_count`.

### 8.2 List my sessions

```python
sessions = service.list_sessions(claims["oid"])
```

Example response:

```json
[
  {
    "session_id": "3f95b6b4-...",
    "title": "How was VPN error 809 fixed?",
    "created_at": "2026-11-06T10:00:00Z",
    "updated_at": "2026-11-06T10:05:00Z",
    "last_message": "The fix was to reset the tunnel...",
    "agent": "snow_agent",
    "message_count": 4
  }
]
```

### 8.3 Open a session and load its transcript

```python
session = service.get_session(claims["oid"], session_id)
if session is None:
    raise NotFoundError("session not found")

for message in session["messages"]:
    print(message["role"], message["content"])
```

`get_session` returns metadata plus messages reconstructed from Memory events:

```json
{
  "session_id": "…",
  "title": "…",
  "created_at": "…",
  "updated_at": "…",
  "last_message": "…",
  "agent": "snow_agent",
  "message_count": 4,
  "messages": [
    {"role": "user", "content": "…", "timestamp": "…"},
    {"role": "assistant", "content": "…", "timestamp": "…"}
  ]
}
```

### 8.4 Build prompt context for the runtime

```python
history = service.get_context_text(
    claims["oid"],
    session_id,
    max_turns=10,
)

prompt = (
    f"Conversation so far:\n{history}\n\n"
    f"User: {new_message}"
)
```

Runtime policy:

- Load last 10 turns by default.
- Truncate very long events before sending to the model.
- If Memory read fails, continue without history and log a warning.
- If Memory write fails, still return the answer and log a warning.

### 8.5 Rename

```python
updated = service.rename_session(
    claims["oid"],
    session_id,
    title="VPN troubleshooting notes",
)
```

Validation:

- Required.
- Whitespace is collapsed.
- Truncated to `SESSION_TITLE_RENAME_MAX_CHARS` (default 100).

### 8.6 Delete

```python
deleted_events = service.delete_session(claims["oid"], session_id)
```

Behavior:

1. Enumerate `list_events` for the actor/session.
2. Call `delete_event` for each event.
3. Delete the DynamoDB metadata.
4. Return the number of deleted Memory events.

The operation is idempotent: deleting an already-absent session returns `0` and
does not fail.

To remove derived long-term memory as well, call `DeleteMemoryRecord` for the
actor's namespaces separately.

---

## 9. Lambda route mapping

One Lambda can expose:

| Route | Service call |
| --- | --- |
| `POST /chat` | `save_turn(...)` after the agent answers |
| `GET /sessions` | `list_sessions(actor_id)` |
| `GET /sessions/{id}` | `get_session(actor_id, id)` |
| `PATCH /sessions/{id}` | `rename_session(actor_id, id, body["title"])` |
| `DELETE /sessions/{id}` | `delete_session(actor_id, id)` |

The Lambda must derive `actor_id` from the API Gateway JWT claims and never
from the request body.

For `POST /chat`:

```python
session_id = body.get("session_id") or new_session_id()

if body.get("session_id"):
    existing = service.get_session(actor_id, session_id)
    if existing is None:
        return 404
    context = service.get_context_text(actor_id, session_id)
else:
    context = ""

answer = await invoke_agent(
    prompt=body["message"],
    agent=body.get("agent"),
    context=context,
    session_id=session_id,
    actor_id=actor_id,
)

session = service.save_turn(
    actor_id=actor_id,
    session_id=session_id,
    user_text=body["message"],
    assistant_text=answer,
    agent_key=body.get("agent") or "snow_agent",
)

return {
    "response": answer,
    "session_id": session["session_id"],
    "title": session["title"],
}
```

---

## 10. First question becomes the title

`title_from_message`:

```python
text = " ".join(message.split())
title = text[:60].rstrip() or "New chat"
```

Examples:

| First question | Title |
| --- | --- |
| `How was VPN error 809 fixed?` | `How was VPN error 809 fixed?` |
| Long question over 60 characters | First 60 characters |
| Empty/whitespace | `New chat` |

The title is stored in DynamoDB only. It is not written to a Memory namespace.

---

## 11. Security and ownership

- `actor_id` comes only from the validated JWT (`oid` / `custom:entra_oid`).
- All DynamoDB operations use `USER#<actor_id>` as the partition key.
- A session ID supplied by the client is looked up under the caller's actor;
  another user's session appears as `404`.
- Do not log tokens, event payloads, or full transcripts.
- Scope the Lambda role to:
  - `bedrock-agentcore:ListEvents`
  - `bedrock-agentcore:DeleteEvent`
  - `bedrock-agentcore:ListSessions`
  - `bedrock-agentcore:DeleteMemoryRecord` (only if long-term memory is used)
  - DynamoDB CRUD on `ChatSessions`
- Scope the runtime role to:
  - `bedrock-agentcore:CreateEvent`
  - `bedrock-agentcore:ListEvents`
  - `bedrock-agentcore:RetrieveMemoryRecords` (if long-term memory is used)
- Use TLS everywhere. Store no credentials in Memory.

---

## 12. Retention

| Data | Mechanism |
| --- | --- |
| Raw chat events | Memory resource `eventExpiryDuration` (3–365 days) |
| Long-term insights | Per-strategy record expiry (minimum 31 days) |
| Session metadata | DynamoDB TTL (`expires_at`) or explicit delete |
| User-requested delete | `delete_session` removes events + metadata |

Deleting a Memory event does not delete long-term records derived from it.

---

## 13. Error handling

| Failure | Recommended behavior |
| --- | --- |
| Memory read fails | Continue with empty history; log warning |
| Memory write fails | Return answer; log warning; optionally retry asynchronously |
| DynamoDB write fails | Return answer but no session metadata; surface a warning |
| Session not found | `404` for that actor |
| Rename validation fails | `400` |
| Delete when absent | `204` / idempotent success |
| Token expired | API Gateway `401`; UI redirects to SSO |

Add monitoring counters:

- `memory_read_failed`
- `memory_write_failed`
- `session_metadata_write_failed`

---

## 14. Testing checklist

- [ ] First turn creates a DynamoDB item with the first question as title.
- [ ] `message_count` starts at 1 and increments on each turn.
- [ ] `GET /sessions` returns newest first.
- [ ] `GET /sessions/{id}` replays user and assistant messages in order.
- [ ] Rename changes only DynamoDB metadata.
- [ ] Delete removes Memory events and the metadata item.
- [ ] User A cannot read/rename/delete User B's session.
- [ ] Expired token returns `401` at API Gateway.
- [ ] `MEMORY_ENABLED=false` still allows metadata operations.
- [ ] Session IDs with hyphens work (Memory receives the sanitized form).
- [ ] Long sessions paginate correctly through `list_events`.

---

## 15. Minimal end-to-end example

```python
from session_memory_store import (
    SessionMemoryConfig,
    SessionMemoryService,
    new_session_id,
)

service = SessionMemoryService(SessionMemoryConfig())

actor_id = claims["oid"]              # from the validated SSO token
session_id = new_session_id()

user_text = "How was VPN error 809 fixed?"
answer = "Past tickets resolved it by resetting the tunnel..."

session = service.save_turn(
    actor_id=actor_id,
    session_id=session_id,
    user_text=user_text,
    assistant_text=answer,
    agent_key="snow_agent",
)

print(session["title"])
# How was VPN error 809 fixed?

print(service.list_sessions(actor_id))

full = service.get_session(actor_id, session_id)
print(full["messages"])

service.rename_session(actor_id, session_id, "VPN notes")
service.delete_session(actor_id, session_id)
```

---

## 16. Notes

- This module is standalone; it is safe to import from the Lambda or runtime
  without changing existing files.
- Keep session metadata and transcripts separate.
- The API should expose the original UUID, while Memory receives the sanitized
  alphanumeric ID.
- Do not derive identity from request bodies once SSO is enabled.
