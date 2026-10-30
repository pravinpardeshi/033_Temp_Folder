# AgentCore Memory for Historic Chats

This document explains how to add **per-user conversation history** to the
ServiceNOW copilot using **Amazon Bedrock AgentCore Memory**, and how that
design evolves when Active Directory SSO is introduced.

> Scope: this is a design + implementation guide. The current code does **not**
> use AgentCore Memory yet. Today the browser keeps conversation history in
> `localStorage` (`static/script.js`) and only the `session_id` travels to the
> backend. AgentCore Memory moves that history server-side, keyed by **actor**
> (user) and **session** (conversation).

---

## 1. Why AgentCore Memory

| Need | Today | With AgentCore Memory |
| --- | --- | --- |
| Multi-turn context | Client resends nothing; backend is stateless | Server-side short-term events per session |
| History across sessions | Lost when browser storage is cleared | Long-term records keyed by user |
| Cross-device continuity | Not possible | Any browser, same user |
| Personalization | None | Extracted preferences/facts |
| Audit / retention | None | Configurable event and record TTL |

AgentCore Memory has two layers:

- **Short-term memory** — immutable conversation events, grouped by
  `actorId` + `sessionId`. This is the raw transcript used to rebuild context
  for the next turn.
- **Long-term memory** — insights (facts, preferences, summaries, episodes)
  extracted **asynchronously** from short-term events by *memory strategies*
  and stored under a *namespace*.

---

## 2. Concepts

| Term | Meaning in this project |
| --- | --- |
| Memory resource | One top-level store (`memoryId`) for the copilot. |
| Actor (`actorId`) | The end user. Until SSO exists, use a stable anonymous ID; after AD SSO, use the Entra/AD object ID. |
| Session (`sessionId`) | One chat conversation. Reuse the value already generated per conversation in the UI. |
| Event | One interaction (user message, assistant message, tool call). |
| Strategy | Built-in extractor (`SEMANTIC`, `SUMMARIZATION`, `USER_PREFERENCE`, `EPISODIC`) or a custom/self-managed one. |
| Namespace | Hierarchical path for long-term records, e.g. `/users/{actorId}/preferences/`. |
| Memory record | A long-term insight retrieved with semantic search. |

AgentCore Memory is a **separate service** from the model and from RDS. It does
not replace the pgvector incident store: RDS holds closed-incident knowledge,
Memory holds user/chat history.

---

## 3. Prerequisites

- `boto3` (already in `requirements.txt`) and optionally the AgentCore SDK
  (`bedrock-agentcore`, also already present).
- Region = `AWS_REGION` (Memory is regional).
- The runtime execution role needs the data-plane permissions in section 4.
- A `memoryId` created by the control plane (section 5).

---

## 4. IAM permissions

Add to the runtime role (scope the resource to the memory ARN where possible):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:GetMemoryRecord",
        "bedrock-agentcore:DeleteMemoryRecord",
        "bedrock-agentcore:BatchCreateMemoryRecords"
      ],
      "Resource": "arn:aws:bedrock-agentcore:<region>:<account>:memory/<memory-id>"
    }
  ]
}
```

Setup-only (CI/admin, not the runtime role): `bedrock-agentcore-control:CreateMemory`,
`GetMemory`, `UpdateMemory`.

If you use a customer-managed KMS key, the role also needs
`kms:Decrypt`/`kms:GenerateDataKey` on that key.

---

## 5. Create the memory resource

### Option A — AgentCore CLI

The CLI commands must run inside an AgentCore CLI project (`agentcore create`).
If this repo is not scaffolded that way, use the boto3 or console path instead.

```bash
agentcore add memory --name SnowCopilotMemory \
  --strategies SEMANTIC,USER_PREFERENCE,SUMMARIZATION
agentcore deploy
agentcore status --json          # copy the memory ARN/ID
agentcore memory get --id <memory-id>
```

### Option B — boto3 (control plane)

```python
import time
import boto3

control = boto3.client("bedrock-agentcore-control", region_name="us-east-1")

