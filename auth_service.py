"""Small Mongo-backed identity and role service for the workspace application."""

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import Depends, Header, HTTPException, status

from mongo_client import mongo_client


ALL_PERMISSIONS = [
    "view_dashboard",
    "handle_conversations",
    "handle_calls",
    "view_customers",
    "manage_assigned_leads",
    "manage_leads",
    "manage_agents",
    "manage_data",
    "manage_integrations",
    "manage_automations",
    "view_analytics",
    "manage_team",
    "manage_roles",
    "manage_workspace",
]

DEFAULT_ROLES = [
    {"name": "owner", "label": "Owner", "description": "Full workspace access.", "permissions": ["*"]},
    {"name": "admin", "label": "Admin", "description": "Full workspace administration and access control.", "permissions": ["*"]},
    {"name": "manager", "label": "Manager", "description": "Run customer operations and review analytics.", "permissions": ["manage_agents", "manage_data", "manage_automations", "manage_leads", "view_customers", "view_analytics", "view_dashboard"]},
    {"name": "agent", "label": "Agent", "description": "Handle assigned conversations, calls, and leads.", "permissions": ["handle_conversations", "handle_calls", "manage_assigned_leads", "view_customers", "view_dashboard"]},
    {"name": "viewer", "label": "Viewer", "description": "Read-only dashboard and analytics access.", "permissions": ["view_dashboard", "view_analytics"]},
]


def _secret() -> bytes:
    configured = (os.getenv("AUTH_SECRET") or "").strip()
    environment = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "development").lower()
    if environment in {"production", "prod"} and (len(configured) < 32):
        raise RuntimeError("AUTH_SECRET must be set to at least 32 characters in production")
    return (configured or "agentflow-local-development-secret").encode("utf-8")


def _encode(value: Dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> Dict[str, Any]:
    padded = value + "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt_value = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_value.encode("utf-8"), 120_000)
    return f"{salt_value}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
        actual = hash_password(password, salt).split("$", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False


def ensure_roles() -> None:
    if not mongo_client.is_connected():
        return
    now = datetime.utcnow()
    for role in DEFAULT_ROLES:
        mongo_client.roles.update_one({"name": role["name"]}, {"$setOnInsert": {**role, "system": True, "created_at": now, "updated_at": now}}, upsert=True)


def role_document(role_name: str) -> Optional[Dict[str, Any]]:
    ensure_roles()
    return mongo_client.roles.find_one({"name": role_name.lower()})


def permissions_for(user: Dict[str, Any]) -> List[str]:
    role = role_document(str(user.get("role", "agent"))) or {}
    return list(dict.fromkeys((role.get("permissions") or []) + (user.get("permissions") or [])))


def has_permission(user: Dict[str, Any], permission: str) -> bool:
    permissions = permissions_for(user)
    return "*" in permissions or permission in permissions


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": str(user.get("_id")), "name": user.get("name", ""), "email": user.get("email", ""), "phone": user.get("phone", ""), "timezone": user.get("timezone", ""), "language": user.get("language", ""), "role": user.get("role", "agent"), "active": user.get("active", True), "workspace_id": user.get("workspace_id", "default"), "workspace_name": user.get("workspace_name", "AgentFlow workspace"), "permissions": permissions_for(user), "created_at": user.get("created_at")}


def issue_token(user: Dict[str, Any]) -> str:
    now = datetime.utcnow()
    payload = {"sub": str(user["_id"]), "workspace_id": user.get("workspace_id", "default"), "auth_version": int(user.get("auth_version", 0)), "iat": int(now.timestamp()), "exp": int((now + timedelta(days=7)).timestamp())}
    body = _encode(payload)
    signature = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def user_from_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = _decode(body)
        if int(payload.get("exp", 0)) < int(datetime.utcnow().timestamp()):
            return None
        user = mongo_client.users.find_one({"_id": __import__("bson").ObjectId(payload["sub"]), "active": {"$ne": False}})
        if user and int(user.get("auth_version", 0)) != int(payload.get("auth_version", 0)):
            return None
        return user
    except Exception:
        return None


def require_user(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in is required")
    user = user_from_token(authorization.split(" ", 1)[1].strip())
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Your session has expired")
    return user


def require_permission(permission: str):
    def dependency(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        if not has_permission(user, permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {permission}")
        return user
    return dependency


def invitation_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
