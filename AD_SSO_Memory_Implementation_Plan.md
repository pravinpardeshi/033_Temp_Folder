# AD SSO + AgentCore Memory — Implementation Plan

Status: **Draft for review**  
Scope: authentication/authorization, server-side chat sessions, session titles,
rename/delete, and AgentCore Memory integration.

This document is intended to be used directly as the coding brief. Defaults are
chosen where the current requirements are unambiguous; open decisions are
listed at the end.

---

## 1. Objective

1. Users sign in through corporate Active Directory using **SSO**.
2. The application never receives or stores AD passwords.
3. Every API request is authenticated and authorized.
4. The authenticated user ID becomes the AgentCore Memory `actorId`.
5. Chat sessions are stored server-side in AgentCore Memory.
6. Session metadata (title, timestamps) is stored in DynamoDB.
7. The first user question becomes the session title.
8. Users can list, open, rename, and delete their own sessions.
9. All UI/API requests continue through the existing single Lambda proxy.

---

## 2. Current state

```text
static UI
  │ POST /chat
  ▼
API Gateway
  ▼
Lambda proxy (lambda_function.py)
  │ InvokeAgentRuntime
  ▼
AgentCore Runtime (runtime_entrypoint.py)
  │
  ├── orchestrator.py
  │     ├── snow_agent
  │     ├── rca_agent
  │     └── change_qa_agent
  │
  └── no AgentCore Memory integration yet
```

Current limitations:

- No authentication or authorization.
- Conversation history lives in browser `localStorage`.
- `session_id` is generated in the browser.
- No server-side session listing, rename, or delete.
- AgentCore Memory is not used.

---

## 3. Target architecture

```text
Browser
  │ 1. Unauthenticated -> redirect to Entra ID / ADFS
  │ 2. Authorization Code + PKCE
  ▼
Identity Provider
  │ 3. ID token + access token
  ▼
Static UI
  │ Authorization: Bearer <access_token>
  ▼
API Gateway
  │ JWT authorizer validates signature, issuer, audience, expiry
  ▼
Single Lambda (dispatcher)
  │ actor_id = validated token claim
  │
  ├── DynamoDB: session metadata (list/rename/delete)
  ├── AgentCore Memory: read transcript for GET /sessions/{id}
  └── AgentCore Runtime: POST /chat
        │
        └── AgentCore Memory: load context, save turn
```

Trust boundary:

- Browser is untrusted.
- API Gateway validates the token.
- Lambda derives identity from validated claims; it must ignore any
  client-supplied `user_id`, `actor_id`, or `sub`.
- AgentCore Runtime is invoked only by the Lambda role (IAM).
- AgentCore Memory namespaces are scoped by `actorId`.

---

## 4. Identity provider decision

### Option A — Recommended: Microsoft Entra ID OIDC

Use when the organization has Entra ID (cloud or hybrid).

- Entra app registration.
- SPA redirect URI.
- Exposed API scope, e.g. `api://snow-copilot/chat`.
- API Gateway HTTP API JWT authorizer.
- No Cognito required.

### Option B — On-prem AD/ADFS only: Cognito federation

Use when only AD FS is available and SAML is required.

- Cognito user pool.
- SAML/OIDC federation to AD FS or Entra ID.
- API Gateway Cognito JWT authorizer.
- Map the Entra/AD object GUID into a Cognito custom attribute
  (`custom:entra_oid`) because Cognito `sub` is a Cognito UUID, not the AD
  object ID.

### Default assumption for implementation

Implement **Option A** first, with the auth abstraction allowing Option B later.

### Actor ID mapping

- Entra ID: use the `oid` claim, falling back to `sub`.
- Cognito federation: use `custom:entra_oid`; reject login if missing.
- Never use email/UPN as `actorId`:
  - AgentCore actor IDs do not allow `@` or `.`.
  - UPNs change when users are renamed.
- Store the display name/email only as non-key metadata.

---

## 5. Data model

### 5.1 DynamoDB: `ChatSessions`

Purpose: fast session listing, title, rename, delete, ownership checks.

