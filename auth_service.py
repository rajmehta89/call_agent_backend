"""Small Mongo-backed identity and role service for the workspace application."""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import Depends, Header, HTTPException, status

from mongo_client import mongo_client

logger = logging.getLogger(__name__)


ALL_PERMISSIONS = [
    "view_dashboard",
    "handle_conversations",
    "assign_conversations",
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
    "export_data",
]

# Two roles for a team, plus the owner who created the workspace:
#   admin        everything, including Team settings; assigns inbox conversations, never receives them
#   team member  works the inbox: assigned conversations, @mentions, team channel; calls and their leads
# ("agent" is the team member role's internal name; the owner is an admin who cannot be removed.)
DEFAULT_ROLES = [
    {"name": "owner", "label": "Owner", "description": "Created the workspace. Same access as an admin and cannot be removed.", "permissions": ["*"]},
    {"name": "admin", "label": "Admin", "description": "Full access: team, settings and the whole inbox. Assigns conversations to team members.", "permissions": ["*"]},
    {"name": "agent", "label": "Team member", "description": "Works the inbox: conversations assigned to them, @mentions and the team channel, plus calls and their leads.",
     "permissions": ["view_dashboard", "handle_conversations", "handle_calls", "manage_assigned_leads", "view_customers"]},
]
TEAM_MEMBER_ROLE = "agent"
# Roles that earlier versions created and that are gone now; anyone still holding one becomes a team member.
RETIRED_ROLES = ("manager", "viewer")
# Bump when DEFAULT_ROLES changes. Built-in roles saved under an older version are upgraded once;
# an admin's later edits to a role are kept because it then carries the current version.
ROLE_DEFAULTS_VERSION = 3


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


_roles_ready = False


def ensure_roles() -> None:
    """Create the built-in roles, and upgrade ones saved by an older version of DEFAULT_ROLES. Runs once per process."""
    global _roles_ready
    if _roles_ready or not mongo_client.is_connected():
        return
    now = datetime.utcnow()
    for role in DEFAULT_ROLES:
        mongo_client.roles.update_one({"name": role["name"]}, {"$setOnInsert": {**role, "system": True, "defaults_version": ROLE_DEFAULTS_VERSION, "created_at": now, "updated_at": now}}, upsert=True)
        upgraded = mongo_client.roles.update_one(
            {"name": role["name"], "system": True, "$or": [{"defaults_version": {"$exists": False}}, {"defaults_version": {"$lt": ROLE_DEFAULTS_VERSION}}]},
            {"$set": {"label": role["label"], "description": role["description"], "permissions": role["permissions"], "defaults_version": ROLE_DEFAULTS_VERSION, "updated_at": now}},
        )
        if upgraded.modified_count:
            logger.info("roles: upgraded built-in role %s to defaults v%d: %s", role["name"], ROLE_DEFAULTS_VERSION, role["permissions"])
    for retired in RETIRED_ROLES:
        moved = mongo_client.users.update_many({"role": retired}, {"$set": {"role": TEAM_MEMBER_ROLE, "updated_at": now}, "$inc": {"auth_version": 1}})
        pending = mongo_client.invitations.update_many({"role": retired, "status": "pending"}, {"$set": {"role": TEAM_MEMBER_ROLE, "updated_at": now}})
        removed = mongo_client.roles.delete_one({"name": retired, "system": True})
        if moved.modified_count or pending.modified_count or removed.deleted_count:
            logger.info("roles: retired %s (%d users and %d invites moved to team member)", retired, moved.modified_count, pending.modified_count)
    _roles_ready = True


def can_assign_conversations(user: Dict[str, Any]) -> bool:
    """Owners and admins hand out inbox conversations; they are never assigned one."""
    return has_permission(user, "assign_conversations")


def role_document(role_name: str) -> Optional[Dict[str, Any]]:
    ensure_roles()
    return mongo_client.roles.find_one({"name": role_name.lower()})


def permissions_for(user: Dict[str, Any]) -> List[str]:
    role_name = str(user.get("role", "agent")).strip().lower()
    role = role_document(role_name) or {}
    configured = list(role.get("permissions") or []) + list(user.get("permissions") or [])
    # Owner/Admin are built-in full-access roles. Keep that invariant even when an older
    # deployment has not created or refreshed its built-in role documents yet.
    if role_name in {"owner", "admin"}:
        configured.insert(0, "*")
    return list(dict.fromkeys(configured))


def has_permission(user: Dict[str, Any], permission: str) -> bool:
    permissions = permissions_for(user)
    return "*" in permissions or permission in permissions


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": str(user.get("_id")), "name": user.get("name", ""), "email": user.get("email", ""), "phone": user.get("phone", ""), "timezone": user.get("timezone", ""), "language": user.get("language", ""), "role": user.get("role", "agent"), "active": user.get("active", True), "workspace_id": user.get("workspace_id", "default"), "workspace_name": user.get("workspace_name", "AgentFlow workspace"), "permissions": permissions_for(user), "created_at": user.get("created_at")}


def issue_token(user: Dict[str, Any]) -> str:
    now = datetime.utcnow()
    # The browser/PWA stores this session locally so reopening the installed app does not
    # require a password every time. Keep the lifetime configurable for deployments that
    # need a shorter security window.
    try:
        ttl_days = max(1, min(365, int(os.getenv("AUTH_TOKEN_TTL_DAYS", "30"))))
    except ValueError:
        ttl_days = 30
    payload = {"sub": str(user["_id"]), "workspace_id": user.get("workspace_id", "default"), "auth_version": int(user.get("auth_version", 0)), "iat": int(now.timestamp()), "exp": int((now + timedelta(days=ttl_days)).timestamp())}
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
