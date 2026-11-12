import asyncio
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import monitoring
from config import settings
from session_memory_store import SessionMemoryConfig, SessionMemoryService, new_session_id
from orchestrator import orchestrator

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = BedrockAgentCoreApp()
session_service = SessionMemoryService(
    config=SessionMemoryConfig(
        region=settings.AWS_REGION,
        memory_id=settings.MEMORY_ID,
        sessions_table=settings.SESSIONS_TABLE,
        endpoint_url=settings.DYNAMODB_ENDPOINT_URL,
        memory_enabled=settings.MEMORY_ENABLED,
        context_turns=settings.MEMORY_CONTEXT_TURNS,
        title_max_chars=settings.SESSION_TITLE_MAX_CHARS,
        rename_max_chars=settings.SESSION_TITLE_RENAME_MAX_CHARS,
        session_ttl_days=settings.SESSION_TTL_DAYS,
    )
)
app.add_middleware(monitoring.MonitoringMiddleware)


def _actor_id(request: Request, payload: dict | None = None) -> str:
    """Resolve the authenticated user id.

    SSO is assumed to run in front of the runtime and inject the Entra object
    id as ``X-Actor-Id`` (or ``X-User-Id``). The body/query ``user_id`` exists
    only so local development can exercise the API without SSO.
    """
    value = (
        request.headers.get("x-actor-id")
        or request.headers.get("x-user-id")
        or (payload or {}).get("user_id")
        or (payload or {}).get("actor_id")
        or request.query_params.get("user_id")
        or "default_user"
    )
    return str(value).strip() or "default_user"



async def _run_agent(prompt: str, session_id: str, agent_key: str | None = None, table_name: str | None = None,) -> str:
    """Execute the orchestrator and record monitoring metrics."""
    start = time.time()
    capture_token = monitoring.start_token_capture()

    try:
        answer = await orchestrator.route_query(session_id, prompt, agent_key=agent_key, table_name=table_name)
    except Exception:
        usage = monitoring.finish_token_capture(capture_token)
        monitoring.record_invocation(
            status="error",
            query=prompt,
            latency=time.time() - start,
            session_id=session_id,
            input_tokens=usage["input_tokens"] if usage["calls"] else monitoring.estimate_tokens(prompt),
            output_tokens=usage["output_tokens"],
            llm_calls=usage["calls"],
            token_source="bedrock" if usage["calls"] else "estimated",
            models=usage["models"],
        )
        raise

    usage = monitoring.finish_token_capture(capture_token)
    has_usage = usage["calls"] > 0
    monitoring.record_invocation(
        status="success",
        query=prompt,
        latency=time.time() - start,
        session_id=session_id,
        input_tokens=usage["input_tokens"] if has_usage else monitoring.estimate_tokens(prompt),
        output_tokens=usage["output_tokens"] if has_usage else monitoring.estimate_tokens(answer),
        llm_calls=usage["calls"],
        token_source="bedrock" if has_usage else "estimated",
        models=usage["models"],
    )
    return answer


@app.entrypoint
async def invoke(payload, context):
    """AgentCore invocation handler (POST /invocations).

    Expected payload:
        {"prompt": "...", "agent": "snow_agent", "session_id": "optional"}
    For change quality assessment:
        {"agent": "ci_qa", "prompt": "CHG0030001"}
    """
    payload = payload or {}
    change_number = payload.get("change_number")
    prompt = (payload.get("prompt") or payload.get("message") or payload.get("change_number")  or "")
    session_id = payload.get("session_id") or str(uuid.uuid4())
    agent_key = payload.get("agent")

    if change_number and not agent_key:
        agent_key = "ci_qa"
    table_name = payload.get("table_name") or payload.get("table")

    if not str(prompt).strip():
        return {"error": "Missing 'prompt' in payload."}

    try:
        answer = await _run_agent(str(prompt), session_id, agent_key=agent_key, table_name=table_name)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {"result": answer, "session_id": session_id}


async def index(request: Request) -> FileResponse:
    """Serve the chatbot UI."""
    return FileResponse(STATIC_DIR / "index.html")


async def tables(request: Request) -> JSONResponse:
    """Incident tables the SNOW agent can query (used by the UI dropdown)."""
    return JSONResponse({ "tables": settings.snow_tables, "default": settings.RDS_TABLE_NAME, })


async def assess_change(request: Request) -> JSONResponse:
    """UI-facing change quality assessment endpoint."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON body."}, status_code=400)

    change_number = payload.get("change_number") or payload.get("change")
    change_number = str(change_number or "").strip()

    if not change_number:
        return JSONResponse({"detail": "Missing 'change_number'."}, status_code=400)

    session_id = payload.get("session_id") or str(uuid.uuid4())

    try:
        assessment = await _run_agent(change_number, session_id, agent_key="ci_qa")
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

    return JSONResponse({ "change_number": change_number, "assessment": assessment, })


def _post_risk_assessment(
    url: str,
    payload: dict,
    token: str,
    timeout: float,
) -> tuple[int, str]:
    """POST a JSON payload to the configured risk assessment service."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


