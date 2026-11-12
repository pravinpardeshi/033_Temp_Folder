"""DynamoDB storage for SSO user profiles.

One item per user, keyed by the identity-provider subject (Entra object id):

    PK = USER#<user_id>
    SK = PROFILE

Attributes written by ``upsert``:
    email, display_name, tenant_id, roles, idp,
    created_at, updated_at, last_login_at

This is intentionally independent of the chat-session store so SSO can be
introduced without touching the agents. Point it at DynamoDB Local with
``DYNAMODB_ENDPOINT_URL`` (e.g. ``http://localhost:8000``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

from session_memory_store import utcnow_iso, validate_actor_id

PROFILE_SK = "PROFILE"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class SsoUserConfig:
    table_name: str = field(default_factory=lambda: _env("SSO_USERS_TABLE", "SsoUsers"))
    region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))
    endpoint_url: str = field(default_factory=lambda: _env("DYNAMODB_ENDPOINT_URL", ""))


class SsoUserRepository:
    """Create/read/update SSO user records."""

    def __init__(
        self,
        table_name: str,
        region: str,
        endpoint_url: str = "",
        dynamodb_resource: Any = None,
    ):
        resource = dynamodb_resource or boto3.resource(
            "dynamodb",
            region_name=region,
            endpoint_url=endpoint_url or None,
        )
        self.table = resource.Table(table_name)

    @staticmethod
    def _keys(user_id: str) -> Dict[str, str]:
        return {
            "PK": f"USER#{validate_actor_id(user_id)}",
            "SK": PROFILE_SK,
        }

    @staticmethod
    def _from_item(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "user_id": str(item.get("PK", "")).removeprefix("USER#"),
            "email": item.get("email", ""),
            "display_name": item.get("display_name", ""),
            "tenant_id": item.get("tenant_id", ""),
            "roles": list(item.get("roles") or []),
            "idp": item.get("idp", ""),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
            "last_login_at": item.get("last_login_at", ""),
        }

    def upsert(
        self,
        user_id: str,
        email: str = "",
        display_name: str = "",
        tenant_id: str = "",
        roles: Optional[List[str]] = None,
        idp: str = "entra",
    ) -> Dict[str, Any]:
        """Create the user on first login, or refresh their claims afterwards."""
        now = utcnow_iso()
        values: Dict[str, Any] = {
            ":email": str(email or ""),
            ":display_name": str(display_name or ""),
            ":tenant_id": str(tenant_id or ""),
            ":roles": list(roles or []),
            ":idp": idp,
            ":now": now,
            ":last_login": now,
        }
        response = self.table.update_item(
            Key=self._keys(user_id),
            UpdateExpression=(
                "SET email = :email, display_name = :display_name, "
                "tenant_id = :tenant_id, roles = :roles, idp = :idp, "
                "updated_at = :now, last_login_at = :last_login, "
                "created_at = if_not_exists(created_at, :now)"
            ),
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return self._from_item(response["Attributes"])

    def get(self, user_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key=self._keys(user_id))
        item = response.get("Item")
        return self._from_item(item) if item else None

    def touch_login(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Update last_login_at for an existing user."""
        try:
            response = self.table.update_item(
                Key=self._keys(user_id),
                UpdateExpression="SET last_login_at = :now, updated_at = :now",
                ExpressionAttributeValues={":now": utcnow_iso()},
                ConditionExpression="attribute_exists(SK)",
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # noqa: BLE001 - missing row is not an error here
            if "ConditionalCheckFailed" in exc.__class__.__name__ or "ConditionalCheckFailed" in str(exc):
                return None
            raise
        return self._from_item(response["Attributes"])

    def list(self, limit: int = 0) -> List[Dict[str, Any]]:
        """Return all user profiles (scan with pagination)."""
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {}
        while True:
            response = self.table.scan(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key or (limit and len(items) >= limit):
                break
            kwargs["ExclusiveStartKey"] = last_key
        items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        if limit:
            items = items[:limit]
        return [self._from_item(item) for item in items]

    def delete(self, user_id: str) -> None:
        self.table.delete_item(Key=self._keys(user_id))


class SsoUserService:
    """Thin service used by the SSO login callback."""

    def __init__(self, config: Optional[SsoUserConfig] = None):
        self.config = config or SsoUserConfig()
        self.repository = SsoUserRepository(
            self.config.table_name,
            self.config.region,
            endpoint_url=self.config.endpoint_url,
        )

    def record_login(
        self,
        user_id: str,
        email: str = "",
        display_name: str = "",
        tenant_id: str = "",
        roles: Optional[List[str]] = None,
        idp: str = "entra",
    ) -> Dict[str, Any]:
        return self.repository.upsert(
            user_id=user_id,
            email=email,
            display_name=display_name,
            tenant_id=tenant_id,
            roles=roles,
            idp=idp,
        )

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        return self.repository.get(user_id)

    def list_users(self, limit: int = 0) -> List[Dict[str, Any]]:
        return self.repository.list(limit=limit)


__all__ = [
    "SsoUserConfig",
    "SsoUserRepository",
    "SsoUserService",
]