```text
Table: ChatSessions
Billing: on-demand
Encryption: AWS-managed KMS (or customer-managed if required)
PITR: enabled

PK: USER#<actor_id>
SK: SESSION#<session_id>

Attributes:
  title          string   # first question, truncated
  created_at     string   # ISO-8601 UTC
  updated_at     string   # ISO-8601 UTC
  last_message   string   # optional, truncated
  agent          string   # last/default agent key
  message_count  number
```

Access patterns:

- List sessions: `Query(PK = USER#<actor_id>)`, sort by `updated_at` descending
  in Lambda.
- Get one: `GetItem(PK, SK)`.
- Rename: `UpdateItem` with ownership implied by PK/SK.
- Delete: `DeleteItem`.
- Cross-user access is impossible because the PK comes from the token.

Optional GSI (only if session volume requires it):

```text
GSI: ByUpdatedAt
PK: USER#<actor_id>
SK: updated_at
```

### 5.2 AgentCore Memory

Purpose: transcript and optional long-term memory.

```text
memoryId: from MEMORY_ID
actorId:  actor_id from token
sessionId: sanitized session ID
```

Session ID rules:

- Alphanumeric, maximum 100 characters.
- Generate server-side as a UUID, then sanitize:

```python
def memory_session_id(session_id: str) -> str:
    cleaned = "".join(c for c in session_id if c.isalnum())
    return cleaned[:100] or "session"
```

- Store the original UUID in DynamoDB `session_id`; pass the sanitized value to
  Memory.
- The API always exposes the original UUID.

Event format per successful turn:

```python
memory.create_event(
    memoryId=settings.MEMORY_ID,
    actorId=actor_id,
    sessionId=memory_session_id(session_id),
    eventTimestamp=datetime.now(timezone.utc),
    payload=[
        {"conversational": {"role": "USER", "content": {"text": user_text}}},
        {"conversational": {"role": "ASSISTANT", "content": {"text": answer}}},
    ],
    metadata={"agent": {"stringValue": agent_key}},
)
```

Long-term memory strategies are optional for phase 1. If enabled later:

- `/facts/{actorId}/`
- `/users/{actorId}/preferences/`
- `/summaries/{actorId}/{sessionId}/`

Do not block the chat response on long-term extraction.

### 5.3 Delete behavior

`DELETE /sessions/{session_id}`:

1. Verify the DynamoDB item exists under `USER#<actor_id>`.
2. Delete the DynamoDB item.
3. Page through `list_events(memoryId, actorId, sessionId)`.
4. Call `delete_event` for each event.
5. If long-term memory is enabled, delete relevant records under the actor's
   namespaces with `DeleteMemoryRecord`.
6. Return `204` even if the session was already absent (idempotent).

Deleting an event does not automatically delete derived long-term records.

---

## 6. API contract

All routes are handled by the same Lambda. API Gateway must use Lambda proxy
integration and pass the `Authorization` header.

### 6.1 `POST /chat`

Request:

```json
{
  "message": "How was VPN error 809 fixed?",
  "agent": "snow_agent",
  "session_id": "optional-existing-session"
}
```

Rules:

- `message` is required.
- `agent` is optional; unknown values are rejected or auto-routed.
- If `session_id` is absent:
  - Generate a server-side UUID.
  - After a successful answer, create DynamoDB metadata with:
    - `title = message[:60]`
    - `created_at = now`
    - `updated_at = now`
    - `message_count = 1`
- If `session_id` is present:
  - Verify it belongs to the token's actor.
  - If not found: return `404` (do not reveal whether another user owns it).
  - Update `updated_at`, `last_message`, increment `message_count`.

Response:

```json
{
  "response": "…",
  "session_id": "…",
  "title": "How was VPN error 809 fixed?"
}
```

### 6.2 `GET /sessions`

Response:

```json
{
  "sessions": [
    {
      "session_id": "…",
      "title": "How was VPN error 809 fixed?",
      "created_at": "2026-11-06T10:00:00Z",
      "updated_at": "2026-11-06T10:05:00Z",
      "last_message": "…",
      "agent": "snow_agent"
    }
  ]
}
```

Rules:

- Query only `USER#<actor_id>`.
- Sort by `updated_at` descending.
- Add pagination later if needed.

