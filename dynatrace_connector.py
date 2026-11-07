"""
Dynatrace API Connector for Application Observability.

Provides a client to fetch health, performance, and error metrics from the
Dynatrace API (v2). Used by the Observability Dashboard to correlate
Dynatrace data with internal application metrics.
"""

import time
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from config import settings

class DynatraceConnector:
    """Client for interacting with the Dynatrace API v2.
    
    Assumes the use of an API Token with a scope including:
    - metrics.read
    - entities.read
    - services.read
    """

    def __init__(self):
        self.base_url = settings.DYNATRACE_URL.rstrip('/')
        self.api_token = settings.DYNATRACE_API_TOKEN
        self.headers = {
            "Authorization": f"Api-Token {self.api_token}",
            "Accept": "application/json; charset=utf-8"
        }


    def _fetch_metric(self, metric_id: str, timeframe: str = "LAST_HOUR", resolution: str = "5m") -> List[Dict]:
        """Fetches time-series data for a specific Dynatrace metric."""
        url = f"{self.base_url}/api/v2/metrics/query"
        params = {
            "metricSelector": metric_id,
            "timeframe": timeframe,
            "resolution": resolution
        }
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json().get("result", [])
        except Exception as e:
            print(f"Dynatrace API Error ({metric_id}): {e}")
            return []


    def get_health_summary(self) -> Dict[str, Any]:
        """Aggregates core KPIs for the application dashboard."""
        # Metric IDs are typically internal Dynatrace identifiers (e.g., builtin:apps.server.response_time)
        # In a real setup, these are mapped to the specific Application Entity ID.
        app_id = settings.DYNATRACE_APP_ID
        
        # Example metric selectors for an App entity
        metrics = {
            "availability": f"builtin:apps.server.availability,entityId({app_id})",
            "throughput": f"builtin:apps.server.requestCount,entityId({app_id})",
            "latency": f"builtin:apps.server.responseTime,entityId({app_id})",
            "errors": f"builtin:apps.server.errors,entityId({app_id})"
        }

        results = {}
        for key, selector in metrics.items():
            data = self._fetch_metric(selector)
            if data and "timeseries" in data[0]:
                # Take the most recent value
                values = data[0]["timeseries"][0].get("values", [])
                results[key] = values[-1] if values else 0
            else:
                results[key] = 0

        return {
            "status": "healthy" if results["availability"] > 99 else "degraded",
            "availability": results["availability"],
            "throughput": results["throughput"],
            "latency": results["latency"],
            "error_rate": results["errors"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


    def get_top_slow_endpoints(self, limit: int = 5) -> List[Dict]:
        """Retrieves the most latent services/endpoints."""
        url = f"{self.base_url}/api/v2/entities"
        params = {
            "entitySelector": "type(SERVICE),entityName.contains("AgentCore")",
            "fields": "entityName,builtin:service.response_time"
        }
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            resp.raise_for_status()
            entities = resp.json().get("entities", [])
            # Sort by latency
            sorted_entities = sorted(
                entities, 
                key=lambda x: x.get("properties", {}).get("builtin:service.response_time", 0), 
                reverse=True
            )
            return sorted_entities[:limit]
        except Exception as e:
            print(f"Dynatrace Entities Error: {e}")
            return []


    def get_error_distribution(self) -> Dict[str, Any]:
        """Fetches error breakdown by type (4xx vs 5xx)."""
        # This usually involves querying the 'builtin:apps.server.errors' with different dimensions
        # or querying the Event API for exception types.
        return {
            "4xx": 0, # Implementation requires specific metric filter
            "5xx": 0,
            "total_errors": 0
        }