resp = control.create_memory(
    name="SnowCopilotMemory",
    description="Per-user chat history for the ServiceNOW copilot",
    eventExpiryDuration=30,            # days of raw events (3-365)
    memoryStrategies=[
        {
            "semanticMemoryStrategy": {
                "name": "SemanticFacts",
                "namespaceTemplates": ["/facts/{actorId}/"],
            }
        },
        {
            "userPreferenceMemoryStrategy": {
                "name": "UserPreferences",
                "namespaceTemplates": ["/users/{actorId}/preferences/"],
            }
        },
        {
            "summaryMemoryStrategy": {
                "name": "SessionSummaries",
                "namespaceTemplates": ["/summaries/{actorId}/{sessionId}/"],
            }
        },
    ],
)

memory_id = resp["memory"]["id"]
print(memory_id)

while control.get_memory(memoryId=memory_id)["memory"]["status"] != "ACTIVE":
    time.sleep(10)
```

Notes:

- Creating a memory takes a few minutes and ends in `ACTIVE`.
- Strategy names must match `^[a-zA-Z]{0,47}$` (letters only, no digits/hyphens).
- A strategy takes exactly **one** namespace template. Use separate strategies
  for separate namespaces.
- If you omit `memoryStrategies`, only short-term memory is available.
- Long-term record TTL is optional per strategy
  (`recordExpirySpecification.recordExpiryDurationDays`, minimum 31 days).
  Without it, long-term records do not expire.
- Add a customer-managed KMS key (`encryptionKeyArn`) if chat data is
  regulated; otherwise the service-managed key is used.

---

## 6. Namespace design

Recommended split for this project:

| Strategy | Namespace template | Contains |
| --- | --- | --- |
| `SEMANTIC` | `/facts/{actorId}/` | Durable facts the user stated (systems they own, recurring issues). |
| `USER_PREFERENCE` | `/users/{actorId}/preferences/` | Formatting/tone/agent preferences. |
| `SUMMARIZATION` | `/summaries/{actorId}/{sessionId}/` | One summary per past conversation. |

Why these separations:

- Retrieval is always **inside one namespace**, so preferences never pollute
  factual recall.
- `/summaries/{actorId}/{sessionId}/` lets you pull a single conversation's
  summary, while `/summaries/{actorId}/` (namespace *path*) searches across all
  of that user's past conversations.
- Always keep the leading and trailing slash; it prevents prefix collisions
  (`/users/alice/` vs `/users/alice2/`).
- Never put a raw email or UPN into a namespace. Actor IDs must match
  `^[a-zA-Z0-9]+([:-][a-zA-Z0-9_/-]+)*[a-zA-Z0-9_/-]*$`, which excludes `@` and
  dots. After SSO, use the Entra **object ID** (GUID), not the UPN.

---

## 7. Write conversation events (short-term memory)

Every turn should be stored as an event. A single event can carry both the user
message and the assistant reply.

```python
from datetime import datetime, timezone
import boto3

memory = boto3.client("bedrock-agentcore", region_name="us-east-1")

