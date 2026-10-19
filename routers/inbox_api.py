"""Team inbox: one conversation thread per caller, admin assignment, and a shared team channel with @mentions.

A thread holds every call from one caller in order, each call's transcript in speaking order
(customer left, AI agent right), and is shown live while a call is in progress.

Folders
    all         every caller thread
    unassigned  nobody owns it yet (admins assign from here)
    mine        assigned to the signed-in user
    mentioned   team-channel messages that @mention the signed-in user
    failed      threads whose latest call never connected (failed, busy, no answer, canceled, missed)

Every write is logged as "inbox: <action> ..." so it can be followed with tail_logs.ps1 -Filter inbox.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import live_transcript
from auth_service import has_permission, require_user
from mongo_client import mongo_client
from routers.calls_api import caller_identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/inbox", tags=["inbox"], dependencies=[Depends(require_user)])

FOLDERS = ("all", "unassigned", "mine", "mentioned", "failed")
FAILED_STATUSES = {"failed", "busy", "no-answer", "no_answer", "canceled", "cancelled", "missed"}
# A call still marked in progress after this long ended without a final save (e.g. the server restarted).
LIVE_WINDOW = timedelta(minutes=3)
LIST_FIELDS = {"phone_number": 1, "lead_id": 1, "status": 1, "call_date": 1, "created_at": 1, "updated_at": 1, "call_summary": 1,
               "provider": 1, "channel_type": 1, "call_session_id": 1, "messages": {"$slice": -1}, "ai_responses": {"$slice": -1}}
MAX_CALLS_SCANNED = 3000


class AssignPayload(BaseModel):
    user_id: Optional[str] = None  # None unassigns


class MessageEditPayload(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    mentions: List[str] = Field(default_factory=list)


class ChannelMessagePayload(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    mentions: List[str] = Field(default_factory=list)  # user ids picked in the composer
    conversation_id: Optional[str] = Field(default=None, max_length=200)  # optional thread being discussed


def _require_db() -> None:
    if not mongo_client.is_connected():
        logger.error("inbox: database not connected")
        raise HTTPException(status_code=503, detail="Database not connected")


def _serialize(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


def _object_id(value: str, what: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        logger.warning("inbox: invalid %s id %r", what, value)
        raise HTTPException(status_code=400, detail=f"Invalid {what} id")
    return ObjectId(value)


def _user_id(user: Dict[str, Any]) -> str:
    return str(user.get("_id"))


def _can_assign(user: Dict[str, Any]) -> bool:
    return user.get("role") in {"owner", "admin"} or has_permission(user, "assign_conversations")


def _members(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = mongo_client.users.find({"workspace_id": user.get("workspace_id", "default"), "active": {"$ne": False}}, {"name": 1, "email": 1, "role": 1}).sort("name", 1)
    return [{"id": str(row["_id"]), "name": row.get("name") or row.get("email", ""), "email": row.get("email", ""), "role": row.get("role", "agent")} for row in rows]


# ---------------------------------------------------------------- threads

def _thread_key(call: Dict[str, Any]) -> str:
    """Which caller a call belongs to. Calls with no usable caller id each get their own thread."""
    identity = caller_identity(call.get("phone_number") or "")
    if identity.lower() in {"", "unknown", "anonymous"}:
        return f"call:{call['_id']}"
    if identity.lower() == "web caller":  # Retell web calls carry no caller id
        return f"web:{call.get('call_session_id') or call['_id']}"
    return identity


def _calls_query(key: str) -> Dict[str, Any]:
    """Mongo query for every call in a thread (the inverse of _thread_key)."""
    inbound = {"direction": {"$ne": "outbound"}}
    if key.startswith("call:"):
        return {**inbound, "_id": _object_id(key[5:], "conversation")}
    if key.startswith("web:"):
        session = key[4:]
        return {**inbound, "$or": [{"call_session_id": session}, *([{"_id": ObjectId(session)}] if ObjectId.is_valid(session) else [])]}
    if key.startswith("client:browser_"):
        return {**inbound, "phone_number": {"$regex": f"^{re.escape(key)}(_|$)"}}
    return {**inbound, "phone_number": key}


def _when(call: Dict[str, Any]) -> datetime:
    value = call.get("call_date") or call.get("created_at")
    return value if isinstance(value, datetime) else datetime.min


def _is_live(call: Dict[str, Any]) -> bool:
    updated = call.get("updated_at")
    return str(call.get("status", "")).lower() == live_transcript.LIVE_STATUS and isinstance(updated, datetime) and datetime.now() - updated < LIVE_WINDOW


def _call_messages(call: Dict[str, Any]) -> List[Dict[str, str]]:
    """The call's transcript in speaking order, from whichever form this record stores."""
    if call.get("messages"):
        return call["messages"]
    tool_transcript = call.get("transcript_with_tool_calls")
    if isinstance(tool_transcript, list) and tool_transcript:
        started = call.get("provider_started_at")
        try:
            started_at = datetime.fromisoformat(started) if isinstance(started, str) else None
        except ValueError:
            started_at = None
        return live_transcript.ordered_from_retell(tool_transcript, started_at)
    return live_transcript.ordered_from_lists(call.get("transcription") or [], call.get("ai_responses") or [])