### 6.3 `GET /sessions/{session_id}`

Response:

```json
{
  "session_id": "…",
  "title": "…",
  "messages": [
    {"role": "user", "content": "…", "timestamp": "…"},
    {"role": "assistant", "content": "…", "timestamp": "…"}
  ]
}
```

Rules:

- Load metadata from DynamoDB.
- Load transcript from `list_events`.
- If metadata is missing, return `404`.

### 6.4 `PATCH /sessions/{session_id}`

Request:

```json
{ "title": "VPN troubleshooting notes" }
```

Rules:

- Title required, trimmed, 1–100 characters.
- Update DynamoDB only.
- Return the updated session metadata.

### 6.5 `DELETE /sessions/{session_id}`

- Idempotent.
- Delete DynamoDB metadata and Memory events.
- Return `204`.

### 6.6 Error format

```json
{ "detail": "Human-readable message" }
```

Status codes:

- `400` invalid body
- `401` missing/invalid token (mostly from API Gateway)
- `403` insufficient role/scope
- `404` session not found for this actor
- `409` title conflict, if ever needed
- `500` backend failure

---

## 7. Lambda dispatcher

The single Lambda must route by path and method. Support both API Gateway
payload formats.

### 7.1 Request shape helpers

```python
def http_method(event):
    return (
        event.get("requestContext", {})
        .get("http", {})
        .get("method")
        or event.get("httpMethod")
        or "POST"
    ).upper()

def request_path(event):
    return event.get("rawPath") or event.get("path") or "/"
```

### 7.2 Claims helper

```python
def jwt_claims(event):
    auth = event.get("requestContext", {}).get("authorizer", {})
    return auth.get("jwt", {}).get("claims") or auth.get("claims") or {}

def actor_id_from_claims(claims):
    actor = claims.get("oid") or claims.get("sub")
    if not actor:
        raise AuthError("Token has no actor identifier")
    return actor
```

For Cognito federation, replace with `claims["custom:entra_oid"]`.

### 7.3 Routes

```text
POST   /chat                     -> chat()
GET    /sessions                 -> list_sessions()
GET    /sessions/{id}            -> get_session()
PATCH  /sessions/{id}            -> rename_session()
DELETE /sessions/{id}            -> delete_session()
OPTIONS *                        -> CORS preflight (API Gateway may handle it)
```

If API Gateway uses a base path (e.g. `/api`), strip it before matching.

### 7.4 CORS

Allow:

- `Authorization`
- `Content-Type`

Do not use `Access-Control-Allow-Origin: *` when credentials/tokens are used;
use the configured UI origin.

---

## 8. AgentCore Runtime changes

### 8.1 New payload fields

```json
{
  "prompt": "…",
  "agent": "snow_agent",
  "session_id": "…",
  "actor_id": "…"
}
```

`actor_id` is trusted because the runtime is invoked only by the Lambda role.
The runtime must reject requests missing `actor_id` when `MEMORY_ENABLED=true`.

### 8.2 Memory composition

Keep agent classes unchanged. Compose memory outside them:

```text
load recent events (actor_id, session_id)
        │
        ▼
build context block
        │
        ▼
orchestrator.route_query(prompt_with_context, agent_key)
        │
        ▼
answer
        │
        ▼
save_turn(actor_id, session_id, user_text, answer)
```

Recommended context policy:

- Last 10 turns (20 events) by default.
- Truncate each event to a configurable maximum.
- Do not send the entire history.
- If the prompt is explicit-agent, history is still included.

Failure policy:

- Memory read failure: continue without history, log a warning.
- Memory write failure: return the answer but log a warning; do not fail the
  request.
- Add monitoring counters:
  - `memory_read_failed`
  - `memory_write_failed`

### 8.3 Runtime module

```python
class AgentCoreMemoryStore:
    def __init__(self, memory_id: str, region: str): ...
    def recent_turns(self, actor_id: str, session_id: str, limit: int = 10) -> list: ...
    def save_turn(
        self,
        actor_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        agent_key: str,
    ) -> None: ...
```

Return `[]` / no-op when `MEMORY_ENABLED=false`.

