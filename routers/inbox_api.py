"""Team inbox: call conversations sorted into folders, admin assignment, and a shared team channel with @mentions.

Folders
    all         every inbound conversation
    unassigned  nobody owns it yet (admins assign from here)
    mine        assigned to the signed-in user
    mentioned   team-channel messages that @mention the signed-in user
    failed      calls that never connected (failed, busy, no answer, canceled, missed)

Every write is logged as "inbox: <action> ..." so it can be followed with tail_logs.ps1 -Filter inbox.
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth_service import has_permission, require_user
from mongo_client import mongo_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/inbox", tags=["inbox"], dependencies=[Depends(require_user)])

FOLDERS = ("all", "unassigned", "mine", "mentioned", "failed")
FAILED_STATUSES = ["failed", "busy", "no-answer", "no_answer", "canceled", "cancelled", "missed"]
CONVERSATION_FIELDS = {"phone_number": 1, "lead_id": 1, "lead": 1, "status": 1, "direction": 1, "duration": 1, "call_date": 1,
                       "created_at": 1, "call_summary": 1, "provider": 1, "channel_type": 1, "assigned_to": 1,
                       "assigned_to_name": 1, "assigned_by_name": 1, "assigned_at": 1, "disconnection_reason": 1}


class AssignPayload(BaseModel):
    user_id: Optional[str] = None  # None unassigns


class MessageEditPayload(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    mentions: List[str] = Field(default_factory=list)


class ChannelMessagePayload(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    mentions: List[str] = Field(default_factory=list)  # user ids picked in the composer
    conversation_id: Optional[str] = None  # optional link to the conversation being discussed


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


def _folder_query(folder: str, user: Dict[str, Any]) -> Dict[str, Any]:
    query: Dict[str, Any] = {"direction": {"$ne": "outbound"}}
    if folder == "unassigned":
        query["$or"] = [{"assigned_to": {"$exists": False}}, {"assigned_to": None}]
        query["status"] = {"$nin": FAILED_STATUSES}
    elif folder == "mine":
        query["assigned_to"] = _user_id(user)
    elif folder == "failed":
        query["status"] = {"$in": FAILED_STATUSES}
    return query


def _mention_query(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"workspace_id": user.get("workspace_id", "default"), "mentions": _user_id(user)}


def _conversation_row(call: Dict[str, Any]) -> Dict[str, Any]:
    lead = call.get("lead") or {}
    name = lead.get("name") or ("Web call" if call.get("channel_type") == "web_call" else str(call.get("phone_number") or "Unknown caller"))
    when = call.get("call_date") or call.get("created_at")
    return _serialize({**call, "name": name, "when": when, "failed": str(call.get("status", "")).lower() in FAILED_STATUSES})


def _notify(recipient: Dict[str, Any], kind: str, title: str, message: str, conversation_id: Optional[str]) -> None:
    mongo_client.notifications.insert_one({"type": kind, "channel": "inbox", "title": title, "message": message,
                                           "recipient": recipient.get("email") or recipient.get("name"), "conversation_id": conversation_id,
                                           "created_at": datetime.utcnow(), "read": False})
    logger.info("inbox: notified %s (%s) about %s", recipient.get("email"), kind, conversation_id or "channel")


@router.get("/counts")
async def folder_counts(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    counts = {folder: mongo_client.calls.count_documents(_folder_query(folder, user)) for folder in ("all", "unassigned", "mine", "failed")}
    counts["mentioned"] = mongo_client.team_messages.count_documents({**_mention_query(user), "read_by": {"$ne": _user_id(user)}})
    logger.debug("inbox: counts for %s -> %s", user.get("email"), counts)
    return {"success": True, "data": counts}


@router.get("/members")
async def team_members(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    return {"success": True, "data": _members(user), "can_assign": _can_assign(user), "me": _user_id(user)}


@router.get("/conversations")
async def list_conversations(folder: str = "all", search: str = "", limit: int = Query(100, ge=1, le=500), user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    if folder not in FOLDERS or folder == "mentioned":
        raise HTTPException(status_code=400, detail=f"Unknown folder {folder!r}")
    query = _folder_query(folder, user)
    if search.strip():
        pattern = {"$regex": re.escape(search.strip()), "$options": "i"}
        query = {"$and": [query, {"$or": [{"phone_number": pattern}, {"lead.name": pattern}, {"call_summary": pattern}, {"assigned_to_name": pattern}]}]}
    rows = [_conversation_row(call) for call in mongo_client.calls.find(query, CONVERSATION_FIELDS).sort("call_date", -1).limit(limit)]
    logger.info("inbox: %s listed %s (%d rows, search=%r)", user.get("email"), folder, len(rows), search)
    return {"success": True, "data": rows}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    call = mongo_client.calls.find_one({"_id": _object_id(conversation_id, "conversation")})
    if not call:
        logger.warning("inbox: %s opened missing conversation %s", user.get("email"), conversation_id)
        raise HTTPException(status_code=404, detail="Conversation not found")
    discussion = list(mongo_client.team_messages.find({"workspace_id": user.get("workspace_id", "default"), "conversation_id": conversation_id}).sort("created_at", 1))
    logger.info("inbox: %s opened conversation %s", user.get("email"), conversation_id)
    return {"success": True, "data": {**_conversation_row(call), "transcription": _serialize(call.get("transcription") or []),
                                      "ai_responses": _serialize(call.get("ai_responses") or []), "recording_url": call.get("recording_url"),
                                      "discussion": _serialize(discussion)}}


@router.post("/conversations/{conversation_id}/assign")
async def assign_conversation(conversation_id: str, payload: AssignPayload, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    if not _can_assign(user):
        logger.warning("inbox: %s (role %s) tried to assign %s without permission", user.get("email"), user.get("role"), conversation_id)
        raise HTTPException(status_code=403, detail="Only admins can assign conversations")
    call_id = _object_id(conversation_id, "conversation")
    call = mongo_client.calls.find_one({"_id": call_id}, {"assigned_to": 1, "phone_number": 1, "lead": 1})
    if not call:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if payload.user_id is None:
        mongo_client.calls.update_one({"_id": call_id}, {"$unset": {"assigned_to": "", "assigned_to_name": "", "assigned_at": ""},
                                                        "$set": {"assigned_by_name": user.get("name")}})
        logger.info("inbox: %s unassigned conversation %s (was %s)", user.get("email"), conversation_id, call.get("assigned_to"))
        return {"success": True}

    assignee = mongo_client.users.find_one({"_id": _object_id(payload.user_id, "user"), "workspace_id": user.get("workspace_id", "default"), "active": {"$ne": False}})
    if not assignee:
        logger.warning("inbox: %s tried to assign %s to unknown user %s", user.get("email"), conversation_id, payload.user_id)
        raise HTTPException(status_code=404, detail="Team member not found")
    mongo_client.calls.update_one({"_id": call_id}, {"$set": {"assigned_to": str(assignee["_id"]), "assigned_to_name": assignee.get("name"),
                                                             "assigned_by_name": user.get("name"), "assigned_at": datetime.utcnow()}})
    logger.info("inbox: %s assigned conversation %s to %s (was %s)", user.get("email"), conversation_id, assignee.get("email"), call.get("assigned_to"))
    if str(assignee["_id"]) != _user_id(user):
        customer = (call.get("lead") or {}).get("name") or call.get("phone_number") or "a caller"
        _notify(assignee, "assignment", "Conversation assigned to you", f"{user.get('name')} assigned you the conversation with {customer}.", conversation_id)
    return {"success": True}


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
    if payload.conversation_id:
        _object_id(payload.conversation_id, "conversation")
    members, mentions = _resolve_mentions(payload.mentions, user)
    message = {"workspace_id": user.get("workspace_id", "default"), "author_id": _user_id(user), "author_name": user.get("name") or user.get("email"),
               "text": text, "mentions": mentions, "mention_names": [members[user_id]["name"] for user_id in mentions],
               "conversation_id": payload.conversation_id, "read_by": [_user_id(user)], "created_at": datetime.utcnow()}
    message["_id"] = mongo_client.team_messages.insert_one(message).inserted_id
    logger.info("inbox: %s posted to team channel (message %s, %d mentions, conversation %s)", user.get("email"), message["_id"], len(mentions), payload.conversation_id)
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
    logger.info("inbox: %s deleted message %s by %s (conversation %s)", user.get("email"), message_id, message.get("author_name"), message.get("conversation_id"))
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
