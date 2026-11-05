"""API Gateway proxy Lambda for the AgentCore runtime.

Deploy this file as the Lambda handler module (handler: ``lambda_function.handler``)
and give the function ``bedrock-agentcore:InvokeAgentRuntime`` permission on the
runtime ARN.
"""

import base64
import json
import os
import uuid

import boto3
from botocore.config import Config

client = boto3.client(
    "bedrock-agentcore",
    config=Config(retries={"max_attempts": 3, "mode": "standard"}),
)
RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]


def _json_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("CORS_ORIGIN", "*"),
        },
        "body": json.dumps(payload),
    }


def handler(event, context):
    try:
        body = _json_body(event)
        session_id = body.get("session_id") or str(uuid.uuid4())

        prompt = (
            body.get("message")
            or body.get("prompt")
            or body.get("change_number")
        )
        if not isinstance(prompt, str) or not prompt.strip():
            return _response(400, {"detail": "Missing 'message' or 'prompt'."})

        payload = {
            "prompt": prompt,
            "agent": body.get("agent"),
            "table_name": body.get("table_name") or body.get("table"),
            "change_number": body.get("change_number"),
        }

        response = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=session_id,
            qualifier=os.environ.get("AGENT_QUALIFIER", "DEFAULT"),
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )

        data = json.loads(response["response"].read() or b"{}")
        return _response(200, {
            "response": data.get("result", data),
            "session_id": session_id,
        })

    except Exception as exc:
        return _response(500, {"detail": str(exc)})
