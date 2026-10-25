from datetime import datetime, timedelta
import logging
import os
import hashlib
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from auth_service import hash_password, invitation_hash, issue_token, public_user, require_user, role_document, verify_password
from email_service import email_service
from mongo_client import mongo_client
from notification_service import notify_team_member_registered


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


class EmailVerificationPayload(BaseModel):
    token: str = Field(min_length=20, max_length=200)


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


def _configured_workspace_name() -> str:
    configured = os.getenv("WORKSPACE_NAME", "").strip()
    if configured:
        return configured[:120]
    identity = mongo_client.platform_settings.find_one({"key": "workspace_identity"}) if mongo_client.is_connected() else None
    stored = (identity or {}).get("value", {}).get("name", "")
    return str(stored).strip()[:120] or "AgentFlow workspace"


def _auth_response(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"token": issue_token(user), "user": public_user(user)}


def _verification_hash(token: str) -> str:
    secret = os.getenv("AUTH_SECRET", "agentflow-local-development-secret")
    return hashlib.sha256(f"{token}:{secret}".encode("utf-8")).hexdigest()


@router.post("/signup")
def signup(payload: SignupPayload):
    raise HTTPException(status_code=403, detail="Account creation is invitation-only. Ask a workspace Admin to send you an invitation.")


@router.get("/config")
def auth_config():
    first_account = mongo_client.users.count_documents({}) == 0 if mongo_client.is_connected() else True
    return {"success": True, "data": {"setup_required": False, "public_signup_enabled": False, "email_verification_required": email_service.configured, "first_account_is_admin": first_account, "workspace_name": _configured_workspace_name()}}


@router.post("/verify-email")
def verify_email(payload: EmailVerificationPayload):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=503, detail="Database not connected")
    user = mongo_client.users.find_one({"email_verification_hash": _verification_hash(payload.token), "email_verified": False})
    if not user or user.get("email_verified") is not False:
        raise HTTPException(status_code=400, detail="This verification request is invalid or already completed.")
    if not user.get("email_verification_expires_at") or user["email_verification_expires_at"] < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This verification link has expired. Start sign-up again.")
    email = str(user.get("email", "")).lower()
    now = datetime.utcnow()
    mongo_client.users.update_one({"_id": user["_id"]}, {"$set": {"email_verified": True, "active": True, "updated_at": now}, "$unset": {"email_verification_hash": "", "email_verification_expires_at": "", "email_verification_sent_at": "", "email_verification_attempts": ""}})
    user.update({"email_verified": True, "active": True, "updated_at": now})
    mongo_client.team_members.update_one({"email": email}, {"$set": {"user_id": user["_id"], "name": user.get("name", ""), "role": user.get("role", "agent"), "active": True, "invitation_status": "accepted", "updated_at": now}}, upsert=True)
    return {"success": True, "data": _auth_response(user)}


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
    if user and user.get("email_verified") is False:
        raise HTTPException(status_code=403, detail="Please verify your email before signing in.")
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        logger.info("auth: failed sign-in for %s (%d in the last 15 min)", email, _record_failure(email))
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.get("active") is False:
        raise HTTPException(status_code=403, detail="This account has been deactivated. Ask a workspace admin to reactivate it.")
    _clear_failures(email)
    return {"success": True, "data": _auth_response(user)}


@router.get("/me")
def me(user: Dict[str, Any] = Depends(require_user)):
    # Refresh the browser/PWA session whenever the workspace boots. The current token
    # must still be valid; expired or revoked sessions continue to require sign-in.
    return {"success": True, "data": {**public_user(user), "token": issue_token(user)}}


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
    user = {"name": payload.name.strip(), "email": email, "password_hash": hash_password(payload.password), "role": invitation.get("role", "agent"), "permissions": invitation.get("permissions", []), "workspace_id": invitation.get("workspace_id", "default"), "workspace_name": invitation.get("workspace_name", "AgentFlow workspace"), "active": True, "email_verified": True, "auth_version": 0, "created_at": now, "updated_at": now}
    result = mongo_client.users.insert_one(user)
    user["_id"] = result.inserted_id
    mongo_client.invitations.update_one({"_id": invitation["_id"]}, {"$set": {"status": "accepted", "accepted_at": now, "user_id": result.inserted_id, "updated_at": now}})
    mongo_client.team_members.update_one({"email": email}, {"$set": {"user_id": result.inserted_id, "name": user["name"], "role": user["role"], "active": True, "invitation_status": "accepted", "updated_at": now}}, upsert=True)
    try:
        notify_team_member_registered(name=user["name"], email=email, role=user["role"])
    except Exception:
        # Registration has succeeded; a notification failure must not undo the new account.
        logger.exception("auth: failed to notify admins about new team member %s", email)
    return {"success": True, "data": _auth_response(user)}
