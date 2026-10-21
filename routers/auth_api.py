from datetime import datetime, timedelta
import hmac
import logging
import os
import re
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from auth_service import ensure_roles, hash_password, invitation_hash, issue_token, public_user, require_user, role_document, verify_password
from mongo_client import mongo_client


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["Authentication"])


class SignupPayload(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    setup_token: str = ""
    # Kept optional for backwards-compatible API clients; the deployment name
    # is controlled by WORKSPACE_NAME and is never collected from end users.
    workspace_name: str = Field(default="", max_length=120)


class LoginPayload(BaseModel):
    email: EmailStr
    password: str


class AcceptInvitePayload(BaseModel):
    token: str = Field(min_length=20)
    name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class ChangePasswordPayload(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ProfilePayload(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(default="", max_length=40)
    timezone: str = Field(default="", max_length=80)
    language: str = Field(default="", max_length=40)


def _workspace_id(name: str, email: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or email.split("@", 1)[0]
    return base[:48]


def _configured_workspace_name() -> str:
    configured = os.getenv("WORKSPACE_NAME", "").strip()
    if configured:
        return configured[:120]
    identity = mongo_client.platform_settings.find_one({"key": "workspace_identity"}) if mongo_client.is_connected() else None
    stored = (identity or {}).get("value", {}).get("name", "")
    return str(stored).strip()[:120] or "AgentFlow workspace"


def _auth_response(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"token": issue_token(user), "user": public_user(user)}


@router.post("/signup")
def signup(payload: SignupPayload):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=503, detail="Database not connected")
    email = payload.email.lower()
    if mongo_client.users.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    ensure_roles()
    now = datetime.utcnow()
    is_first_user = mongo_client.users.count_documents({}) == 0
    configured_setup_token = os.getenv("BOOTSTRAP_SETUP_TOKEN", "")
    if is_first_user and configured_setup_token and not hmac.compare_digest(payload.setup_token, configured_setup_token):
        raise HTTPException(status_code=403, detail="A setup token is required to create the first Admin account")
    public_signup_enabled = os.getenv("ALLOW_PUBLIC_SIGNUP", "false").lower() in {"1", "true", "yes", "on"}
    if not is_first_user and not public_signup_enabled:
        raise HTTPException(status_code=403, detail="Public signup is disabled. Ask a workspace Admin for an invitation.")
    workspace_name = _configured_workspace_name()
    workspace_id = _workspace_id(workspace_name, email)
    role = "admin" if is_first_user else "agent"
    user = {"name": payload.name.strip(), "email": email, "password_hash": hash_password(payload.password), "role": role, "permissions": [], "workspace_id": workspace_id, "workspace_name": workspace_name, "active": True, "auth_version": 0, "created_at": now, "updated_at": now}
    result = mongo_client.users.insert_one(user)
    user["_id"] = result.inserted_id
    if is_first_user:
        mongo_client.platform_settings.update_one({"key": "workspace_identity"}, {"$set": {"value": {"name": workspace_name, "owner_id": str(result.inserted_id)}, "updated_at": now}, "$setOnInsert": {"created_at": now}}, upsert=True)
    return {"success": True, "data": _auth_response(user)}


@router.get("/config")
def auth_config():
    first_account = mongo_client.users.count_documents({}) == 0 if mongo_client.is_connected() else True
    return {"success": True, "data": {"setup_required": bool(os.getenv("BOOTSTRAP_SETUP_TOKEN")), "public_signup_enabled": os.getenv("ALLOW_PUBLIC_SIGNUP", "false").lower() in {"1", "true", "yes", "on"}, "first_account_is_admin": first_account, "workspace_name": _configured_workspace_name()}}


# Password guessing: after LOGIN_MAX_FAILURES wrong passwords for one email within LOGIN_WINDOW, that email is
# locked for the rest of the window. Failures live in MongoDB (login_failures, TTL index) so the lock survives
# restarts and holds across servers.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW = timedelta(minutes=15)


def _recent_failures(email: str) -> int:
    return mongo_client.login_failures.count_documents({"email": email, "at": {"$gt": datetime.utcnow() - LOGIN_WINDOW}})


def _record_failure(email: str) -> int:
    mongo_client.login_failures.insert_one({"email": email, "at": datetime.utcnow()})
    return _recent_failures(email)


def _clear_failures(email: str) -> None:
    mongo_client.login_failures.delete_many({"email": email})


@router.post("/login")
def login(payload: LoginPayload):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=503, detail="Database not connected")
    email = payload.email.lower()
    if _recent_failures(email) >= LOGIN_MAX_FAILURES:
        logger.warning("auth: %s is locked after %d failed sign-ins", email, LOGIN_MAX_FAILURES)
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts. Try again in 15 minutes.")
    user = mongo_client.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        logger.info("auth: failed sign-in for %s (%d in the last 15 min)", email, _record_failure(email))
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.get("active") is False:
        raise HTTPException(status_code=403, detail="This account has been deactivated. Ask a workspace admin to reactivate it.")
    _clear_failures(email)
    return {"success": True, "data": _auth_response(user)}


@router.get("/me")
def me(user: Dict[str, Any] = Depends(require_user)):
    return {"success": True, "data": public_user(user)}


@router.put("/profile")
def update_profile(payload: ProfilePayload, user: Dict[str, Any] = Depends(require_user)):
    values = {"name": payload.name.strip(), "phone": payload.phone.strip(), "timezone": payload.timezone.strip(), "language": payload.language.strip(), "updated_at": datetime.utcnow()}
    mongo_client.users.update_one({"_id": user["_id"]}, {"$set": values})
    return {"success": True, "data": public_user(mongo_client.users.find_one({"_id": user["_id"]}))}


@router.post("/logout")
def logout(everywhere: bool = False, user: Dict[str, Any] = Depends(require_user)):
    """Signing out of one browser just drops its token there; everywhere=true also invalidates every other session."""
    if everywhere:
        mongo_client.users.update_one({"_id": user["_id"]}, {"$inc": {"auth_version": 1}, "$set": {"updated_at": datetime.utcnow()}})
        logger.info("auth: %s signed out everywhere", user.get("email"))
    return {"success": True, "data": {"logged_out": True, "everywhere": everywhere}}


@router.post("/password")
def change_password(payload: ChangePasswordPayload, user: Dict[str, Any] = Depends(require_user)):
    if not verify_password(payload.current_password, user.get("password_hash", "")):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="Choose a new password")
    mongo_client.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(payload.new_password), "updated_at": datetime.utcnow()}, "$inc": {"auth_version": 1}})
    logger.info("auth: %s changed their password (other sessions signed out)", user.get("email"))
    # Other devices are signed out (auth_version moved on); this browser gets a fresh token and stays signed in.
    return {"success": True, "data": {"password_updated": True, "token": issue_token(mongo_client.users.find_one({"_id": user["_id"]}))}}


