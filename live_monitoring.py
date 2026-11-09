"""Live monitoring data for the dashboard.

Application metrics come from the in-process collector in ``monitoring.py``.
AWS service metrics come from CloudWatch using the configured resource names.
"""

import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

import monitoring
from config import settings
from dynatrace_connector import build_dynatrace_feed

_CACHE = {"key": None, "expires_at": 0.0, "data": None}


def _trend_points(bucket_minutes: int = 5, limit: int = 12) -> list:
    now = datetime.now(timezone.utc)
    bucket_seconds = bucket_minutes * 60
    buckets = []
    for index in range(limit):
        timestamp = now - timedelta(seconds=(limit - 1 - index) * bucket_seconds)
        buckets.append({
            "timestamp": timestamp.isoformat(),
            "requests": 0,
            "latency_total": 0.0,
            "latency_count": 0,
            "tokens": 0,
        })

    for item in monitoring.metrics_store["invocations"]:
        try:
            timestamp = datetime.fromisoformat(item["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age_seconds = (now - timestamp).total_seconds()
        if age_seconds < 0 or age_seconds >= limit * bucket_seconds:
            continue
        age_index = int(age_seconds // bucket_seconds)
        index = limit - 1 - age_index
        bucket = buckets[index]
        bucket["requests"] += 1
        bucket["latency_total"] += float(item.get("latency") or 0) * 1000
        bucket["latency_count"] += 1
        bucket["tokens"] += int(item.get("tokens") or 0)

    points = []
    for bucket in buckets:
        count = bucket["latency_count"]
        points.append({
            "timestamp": bucket["timestamp"],
            "requests": bucket["requests"],
            "latency_ms": round(bucket["latency_total"] / count, 2) if count else 0,
            "tokens": bucket["tokens"],
        })
    return points


def _client_kwargs() -> dict:
    kwargs = {
        "region_name": settings.AWS_REGION,
        "config": Config(
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=3,
            read_timeout=8,
        ),
    }
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        if settings.AWS_SESSION_TOKEN:
            kwargs["aws_session_token"] = settings.AWS_SESSION_TOKEN
    return kwargs


def _credentials_source() -> str:
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        return "config.py"
    return "default AWS credential chain"


def _empty_aws(message: str, configured: bool) -> dict:
    return {
        "enabled": bool(settings.MONITOR_AWS_ENABLED),
        "configured": configured,
        "simulated": False,
        "region": settings.AWS_REGION,
        "lookback_minutes": settings.MONITOR_LOOKBACK_MINUTES,
        "credentials_source": _credentials_source(),
        "message": message,
        "services": [],
    }


def _metric_value(values, stat: str) -> float:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return 0.0
    if stat == "Average":
        return sum(numbers) / len(numbers)
    if stat == "Maximum":
        return max(numbers)
    if stat == "Minimum":
        return min(numbers)
    return sum(numbers)


def _format_metric(label: str, value: float, unit: str) -> dict:
    if unit in ("Count", "Tokens"):
        formatted = int(round(value))
    elif unit == "ms":
        formatted = round(value, 1)
    else:
        formatted = round(value, 2)
    return {"label": label, "value": formatted, "unit": unit}


def _cloudwatch_feed() -> dict:
    if not settings.MONITOR_AWS_ENABLED:
        return _empty_aws("AWS monitoring is disabled in config.py.", False)

    lookback = max(1, min(int(settings.MONITOR_LOOKBACK_MINUTES), 1440))
    lambda_name = settings.MONITOR_LAMBDA_FUNCTION_NAME.strip()
    api_type = (settings.MONITOR_API_GATEWAY_TYPE or "http").strip().lower()
    api_id = settings.MONITOR_API_GATEWAY_API_ID.strip()
    api_name = settings.MONITOR_API_GATEWAY_API_NAME.strip()
    api_stage = settings.MONITOR_API_GATEWAY_STAGE.strip()
    api_resource = api_name if api_type == "rest" else api_id
    bedrock_model = settings.MONITOR_BEDROCK_MODEL_ID.strip() or settings.LLM_MODEL_ID

    configured = bool(lambda_name or api_resource or settings.MONITOR_BEDROCK_MODEL_ID.strip())
    if not configured:
        return _empty_aws(
            "Set MONITOR_LAMBDA_FUNCTION_NAME, MONITOR_API_GATEWAY_API_ID "
            "(HTTP API), MONITOR_API_GATEWAY_API_NAME (REST API), or "
            "MONITOR_BEDROCK_MODEL_ID in config.py to enable AWS metrics.",
            False,
        )

    cache_seconds = max(0, int(settings.MONITOR_AWS_CACHE_SECONDS))
    cache_key = (
        settings.AWS_REGION,
        lookback,
        lambda_name,
        api_type,
        api_id,
        api_name,
        api_stage,
        bedrock_model,
        settings.MONITOR_BEDROCK_MODEL_ID.strip(),
    )
    now_monotonic = time.monotonic()
    if (
        cache_seconds
        and _CACHE["key"] == cache_key
        and _CACHE["data"] is not None
        and now_monotonic < _CACHE["expires_at"]
    ):
        return _CACHE["data"]

    period = max(60, min(300, lookback * 60 // 6 or 60))
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=lookback)
    queries = []
    metadata = []

    def add_query(service, resource, label, unit, namespace, metric, stat, dimensions):
        query_id = f"q{len(queries)}"
        queries.append({
            "Id": query_id,
            "Label": f"{service} {label}",
            "MetricStat": {
                "Metric": {
                    "Namespace": namespace,
                    "MetricName": metric,
                    "Dimensions": dimensions,
                },
                "Period": period,
                "Stat": stat,
            },
            "ReturnData": True,
        })
        metadata.append({
            "id": query_id,
            "service": service,
            "resource": resource,
            "label": label,
            "unit": unit,
            "stat": stat,
        })

    if lambda_name:
        dimensions = [{"Name": "FunctionName", "Value": lambda_name}]
        add_query("AWS Lambda", lambda_name, "Invocations", "Count", "AWS/Lambda", "Invocations", "Sum", dimensions)
        add_query("AWS Lambda", lambda_name, "Errors", "Count", "AWS/Lambda", "Errors", "Sum", dimensions)
        add_query("AWS Lambda", lambda_name, "Duration", "ms", "AWS/Lambda", "Duration", "Average", dimensions)
        add_query("AWS Lambda", lambda_name, "Throttles", "Count", "AWS/Lambda", "Throttles", "Sum", dimensions)

    if api_resource:
        is_rest = api_type == "rest"
        dimension_name = "ApiName" if is_rest else "ApiId"
        error_metric = "5XXError" if is_rest else "5xx"
        error_label = "5XX errors" if is_rest else "5xx errors"
        dimensions = [{"Name": dimension_name, "Value": api_resource}]
        if api_stage:
            dimensions.append({"Name": "Stage", "Value": api_stage})
        add_query("Amazon API Gateway", api_resource, "Requests", "Count", "AWS/ApiGateway", "Count", "Sum", dimensions)
        add_query("Amazon API Gateway", api_resource, error_label, "Count", "AWS/ApiGateway", error_metric, "Sum", dimensions)
        add_query("Amazon API Gateway", api_resource, "Latency", "ms", "AWS/ApiGateway", "Latency", "Average", dimensions)
        add_query("Amazon API Gateway", api_resource, "Integration latency", "ms", "AWS/ApiGateway", "IntegrationLatency", "Average", dimensions)

    if settings.MONITOR_BEDROCK_MODEL_ID.strip():
        dimensions = [{"Name": "ModelId", "Value": bedrock_model}]
        add_query("Amazon Bedrock", bedrock_model, "Invocations", "Count", "AWS/Bedrock", "Invocations", "Sum", dimensions)
        add_query("Amazon Bedrock", bedrock_model, "Input tokens", "Tokens", "AWS/Bedrock", "InputTokenCount", "Sum", dimensions)
        add_query("Amazon Bedrock", bedrock_model, "Output tokens", "Tokens", "AWS/Bedrock", "OutputTokenCount", "Sum", dimensions)
        add_query("Amazon Bedrock", bedrock_model, "Invocation latency", "ms", "AWS/Bedrock", "InvocationLatency", "Average", dimensions)

    try:
        response = boto3.client("cloudwatch", **_client_kwargs()).get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampDescending",
        )
    except Exception as exc:  # noqa: BLE001 - surfaced in the dashboard
        return _empty_aws(
            f"CloudWatch request failed: {exc}. Check AWS credentials and permissions in config.py.",
            True,
        )

    results = {result.get("Id"): result for result in response.get("MetricDataResults", [])}
    grouped = OrderedDict()

    for item in metadata:
        result = results.get(item["id"], {})
        value = _metric_value(result.get("Values") or [], item["stat"])
        metric = _format_metric(item["label"], value, item["unit"])
        key = (item["service"], item["resource"])
        grouped.setdefault(key, []).append(metric)

    services = []
    for (service, resource), metrics in grouped.items():
        by_label = {metric["label"]: metric["value"] for metric in metrics}
        if "Invocations" in by_label and "Errors" in by_label and by_label["Invocations"]:
            rate = by_label["Errors"] / by_label["Invocations"] * 100
            metrics.append(_format_metric("Error rate", rate, "%"))
        error_key = "5XX errors" if "5XX errors" in by_label else "5xx errors"
        if "Requests" in by_label and error_key in by_label and by_label["Requests"]:
            rate = by_label[error_key] / by_label["Requests"] * 100
            metrics.append(_format_metric("5xx rate", rate, "%"))
        services.append({
            "service": service,
            "resource": resource,
            "metrics": metrics,
        })

    incomplete = [
        result.get("Id")
        for result in response.get("MetricDataResults", [])
        if result.get("StatusCode") == "InternalError"
    ]
    message = f"CloudWatch metrics from the last {lookback} minutes."
    if incomplete:
        message += f" {len(incomplete)} metric queries returned partial data."

    data = {
        "enabled": True,
        "configured": True,
        "simulated": False,
        "region": settings.AWS_REGION,
        "lookback_minutes": lookback,
        "credentials_source": _credentials_source(),
        "message": message,
        "services": services,
    }

    if cache_seconds:
        _CACHE["key"] = cache_key
        _CACHE["data"] = data
        _CACHE["expires_at"] = now_monotonic + cache_seconds
    return data


def build_live_dashboard() -> dict:
    snapshot = monitoring.get_snapshot()
    invocations = list(reversed(monitoring.metrics_store["invocations"][-20:]))

    return {
        "mode": "live",
        "source": "live",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": snapshot.get("status", "healthy"),
        "uptime": snapshot.get("uptime", "0s"),
        "stats": {
            "total_requests": snapshot.get("total_requests", 0),
            "input_tokens": snapshot.get("input_tokens", 0),
            "output_tokens": snapshot.get("output_tokens", 0),
            "total_tokens": snapshot.get("total_tokens", 0),
            "avg_latency_ms": snapshot.get("avg_latency_ms", 0),
            "error_rate": snapshot.get("error_rate", 0),
            "active_sessions": snapshot.get("active_sessions", 0),
            "tokens_estimated": snapshot.get("tokens_estimated", True),
            "token_source": snapshot.get("token_source", "estimated"),
            "llm_calls": snapshot.get("llm_calls", 0),
            "bedrock_input_tokens": snapshot.get("bedrock_input_tokens", 0),
            "bedrock_output_tokens": snapshot.get("bedrock_output_tokens", 0),
            "bedrock_models": snapshot.get("bedrock_models", {}),
            "invocations_with_usage": snapshot.get("invocations_with_usage", 0),
            "invocations_with_estimates": snapshot.get("invocations_with_estimates", 0),
        },
        "endpoints": snapshot.get("endpoints", []),
        "invocations": invocations,
        "trend": _trend_points(),
        "aws": _cloudwatch_feed(),
        "dynatrace": build_dynatrace_feed(),
    }
