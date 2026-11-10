"""Lightweight in-process monitoring endpoints.

Provides the routes expected by the AgentCore monitoring dashboard:
    GET /health
    GET /api/monitor/metrics
    GET /api/monitor/invocations?limit=50
"""

import asyncio
import contextvars
import time
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from config import settings

_PROCESS_START = time.time()

metrics_store = {
    "total_requests": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_errors": 0,
    "latencies": [],
    "invocations": [],
    "active_sessions": set(),
    "bedrock_calls": 0,
    "bedrock_input_tokens": 0,
    "bedrock_output_tokens": 0,
    "bedrock_models": {},
    "invocations_with_usage": 0,
    "invocations_with_estimates": 0,
}

_TOKEN_CAPTURE = contextvars.ContextVar("bedrock_token_capture", default=None)

endpoint_store: dict = {}

_ENDPOINT_SAMPLE_LIMIT = 100
_IGNORED_ENDPOINT_PREFIXES = (
    "/api/monitor",
    "/static",
    "/ping",
    "/health",
    "/favicon.ico",
)


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


def start_token_capture():
    """Start collecting exact Bedrock usage for the current request context."""
    return _TOKEN_CAPTURE.set([])


def finish_token_capture(capture_token) -> dict:
    """Finish collection and return summed usage for the current request."""
    calls = _TOKEN_CAPTURE.get() or []
    try:
        _TOKEN_CAPTURE.reset(capture_token)
    except ValueError:
        pass

    models = sorted({
        call["model_id"]
        for call in calls
        if call.get("model_id")
    })
    return {
        "calls": len(calls),
        "input_tokens": sum(int(call.get("input_tokens", 0)) for call in calls),
        "output_tokens": sum(int(call.get("output_tokens", 0)) for call in calls),
        "models": models,
    }


def record_token_usage(
    model_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    agent_key: str | None = None,
    operation: str | None = None,
) -> None:
    """Record exact usage from a Bedrock LLM response.

    This updates lifetime totals and, when called inside an agent invocation,
    attaches the usage to that invocation via a context variable.
    """
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    model_id = model_id or "unknown"

    metrics_store["bedrock_calls"] += 1
    metrics_store["bedrock_input_tokens"] += input_tokens
    metrics_store["bedrock_output_tokens"] += output_tokens

    model_stats = metrics_store["bedrock_models"].setdefault(
        model_id, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    )
    model_stats["calls"] += 1
    model_stats["input_tokens"] += input_tokens
    model_stats["output_tokens"] += output_tokens

    capture = _TOKEN_CAPTURE.get()
    if capture is not None:
        capture.append({
            "model_id": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "agent_key": agent_key,
            "operation": operation,
        })


def record_invocation(
    status: str,
    query: str,
    latency: float,
    session_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_calls: int = 0,
    token_source: str = "estimated",
    models: list | None = None,
) -> None:
    """Record one agent invocation for the monitoring dashboard."""
    metrics_store["total_requests"] += 1
    if status == "error":
        metrics_store["total_errors"] += 1

    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    metrics_store["total_input_tokens"] += input_tokens
    metrics_store["total_output_tokens"] += output_tokens
    if token_source == "bedrock":
        metrics_store["invocations_with_usage"] += 1
    else:
        metrics_store["invocations_with_estimates"] += 1

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
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "llm_calls": max(0, int(llm_calls or 0)),
        "token_source": token_source,
        "models": list(models or []),
        "latency": round(latency, 2),
    })
    if len(metrics_store["invocations"]) > 50:
        metrics_store["invocations"] = metrics_store["invocations"][-50:]


def _should_track_endpoint(path: str) -> bool:
    return not any(path.startswith(prefix) for prefix in _IGNORED_ENDPOINT_PREFIXES)


