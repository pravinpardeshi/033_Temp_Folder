"""Standalone user-session and chat-history storage.

This module is intentionally independent of the existing application code. It
can be imported by the Lambda proxy, the AgentCore runtime, or a small test
script without changing ``lambda_function.py``, ``runtime_entrypoint.py``, or
the agents.

Two stores are used:

* ``SessionRepository``     - DynamoDB session metadata (title, timestamps,
  ownership) plus the transcript turns, so history works without Memory.
* ``AgentCoreMemoryStore``  - optional AgentCore Memory transcript (user/assistant turns).

``SessionMemoryService`` combines them into the operations the API needs:
create a session, save a turn, list sessions, load a transcript, rename, and
delete.

Configuration is via ``SessionMemoryConfig`` (environment variables by
default):

    AWS_REGION
    MEMORY_ID
    MEMORY_ENABLED
    SESSIONS_TABLE
    MEMORY_CONTEXT_TURNS
    SESSION_TITLE_MAX_CHARS
    SESSION_TITLE_RENAME_MAX_CHARS
    SESSION_TTL_DAYS
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def new_session_id() -> str:
    return str(uuid.uuid4())


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").split())


def title_from_message(message: str, max_chars: int = 60) -> str:
    """The first question becomes the session title."""
    text = normalize_text(message)
    if not text:
        return "New chat"
    return text[:max_chars].rstrip()


def sanitize_session_id(session_id: str) -> str:
    """AgentCore Memory session IDs are alphanumeric, max 100 characters."""
    cleaned = "".join(ch for ch in str(session_id or "") if ch.isalnum())
    return cleaned[:100] or "session"


def validate_actor_id(actor_id: str) -> str:
    """Basic guard for AgentCore actor IDs (no email/UPN characters)."""
    actor_id = str(actor_id or "").strip()
    if not actor_id:
        raise ValueError("actor_id is required")
    if "@" in actor_id or "." in actor_id:
        raise ValueError("actor_id must be the AD object ID, not a UPN/email")
    if len(actor_id) > 255:
        raise ValueError("actor_id exceeds 255 characters")
    return actor_id


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        return (
            datetime.fromtimestamp(value, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return str(value or "")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class SessionMemoryConfig:
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    memory_id: str = field(default_factory=lambda: os.environ.get("MEMORY_ID", ""))
    sessions_table: str = field(default_factory=lambda: os.environ.get("SESSIONS_TABLE", "ChatSessions"))
    endpoint_url: str = field(default_factory=lambda: os.environ.get("DYNAMODB_ENDPOINT_URL", ""))
    memory_enabled: bool = field(default_factory=lambda: _env_bool("MEMORY_ENABLED", True))
    context_turns: int = field(default_factory=lambda: _env_int("MEMORY_CONTEXT_TURNS", 10))
    title_max_chars: int = field(default_factory=lambda: _env_int("SESSION_TITLE_MAX_CHARS", 60))
    rename_max_chars: int = field(default_factory=lambda: _env_int("SESSION_TITLE_RENAME_MAX_CHARS", 100))
    session_ttl_days: int = field(default_factory=lambda: _env_int("SESSION_TTL_DAYS", 30))


# ---------------------------------------------------------------------------
# DynamoDB: session metadata
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    actor_id: str
    session_id: str
    title: str
    created_at: str
    updated_at: str
    last_message: str = ""
    agent: str = ""
    message_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_message": self.last_message,
            "agent": self.agent,
            "message_count": self.message_count,
        }


_MESSAGE_SK_PREFIX = "MESSAGE#"


def _message_sort_key(session_id: str, sequence: int) -> str:
    """Sortable message SK so transcripts read back in insertion order."""
    return f"{_MESSAGE_SK_PREFIX}{session_id}#{sequence:020d}#{uuid.uuid4().hex[:8]}"


class SessionRepository:
    """DynamoDB CRUD for session metadata and transcript.

    Table schema::

        PK = USER#<actor_id>
        SK = SESSION#<session_id>                  (metadata)
        SK = MESSAGE#<session_id>#<sequence>       (transcript turn)
    """

    def __init__(
        self,
        table_name: str,
        region: str,
        dynamodb_resource: Any = None,
        ttl_days: int = 30,
        endpoint_url: str = "",
    ):
        resource = dynamodb_resource or boto3.resource(
            "dynamodb",
            region_name=region,
            endpoint_url=endpoint_url or None,
        )
        self.table = resource.Table(table_name)
        self.ttl_days = ttl_days

    def _ttl_epoch(self) -> Optional[int]:
        """Epoch seconds when the row should expire; None disables TTL."""
        if self.ttl_days <= 0:
            return None
        return int(time.time()) + self.ttl_days * 86400

    @staticmethod
    def _keys(actor_id: str, session_id: str) -> Dict[str, str]:
        return {
            "PK": f"USER#{validate_actor_id(actor_id)}",
            "SK": f"SESSION#{session_id}",
        }

    @staticmethod
    def _from_item(item: Dict[str, Any]) -> SessionRecord:
        return SessionRecord(
            actor_id=str(item.get("PK", "")).removeprefix("USER#"),
            session_id=str(item.get("SK", "")).removeprefix("SESSION#"),
            title=item.get("title", ""),
            created_at=item.get("created_at", ""),
            updated_at=item.get("updated_at", ""),
            last_message=item.get("last_message", ""),
            agent=item.get("agent", ""),
            message_count=int(item.get("message_count", 0)),
        )

    def create(
        self,
        actor_id: str,
        session_id: str,
        title: str,
        agent: str = "",
    ) -> SessionRecord:
        now = utcnow_iso()
        item = {
            **self._keys(actor_id, session_id),
            "title": title,
            "created_at": now,
            "updated_at": now,
            "last_message": "",
            "agent": agent,
            "message_count": 0,
        }
        ttl = self._ttl_epoch()
        if ttl is not None:
            item["ttl"] = ttl
        self.table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(SK)",
        )
        return self._from_item(item)

    def get(self, actor_id: str, session_id: str) -> Optional[SessionRecord]:
        response = self.table.get_item(Key=self._keys(actor_id, session_id))
        item = response.get("Item")
        return self._from_item(item) if item else None

    def list(self, actor_id: str) -> List[SessionRecord]:
        kwargs = {
            "KeyConditionExpression": Key("PK").eq(
                f"USER#{validate_actor_id(actor_id)}"
            )
        }
        items: List[Dict[str, Any]] = []
        while True:
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

        records = [self._from_item(item) for item in items]
        records.sort(key=lambda record: record.updated_at, reverse=True)
        return records

    def touch(
        self,
        actor_id: str,
        session_id: str,
        last_message: str,
        agent: str,
    ) -> SessionRecord:
        now = utcnow_iso()
        update_expression = (
            "SET updated_at = :now, last_message = :last, agent = :agent"
        )
        values: Dict[str, Any] = {
            ":now": now,
            ":last": normalize_text(last_message)[:500],
            ":agent": agent,
            ":one": 1,
        }
        ttl = self._ttl_epoch()
        if ttl is not None:
            update_expression += ", ttl = :ttl"
            values[":ttl"] = ttl
        update_expression += " ADD message_count :one"
        response = self.table.update_item(
            Key=self._keys(actor_id, session_id),
            UpdateExpression=update_expression,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(SK)",
            ReturnValues="ALL_NEW",
        )
        return self._from_item(response["Attributes"])

    def rename(
        self,
        actor_id: str,
        session_id: str,
        title: str,
        max_chars: int = 100,
    ) -> SessionRecord:
        title = normalize_text(title)[:max_chars].rstrip()
        if not title:
            raise ValueError("title is required")
        response = self.table.update_item(
            Key=self._keys(actor_id, session_id),
            UpdateExpression="SET title = :title, updated_at = :now",
            ExpressionAttributeValues={
                ":title": title,
                ":now": utcnow_iso(),
            },
            ConditionExpression="attribute_exists(SK)",
            ReturnValues="ALL_NEW",
        )
        return self._from_item(response["Attributes"])

    def delete(self, actor_id: str, session_id: str) -> None:
        """Delete the transcript turns and the session metadata row."""
        self.delete_messages(actor_id, session_id)
        self.table.delete_item(Key=self._keys(actor_id, session_id))

    # -- transcript (stored in the same table) -----------------------------

    def append_message(
        self,
        actor_id: str,
        session_id: str,
        role: str,
        content: str,
        agent: str = "",
    ) -> None:
        """Append one chat turn to the DynamoDB transcript."""
        item = {
            "PK": f"USER#{validate_actor_id(actor_id)}",
            "SK": _message_sort_key(session_id, time.time_ns()),
            "session_id": session_id,
            "role": str(role or "user"),
            "content": str(content or "")[:4000],
            "agent": agent,
            "created_at": utcnow_iso(),
        }
        ttl = self._ttl_epoch()
        if ttl is not None:
            item["ttl"] = ttl
        self.table.put_item(Item=item)

    def _query_messages(self, actor_id: str, session_id: str) -> List[Dict[str, Any]]:
        kwargs = {
            "KeyConditionExpression": (
                Key("PK").eq(f"USER#{validate_actor_id(actor_id)}")
                & Key("SK").begins_with(f"{_MESSAGE_SK_PREFIX}{session_id}#")
            )
        }
        items: List[Dict[str, Any]] = []
        while True:
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

        items.sort(key=lambda item: str(item.get("SK", "")))
        return items

    def list_messages(
        self,
        actor_id: str,
        session_id: str,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return transcript turns, oldest first."""
        items = self._query_messages(actor_id, session_id)
        if limit:
            items = items[-limit:]
        return [
            {
                "role": item.get("role", ""),
                "content": item.get("content", ""),
                "agent": item.get("agent", ""),
                "timestamp": item.get("created_at", ""),
            }
            for item in items
        ]

    def delete_messages(self, actor_id: str, session_id: str) -> int:
        items = self._query_messages(actor_id, session_id)
        for item in items:
            self.table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        return len(items)


