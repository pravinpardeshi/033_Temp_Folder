"""SSO chat-session workflow backed by DynamoDB metadata and AgentCore Memory.

Workflow
--------
1. SSO login (upstream).
   - The SSO layer validates the token and derives a stable actor id
     (Entra object id / ``oid``). Every API call below is scoped to that
     ``actor_id`` (passed here as ``X-Actor-Id`` or ``user_id`` for local dev).

2. User clicks "Chat" -> load session index from DynamoDB.
   - ``GET /api/chat/sessions``
   - Backend: ``ChatSessionWorkflow.list_sessions(actor_id)``
   - Source of truth: DynamoDB ``ChatSessions`` table (PK ``USER#<actor_id>``,
     SK ``SESSION#<session_id>``).
   - Response is the list rendered in the browser's left nav.

3. Sessions displayed in the browser.
   - UI renders title / updated_at / last_message. No transcript fetched yet.

4. User selects a session -> transcript pulled from AgentCore Memory.
   - ``GET /api/chat/sessions/{session_id}``
   - Backend: ``ChatSessionWorkflow.get_session(actor_id, session_id)``
   - Transcript source: AgentCore Memory events for (memoryId, actorId, sessionId).
   - DynamoDB metadata is included for title/agent; the message list comes from
     AgentCore so the user can continue the historic conversation.

5. Further interactions -> AgentCore Memory updated.
   - ``POST /api/chat/messages`` with ``{session_id, message, agent}``
   - Backend runs the agent, then ``ChatSessionWorkflow.save_interaction(...)``:
       a. ``AgentCoreMemoryStore.save_turn(...)`` appends the USER+ASSISTANT turn.
       b. DynamoDB session metadata is touched (updated_at, last_message,
          message_count, title on first turn).
   - The same ``session_id`` is reused, so AgentCore accumulates the history.

6. "New Chat" -> DynamoDB entry created immediately.
   - ``POST /api/chat/sessions`` with optional ``{agent, title}``
   - Backend: ``ChatSessionWorkflow.create_session(actor_id, ...)`` creates the
     ``SESSION#`` row before any message is sent and returns the new session id.
   - UI selects it and shows an empty thread; the first message continues it.

Configuration (environment variables)
------------------------------------
    AWS_REGION              default us-east-1
    SESSIONS_TABLE          default ChatSessions
    MEMORY_ID               AgentCore Memory resource id (required for transcripts)
    MEMORY_ENABLED          default true
    DYNAMODB_ENDPOINT_URL   e.g. http://localhost:8000 for local DynamoDB
    SESSION_TTL_DAYS        default 30 (0 disables TTL writes)

This module is standalone but reuses ``SessionRepository`` and
``AgentCoreMemoryStore`` from ``session_memory_store.py`` in the same folder.
Run as a small demo API with:
    python chat_session_flow.py
or import ``ChatSessionWorkflow`` / ``register_routes`` into the main app.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from session_memory_store import (
    AgentCoreMemoryStore,
    SessionRepository,
    new_session_id,
    title_from_message,
    utcnow_iso,
    validate_actor_id,
)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class ChatSessionFlowConfig:
    region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))
    sessions_table: str = field(default_factory=lambda: _env("SESSIONS_TABLE", "ChatSessions"))
    memory_id: str = field(default_factory=lambda: _env("MEMORY_ID", ""))
    memory_enabled: bool = field(default_factory=lambda: _env_bool("MEMORY_ENABLED", True))
    endpoint_url: str = field(default_factory=lambda: _env("DYNAMODB_ENDPOINT_URL", ""))
    session_ttl_days: int = field(default_factory=lambda: _env_int("SESSION_TTL_DAYS", 30))
    title_max_chars: int = field(default_factory=lambda: _env_int("SESSION_TITLE_MAX_CHARS", 60))


class ChatSessionWorkflow:
    """Implements steps 2-6 of the SSO chat-session workflow."""

    def __init__(self, config: Optional[ChatSessionFlowConfig] = None):
        self.config = config or ChatSessionFlowConfig()
        self.sessions = SessionRepository(
            self.config.sessions_table,
            self.config.region,
            ttl_days=self.config.session_ttl_days,
            endpoint_url=self.config.endpoint_url,
        )
        self.memory = AgentCoreMemoryStore(
            self.config.memory_id,
            self.config.region,
            enabled=self.config.memory_enabled,
        )

    # -- Step 2: DynamoDB session index ------------------------------------

    def list_sessions(self, actor_id: str) -> List[Dict[str, Any]]:
        """Return the user's chat history sessions from DynamoDB."""
        actor_id = validate_actor_id(actor_id)
        return [record.to_dict() for record in self.sessions.list(actor_id)]

    # -- Step 6: New Chat creates a DynamoDB entry immediately --------------

    def create_session( self, actor_id: str, agent: str = "snow", title: str = "New chat",) -> Dict[str, Any]:
        """Create a new session row in DynamoDB and return it."""
        actor_id = validate_actor_id(actor_id)
        session_id = new_session_id()
        record = self.sessions.create(
            actor_id=actor_id,
            session_id=session_id,
            title=title or "New chat",
            agent=agent,
        )
        return record.to_dict()

    # -- Step 4: transcript pulled from AgentCore Memory on selection -------

    def get_session(self, actor_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """Load session metadata from DynamoDB and transcript from AgentCore."""
        actor_id = validate_actor_id(actor_id)
        record = self.sessions.get(actor_id, session_id)
        if record is None:
            return None

        # Transcript comes from AgentCore Memory so the user can continue the
        # historic conversation. Fall back to DynamoDB-stored messages if Memory
        # is unavailable or empty (useful for local dev).
        messages = self.memory.list_messages(actor_id, session_id)
        if not messages:
            messages = self.sessions.list_messages(actor_id, session_id)

        return {
            **record.to_dict(),
            "messages": messages,
        }

    # -- Step 5: interactions update AgentCore Memory -----------------------

    async def continue_session(
        self,
        actor_id: str,
        session_id: str,
        user_message: str,
        agent: str,
        answer_fn: Callable[[str, str, str], Awaitable[str]],
    ) -> Dict[str, Any]:
        """Run the agent, persist the turn to AgentCore, and touch DynamoDB.

        ``answer_fn(session_id, user_message, agent) -> assistant_text`` is the
        agent/orchestrator call (async). This keeps the workflow independent of
        the agent implementation.
        """
        actor_id = validate_actor_id(actor_id)
        user_message = str(user_message or "").strip()
        if not user_message:
            raise ValueError("message is required")

        # Ensure the session row exists (covers clients that skip "New Chat").
        record = self.sessions.get(actor_id, session_id)
        if record is None:
            record = self.sessions.create(
                actor_id=actor_id,
                session_id=session_id,
                title=title_from_message(user_message, self.config.title_max_chars),
                agent=agent,
            )

        assistant_text = await answer_fn(session_id, user_message, agent)

        # Persist the turn to AgentCore Memory (historic session updated there).
        self.memory.save_turn(
            actor_id=actor_id,
            session_id=session_id,
            user_text=user_message,
            assistant_text=assistant_text,
            agent_key=agent,
        )

        # Keep DynamoDB metadata in sync for the session list.
        if record.title in ("", "New chat") and record.message_count == 0:
            self.sessions.rename(
                actor_id,
                session_id,
                title_from_message(user_message, self.config.title_max_chars),
                max_chars=self.config.title_max_chars,
            )
        self.sessions.touch(
            actor_id=actor_id,
            session_id=session_id,
            last_message=assistant_text or user_message,
            agent=agent,
        )

        return {
            "session_id": session_id,
            "response": assistant_text,
        }

    def save_interaction(
        self,
        actor_id: str,
        session_id: str,
        user_message: str,
        assistant_text: str,
        agent: str = "snow",
    ) -> Dict[str, Any]:
        """Persist an already-computed turn (sync variant, no agent call)."""
        actor_id = validate_actor_id(actor_id)
        record = self.sessions.get(actor_id, session_id)
        if record is None:
            record = self.sessions.create(
                actor_id=actor_id,
                session_id=session_id,
                title=title_from_message(user_message, self.config.title_max_chars),
                agent=agent,
            )

        self.memory.save_turn(
            actor_id=actor_id,
            session_id=session_id,
            user_text=user_message,
            assistant_text=assistant_text,
            agent_key=agent,
        )

        if record.title in ("", "New chat") and record.message_count == 0:
            self.sessions.rename(
                actor_id,
                session_id,
                title_from_message(user_message, self.config.title_max_chars),
                max_chars=self.config.title_max_chars,
            )
        updated = self.sessions.touch(
            actor_id=actor_id,
            session_id=session_id,
            last_message=assistant_text or user_message,
            agent=agent,
        )
        return updated.to_dict()


# ---------------------------------------------------------------------------
# Example HTTP API (Starlette) — wire into the app and replace the demo
# ``answer_fn`` with ``orchestrator.route_query`` / ``_run_agent``.
# ---------------------------------------------------------------------------

def _actor_id(request: Request, payload: Optional[dict] = None) -> str:
    value = (
        request.headers.get("x-actor-id")
        or request.headers.get("x-user-id")
        or (payload or {}).get("user_id")
        or request.query_params.get("user_id")
        or "default_user"
    )
    return str(value).strip() or "default_user"


def register_routes(app_routes: list, workflow: ChatSessionWorkflow) -> None:
    """Append the workflow's HTTP routes to a Starlette route list."""

    async def list_sessions(request: Request) -> JSONResponse:
        try:
            sessions = workflow.list_sessions(_actor_id(request))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=500)
        return JSONResponse({"sessions": sessions})

    async def create_session(request: Request) -> JSONResponse:
        payload = {}
        try:
            if request.headers.get("content-length") not in (None, "0"):
                payload = await request.json()
        except Exception:
            payload = {}
        try:
            session = workflow.create_session(
                actor_id=_actor_id(request, payload),
                agent=str(payload.get("agent") or "snow"),
                title=str(payload.get("title") or "New chat"),
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=500)
        return JSONResponse(session, status_code=201)

    async def get_session(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        try:
            data = workflow.get_session(_actor_id(request), session_id)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=500)
        if data is None:
            return JSONResponse({"detail": "Session not found."}, status_code=404)
        return JSONResponse(data)

    async def post_message(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON body."}, status_code=400)

        session_id = str(payload.get("session_id") or "").strip()
        message = str(payload.get("message") or "").strip()
        agent = str(payload.get("agent") or "snow")
        if not session_id:
            return JSONResponse({"detail": "Missing 'session_id'."}, status_code=400)
        if not message:
            return JSONResponse({"detail": "Missing 'message'."}, status_code=400)

        async def answer_fn(sid: str, text: str, agent_key: str) -> str:
            # Replace with: return await orchestrator.route_query(sid, text, agent_key=agent_key)
            return f"(demo) received: {text}"

        try:
            result = await workflow.continue_session(
                actor_id=_actor_id(request, payload),
                session_id=session_id,
                user_message=message,
                agent=agent,
                answer_fn=answer_fn,
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=500)
        return JSONResponse(result)

    app_routes.extend([
        Route("/api/chat/sessions", list_sessions, methods=["GET"]),
        Route("/api/chat/sessions", create_session, methods=["POST"]),
        Route("/api/chat/sessions/{session_id}", get_session, methods=["GET"]),
        Route("/api/chat/messages", post_message, methods=["POST"]),
    ])


if __name__ == "__main__":
    import uvicorn

    workflow = ChatSessionWorkflow()
    routes: list = []
    register_routes(routes, workflow)
    app = Starlette(routes=routes)
    print("Chat session workflow demo listening on http://localhost:8090")
    print("  GET  /api/chat/sessions")
    print("  POST /api/chat/sessions           -> creates DynamoDB entry (New Chat)")
    print("  GET  /api/chat/sessions/{id}      -> transcript from AgentCore Memory")
    print("  POST /api/chat/messages           -> saves turn to AgentCore Memory")
    uvicorn.run(app, host="0.0.0.0", port=8090)