def record_endpoint(method: str, path: str, status_code: int, latency: float) -> None:
    """Record one HTTP endpoint response for the monitoring dashboard."""
    if not _should_track_endpoint(path):
        return

    key = f"{(method or 'GET').upper()} {path}"
    entry = endpoint_store.get(key)
    if entry is None:
        entry = {
            "method": (method or "GET").upper(),
            "path": path,
            "requests": 0,
            "errors": 0,
            "total_latency": 0.0,
            "latencies": [],
            "last_status": None,
            "last_called": None,
        }
        endpoint_store[key] = entry

    latency_ms = max(0.0, latency * 1000)
    entry["requests"] += 1
    if status_code >= 400:
        entry["errors"] += 1
    entry["total_latency"] += latency_ms
    entry["latencies"].append(latency_ms)
    if len(entry["latencies"]) > _ENDPOINT_SAMPLE_LIMIT:
        entry["latencies"] = entry["latencies"][-_ENDPOINT_SAMPLE_LIMIT:]
    entry["last_status"] = status_code
    entry["last_called"] = datetime.now(timezone.utc).isoformat()


def _percentile(values: list, percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((percentile / 100) * len(ordered))) - 1))
    return ordered[index]


def _endpoint_snapshot() -> list:
    endpoints = []
    for entry in endpoint_store.values():
        latencies = entry["latencies"]
        requests = entry["requests"]
        endpoints.append({
            "method": entry["method"],
            "path": entry["path"],
            "requests": requests,
            "errors": entry["errors"],
            "error_rate": round((entry["errors"] / requests * 100) if requests else 0, 2),
            "avg_latency_ms": round(entry["total_latency"] / requests, 2) if requests else 0,
            "min_latency_ms": round(min(latencies), 2) if latencies else 0,
            "max_latency_ms": round(max(latencies), 2) if latencies else 0,
            "p95_latency_ms": round(_percentile(latencies, 95), 2),
            "last_status": entry["last_status"],
            "last_called": entry["last_called"],
        })
    return sorted(endpoints, key=lambda item: item["requests"], reverse=True)


def get_snapshot() -> dict:
    """Return a JSON-safe snapshot used by the dashboard and monitoring agent."""
    latencies = metrics_store["latencies"]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    requests = metrics_store["total_requests"]
    error_rate = (metrics_store["total_errors"] / requests * 100) if requests else 0

    return {
        "status": "healthy",
        "total_requests": requests,
        "total_tokens": metrics_store["total_input_tokens"] + metrics_store["total_output_tokens"],
        "input_tokens": metrics_store["total_input_tokens"],
        "output_tokens": metrics_store["total_output_tokens"],
        "tokens_estimated": metrics_store["bedrock_calls"] == 0,
        "token_source": "bedrock" if metrics_store["bedrock_calls"] else "estimated",
        "llm_calls": metrics_store["bedrock_calls"],
        "bedrock_input_tokens": metrics_store["bedrock_input_tokens"],
        "bedrock_output_tokens": metrics_store["bedrock_output_tokens"],
        "bedrock_models": metrics_store["bedrock_models"],
        "invocations_with_usage": metrics_store["invocations_with_usage"],
        "invocations_with_estimates": metrics_store["invocations_with_estimates"],
        "avg_latency_ms": round(avg_latency * 1000, 2),
        "error_rate": round(error_rate, 2),
        "active_sessions": len(metrics_store["active_sessions"]),
        "uptime": _format_uptime(),
        "endpoints": _endpoint_snapshot(),
    }


class MonitoringMiddleware:
    """ASGI middleware that records latency for every application endpoint."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            record_endpoint(
                scope.get("method", "GET"),
                scope.get("path", "/"),
                status_code,
                time.perf_counter() - start,
            )


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})


async def get_metrics(request: Request) -> JSONResponse:
    return JSONResponse(get_snapshot())


async def get_dashboard(request: Request) -> JSONResponse:
    """Return live dashboard data."""
    try:
        from live_monitoring import build_live_dashboard
        data = await asyncio.to_thread(build_live_dashboard)
    except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard
        return JSONResponse(
            {"mode": "live", "error": f"Could not build live dashboard: {exc}"},
            status_code=500,
        )

    return JSONResponse(data)


async def get_endpoints(request: Request) -> JSONResponse:
    return JSONResponse({"endpoints": _endpoint_snapshot()})


# Removed get_dynatrace endpoint



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