---

## 9. UI changes

### 9.1 New files

```text
static/config.js      # issuer, client_id, redirect_uri, scopes, api_base
static/auth.js        # PKCE login, callback, token refresh, logout
```

No build step: implement PKCE with Web Crypto.

### 9.2 Boot flow

1. `auth.js` checks for `?code=` and `?state=` in the URL.
2. If present: exchange code for tokens, clean the URL, continue.
3. If no token: redirect to the IdP authorize endpoint.
4. If token exists: render the app.
5. On `401`: clear tokens and redirect to login.

### 9.3 API calls

Replace direct `fetch` with:

```javascript
authFetch(url, options)
```

It adds:

```http
Authorization: Bearer <access_token>
```

Chat body:

```json
{
  "message": "…",
  "agent": "snow_agent",
  "session_id": "optional"
}
```

Change QA body:

```json
{
  "message": "CHG0030001",
  "agent": "change_qa_agent"
}
```

### 9.4 Session sidebar

- Load from `GET /sessions`.
- New chat: clear active session; do not create a session until the first
  message is sent.
- On first successful response:
  - store the returned `session_id`
  - display the returned `title`
- Open session: `GET /sessions/{id}` and render messages.
- Rename: inline edit or prompt, `PATCH /sessions/{id}`.
- Delete: confirmation, `DELETE /sessions/{id}`.
- Remove or stop using localStorage conversation persistence.
- Keep only UI preferences (theme, selected agent, table) in localStorage.

### 9.5 User menu

- Display `name` or `preferred_username`.
- Logout calls the IdP `end_session_endpoint` when available and clears tokens.

---

## 10. File-level implementation checklist

### New files

| File | Responsibility |
| --- | --- |
| `auth/__init__.py` | Auth package. |
| `auth/claims.py` | Extract/normalize JWT claims; actor ID; roles. |
| `auth/errors.py` | `AuthError`, `ForbiddenError`, `NotFoundError`. |
| `memory/__init__.py` | Memory package. |
| `memory/agentcore_memory.py` | AgentCore Memory wrapper. |
| `sessions/__init__.py` | Session package. |
| `sessions/repository.py` | DynamoDB CRUD. |
| `sessions/service.py` | Create/list/get/rename/delete logic. |
| `infra/setup_sso.py` | Idempotent AWS setup: DynamoDB, Memory, IAM. |
| `infra/setup_entra_app.py` | Optional Microsoft Graph app registration. |
| `static/config.js` | Runtime UI auth configuration. |
| `static/auth.js` | OIDC PKCE client. |

### Modified files

| File | Change |
| --- | --- |
| `lambda_function.py` | Dispatcher, auth claims, session routes, AgentCore invocation. |
| `runtime_entrypoint.py` | Accept `actor_id`; load/save Memory; reject missing actor. |
| `orchestrator.py` | No agent changes required; optional context passthrough. |
| `config.py` | Add auth/memory/session settings. |
| `static/index.html` | Load `config.js` and `auth.js`; add user menu/rename/delete controls. |
| `static/script.js` | Server-side sessions; `authFetch`; title/rename/delete. |
| `static/style.css` | Session actions, user menu, login states. |
| `requirements.txt` | Add `PyJWT[crypto]` if server-side JWT verification is used. |
| `README.md` | Deployment/auth/session documentation. |

### Do not change

- Agent prompts and retrieval logic.
- `agents/snow_agent.py`
- `agents/rca_agent.py`
- `agents/change_qa_agent.py`
- RDS/pgvector retrieval behavior.

---

## 11. Configuration

Add to `config.py` and `.env.example`:

```env
# Authentication
AUTH_ENABLED=true
AUTH_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
AUTH_AUDIENCE=api://snow-copilot
AUTH_REQUIRED_SCOPE=chat
AUTH_ACTOR_CLAIM=oid
AUTH_ROLES_CLAIM=roles
UI_ORIGIN=https://<ui-host>

# AgentCore Memory
MEMORY_ENABLED=true
MEMORY_ID=<memory-id>
MEMORY_CONTEXT_TURNS=10
MEMORY_EVENT_MAX_CHARS=4000

# Sessions
SESSIONS_TABLE=ChatSessions
SESSION_TITLE_MAX_CHARS=60
SESSION_TITLE_RENAME_MAX_CHARS=100
```

