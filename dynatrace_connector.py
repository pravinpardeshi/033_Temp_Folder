"""
Dynatrace API connector for application observability.

Fetches health, performance, and error metrics for ANOTHER application from
the Dynatrace API v2 and shapes them into the dashboard "observability" feed.

Dynatrace API notes:
    GET /api/v2/metrics/query
        metricSelector  comma-separated metric ids (max 10 per call)
        entitySelector  scopes every metric to one entity: entityId("APP-123")
        from / resolution   e.g. from=now-1h, resolution=5m
    Authorization header:  Api-Token <token>   (scopes: metrics.read,
    entities.read, services.read)

Only the Python standard library is used (urllib), so no extra dependency is
required in the deployment package.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import settings

# Golden-signal KPIs for an APPLICATION entity (see Dynatrace_Observability.md).
_KPI_METRICS = {
    "availability": "builtin:apps.server.availability",
    "throughput": "builtin:apps.server.requestCount",
    "latency": "builtin:apps.server.responseTime",
    "errors": "builtin:apps.server.errors",
}

# Extended KPIs, queried in a SECOND batch so a failure or invalid key here
# never breaks the golden signals. All selectors collapse dimensions with
# :merge(0) so the metric resolves to a single value. Web-RUM metrics
# (builtin:apps.web.*) return no data for non-web applications and are then
# omitted from the feed. (key, metric selector, label, unit)
_EXTENDED_METRICS = [
    (
        "p95_latency",
        "builtin:apps.server.responseTime:merge(0):percentile(95)",
        "P95 response time",
        "ms",
    ),
    (
        "apdex",
        "builtin:apps.web.apdex.userType:merge(0)",
        "Apdex",
        "score",
    ),
    (
        "sessions",
        "builtin:apps.web.activeSessions:merge(0)",
        "Active sessions",
        "Count",
    ),
    (
        "users",
        "builtin:apps.web.activeUsersEst:merge(0)",
        "Active users",
        "Count",
    ),
    (
        "lcp",
        "builtin:apps.web.largestContentfulPaint.load.browser:merge(0)",
        "Largest Contentful Paint",
        "ms",
    ),
    (
        "js_error_actions",
        "builtin:apps.web.action.percentageOfUserActionsAffectedByErrors:merge(0)",
        "Actions with JS errors",
        "%",
    ),
]

# Extended KPIs, queried in a SECOND batch so a failure or invalid key here
# never breaks the golden signals. Each selector collapses dimensions with
# :merge(0) so the metric resolves to a single value. Web-RUM metrics
# (builtin:apps.web.*) return no data for non-web applications and are then
# omitted from the feed. (key, metric selector, label, unit)
_EXTENDED_METRICS = [
    (
        "p95_latency",
        "builtin:apps.server.responseTime:merge(0):percentile(95)",
        "P95 response time",
        "ms",
    ),
    (
        "apdex",
        "builtin:apps.web.apdex.userType:merge(0)",
        "Apdex",
        "score",
    ),
    (
        "sessions",
        "builtin:apps.web.activeSessions:merge(0)",
        "Active sessions",
        "Count",
    ),
    (
        "users",
        "builtin:apps.web.activeUsersEst:merge(0)",
        "Active users",
        "Count",
    ),
    (
        "lcp",
        "builtin:apps.web.largestContentfulPaint.load.browser:merge(0)",
        "Largest Contentful Paint",
        "ms",
    ),
    (
        "js_error_actions",
        "builtin:apps.web.action.percentageOfUserActionsAffectedByErrors:merge(0)",
        "Actions with JS errors",
        "%",
    ),
]

_CACHE: Dict[str, Any] = {"key": None, "expires_at": 0.0, "data": None}


class DynatraceConnector:
    """Client for the Dynatrace API v2."""

    def __init__(self) -> None:
        self.base_url = (settings.DYNATRACE_URL or "").strip().rstrip("/")
        self.api_token = (settings.DYNATRACE_API_TOKEN or "").strip()
        self.app_id = (settings.DYNATRACE_APP_ID or "").strip()
        self.app_name = (
            (settings.DYNATRACE_APP_NAME or "").strip()
            or self.app_id
            or "Monitored application"
        )
        self.timeout = 10

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_token and self.app_id)

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Api-Token {self.api_token}",
            "Accept": "application/json; charset=utf-8",
        }

    def _get(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers=self.headers,
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _find_value(
        payload: Dict[str, Any],
        metric_id: str,
        fallback_index: Optional[int] = None,
    ) -> Optional[float]:
        """Most recent non-null point of a metric, or None when there is no data."""
        results = payload.get("result") or []
        matched = [r for r in results if r.get("metricId") == metric_id]
        if not matched and fallback_index is not None and fallback_index < len(results):
            matched = [results[fallback_index]]
        for result in matched:
            for data in result.get("data") or []:
                for series in data.get("timeseries") or []:
                    values = [v for v in series.get("values") or [] if v is not None]
                    if values:
                        return float(values[-1])
        return None

    @classmethod
    def _last_value(
        cls,
        payload: Dict[str, Any],
        metric_id: str,
        fallback_index: Optional[int] = None,
    ) -> float:
        """Most recent non-null point of a metric; 0.0 when the metric has no data."""
        value = cls._find_value(payload, metric_id, fallback_index)
        return 0.0 if value is None else value

    def get_health_summary(self) -> Dict[str, Any]:
        """Aggregate the golden-signal KPIs for the monitored application.

        All four metrics are fetched in a single metrics/query call.
        Raises on transport/API errors so callers can cache the failure.
        """
        if not self.configured:
            raise RuntimeError("Dynatrace connector is not configured")

        selector = ",".join(_KPI_METRICS.values())
        payload = self._get(
            "/api/v2/metrics/query",
            {
                "metricSelector": selector,
                "entitySelector": f'entityId("{self.app_id}")',
                "from": "now-1h",
                "resolution": "5m",
            },
        )

        values: Dict[str, float] = {}
        for index, (key, metric_id) in enumerate(_KPI_METRICS.items()):
            values[key] = self._last_value(payload, metric_id, fallback_index=index)

        requests_count = values["throughput"]
        errors = values["errors"]
        error_rate = (errors / requests_count * 100) if requests_count else 0.0
        availability = values["availability"]

        if availability >= 99 and error_rate < 2:
            status = "healthy"
        elif availability >= 90:
            status = "degraded"
        else:
            status = "unavailable"

        return {
            "configured": True,
            "status": status,
            "availability": round(availability, 2),
            "throughput": int(requests_count),
            "latency": round(values["latency"], 1),
            "error_rate": round(error_rate, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def get_extended_metrics(self) -> List[Dict[str, Any]]:
        """Extended KPIs (P95, Apdex, sessions, users, web vitals, JS errors).

        Fetched in one metrics/query call, separate from the golden signals
        so an unsupported key only affects this group. Metrics without data
        in the window are omitted rather than reported as zero.
        """
        if not self.configured:
            return []
        selector = ",".join(m[1] for m in _EXTENDED_METRICS)
        payload = self._get(
            "/api/v2/metrics/query",
            {
                "metricSelector": selector,
                "entitySelector": f'entityId("{self.app_id}")',
                "from": "now-1h",
                "resolution": "5m",
            },
        )

        kpis: List[Dict[str, Any]] = []
        for index, (key, metric_id, label, unit) in enumerate(_EXTENDED_METRICS):
            value = self._find_value(payload, metric_id, fallback_index=index)
            if value is None:
                continue
            if unit == "ms":
                value = round(value, 1)
            elif unit == "%":
                value = round(value, 2)
            elif unit == "score":
                value = round(value, 3)
            kpis.append({"label": label, "value": value, "unit": unit, "key": key})
        return kpis

    def get_extended_metrics(self) -> List[Dict[str, Any]]:
        """Extended KPIs (P95, Apdex, sessions, users, web vitals, JS errors).

        Fetched in one metrics/query call, separate from the golden signals
        so an unsupported key only affects this group. Metrics without data
        in the window are omitted rather than reported as zero.
        """
        if not self.configured:
            return []
        selector = ",".join(m[1] for m in _EXTENDED_METRICS)
        payload = self._get(
            "/api/v2/metrics/query",
            {
                "metricSelector": selector,
                "entitySelector": f'entityId("{self.app_id}")',
                "from": "now-1h",
                "resolution": "5m",
            },
        )

        kpis: List[Dict[str, Any]] = []
        for index, (key, metric_id, label, unit) in enumerate(_EXTENDED_METRICS):
            value = self._find_value(payload, metric_id, fallback_index=index)
            if value is None:
                continue
            if unit == "ms":
                value = round(value, 1)
            elif unit == "%":
                value = round(value, 2)
            elif unit == "score":
                value = round(value, 3)
            kpis.append({"label": label, "value": value, "unit": unit, "key": key})
        return kpis
        """Top matching services from the Dynatrace entities API."""
        if not self.configured:
            return []
        payload = self._get(
            "/api/v2/entities",
            {
                "entitySelector": 'type(SERVICE),entityName.contains("AgentCore")',
                "fields": "entityId,entityName",
            },
        )
        entities = payload.get("entities") or []
        return [
            {
                "entity_id": entity.get("entityId", ""),
                "name": entity.get("displayName", ""),
            }
            for entity in entities[:limit]
        ]

    def get_error_distribution(self) -> Dict[str, Any]:
        """4xx vs 5xx split; requires per-status metric selectors to populate."""
        return {"4xx": 0, "5xx": 0, "total_errors": 0}


def build_dynatrace_feed() -> Dict[str, Any]:
    """Build the dashboard feed for the monitored (other) application.

    Always returns a JSON-safe payload; failures degrade to a message instead
    of raising so the dashboard keeps rendering.
    """
    connector = DynatraceConnector()
    base: Dict[str, Any] = {
        "enabled": bool(settings.DYNATRACE_ENABLED),
        "configured": connector.configured,
        "simulated": False,
        "app_name": connector.app_name,
        "url": connector.base_url,
    }

    if not settings.DYNATRACE_ENABLED:
        return {
            **base,
            "configured": False,
            "status": "disabled",
            "kpis": [],
            "extended": [],
            "message": "Dynatrace monitoring is disabled (DYNATRACE_ENABLED=false).",
        }

    if not connector.configured:
        return {
            **base,
            "status": "unconfigured",
            "kpis": [],
            "extended": [],
            "message": (
                "Set DYNATRACE_URL, DYNATRACE_API_TOKEN and DYNATRACE_APP_ID "
                "to monitor another application via Dynatrace."
            ),
        }

    cache_seconds = max(0, int(settings.DYNATRACE_CACHE_SECONDS))
    now = time.monotonic()
    cache_key = f"{connector.base_url}|{connector.app_id}"
    if (
        cache_seconds
        and _CACHE["key"] == cache_key
        and _CACHE["data"] is not None
        and now < _CACHE["expires_at"]
    ):
        return _CACHE["data"]

    try:
        summary = connector.get_health_summary()
    except Exception as exc:  # noqa: BLE001 - surfaced in the feed message
        data = {
            **base,
            "status": "unavailable",
            "kpis": [],
            "extended": [],
            "message": f"Dynatrace request failed: {exc}",
        }
        # Brief negative cache so a broken endpoint is not hammered.
        _CACHE["key"] = cache_key
        _CACHE["data"] = data
        _CACHE["expires_at"] = now + min(cache_seconds, 30)
        return data

    # Extended metrics are best-effort: core KPIs already succeeded above.
    extended: List[Dict[str, Any]] = []
    message = "Metrics from the last hour (resolution 5m)."
    try:
        extended = connector.get_extended_metrics()
    except Exception as exc:  # noqa: BLE001 - note it, keep the golden signals
        message += f" Extended metrics unavailable: {exc}"

    data = {
        **base,
        "status": summary["status"],
        "kpis": [
            {"label": "Availability", "value": summary["availability"], "unit": "%"},
            {"label": "Throughput", "value": summary["throughput"], "unit": "Count"},
            {"label": "Response time", "value": summary["latency"], "unit": "ms"},
            {"label": "Error rate", "value": summary["error_rate"], "unit": "%"},
        ],
        "extended": extended,
        "message": message,
        "timestamp": summary["timestamp"],
    }

    if cache_seconds:
        _CACHE["key"] = cache_key
        _CACHE["data"] = data
        _CACHE["expires_at"] = now + cache_seconds
    return data
