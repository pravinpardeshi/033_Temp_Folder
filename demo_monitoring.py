"""Synthetic monitoring data for the dashboard demo mode.

No AWS calls are made. Values vary slightly every refresh so the dashboard
feels live, while remaining internally consistent.
"""

import random
import time
from datetime import datetime, timedelta, timezone

from config import settings

_DEMO_START = time.time() - (4 * 3600 + 12 * 60)

_SAMPLE_QUERIES = [
    ("How was VPN error 809 resolved in past tickets?", "snow_agent", "success"),
    ("Show resolutions for database failover incidents.", "snow_agent", "success"),
    ("What is the standard fix for disk space alerts?", "snow_agent", "success"),
    ("New P1: checkout API returns 502 after a deploy.", "rca_agent", "success"),
    ("Payment service latency spiked 10x; users report timeouts.", "rca_agent", "success"),
    ("Nightly batch job fails with ORA-01555.", "rca_agent", "error"),
    ("CHG0030001", "change_qa_agent", "success"),
    ("CHG0030014", "change_qa_agent", "success"),
    ("How do I escalate a P1 incident?", "snow_agent", "success"),
    ("Why did the auth service restart at 03:00?", "rca_agent", "success"),
    ("Draft a short status update for an ongoing outage.", "snow_agent", "success"),
    ("CHG0030022", "change_qa_agent", "error"),
]