def save_turn(memory_id, actor_id, session_id, user_text, assistant_text):
    memory.create_event(
        memoryId=memory_id,
        actorId=actor_id,
        sessionId=session_id,
        eventTimestamp=datetime.now(timezone.utc),
        payload=[
            {"conversational": {"role": "USER", "content": {"text": user_text}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": assistant_text}}},
        ],
        metadata={"agent": {"stringValue": "snow"}},
    )
```

Rules and gotchas:

- `sessionId` is alphanumeric (max 100). UUIDs contain `-`, so normalise:

  ```python
  def memory_session_id(session_id: str) -> str:
      cleaned = "".join(c for c in session_id if c.isalnum())
      return cleaned[:100] or "session"
  ```

- `actorId` max 255 chars, allows `: - _ /` but **not** `@` or `.`.
- `eventTimestamp` is required by the API in practice; pass a timezone-aware
  `datetime` from boto3, or an ISO-8601 string with the CLI.
- Set `extractionMode="SKIP"` on internal/system-only events if you do not want
  them fed to long-term extraction.
- `clientToken` makes retries idempotent — use the event's message ID.
- Use `metadata` for `agent`, `ticket_id`, `priority`, etc. Note: without a
  `memoryRecordSchema` on the strategy, event metadata is not copied onto
  long-term records.
- Long-term extraction only processes events created **after** the strategy
  became `ACTIVE`.

---

## 8. Read short-term history (immediate context)

Short-term reads are synchronous, so they are the right choice for
"what did we just say?" context on the next turn.

```python
def get_recent_turns(memory_id, actor_id, session_id, max_results=10):
    resp = memory.list_events(
        memoryId=memory_id,
        actorId=actor_id,
        sessionId=session_id,
        includePayloads=True,
        maxResults=max_results,
    )
    turns = []
    for event in resp.get("events", []):
        for item in event.get("payload", []):
            conv = item.get("conversational")
            if conv:
                turns.append((conv["role"], conv["content"]["text"]))
    return turns
```

`ListEvents` is paginated; `maxResults` is 1-100 (default 20). Paginate with
`nextToken` for longer sessions, then trim to the most recent N turns before
putting them in the prompt to control token usage.

Prompt formatting sketch:

```python
def format_history(turns, max_chars=4000):
    lines = [f"{role}: {text}" for role, text in turns]
    text = "\n".join(lines)
    return text[-max_chars:]
```

Pass this as prior context to `RAGAgent`/`ChatAgent`. Keep it bounded — full
transcripts will blow up input tokens and latency.

---

## 9. Retrieve long-term insights

Long-term extraction is **asynchronous** (often a minute or more). Design the
request path to tolerate that:

1. Always load short-term history first (section 8).
2. Add long-term recall when available; on empty results, continue normally.

```python
def recall_long_term(memory_id, actor_id, session_id, query, top_k=5):
    results = {}
    for namespace in (
        f"/facts/{actor_id}/",
        f"/users/{actor_id}/preferences/",
        f"/summaries/{actor_id}/{session_id}/",
    ):
        try:
            resp = memory.retrieve_memory_records(
                memoryId=memory_id,
                namespace=namespace,
                searchCriteria={"searchQuery": query, "topK": top_k},
            )
            results[namespace] = [
                r["content"]["text"]
                for r in resp.get("memoryRecordSummaries", [])
                if "text" in r.get("content", {})
            ]
        except Exception:
            results[namespace] = []
    return results
```

Use `namespace` for one exact namespace. To search a hierarchy (for example all
of a user's summaries), use `namespacePath=f"/summaries/{actor_id}/"` instead.

`ListMemoryRecords` enumerates everything in a namespace (auditing, "show my
memory" features); `GetMemoryRecord` / `DeleteMemoryRecord` operate on a single
record ID and are what a "forget this" or privacy workflow needs.

---

## 10. Integrating with this codebase

### 10.1 Settings (`config.py`)

```python
# AgentCore Memory
MEMORY_ENABLED: bool = False
MEMORY_ID: str = ""
MEMORY_HISTORY_TURNS: int = 10
MEMORY_TOP_K: int = 5
MEMORY_EXTRACTION_ENABLED: bool = True
```

Add the same keys to `.env.example` and the Runtime environment. Keep
`MEMORY_ENABLED=false` until the memory resource exists.

### 10.2 New module `memory_store.py`

Wrap the boto3 calls from sections 7-9 in a small class, for example:

```python
class AgentCoreMemoryStore:
    def __init__(self, memory_id, region): ...
    def save_turn(self, actor_id, session_id, user_text, assistant_text, metadata): ...
    def recent_history(self, actor_id, session_id, max_turns): ...
    def recall(self, actor_id, session_id, query, top_k): ...
    @staticmethod
    def sanitize_session_id(session_id): ...
```

Return `[]` / no-op when `MEMORY_ENABLED` is false so local development does
not need AWS Memory.

### 10.3 Resolve the actor

The router and graph currently key only on `session_id`. Add an `actor_id`
alongside it:

- `graph.AgentState` — add `actor_id: str`.
- `run_graph(...)` — accept and pass `actor_id`.
- `AgentOrchestrator.route_query(...)` — accept `actor_id`, rebuild the prompt
  with history/recall, then invoke the agent.

Keep the agent classes unchanged: they still receive a single prompt string.
Memory is composed **outside** the agent:

```text
retrieve history + long-term recall
        |
        v
system/user prompt  ->  graph router  ->  agent  ->  answer
        |
        v
save user turn + answer as one memory event
```

### 10.4 `runtime_entrypoint.py`

Both `/chat` and `/invocations` already resolve `session_id`. Extend them:

```python
actor_id = resolve_actor(payload, request)   # see section 11
answer = await _run_agent(
    str(prompt), session_id, agent_key=agent_key, actor_id=actor_id
)
```

- Persist the event **after** a successful answer, so failed invocations do not
  create half-turns.
- Do not block the HTTP response on long-term extraction; `CreateEvent` is
  synchronous only for short-term storage. Extraction happens in the background.
- Monitoring can still use the existing `record_invocation`; add a
  `memory_write_failed` counter rather than failing the request.

### 10.5 Chat UI (`static/script.js`)

The UI already creates and persists one `session_id` per conversation. Continue
using it. When SSO lands, prefer the backend-derived actor and stop sending any
user identifier from the browser.

---

## 11. Identity: today vs. Active Directory SSO

### Today (no SSO)

There is no authenticated user, so an `actorId` must be *stable per browser*:

- Generate a random anonymous actor ID once and store it in `localStorage`
  (e.g. `agentcore.actor`), then send it as `user_id` in the request body.
- Treat it as **untrusted**. It only separates casual users; it is not a
  security boundary. Do not rely on it for access control or to expose
  sensitive history.
- If you need stronger separation before SSO, keep memory disabled or scope it
  to a short-lived session-only actor.

### Long-term plan (AD SSO)

Target flow:

```text
Browser (Entra ID / AD SSO login)
   |  Authorization: Bearer <JWT>
   v
API Gateway (JWT authorizer, validates issuer/audience)
   v
Lambda proxy (forwards token or trusted claims)
   v
AgentCore Runtime
   |  validate token (AgentCore Identity inbound JWT or in-app JWKS validation)
   |  actorId = token.sub / Entra object ID
   v
AgentCore Memory (actor-scoped namespaces)
```

Implementation choices:

1. **AgentCore Identity inbound JWT** supports Cognito, Okta, Auth0, Azure AD
   (Entra ID), and custom OIDC. Configure the runtime to validate inbound
   tokens from your Entra tenant; the agent then receives authenticated
   identity instead of a client-supplied `user_id`.
2. **API Gateway JWT authorizer** can validate the Entra token first and
   reject unauthenticated calls before they reach Lambda (defense in depth).
3. **Actor mapping**:
   - `actorId = Entra object ID` (GUID). Valid characters, stable across
     renames, and not PII in the namespace.
   - Do **not** use the UPN/email — it fails the actor-ID character rules and
     changes when people are renamed.
   - If you must expose a human-readable label, store it in event `metadata`,
     not in the namespace.
4. **Never trust `user_id` from the request body once SSO exists.** Derive the
   actor only from the validated token. Ignore or reject a body `user_id`.
5. **Authorization**: namespace isolation by actor is a data-partitioning
   mechanism, not a substitute for IAM. Scope the runtime role to the single
   memory ARN and keep per-user checks in application logic.
6. **Token lifetime**: long-lived browser sessions should refresh tokens; do not
   cache raw tokens in Memory or logs.
7. **Group/tenant mapping (optional)**: if support staff may view another user's
   history, model that as an explicit application role from AD groups, and log
   every cross-actor read.
8. **Interim option (IAM invoke)**: while the UI still calls through the Lambda
   proxy with SigV4, the caller can propagate a user identity with the
   `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header. This is weaker than a JWT
   authorizer (the backend must be trusted to set it), so use it only as a
   transition step; validating an Entra JWT is the target design.

Entra ID specifics to confirm during implementation:

- App registration with redirect URI for the UI.
- Expose an API scope (e.g. `api://snow-copilot/chat`) and require it in the
  authorizer.
- Validate `iss`, `aud`, `exp`, `nbf`, and signing keys via OIDC discovery
  (`https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration`).
- If on-prem AD only (AD FS), expose an OIDC/SAML endpoint and map the object
  GUID (`objectGUID`) the same way.

---

## 12. Security and retention

- **Retention**: `eventExpiryDuration` (3-365 days) is applied at write time;
  changing it does not extend old events. Set long-term record TTL per strategy
  (minimum 31 days) if chat insights should expire.
- **Encryption**: use a customer-managed KMS key for regulated data. Memory is
  encrypted in transit by TLS by default.
- **PII**: transcripts can contain incident details, names, and emails. Set
  CloudWatch Logs retention, restrict log access, and avoid logging event
  payloads in application logs.
- **Right to be forgotten**: implement a delete path using
  `DeleteMemoryRecord` and, for full removal, delete the actor's sessions or the
  memory resource according to policy. There is no per-actor bulk delete in the
  data plane — enumerate namespaces with `ListMemoryRecords` first.
- **Least privilege**: scope IAM to the memory ARN and the specific actions
  listed in section 4.
- **No secrets in memory**: never store credentials, bearer tokens, or
  connection strings as events.

---

## 13. Testing

Create a test event:

```bash
aws bedrock-agentcore create-event \
  --memory-id "<memory-id>" \
  --actor-id "user-123" \
  --session-id "sessabc123" \
  --event-timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --payload '[{"conversational":{"role":"USER","content":{"text":"How was VPN error 809 fixed?"}}}]'
```

Read short-term history:

```bash
aws bedrock-agentcore list-events \
  --memory-id "<memory-id>" \
  --actor-id "user-123" \
  --session-id "sessabc123" \
  --include-payloads \
  --max-results 10
```

Read long-term memory:

```bash
aws bedrock-agentcore list-memory-records \
  --memory-id "<memory-id>" \
  --namespace "/facts/user-123/"
```

Or with the SDK:

```python
records = memory.retrieve_memory_records(
    memoryId="<memory-id>",
    namespace="/facts/user-123/",
    searchCriteria={"searchQuery": "VPN troubleshooting", "topK": 5},
)
for r in records["memoryRecordSummaries"]:
    print(r["content"]["text"])
```

End-to-end check in this project:

1. Send two turns with the same `session_id`.
2. Confirm `list-events` shows four conversational payloads.
3. Ask a follow-up that depends on the first turn and verify the answer uses it.
4. Wait for extraction, then confirm `/facts/<actor>/` has records.
5. Clear browser storage, log in again with the same actor, and confirm
   long-term recall still works (short-term history will be empty by design).

---

## 14. Rollout checklist

- [ ] Create the memory resource with the three strategies; wait for `ACTIVE`.
- [ ] Add `MEMORY_ENABLED`, `MEMORY_ID`, and tuning values to config/env.
- [ ] Grant the runtime role the data-plane actions on the memory ARN.
- [ ] Add `memory_store.py` and wire it into `runtime_entrypoint.py` and
      `orchestrator.py`/`graph.py`.
- [ ] Decide the pre-SSO actor strategy and document its limitations.
- [ ] Add bounded history (last N turns) and long-term recall to the prompt.
- [ ] Add a memory-write failure counter to monitoring.
- [ ] Define retention/TTL and a deletion procedure.
- [ ] When SSO starts: switch `actorId` to the Entra object ID, validate tokens
      at the edge and in the runtime, and ignore client-supplied user IDs.

---

## 15. Cost and limits (planning notes)

- You pay for event storage/retention and for the model calls that extract and
  consolidate long-term memories. Extraction is per strategy, so enabling many
  strategies multiplies extraction work.
- Short-term reads are cheap and immediate; prefer them for turn-by-turn
  context. Use long-term recall to avoid sending the entire transcript.
- Watch `ThrottledException` and `ServiceQuotaExceededException`; retry with
  exponential backoff. `CreateEvent` is rate limited.
- Keep prompts bounded (history chars + top-K records) to control both memory
  and model cost.

---

## References

- Memory organization: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-organization.html
- Save and retrieve insights: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-saving-and-retrieving-insights.html
- Create a memory: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-create-a-memory-store.html
- Get started with Memory: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-get-started.html
- `CreateEvent` API: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_CreateEvent.html
- `ListEvents` API: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_ListEvents.html
- AgentCore Identity — inbound JWT authorizer: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/inbound-jwt-authorizer.html
- AgentCore Identity — private IdP: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-private-idp.html