async def risk_assessment(request: Request) -> JSONResponse:
    """UI-facing risk assessment endpoint.

    Proxies the change item to the external risk service configured in
    ``RISK_ASSESSMENT_URL`` and returns its response unchanged.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON body."}, status_code=400)

    if not isinstance(payload, dict):
        payload = {"change_number": payload}

    change_item = (
        payload.get("change_item_number")
        or payload.get("change_number")
        or payload.get("change")
    )
    change_item = str(change_item or "").strip()

    if not change_item:
        return JSONResponse(
            {"detail": "Missing 'change_item_number'."}, status_code=400
        )

    url = (settings.RISK_ASSESSMENT_URL or "").strip()
    if not url:
        return JSONResponse(
            {
                "detail": (
                    "Risk assessment service is not configured. "
                    "Set RISK_ASSESSMENT_URL to the external endpoint."
                )
            },
            status_code=503,
        )

    body = dict(payload)
    body["change_item_number"] = change_item
    body.setdefault("change_number", change_item)

    try:
        status, raw = await asyncio.to_thread(
            _post_risk_assessment,
            url,
            body,
            settings.RISK_ASSESSMENT_TOKEN,
            settings.RISK_ASSESSMENT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"detail": f"Risk assessment request failed: {exc}"}, status_code=502
        )

    if status >= 400:
        return JSONResponse(
            {
                "detail": f"Risk assessment service returned HTTP {status}.",
                "response": raw[:2000],
            },
            status_code=502,
        )

    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = raw

    return JSONResponse({"change_number": change_item, "assessment": data})


async def chat(request: Request) -> JSONResponse:
    """UI-facing chat endpoint (kept separate from the AgentCore contract)."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON body."}, status_code=400)

    message = payload.get("message") or payload.get("prompt") or ""
    user_id = _actor_id(request, payload)
    requested_session_id = str(payload.get("session_id") or "").strip() or None
    agent_key = payload.get("agent")
    table_name = payload.get("table_name") or payload.get("table")

    if not str(message).strip():
        return JSONResponse({"detail": "Missing 'message'."}, status_code=400)

    if agent_key and orchestrator.resolve_agent_key(agent_key) is None:
        return JSONResponse(
            {"detail": f"Unknown agent: {agent_key}"}, status_code=400
        )

    # Reuse the caller's session when one is supplied (existing conversation);
    # otherwise start a brand-new session. `new_session: true` forces a fresh
    # id even if a stale session_id is present. We never silently attach a new
    # chat to the user's most recent conversation.
    force_new = bool(payload.get("new_session"))
    session_id = new_session_id() if (force_new or not requested_session_id) else requested_session_id

    try:
        response_text = await _run_agent(str(message), session_id, agent_key=agent_key, table_name=table_name)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

    # Append this turn to the resolved session (AgentCore Memory transcript +
    # DynamoDB metadata). Failures here must never break the chat response.
    try:
        await asyncio.to_thread(
            session_service.save_turn,
            actor_id=user_id,
            session_id=session_id,
            user_text=str(message),
            assistant_text=response_text,
            agent_key=agent_key or "snow_agent",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"session persistence failed: {exc}")

    return JSONResponse({"response": response_text, "session_id": session_id})


async def list_sessions(request: Request) -> JSONResponse:
    """List this user's chat sessions, newest first (left-nav history)."""
    actor_id = _actor_id(request)
    try:
        sessions = await asyncio.to_thread(session_service.list_sessions, actor_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return JSONResponse({"user_id": actor_id, "sessions": sessions})


async def get_session(request: Request) -> JSONResponse:
    """Return one session's metadata and transcript."""
    actor_id = _actor_id(request)
    session_id = request.path_params.get("session_id", "")
    try:
        data = await asyncio.to_thread(
            session_service.get_session, actor_id, session_id
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"detail": str(exc)}, status_code=500)
    if data is None:
        return JSONResponse({"detail": "Session not found."}, status_code=404)
    return JSONResponse(data)


async def rename_session(request: Request) -> JSONResponse:
    """Rename a chat (title stored in DynamoDB)."""
    actor_id = _actor_id(request)
    session_id = request.path_params.get("session_id", "")
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON body."}, status_code=400)

    title = str(payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"detail": "Missing 'title'."}, status_code=400)

    try:
        record = await asyncio.to_thread(
            session_service.rename_session, actor_id, session_id, title
        )
    except Exception as exc:  # noqa: BLE001
        if "ConditionalCheckFailed" in exc.__class__.__name__ or "ConditionalCheckFailed" in str(exc):
            return JSONResponse({"detail": "Session not found."}, status_code=404)
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return JSONResponse(record)


async def delete_session(request: Request) -> JSONResponse:
    """Delete a chat's metadata and transcript."""
    actor_id = _actor_id(request)
    session_id = request.path_params.get("session_id", "")
    try:
        deleted_events = await asyncio.to_thread(
            session_service.delete_session, actor_id, session_id
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return JSONResponse({"deleted": True, "session_id": session_id, "deleted_events": deleted_events})


# AgentCore SDK registers /invocations, /ping and /ws in __init__.
# Append UI, health, monitoring and static routes afterwards.
app.router.routes.extend([
    Route("/", index, methods=["GET"]),
    Route("/chat", chat, methods=["POST"]),
    Route("/api/sessions", list_sessions, methods=["GET"]),
    Route("/api/sessions/{session_id}", get_session, methods=["GET"]),
    Route("/api/sessions/{session_id}", rename_session, methods=["PATCH"]),
    Route("/api/sessions/{session_id}", delete_session, methods=["DELETE"]),
    Route("/api/change/assess", assess_change, methods=["POST"]),
    Route("/riskassessment", risk_assessment, methods=["POST"]),
    Route("/api/tables", tables, methods=["GET"]),
    Route("/health", monitoring.health, methods=["GET"]),
    Route("/api/monitor/dashboard", monitoring.get_dashboard, methods=["GET"]),
    Route("/api/monitor/metrics", monitoring.get_metrics, methods=["GET"]),
    Route("/api/monitor/endpoints", monitoring.get_endpoints, methods=["GET"]),
    Route("/api/monitor/invocations", monitoring.get_invocations, methods=["GET"]),
    Route("/favicon.ico", monitoring.favicon, methods=["GET"]),
    Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
])


if __name__ == "__main__":
    app.run(port=settings.APP_PORT, host=settings.APP_HOST)