def _uptime() -> str:
    elapsed = int(time.time() - _DEMO_START)
    hours, rem = divmod(elapsed, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


def _endpoint(method, path, requests, avg_ms, p95_ms, errors, status=200):
    return {
        "method": method,
        "path": path,
        "requests": requests,
        "errors": errors,
        "error_rate": round((errors / requests * 100) if requests else 0, 2),
        "avg_latency_ms": round(avg_ms, 2),
        "min_latency_ms": round(max(1.0, avg_ms * 0.18), 2),
        "max_latency_ms": round(p95_ms * 1.6, 2),
        "p95_latency_ms": round(p95_ms, 2),
        "last_status": status,
        "last_called": (datetime.now(timezone.utc) - timedelta(seconds=random.randint(2, 180))).isoformat(),
    }


def _trend(now, rng):
    points = []
    for index in range(11, -1, -1):
        timestamp = now - timedelta(minutes=index * 5)
        points.append({
            "timestamp": timestamp.isoformat(),
            "requests": rng.randint(22, 48),
            "latency_ms": round(rng.uniform(1350, 2250), 2),
            "tokens": rng.randint(14000, 38000),
        })
    return points


def _invocations(now, rng):
    items = []
    for index in range(20):
        query, agent, status = _SAMPLE_QUERIES[index % len(_SAMPLE_QUERIES)]
        success = status == "success" and rng.random() > 0.06
        input_tokens = rng.randint(260, 920)
        output_tokens = rng.randint(90, 420)
        items.append({
            "timestamp": (now - timedelta(seconds=index * 47 + rng.randint(0, 30))).isoformat(),
            "status": "success" if success else "error",
            "query": query,
            "agent": agent,
            "tokens": input_tokens + output_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency": round(rng.uniform(1.1, 3.9), 2),
        })
    return items


def _aws_services(total_requests, rng):
    lambda_invocations = int(total_requests * 0.82)
    api_requests = int(total_requests * 0.79)
    bedrock_invocations = int(total_requests * 0.76)
    return [
        {
            "service": "AWS Lambda",
            "resource": "snow-agent (demo)",
            "metrics": [
                {"label": "Invocations", "value": lambda_invocations, "unit": "Count"},
                {"label": "Errors", "value": int(lambda_invocations * 0.011), "unit": "Count"},
                {"label": "Duration", "value": round(rng.uniform(1450, 2350), 1), "unit": "ms"},
                {"label": "Throttles", "value": rng.randint(0, 2), "unit": "Count"},
            ],
        },
        {
            "service": "Amazon API Gateway",
            "resource": "agentcore-api (demo)",
            "metrics": [
                {"label": "Requests", "value": api_requests, "unit": "Count"},
                {"label": "5XX errors", "value": int(api_requests * 0.007), "unit": "Count"},
                {"label": "Latency", "value": round(rng.uniform(160, 260), 1), "unit": "ms"},
                {"label": "Integration latency", "value": round(rng.uniform(140, 240), 1), "unit": "ms"},
            ],
        },
        {
            "service": "Amazon Bedrock",
            "resource": settings.LLM_MODEL_ID,
            "metrics": [
                {"label": "Invocations", "value": bedrock_invocations, "unit": "Count"},
                {"label": "Input tokens", "value": int(total_requests * 540), "unit": "Tokens"},
                {"label": "Output tokens", "value": int(total_requests * 190), "unit": "Tokens"},
                {"label": "Invocation latency", "value": round(rng.uniform(1250, 2050), 1), "unit": "ms"},
            ],
        },
    ]


def build_demo_dynatrace() -> dict:
    rng = random.Random(int(time.time() // 15))
    return {
        "enabled": True,
        "configured": True,
        "simulated": True,
        "app_name": "checkout-web (demo)",
        "url": "https://demo.live.dynatrace.com",
        "status": "healthy" if rng.random() > 0.2 else "degraded",
        "kpis": [
            {"label": "Availability", "value": round(99.92 + rng.uniform(0, 0.07), 2), "unit": "%"},
            {"label": "Throughput", "value": rng.randint(380, 640), "unit": "Count"},
            {"label": "Response time", "value": round(rng.uniform(180, 430), 1), "unit": "ms"},
            {"label": "Error rate", "value": round(rng.uniform(0.2, 1.5), 2), "unit": "%"},
        ],
        "extended": [
            {"label": "P95 response time", "value": round(rng.uniform(540, 1250), 1), "unit": "ms", "key": "p95_latency"},
            {"label": "Apdex", "value": round(rng.uniform(0.9, 0.99), 3), "unit": "score", "key": "apdex"},
            {"label": "Active sessions", "value": rng.randint(40, 95), "unit": "Count", "key": "sessions"},
            {"label": "Active users", "value": rng.randint(25, 70), "unit": "Count", "key": "users"},
            {"label": "Largest Contentful Paint", "value": round(rng.uniform(1400, 2600), 1), "unit": "ms", "key": "lcp"},
            {"label": "Actions with JS errors", "value": round(rng.uniform(0.1, 1.4), 2), "unit": "%", "key": "js_error_actions"},
        ],
        "message": "Demo mode uses synthetic Dynatrace metrics for another application.",
    }


def build_demo_dashboard() -> dict:
    now = datetime.now(timezone.utc)
    rng = random.Random(int(time.time() // 15))
    elapsed = max(1, int(time.time() - _DEMO_START))

    total_requests = 780 + int(elapsed / 2.4) + rng.randint(-12, 12)
    input_tokens = int(total_requests * rng.uniform(560, 680))
    output_tokens = int(total_requests * rng.uniform(185, 235))
    avg_latency = rng.uniform(1520, 1980)
    error_rate = rng.uniform(0.7, 1.7)
    active_sessions = rng.randint(6, 15)

    chat_requests = int(total_requests * 0.77)
    change_requests = int(total_requests * 0.11)
    table_requests = int(total_requests * 0.08)
    root_requests = max(1, total_requests - chat_requests - change_requests - table_requests)

    endpoints = [
        _endpoint("POST", "/chat", chat_requests, avg_latency, avg_latency * 2.15, int(chat_requests * error_rate / 100)),
        _endpoint("POST", "/api/change/assess", change_requests, 3080, 6200, int(change_requests * 1.8 / 100)),
        _endpoint("GET", "/api/tables", table_requests, 34, 92, 0),
        _endpoint("GET", "/", root_requests, 12, 31, 0),
    ]

    return {
        "mode": "demo",
        "source": "demo",
        "generated_at": now.isoformat(),
        "status": "healthy",
        "uptime": _uptime(),
        "stats": {
            "total_requests": total_requests,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "avg_latency_ms": round(avg_latency, 2),
            "error_rate": round(error_rate, 2),
            "active_sessions": active_sessions,
            "tokens_estimated": True,
            "token_source": "demo",
            "llm_calls": int(total_requests * 1.18),
            "invocations_with_usage": int(total_requests * 0.96),
            "invocations_with_estimates": int(total_requests * 0.04),
        },
        "endpoints": endpoints,
        "invocations": _invocations(now, rng),
        "trend": _trend(now, rng),
        "aws": {
            "enabled": True,
            "configured": True,
            "simulated": True,
            "region": settings.AWS_REGION,
            "lookback_minutes": settings.MONITOR_LOOKBACK_MINUTES,
            "message": "Demo mode uses synthetic application and CloudWatch metrics.",
            "services": _aws_services(total_requests, rng),
        },
        "dynatrace": build_demo_dynatrace(),
    }