def _browser_owner_names() -> Dict[str, str]:
    """client:browser_<last 12 chars of a user id> -> that user's name, to label browser test calls."""
    return {f"client:browser_{str(row['_id'])[-12:]}": row.get("name") or row.get("email", "") for row in mongo_client.users.find({}, {"name": 1, "email": 1})}


def _display_name(key: str, lead: Optional[Dict[str, Any]], owners: Dict[str, str]) -> str:
    if key in owners:
        return f"Browser test · {owners[key]}"
    name = (lead or {}).get("name") or ""
    generic = not name or name.startswith(("Inbound caller", "Browser caller", "Web caller"))
    if key.startswith(("web:", "call:")):
        return name if not generic else "Web caller" if key.startswith("web:") else "Unknown caller"
    return key if generic else name


def _threads() -> List[Dict[str, Any]]:
    """Every caller thread, newest activity first, with its assignment."""
    calls = list(mongo_client.calls.find({"direction": {"$ne": "outbound"}}, LIST_FIELDS).sort("call_date", -1).limit(MAX_CALLS_SCANNED))
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for call in calls:
        grouped.setdefault(_thread_key(call), []).append(call)

    lead_ids = {ObjectId(str(call["lead_id"])) for call in calls if call.get("lead_id") and ObjectId.is_valid(str(call["lead_id"]))}
    leads = {str(lead["_id"]): lead for lead in mongo_client.leads.find({"_id": {"$in": list(lead_ids)}}, {"name": 1, "company": 1})} if lead_ids else {}
    states = {state["_id"]: state for state in mongo_client.inbox_threads.find({"_id": {"$in": list(grouped)}})}
    owners = _browser_owner_names()

    threads = []
    for key, items in grouped.items():
        latest = max(items, key=_when)
        lead = next((leads.get(str(call.get("lead_id"))) for call in items if leads.get(str(call.get("lead_id")))), None)
        last_line = (latest.get("messages") or [{}])[-1].get("text") or (latest.get("ai_responses") or [{}])[-1].get("content") or ""
        state = states.get(key, {})
        threads.append({
            "key": key, "name": _display_name(key, lead, owners), "phone": caller_identity(latest.get("phone_number") or ""),
            "last_at": _when(latest), "preview": last_line or latest.get("call_summary") or "", "summary": latest.get("call_summary") or "",
            "call_count": len(items), "status": latest.get("status", "completed"), "provider": latest.get("provider"),
            "failed": str(latest.get("status", "")).lower() in FAILED_STATUSES, "live": any(_is_live(call) for call in items),
            "assigned_to": state.get("assigned_to"), "assigned_to_name": state.get("assigned_to_name"),
            "assigned_by_name": state.get("assigned_by_name"), "assigned_at": state.get("assigned_at"),
        })
    threads.sort(key=lambda thread: thread["last_at"], reverse=True)
    if len(calls) == MAX_CALLS_SCANNED:
        logger.warning("inbox: scanned the newest %d calls only; older threads are not listed", MAX_CALLS_SCANNED)
    return threads


def _in_folder(thread: Dict[str, Any], folder: str, me: str) -> bool:
    if folder == "unassigned":
        return not thread["assigned_to"] and not thread["failed"]
    if folder == "mine":
        return thread["assigned_to"] == me
    if folder == "failed":
        return thread["failed"]
    return True


def _mention_query(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"workspace_id": user.get("workspace_id", "default"), "mentions": _user_id(user)}


def _notify(recipient: Dict[str, Any], kind: str, title: str, message: str, conversation_id: Optional[str]) -> None:
    mongo_client.notifications.insert_one({"type": kind, "channel": "inbox", "title": title, "message": message,
                                           "recipient": recipient.get("email") or recipient.get("name"), "conversation_id": conversation_id,
                                           "created_at": datetime.utcnow(), "read": False})
    logger.info("inbox: notified %s (%s) about %s", recipient.get("email"), kind, conversation_id or "channel")


