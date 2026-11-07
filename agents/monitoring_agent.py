"""Application monitoring agent.

Answers operational questions about the running application using the
in-process metrics collected by ``monitoring.py``. The same snapshot powers
the UI dashboard, so the agent and dashboard always agree.
"""

import monitoring
from .base import BaseAgent


class MonitoringAgent(BaseAgent):
    """Summarize application health, token usage, and endpoint latency."""

    async def invoke(
        self,
        session_id: str,
        prompt: str,
        table_name: str | None = None,
    ) -> str:
        snapshot = monitoring.get_snapshot()
        endpoints = snapshot.get("endpoints", [])

        lines = [
            "## Application health",
            f"- Status: **{snapshot['status']}**",
            f"- Uptime: **{snapshot['uptime']}**",
            f"- Agent requests: **{snapshot['total_requests']}**",
            f"- Error rate: **{snapshot['error_rate']}%**",
            f"- Active sessions: **{snapshot['active_sessions']}**",
            "",
            "## Token usage",
            f"- Source: **{snapshot.get('token_source', 'estimated')}**",
            f"- Bedrock LLM calls: **{snapshot.get('llm_calls', 0)}**",
            f"- Input: **{snapshot['input_tokens']}**",
            f"- Output: **{snapshot['output_tokens']}**",
            f"- Total: **{snapshot['total_tokens']}**",
            "",
            "## Agent latency",
            f"- Average: **{snapshot['avg_latency_ms']} ms**",
        ]

        if endpoints:
            lines.extend([
                "",
                "## API endpoints",
                "| Method | Path | Requests | Avg | P95 | Errors |",
                "| --- | --- | ---: | ---: | ---: | ---: |",
            ])
            for endpoint in endpoints:
                lines.append(
                    f"| {endpoint['method']} | `{endpoint['path']}` | "
                    f"{endpoint['requests']} | {endpoint['avg_latency_ms']} ms | "
                    f"{endpoint['p95_latency_ms']} ms | {endpoint['errors']} |"
                )
        else:
            lines.extend(["", "No API endpoint samples have been recorded yet."])

        lines.extend([
            "",
            "_Metrics are kept in process memory and reset when the runtime restarts. "
            "Live token counts come from Bedrock response usage metadata when available; "
            "otherwise they are estimated from prompt and response length._",
        ])
        return "\n".join(lines)