# ---------------------------------------------------------------------------
# AgentCore Memory: transcript
# ---------------------------------------------------------------------------

class AgentCoreMemoryStore:
    """Wrapper around the AgentCore Memory data plane."""

    def __init__(
        self,
        memory_id: str,
        region: str,
        enabled: bool = True,
        client: Any = None,
    ):
        self.memory_id = memory_id
        self.enabled = bool(enabled and memory_id)
        self._region = region
        self._client = client

    @property
    def client(self) -> Any:
        """Create the data-plane client lazily so Memory stays optional."""
        if self._client is None:
            self._client = boto3.client(
                "bedrock-agentcore", region_name=self._region
            )
        return self._client

    def save_turn(
        self,
        actor_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        agent_key: str = "",
    ) -> None:
        """Persist one user/assistant turn as a single event."""
        if not self.enabled:
            return

        payload = [
            {
                "conversational": {
                    "role": "USER",
                    "content": {"text": str(user_text)},
                }
            },
            {
                "conversational": {
                    "role": "ASSISTANT",
                    "content": {"text": str(assistant_text)},
                }
            },
        ]
        self.client.create_event(
            memoryId=self.memory_id,
            actorId=validate_actor_id(actor_id),
            sessionId=sanitize_session_id(session_id),
            eventTimestamp=datetime.now(timezone.utc),
            payload=payload,
            metadata={"agent": {"stringValue": agent_key}},
        )

    def list_messages(
        self,
        actor_id: str,
        session_id: str,
    ) -> List[Dict[str, Any]]:
        """Return the full transcript, oldest first."""
        if not self.enabled:
            return []

        messages: List[Dict[str, Any]] = []
        next_token: Optional[str] = None
        while True:
            kwargs = {
                "memoryId": self.memory_id,
                "actorId": validate_actor_id(actor_id),
                "sessionId": sanitize_session_id(session_id),
                "includePayloads": True,
                "maxResults": 100,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.client.list_events(**kwargs)

            for event in response.get("events", []):
                timestamp = _iso_timestamp(event.get("eventTimestamp"))
                for item in event.get("payload", []):
                    conversational = item.get("conversational") or {}
                    role = str(conversational.get("role", "")).lower()
                    content = conversational.get("content") or {}
                    text = content.get("text")
                    if text is None:
                        continue
                    messages.append({
                        "role": role or "other",
                        "content": str(text),
                        "timestamp": timestamp,
                    })

            next_token = response.get("nextToken")
            if not next_token:
                break

        return messages

    def recent_messages(
        self,
        actor_id: str,
        session_id: str,
        max_turns: int = 10,
    ) -> List[Dict[str, Any]]:
        """Return the most recent turns for prompt context."""
        messages = self.list_messages(actor_id, session_id)
        max_messages = max(0, int(max_turns)) * 2
        return messages[-max_messages:] if max_messages else []

    def delete_session(self, actor_id: str, session_id: str) -> int:
        """Delete all events for a session. Returns the deleted event count."""
        if not self.enabled:
            return 0

        event_ids: List[str] = []
        next_token: Optional[str] = None
        while True:
            kwargs = {
                "memoryId": self.memory_id,
                "actorId": validate_actor_id(actor_id),
                "sessionId": sanitize_session_id(session_id),
                "includePayloads": False,
                "maxResults": 100,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.client.list_events(**kwargs)
            event_ids.extend(
                event["eventId"]
                for event in response.get("events", [])
                if event.get("eventId")
            )
            next_token = response.get("nextToken")
            if not next_token:
                break

        for event_id in event_ids:
            self.client.delete_event(
                memoryId=self.memory_id,
                actorId=validate_actor_id(actor_id),
                sessionId=sanitize_session_id(session_id),
                eventId=event_id,
            )
        return len(event_ids)

    def list_session_ids(self, actor_id: str) -> List[str]:
        """List session IDs known to Memory for one actor."""
        if not self.enabled:
            return []

        session_ids: List[str] = []
        next_token: Optional[str] = None
        while True:
            kwargs = {
                "memoryId": self.memory_id,
                "actorId": validate_actor_id(actor_id),
                "maxResults": 100,
                "filter": {"eventFilter": "HAS_EVENTS"},
            }
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.client.list_sessions(**kwargs)
            session_ids.extend(
                item["sessionId"]
                for item in response.get("sessionSummaries", [])
                if item.get("sessionId")
            )
            next_token = response.get("nextToken")
            if not next_token:
                break
        return session_ids


# ---------------------------------------------------------------------------
# High-level service
# ---------------------------------------------------------------------------

class SessionMemoryService:
    """Use this class from the Lambda or runtime.

    The service assumes the caller has already authenticated the user and has
    derived ``actor_id`` from the validated SSO token (Entra object ID).
    """

    def __init__(
        self,
        config: Optional[SessionMemoryConfig] = None,
        repository: Optional[SessionRepository] = None,
        memory: Optional[AgentCoreMemoryStore] = None,
    ):
        self.config = config or SessionMemoryConfig()
        self.repository = repository or SessionRepository(
            self.config.sessions_table,
            self.config.region,
            ttl_days=self.config.session_ttl_days,
            endpoint_url=self.config.endpoint_url,
        )
        self.memory = memory or AgentCoreMemoryStore(
            self.config.memory_id,
            self.config.region,
            enabled=self.config.memory_enabled,
        )

    # -- writes ------------------------------------------------------------

    def save_turn(
        self,
        actor_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        agent_key: str = "",
    ) -> Dict[str, Any]:
        """Save a completed turn and create/touch session metadata.

        Call only after the agent has produced an answer, so failed requests do
        not create half-turns.
        """
        actor_id = validate_actor_id(actor_id)
        session_id = str(session_id or new_session_id())

        # AgentCore Memory is optional; DynamoDB is the durable transcript.
        try:
            self.memory.save_turn(
                actor_id=actor_id,
                session_id=session_id,
                user_text=user_text,
                assistant_text=assistant_text,
                agent_key=agent_key,
            )
        except Exception as exc:  # noqa: BLE001 - never break the chat turn
            print(f"AgentCore Memory save_turn failed: {exc}")

        record = self.repository.get(actor_id, session_id)
        if record is None:
            record = self.repository.create(
                actor_id=actor_id,
                session_id=session_id,
                title=title_from_message(
                    user_text, self.config.title_max_chars
                ),
                agent=agent_key,
            )

        self.repository.append_message(
            actor_id, session_id, "user", user_text, agent_key
        )
        self.repository.append_message(
            actor_id, session_id, "assistant", assistant_text, agent_key
        )

        return self.repository.touch(
            actor_id=actor_id,
            session_id=session_id,
            last_message=assistant_text or user_text,
            agent=agent_key,
        ).to_dict()

    def rename_session(
        self,
        actor_id: str,
        session_id: str,
        title: str,
    ) -> Dict[str, Any]:
        return self.repository.rename(
            actor_id,
            session_id,
            title,
            max_chars=self.config.rename_max_chars,
        ).to_dict()

    def delete_session(self, actor_id: str, session_id: str) -> int:
        """Delete metadata and Memory events. Idempotent."""
        deleted_events = self.memory.delete_session(actor_id, session_id)
        self.repository.delete(actor_id, session_id)
        return deleted_events

    # -- reads -------------------------------------------------------------

    def list_sessions(self, actor_id: str) -> List[Dict[str, Any]]:
        return [
            record.to_dict()
            for record in self.repository.list(actor_id)
        ]

    def get_session(
        self,
        actor_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        record = self.repository.get(actor_id, session_id)
        if record is None:
            return None
        messages = self.repository.list_messages(actor_id, session_id)
        if not messages:
            messages = self.memory.list_messages(actor_id, session_id)
        return {
            **record.to_dict(),
            "messages": messages,
        }

    def session_exists(self, actor_id: str, session_id: str) -> bool:
        """Lightweight check: does this actor own the session metadata row?"""
        return self.repository.get(actor_id, session_id) is not None

    def get_latest_session_id(self, actor_id: str) -> Optional[str]:
        """Most recently updated session id for this actor, or None."""
        records = self.repository.list(actor_id)
        return records[0].session_id if records else None

    def get_context(
        self,
        actor_id: str,
        session_id: str,
        max_turns: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return self.memory.recent_messages(
            actor_id,
            session_id,
            max_turns=max_turns or self.config.context_turns,
        )

    def get_context_text(
        self,
        actor_id: str,
        session_id: str,
        max_turns: Optional[int] = None,
    ) -> str:
        """Render recent memory as a prompt context block."""
        messages = self.get_context(actor_id, session_id, max_turns)
        lines = []
        for message in messages:
            role = str(message.get("role", "user")).title()
            lines.append(f"{role}: {message.get('content', '')}")
        return "\n".join(lines)


__all__ = [
    "AgentCoreMemoryStore",
    "SessionMemoryConfig",
    "SessionMemoryService",
    "SessionRecord",
    "SessionRepository",
    "new_session_id",
    "sanitize_session_id",
    "title_from_message",
    "utcnow_iso",
    "validate_actor_id",
]
