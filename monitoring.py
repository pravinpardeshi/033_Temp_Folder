"""Lightweight in-process monitoring endpoints.

Provides the routes expected by the AgentCore monitoring dashboard:
    GET /health
    GET /api/monitor/metrics
    GET /api/monitor/invocations?limit=50
"""

import time
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_PROCESS_START = time.time()

metrics_store = {
    "total_requests": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_errors": 0,
    "latencies": [],
    "invocations": [],
    "active_sessions": set(),
}


def estimate_tokens(text: str) -> int:
    """Rough fallback (~1 token per 4 chars) when usage metadata is unavailable."""
    return max(1, len(text or "") // 4)


def _format_uptime() -> str:
    elapsed = int(time.time() - _PROCESS_START)
    hours, rem = divmod(elapsed, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def record_invocation(
    status: str,
    query: str,
    latency: float,
    session_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Record one agent invocation for the monitoring dashboard."""
    metrics_store["total_requests"] += 1
    if status == "error":
        metrics_store["total_errors"] += 1

    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    metrics_store["total_input_tokens"] += input_tokens
    metrics_store["total_output_tokens"] += output_tokens

    metrics_store["latencies"].append(latency)
    if len(metrics_store["latencies"]) > 100:
        metrics_store["latencies"] = metrics_store["latencies"][-100:]

    if session_id:
        metrics_store["active_sessions"].add(session_id)
        if len(metrics_store["active_sessions"]) > 100:
            metrics_store["active_sessions"] = set(
                list(metrics_store["active_sessions"])[-100:]
            )

    metrics_store["invocations"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "query": (query or "")[:80],
        "tokens": input_tokens + output_tokens,
        "latency": round(latency, 2),
    })
    if len(metrics_store["invocations"]) > 50:
        metrics_store["invocations"] = metrics_store["invocations"][-50:]


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})


async def get_metrics(request: Request) -> JSONResponse:
    latencies = metrics_store["latencies"]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    requests = metrics_store["total_requests"]
    error_rate = (metrics_store["total_errors"] / requests * 100) if requests else 0

    return JSONResponse({
        "status": "healthy",
        "total_requests": requests,
        "total_tokens": metrics_store["total_input_tokens"] + metrics_store["total_output_tokens"],
        "input_tokens": metrics_store["total_input_tokens"],
        "output_tokens": metrics_store["total_output_tokens"],
        "avg_latency_ms": round(avg_latency * 1000, 2),
        "error_rate": round(error_rate, 2),
        "active_sessions": len(metrics_store["active_sessions"]),
        "uptime": _format_uptime(),
    })


async def get_invocations(request: Request) -> JSONResponse:
    try:
        limit = int(request.query_params.get("limit", 10))
    except ValueError:
        limit = 10
    limit = max(1, min(limit, 50))
    return JSONResponse({
        "invocations": list(reversed(metrics_store["invocations"][-limit:]))
    })


_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#5436da"/>'
    '<text x="16" y="22" font-family="Arial, sans-serif" font-size="14" '
    'font-weight="bold" fill="#ffffff" text-anchor="middle">AI</text></svg>'
)


async def favicon(request: Request) -> Response:
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")
