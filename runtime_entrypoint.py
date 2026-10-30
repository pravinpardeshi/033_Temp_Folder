"""AgentCore Runtime entrypoint.

This is the file AWS Bedrock AgentCore Runtime executes:
    python runtime_entrypoint.py

It uses the official Amazon Bedrock AgentCore Python SDK
(``BedrockAgentCoreApp``) which provides:
    POST /invocations   - AgentCore invocation contract
    GET  /ping          - AgentCore health contract

The same app also serves the ChatGPT-style UI and the UI-friendly
``POST /chat`` route, so behavior is identical locally and in AWS.
"""

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


async def _run_agent(
    prompt: str,
    session_id: str,
    agent_key: str | None = None,
    table_name: str | None = None,
) -> str:
    """Execute the orchestrator and record monitoring metrics."""
    start = time.time()
    try:
        answer = await orchestrator.route_query(
            session_id, prompt, agent_key=agent_key, table_name=table_name
        )
        monitoring.record_invocation(
            status="success",
            query=prompt,
            latency=time.time() - start,
            session_id=session_id,
            input_tokens=monitoring.estimate_tokens(prompt),
            output_tokens=monitoring.estimate_tokens(answer),
        )
        return answer
    except Exception:
        monitoring.record_invocation(
            status="error",
            query=prompt,
            latency=time.time() - start,
            session_id=session_id,
            input_tokens=monitoring.estimate_tokens(prompt),
        )
        raise


@app.entrypoint
async def invoke(payload, context):
    """AgentCore invocation handler (POST /invocations).

    Expected payload:
        {"prompt": "...", "session_id": "optional"}
    """
    payload = payload or {}
    prompt = payload.get("prompt") or payload.get("message") or ""
    session_id = payload.get("session_id") or str(uuid.uuid4())
    agent_key = payload.get("agent")
    table_name = payload.get("table_name") or payload.get("table")

    if not str(prompt).strip():
        return {"error": "Missing 'prompt' in payload."}

    try:
        answer = await _run_agent(
            str(prompt), session_id, agent_key=agent_key, table_name=table_name
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {"result": answer, "session_id": session_id}


async def index(request: Request) -> FileResponse:
    """Serve the chatbot UI."""
    return FileResponse(STATIC_DIR / "index.html")


async def tables(request: Request) -> JSONResponse:
    """Incident tables the SNOW agent can query (used by the UI dropdown)."""
    return JSONResponse({
        "tables": settings.snow_tables,
        "default": settings.RDS_TABLE_NAME,
    })


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
        response_text = await _run_agent(
            str(message), session_id, agent_key=agent_key, table_name=table_name
        )
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

    return JSONResponse({"response": response_text, "session_id": session_id})


# AgentCore SDK registers /invocations, /ping and /ws in __init__.
# Append UI, health, monitoring and static routes afterwards.
app.router.routes.extend([
    Route("/", index, methods=["GET"]),
    Route("/chat", chat, methods=["POST"]),
    Route("/api/tables", tables, methods=["GET"]),
    Route("/health", monitoring.health, methods=["GET"]),
    Route("/api/monitor/metrics", monitoring.get_metrics, methods=["GET"]),
    Route("/api/monitor/invocations", monitoring.get_invocations, methods=["GET"]),
    Route("/favicon.ico", monitoring.favicon, methods=["GET"]),
    Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
])


if __name__ == "__main__":
    app.run(port=settings.APP_PORT, host=settings.APP_HOST)
