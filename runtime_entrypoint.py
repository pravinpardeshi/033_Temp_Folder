import time
import uuid
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import monitoring
from config import settings
from orchestrator import orchestrator

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = BedrockAgentCoreApp()
app.add_middleware(monitoring.MonitoringMiddleware)


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
        {"agent": "change_qa_agent", "prompt": "CHG0030001"}
    """
    payload = payload or {}
    change_number = payload.get("change_number")
    prompt = (payload.get("prompt") or payload.get("message") or payload.get("change_number")  or "")
    session_id = payload.get("session_id") or str(uuid.uuid4())
    agent_key = payload.get("agent")

    if change_number and not agent_key:
        agent_key = "change_qa_agent"
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
        assessment = await _run_agent(change_number, session_id, agent_key="change_qa_agent")
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

    return JSONResponse({ "change_number": change_number, "assessment": assessment, })


async def chat(request: Request) -> JSONResponse:
    """UI-facing chat endpoint (kept separate from the AgentCore contract)."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON body."}, status_code=400)

    message = payload.get("message") or payload.get("prompt") or ""
    session_id = payload.get("session_id") or str(uuid.uuid4())
    agent_key = payload.get("agent")
    table_name = payload.get("table_name") or payload.get("table")

    if not str(message).strip():
        return JSONResponse({"detail": "Missing 'message'."}, status_code=400)

    try:
        response_text = await _run_agent(str(message), session_id, agent_key=agent_key, table_name=table_name)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

    return JSONResponse({"response": response_text, "session_id": session_id})


# AgentCore SDK registers /invocations, /ping and /ws in __init__.
# Append UI, health, monitoring and static routes afterwards.
app.router.routes.extend([
    Route("/", index, methods=["GET"]),
    Route("/chat", chat, methods=["POST"]),
    Route("/api/change/assess", assess_change, methods=["POST"]),
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