For Cognito federation:

```env
AUTH_ACTOR_CLAIM=custom:entra_oid
```

---

## 12. IAM and infrastructure

### Runtime role

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock-agentcore:CreateEvent",
    "bedrock-agentcore:ListEvents",
    "bedrock-agentcore:RetrieveMemoryRecords",
    "bedrock-agentcore:ListMemoryRecords"
  ],
  "Resource": "arn:aws:bedrock-agentcore:<region>:<account>:memory/<memory-id>"
}
```

### Lambda role

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:ListEvents",
    "bedrock-agentcore:DeleteEvent",
    "bedrock-agentcore:ListSessions",
    "bedrock-agentcore:DeleteMemoryRecord",
    "bedrock-agentcore:ListMemoryRecords"
  ],
  "Resource": [
    "arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>",
    "arn:aws:bedrock-agentcore:<region>:<account>:memory/<memory-id>"
  ]
}
```

### DynamoDB

```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:Query",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem"
  ],
  "Resource": "arn:aws:dynamodb:<region>:<account>:table/ChatSessions"
}
```

---

## 13. Setup script specification

### `infra/setup_sso.py`

Idempotent boto3 script.

Steps:

1. Read config from environment or CLI flags.
2. Create DynamoDB table `ChatSessions` if missing.
3. Enable PITR.
4. Create AgentCore Memory resource if `MEMORY_ID` is not provided.
5. Create strategies only if long-term memory is enabled.
6. Wait for Memory status `ACTIVE`.
7. Print/update:
   - `MEMORY_ID`
   - table name
8. Optionally attach IAM policies or print the required JSON.

Do not create the Entra app silently; that requires tenant admin consent.

### `infra/setup_entra_app.py` (optional)

Uses Microsoft Graph to create/update:

- SPA app registration
- Redirect URI
- Exposed API scope
- Optional app roles for AD groups

Requires:

- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`
- Graph permissions: `Application.ReadWrite.All`, `AppRoleAssignment.ReadWrite.All`

If the organization does not permit Graph automation, document the manual portal
steps in `README.md` instead.

---

## 14. Security requirements

1. Never log access tokens, ID tokens, authorization codes, or AD passwords.
2. Do not accept `user_id`/`actor_id` from the request body.
3. Enforce session ownership on every read/write/delete.
4. Use `oid` (Entra object ID) as `actorId`.
5. Validate token issuer, audience, signature, `exp`, and `nbf` at API Gateway.
6. Use least-privilege IAM; runtime and Lambda roles differ.
7. Restrict CORS to the configured UI origin.
8. Store tokens in memory or `sessionStorage`; clear on logout.
9. Do not store raw tokens in AgentCore Memory.
10. Configure retention:
    - Memory short-term events: 3–365 days.
    - DynamoDB: TTL policy if required.
11. For regulated data, configure a customer-managed KMS key for Memory.
12. Log every cross-actor access if an admin role is ever introduced.

### Authorization matrix

| Action | Required |
| --- | --- |
| Login | Valid SSO account |
| Use chat | Valid token, allowed scope/group |
| List sessions | Own `actor_id` only |
| Read session | Own `actor_id` only |
| Rename session | Own `actor_id` only |
| Delete session | Own `actor_id` only |
| Admin cross-user view | Optional AD role, not in v1 |

---

## 15. Testing plan

### Unit tests

- Actor claim extraction for Entra and Cognito.
- Missing actor claim raises `AuthError`.
- Session ID sanitization.
- Title truncation at 60 characters.
- Rename validation (empty, too long).
- DynamoDB repository key construction.
- Memory event mapping to UI messages.

### Integration tests

- `401` without token.
- `403` with a token missing the required scope/group.
- User A cannot read, rename, or delete User B's session.
- First message creates a session with the correct title.
- Second message reuses the session.
- Rename updates the title.
- Delete removes metadata and Memory events.
- Deleted session returns `404` afterward.
- `GET /sessions` returns newest first.

### End-to-end tests

1. Open the app unauthenticated → redirected to SSO.
2. Sign in with an AD account.
3. Send the first question → sidebar title equals the question.
4. Reload the browser → session list loads from the backend.
5. Open the session → messages load from Memory.
6. Rename the session.
7. Delete the session.
8. Sign out → token cleared; app redirects to SSO.

### Failure tests

- Memory unavailable → chat still answers; warning logged.
- DynamoDB unavailable → chat answers but session metadata is not saved; return
  a warning or degrade gracefully.
- Expired token → UI redirects to SSO.
- API Gateway timeout → verify the existing async pattern if requests exceed
  29–30 seconds.

---

## 16. Implementation sequence

### Phase 1 — AWS resources

- Add `infra/setup_sso.py`.
- Create `ChatSessions`.
- Create AgentCore Memory.
- Add IAM policies.
- Add config values.

### Phase 2 — Authentication

- Configure API Gateway JWT authorizer.
- Add `static/config.js` and `static/auth.js`.
- Add login/logout/user display.
- Add `authFetch`.
- Verify `401` behavior.

### Phase 3 — Session metadata

- Add `sessions/repository.py` and `sessions/service.py`.
- Add `GET /sessions`, `PATCH /sessions/{id}`, `DELETE /sessions/{id}`.
- Add Lambda path routing.
- Add title creation on first message.

### Phase 4 — AgentCore Memory

- Add `memory/agentcore_memory.py`.
- Runtime accepts `actor_id`.
- Load recent turns before the agent call.
- Save the turn after success.
- Add `GET /sessions/{id}` transcript loading.

### Phase 5 — UI integration

- Replace localStorage conversations with backend sessions.
- Rename/delete controls.
- Load old conversations.
- Remove client-generated session IDs.

### Phase 6 — Hardening

- Cross-user tests.
- Retention/TTL.
- Monitoring counters.
- Documentation.
- Optional long-term memory strategies.

---

## 17. Acceptance criteria

- [ ] Unauthenticated users are redirected to Entra/ADFS SSO.
- [ ] No AD password is handled by the application.
- [ ] API Gateway rejects invalid/expired tokens.
- [ ] Lambda derives `actor_id` only from validated claims.
- [ ] First chat message becomes the session title.
- [ ] Sessions persist across browsers and reloads.
- [ ] Users can list, open, rename, and delete their own sessions.
- [ ] Users cannot access another user's sessions.
- [ ] Chat turns are stored in AgentCore Memory and replayed correctly.
- [ ] Delete removes the session metadata and Memory events.
- [ ] All requests still go through one Lambda and one API Gateway route set.
- [ ] Existing snow, RCA, and change QA behavior is unchanged.

---

## 18. Open decisions

Defaults in parentheses will be used if no answer is provided.

1. Is the IdP **Entra ID OIDC** or **ADFS/SAML only**? (Entra ID OIDC)
2. Single-tenant or multi-tenant? (Single-tenant)
3. Which AD groups/roles may use the app? (All authenticated users)
4. Should long-term Memory strategies be enabled in v1? (No; short-term only)
5. Should Change QA results be saved as sessions? (No; chat only)
6. Session retention period? (30 days for Memory, DynamoDB until deleted)
7. Should users be able to delete sessions permanently? (Yes)
8. Is the UI hosted on S3/CloudFront or by the runtime? (S3/CloudFront)
9. Should admins view other users' sessions? (No)
10. Who owns Entra app registration/admin consent? (Customer IT admin)

---

## 19. Notes for the coding agent

- Keep session metadata and transcripts separate: DynamoDB for metadata,
  AgentCore Memory for conversation content.
- Do not store titles in Memory namespaces.
- Do not trust client-supplied identity.
- Do not change agent retrieval logic.
- Preserve the single-Lambda routing model.
- Use API Gateway proxy integration and return proper proxy responses.
- Keep long-running AgentCore calls within the API Gateway timeout; if not
  possible, switch `/chat` to an async submit/poll pattern.
- Prefer small modules (`auth/`, `sessions/`, `memory/`) over growing
  `lambda_function.py`.