@router.get("/counts")
async def folder_counts(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    threads = _threads()
    counts = {folder: sum(1 for thread in threads if _in_folder(thread, folder, _user_id(user))) for folder in ("all", "unassigned", "mine", "failed")}
    counts["mentioned"] = mongo_client.team_messages.count_documents({**_mention_query(user), "read_by": {"$ne": _user_id(user)}})
    counts["live"] = sum(1 for thread in threads if thread["live"])
    logger.debug("inbox: counts for %s -> %s", user.get("email"), counts)
    return {"success": True, "data": counts}


@router.get("/members")
async def team_members(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    return {"success": True, "data": _members(user), "can_assign": _can_assign(user), "me": _user_id(user)}


@router.get("/conversations")
async def list_conversations(folder: str = "all", search: str = "", limit: int = Query(200, ge=1, le=1000), user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    if folder not in FOLDERS or folder == "mentioned":
        raise HTTPException(status_code=400, detail=f"Unknown folder {folder!r}")
    needle = search.strip().lower()
    rows = [thread for thread in _threads() if _in_folder(thread, folder, _user_id(user))
            and (not needle or needle in " ".join(str(thread.get(field) or "") for field in ("name", "phone", "preview", "summary", "assigned_to_name")).lower())]
    logger.info("inbox: %s listed %s (%d threads, search=%r)", user.get("email"), folder, len(rows), search)
    return {"success": True, "data": _serialize(rows[:limit])}


@router.post("/conversations/{key:path}/assign")
async def assign_conversation(key: str, payload: AssignPayload, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    if not _can_assign(user):
        logger.warning("inbox: %s (role %s) tried to assign %s without permission", user.get("email"), user.get("role"), key)
        raise HTTPException(status_code=403, detail="Only admins can assign conversations")
    calls = list(mongo_client.calls.find(_calls_query(key), {"phone_number": 1}).limit(1))
    if not calls:
        logger.warning("inbox: %s tried to assign missing thread %s", user.get("email"), key)
        raise HTTPException(status_code=404, detail="Conversation not found")
    before = (mongo_client.inbox_threads.find_one({"_id": key}) or {}).get("assigned_to")

    if payload.user_id is None:
        mongo_client.inbox_threads.update_one({"_id": key}, {"$unset": {"assigned_to": "", "assigned_to_name": "", "assigned_at": ""},
                                                             "$set": {"assigned_by_name": user.get("name"), "updated_at": datetime.utcnow()}}, upsert=True)
        logger.info("inbox: %s unassigned thread %s (was %s)", user.get("email"), key, before)
        return {"success": True}

    assignee = mongo_client.users.find_one({"_id": _object_id(payload.user_id, "user"), "workspace_id": user.get("workspace_id", "default"), "active": {"$ne": False}})
    if not assignee:
        logger.warning("inbox: %s tried to assign %s to unknown user %s", user.get("email"), key, payload.user_id)
        raise HTTPException(status_code=404, detail="Team member not found")
    mongo_client.inbox_threads.update_one({"_id": key}, {"$set": {"assigned_to": str(assignee["_id"]), "assigned_to_name": assignee.get("name"),
                                                                  "assigned_by_name": user.get("name"), "assigned_at": datetime.utcnow(),
                                                                  "updated_at": datetime.utcnow()}}, upsert=True)
    logger.info("inbox: %s assigned thread %s to %s (was %s)", user.get("email"), key, assignee.get("email"), before)
    if str(assignee["_id"]) != _user_id(user):
        _notify(assignee, "assignment", "Conversation assigned to you", f"{user.get('name')} assigned you the conversation with {calls[0].get('phone_number') or 'a caller'}.", key)
    return {"success": True}


@router.get("/conversations/{key:path}")
async def get_conversation(key: str, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    calls = sorted(mongo_client.calls.find(_calls_query(key)), key=_when)
    if not calls:
        logger.warning("inbox: %s opened missing thread %s", user.get("email"), key)
        raise HTTPException(status_code=404, detail="Conversation not found")
    thread = next((item for item in _threads() if item["key"] == key), None) or {"key": key, "name": key}
    history = [{"id": str(call["_id"]), "at": _when(call), "status": call.get("status", "completed"), "live": _is_live(call),
                "failed": str(call.get("status", "")).lower() in FAILED_STATUSES, "provider": call.get("provider") or "voice",
                "channel_type": call.get("channel_type"), "duration": call.get("duration") or 0, "summary": call.get("call_summary") or "",
                "recording_url": call.get("recording_url"), "disconnection_reason": call.get("disconnection_reason"),
                "messages": _call_messages(call)} for call in calls]
    discussion = list(mongo_client.team_messages.find({"workspace_id": user.get("workspace_id", "default"), "conversation_id": key}).sort("created_at", 1))
    logger.info("inbox: %s opened thread %s (%d calls, live=%s)", user.get("email"), key, len(history), thread.get("live"))
    return {"success": True, "data": _serialize({**thread, "calls": history, "discussion": discussion})}


# ---------------------------------------------------------------- team channel and mentions

def _resolve_mentions(requested: List[str], user: Dict[str, Any]):
    """Keep only mentions of active teammates in this workspace."""
    members = {member["id"]: member for member in _members(user)}
    mentions = [user_id for user_id in dict.fromkeys(requested) if user_id in members]
    dropped = set(requested) - set(mentions)
    if dropped:
        logger.warning("inbox: %s mentioned unknown users %s; ignored", user.get("email"), sorted(dropped))
    return members, mentions


def _own_message(message_id: str, user: Dict[str, Any], action: str) -> Dict[str, Any]:
    """Load a team message the user may change: authors edit/delete their own, admins may delete any."""
    message = mongo_client.team_messages.find_one({"_id": _object_id(message_id, "message"), "workspace_id": user.get("workspace_id", "default")})
    if not message:
        logger.warning("inbox: %s tried to %s missing message %s", user.get("email"), action, message_id)
        raise HTTPException(status_code=404, detail="Message not found")
    is_author = message.get("author_id") == _user_id(user)
    if not is_author and not (action == "delete" and _can_assign(user)):
        logger.warning("inbox: %s tried to %s message %s by %s", user.get("email"), action, message_id, message.get("author_name"))
        raise HTTPException(status_code=403, detail=f"You can only {action} your own messages")
    return message


@router.get("/channel")
async def channel_messages(limit: int = Query(200, ge=1, le=500), user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    rows = list(mongo_client.team_messages.find({"workspace_id": user.get("workspace_id", "default")}).sort("created_at", -1).limit(limit))
    rows.reverse()
    logger.debug("inbox: %s read team channel (%d messages)", user.get("email"), len(rows))
    return {"success": True, "data": _serialize(rows)}


@router.post("/channel")
async def post_channel_message(payload: ChannelMessagePayload, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    members, mentions = _resolve_mentions(payload.mentions, user)
    message = {"workspace_id": user.get("workspace_id", "default"), "author_id": _user_id(user), "author_name": user.get("name") or user.get("email"),
               "text": text, "mentions": mentions, "mention_names": [members[user_id]["name"] for user_id in mentions],
               "conversation_id": payload.conversation_id or None, "read_by": [_user_id(user)], "created_at": datetime.utcnow()}
    message["_id"] = mongo_client.team_messages.insert_one(message).inserted_id
    logger.info("inbox: %s posted to team channel (message %s, %d mentions, thread %s)", user.get("email"), message["_id"], len(mentions), payload.conversation_id)
    for user_id in mentions:
        if user_id != _user_id(user):
            _notify(members[user_id], "mention", f"{message['author_name']} mentioned you", text[:200], payload.conversation_id)
    return {"success": True, "data": _serialize(message)}


@router.put("/channel/{message_id}")
async def edit_channel_message(message_id: str, payload: MessageEditPayload, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    message = _own_message(message_id, user, "edit")
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    members, mentions = _resolve_mentions(payload.mentions, user)
    added = [user_id for user_id in mentions if user_id not in (message.get("mentions") or [])]
    updates = {"text": text, "mentions": mentions, "mention_names": [members[user_id]["name"] for user_id in mentions], "edited_at": datetime.utcnow()}
    # Newly mentioned people have not seen it yet; keep it unread for them.
    mongo_client.team_messages.update_one({"_id": message["_id"]}, {"$set": updates, "$pull": {"read_by": {"$in": added}}})
    logger.info("inbox: %s edited message %s (%d mentions, %d new)", user.get("email"), message_id, len(mentions), len(added))
    for user_id in added:
        if user_id != _user_id(user):
            _notify(members[user_id], "mention", f"{message.get('author_name')} mentioned you", text[:200], message.get("conversation_id"))
    return {"success": True, "data": _serialize({**message, **updates})}


@router.delete("/channel/{message_id}")
async def delete_channel_message(message_id: str, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    message = _own_message(message_id, user, "delete")
    mongo_client.team_messages.delete_one({"_id": message["_id"]})
    logger.info("inbox: %s deleted message %s by %s (thread %s)", user.get("email"), message_id, message.get("author_name"), message.get("conversation_id"))
    return {"success": True}


@router.get("/mentions")
async def my_mentions(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    rows = list(mongo_client.team_messages.find(_mention_query(user)).sort("created_at", -1).limit(200))
    logger.info("inbox: %s listed mentions (%d)", user.get("email"), len(rows))
    return {"success": True, "data": _serialize(rows)}


@router.post("/mentions/read")
async def mark_mentions_read(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    result = mongo_client.team_messages.update_many({**_mention_query(user), "read_by": {"$ne": _user_id(user)}}, {"$addToSet": {"read_by": _user_id(user)}})
    logger.info("inbox: %s marked %d mentions read", user.get("email"), result.modified_count)
    return {"success": True, "updated": result.modified_count}