@router.get("/invite/{token}")
def invite_preview(token: str):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=503, detail="Database not connected")
    invitation = mongo_client.invitations.find_one({"token_hash": invitation_hash(token), "status": "pending", "expires_at": {"$gt": datetime.utcnow()}})
    if not invitation:
        raise HTTPException(status_code=404, detail="This invitation is invalid or expired")
    return {"success": True, "data": {"email": invitation.get("email", ""), "name": invitation.get("name", ""), "role": invitation.get("role", "agent"), "workspace_name": invitation.get("workspace_name", "My workspace")}}


@router.post("/accept-invite")
def accept_invite(payload: AcceptInvitePayload):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=503, detail="Database not connected")
    invitation = mongo_client.invitations.find_one({"token_hash": invitation_hash(payload.token), "status": "pending", "expires_at": {"$gt": datetime.utcnow()}})
    if not invitation:
        raise HTTPException(status_code=400, detail="This invitation is invalid or expired")
    email = invitation["email"].lower()
    if mongo_client.users.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    now = datetime.utcnow()
    user = {"name": payload.name.strip(), "email": email, "password_hash": hash_password(payload.password), "role": invitation.get("role", "agent"), "permissions": invitation.get("permissions", []), "workspace_id": invitation.get("workspace_id", "default"), "workspace_name": invitation.get("workspace_name", "AgentFlow workspace"), "active": True, "auth_version": 0, "created_at": now, "updated_at": now}
    result = mongo_client.users.insert_one(user)
    user["_id"] = result.inserted_id
    mongo_client.invitations.update_one({"_id": invitation["_id"]}, {"$set": {"status": "accepted", "accepted_at": now, "user_id": result.inserted_id, "updated_at": now}})
    mongo_client.team_members.update_one({"email": email}, {"$set": {"user_id": result.inserted_id, "name": user["name"], "role": user["role"], "active": True, "invitation_status": "accepted", "updated_at": now}}, upsert=True)
    return {"success": True, "data": _auth_response(user)}
