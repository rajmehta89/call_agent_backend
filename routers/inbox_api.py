"""Team inbox: one conversation thread per caller, admin assignment, and a shared team channel with @mentions.

A thread holds every call from one caller in order, each call's transcript in speaking order
(customer left, AI agent right), and is shown live while a call is in progress.

Folders
    all         every caller thread
    unassigned  nobody owns it yet (admins assign from here)
    mine        assigned to the signed-in team member (admins hand work out rather than receive it)
    assigned    every thread that has an owner (admins track who has what)
    mentioned   team-channel messages that @mention the signed-in user
    failed      threads whose latest call never connected (failed, busy, no answer, canceled, missed)

Status (the sidebar's STATUS group, like Podium): every thread sits in one pipeline stage (STAGES), "New lead" until
someone moves it. Admins and the thread's assignee change it; ?stage= filters the list and /counts has "stage:<id>".

Team messages (the team channel and notes on a conversation): a message that @mentions people is visible
only to its author and those people, admins included; a message without mentions is for the whole team.

Every write is logged as "inbox: <action> ..." so it can be followed with tail_logs.ps1 -Filter inbox.
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from timeutil import utc_now
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

import live_transcript
import realtime
from auth_service import can_assign_conversations, has_permission, require_permission, require_user
from mongo_client import mongo_client
from routers.calls_api import caller_identity

logger = logging.getLogger(__name__)

# Every inbox route needs handle_conversations (admins and team members).
router = APIRouter(prefix="/api/inbox", tags=["inbox"], dependencies=[Depends(require_permission("handle_conversations"))])

FOLDERS = ("all", "unassigned", "mine", "assigned", "mentioned", "failed")
FAILED_STATUSES = {"failed", "busy", "no-answer", "no_answer", "canceled", "cancelled", "missed"}
# A call still marked in progress after this long ended without a final save (e.g. the server restarted).
LIVE_WINDOW = timedelta(minutes=3)
LIST_FIELDS = {"phone_number": 1, "lead_id": 1, "status": 1, "call_date": 1, "created_at": 1, "updated_at": 1, "call_summary": 1, "human_summary": 1,
               "provider": 1, "channel_type": 1, "call_session_id": 1, "messages": {"$slice": -1}, "ai_responses": {"$slice": -1}}
MAX_CALLS_SCANNED = 3000


def _full_call_summary(call: Dict[str, Any]) -> str:
    ai = str(call.get("call_summary") or "").strip()
    human = str(call.get("human_summary") or "").strip()
    if ai and human:
        return f"AI: {ai}\nHuman: {human}"
    return human or ai
# Pipeline stages, in order. Keep in step with components/stages.ts in the frontend.
STAGES = [("new_lead", "New lead"), ("outreach", "Outreach"), ("scheduling", "Scheduling"), ("servicing", "Servicing"),
          ("payment_collection", "Payment collection"), ("follow_up", "Follow-up"), ("lost", "Lost"), ("unqualified", "Unqualified"), ("won", "Won")]
CONVERSATION_STATES = ("open", "closed", "all")
STAGE_LABELS = dict(STAGES)
DEFAULT_STAGE = "new_lead"


class AssignPayload(BaseModel):
    user_id: Optional[str] = None  # None unassigns


class StagePayload(BaseModel):
    stage: str


class ClosePayload(BaseModel):
    closed: bool = True


class ProfilePayload(BaseModel):
    """Contact fields editable from the inbox; a field left out is not changed."""
    name: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=40)
    email: Optional[str] = Field(default=None, max_length=200)
    company: Optional[str] = Field(default=None, max_length=160)
    address: Optional[str] = Field(default=None, max_length=300)
    tags: Optional[List[str]] = None


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
    return can_assign_conversations(user)


def _members(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Teammates who use the inbox (so can be @mentioned). Assignable = works conversations rather than handing them out."""
    rows = mongo_client.users.find({"workspace_id": user.get("workspace_id", "default"), "active": {"$ne": False}}, {"name": 1, "email": 1, "role": 1, "permissions": 1}).sort("name", 1)
    return [{"id": str(row["_id"]), "name": row.get("name") or row.get("email", ""), "email": row.get("email", ""), "role": row.get("role", "agent"),
             "assignable": not _can_assign(row)} for row in rows if has_permission(row, "handle_conversations")]


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


def _utc(value: datetime) -> datetime:
    """Call, lead and customer times are stored in UTC (timeutil; older rows moved by scripts/migrate_times_to_utc.py)."""
    return value


def _when_param(value: str, name: str) -> datetime | None:
    """An ISO time from the query string as naive UTC (how times are stored); None when empty."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be an ISO date-time")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _event(key: str, kind: str, user: Dict[str, Any], text: str) -> None:
    """Record something that happened to a thread (closed, reopened, assigned...) for its timeline."""
    mongo_client.inbox_threads.update_one({"_id": key}, {"$push": {"events": {"type": kind, "text": text, "by_name": user.get("name"), "at": datetime.utcnow()}}}, upsert=True)


def _is_live(call: Dict[str, Any]) -> bool:
    updated = call.get("updated_at")
    return str(call.get("status", "")).lower() == live_transcript.LIVE_STATUS and isinstance(updated, datetime) and utc_now() - updated < LIVE_WINDOW


def _call_messages(call: Dict[str, Any]) -> List[Dict[str, str]]:
    """The call's transcript in speaking order, from whichever form this record stores."""
    if call.get("messages"):
        lines = list(call["messages"])
    else:
        tool_transcript = call.get("transcript_with_tool_calls")
        if isinstance(tool_transcript, list) and tool_transcript:
            started = call.get("provider_started_at")
            try:
                started_at = datetime.fromisoformat(started) if isinstance(started, str) else None
            except ValueError:
                started_at = None
            lines = live_transcript.ordered_from_retell(tool_transcript, started_at)
        else:
            lines = live_transcript.ordered_from_lists(call.get("transcription") or [], call.get("ai_responses") or [])
    # The two phases are sequential. Keep their saved speaking order even when an older
    # provider omitted timestamps, and label the caller's human-phase lines too.
    ai_lines = [{**line, "segment": "ai"} for line in lines]
    human_lines = [{**line, "segment": "human"} for line in (call.get("human_transcript") or [])]
    return ai_lines + human_lines


def _browser_owner_names() -> Dict[str, str]:
    """client:browser_<last 12 chars of a user id> -> that user's name, to label browser test calls."""
    return {f"client:browser_{str(row['_id'])[-12:]}": row.get("name") or row.get("email", "") for row in mongo_client.users.find({}, {"name": 1, "email": 1})}


def _display_name(key: str, lead: Optional[Dict[str, Any]], owners: Dict[str, str]) -> str:
    lead_name = str((lead or {}).get("name") or "").strip()
    lead_phone = str((lead or {}).get("phone") or "").strip()
    if key in owners and lead_name and lead_phone and not lead_phone.startswith(("client:", "web:")):
        return lead_name
    if key in owners:
        return f"Browser test · {owners[key]}"
    name = lead_name
    generic = not name or name.startswith(("Inbound caller", "Browser caller", "Web caller"))
    if key.startswith(("web:", "call:")):
        return name if not generic else "Web caller" if key.startswith("web:") else "Unknown caller"
    return key if generic else name


# The sidebar, the list and the open conversation all poll; reuse the thread list briefly instead of
# re-reading every call each time. Any change (realtime.publish) makes it stale immediately.
THREADS_CACHE_SECONDS = 5
_threads_cache: Dict[str, Any] = {"version": -1, "at": 0.0, "rows": []}


def _threads() -> List[Dict[str, Any]]:
    """Every caller thread, newest activity first, with its assignment (cached, see THREADS_CACHE_SECONDS)."""
    now = time.monotonic()
    if _threads_cache["version"] == realtime.version and now - _threads_cache["at"] < THREADS_CACHE_SECONDS:
        return _threads_cache["rows"]
    rows = _build_threads()
    _threads_cache.update(version=realtime.version, at=now, rows=rows)
    return rows


def _build_threads() -> List[Dict[str, Any]]:
    calls = list(mongo_client.calls.find({"direction": {"$ne": "outbound"}}, LIST_FIELDS).sort("call_date", -1).limit(MAX_CALLS_SCANNED))
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for call in calls:
        grouped.setdefault(_thread_key(call), []).append(call)

    lead_ids = {ObjectId(str(call["lead_id"])) for call in calls if call.get("lead_id") and ObjectId.is_valid(str(call["lead_id"]))}
    leads = {str(lead["_id"]): lead for lead in mongo_client.leads.find({"_id": {"$in": list(lead_ids)}}, {"name": 1, "company": 1, "phone": 1})} if lead_ids else {}
    states = {state["_id"]: state for state in mongo_client.inbox_threads.find({"_id": {"$in": list(grouped)}})}
    owners = _browser_owner_names()

    threads = []
    for key, items in grouped.items():
        latest = max(items, key=_when)
        lead = next((leads.get(str(call.get("lead_id"))) for call in items if leads.get(str(call.get("lead_id")))), None)
        lead_phone = str((lead or {}).get("phone") or "").strip()
        display_phone = lead_phone if lead_phone and not lead_phone.startswith(("client:", "web:")) else caller_identity(latest.get("phone_number") or "")
        last_line = (latest.get("messages") or [{}])[-1].get("text") or (latest.get("ai_responses") or [{}])[-1].get("content") or ""
        state = states.get(key, {})
        last_at = _utc(_when(latest))
        closed_at = state.get("closed_at")
        threads.append({
            "key": key, "name": _display_name(key, lead, owners), "phone": display_phone,
            # Closing lasts until the caller calls again (a newer call reopens it), like Podium.
            "closed": isinstance(closed_at, datetime) and last_at <= closed_at, "closed_by_name": state.get("closed_by_name"),
            "last_at": last_at, "preview": last_line or _full_call_summary(latest), "summary": _full_call_summary(latest),
            "call_count": len(items), "status": latest.get("status", "completed"), "provider": latest.get("provider"),
            "failed": str(latest.get("status", "")).lower() in FAILED_STATUSES, "live": any(_is_live(call) for call in items),
            "assigned_to": state.get("assigned_to"), "assigned_to_name": state.get("assigned_to_name"),
            "assigned_by_name": state.get("assigned_by_name"), "assigned_at": state.get("assigned_at"),
            "stage": state.get("stage") if state.get("stage") in STAGE_LABELS else DEFAULT_STAGE,
            "stage_by_name": state.get("stage_by_name"), "stage_at": state.get("stage_at"), "stage_reason": state.get("stage_reason"),
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
    if folder == "assigned":
        return bool(thread["assigned_to"])
    if folder == "failed":
        return thread["failed"]
    return True


def _visible_query(user: Dict[str, Any]) -> Dict[str, Any]:
    """Team messages this user may read: ones without mentions, and ones they wrote or are mentioned in."""
    me = _user_id(user)
    return {"$or": [{"mentions": {"$size": 0}}, {"mentions": {"$exists": False}}, {"author_id": me}, {"mentions": me}]}


def _unread_channel_query(user: Dict[str, Any]) -> Dict[str, Any]:
    """Team channel posts (not conversation notes) this user can see, did not write, and has not read yet."""
    me = _user_id(user)
    return {"$and": [{"workspace_id": user.get("workspace_id", "default"), "conversation_id": {"$in": [None, ""]}, "author_id": {"$ne": me}, "read_by": {"$ne": me}},
                     _visible_query(user)]}


def _read_notifications(user: Dict[str, Any], conversation_id: Optional[str]) -> int:
    """Mark this user's inbox notifications about a conversation (or, with None, the team channel) as read."""
    recipients = [value for value in (user.get("email"), user.get("name")) if value]
    where = {"channel": "inbox", "read": False, "recipient": {"$in": recipients}}
    where["conversation_id"] = conversation_id if conversation_id else {"$in": [None, ""]}
    return mongo_client.notifications.update_many(where, {"$set": {"read": True, "read_at": datetime.utcnow()}}).modified_count


def _mention_query(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"workspace_id": user.get("workspace_id", "default"), "mentions": _user_id(user)}




def notification_link(kind: str, conversation_id: Optional[str]) -> str:
    """The dashboard page a notification opens: the conversation it is about, else the team channel."""
    if not conversation_id:
        return "/inbox/channel"
    return f"/inbox{'/mine' if kind == 'assignment' else ''}?open={quote(conversation_id, safe='')}"


def _notify(recipient: Dict[str, Any], kind: str, title: str, message: str, conversation_id: Optional[str]) -> None:
    mongo_client.notifications.insert_one({"type": kind, "channel": "inbox", "title": title, "message": message,
                                           "recipient": recipient.get("email") or recipient.get("name"), "conversation_id": conversation_id,
                                           "workspace_id": recipient.get("workspace_id", "default"), "link": notification_link(kind, conversation_id),
                                           "created_at": datetime.utcnow(), "read": False})
    logger.info("inbox: notified %s (%s) about %s", recipient.get("email"), kind, conversation_id or "channel")


def _notify_team_operation(user: Dict[str, Any], kind: str, title: str, message: str, conversation_id: str) -> None:
    """Put an operational update in every active inbox member's personal notifications."""
    members = mongo_client.users.find(
        {"workspace_id": user.get("workspace_id", "default"), "active": {"$ne": False}},
        {"name": 1, "email": 1, "role": 1, "permissions": 1, "workspace_id": 1},
    )
    actor_id = _user_id(user)
    for member in members:
        if str(member.get("_id")) != actor_id and has_permission(member, "handle_conversations"):
            _notify(member, kind, title, message, conversation_id)


@router.get("/counts")
def folder_counts(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    threads = _threads()
    threads = [thread for thread in threads if not thread["closed"]]  # every count is of open conversations
    counts = {folder: sum(1 for thread in threads if _in_folder(thread, folder, _user_id(user))) for folder in ("all", "unassigned", "mine", "assigned", "failed")}
    counts["mentioned"] = mongo_client.team_messages.count_documents({**_mention_query(user), "read_by": {"$ne": _user_id(user)}})
    counts["channel"] = mongo_client.team_messages.count_documents(_unread_channel_query(user))
    counts["live"] = sum(1 for thread in threads if thread["live"])
    for stage in STAGE_LABELS:
        counts[f"stage:{stage}"] = sum(1 for thread in threads if thread["stage"] == stage)
    logger.debug("inbox: counts for %s -> %s", user.get("email"), counts)
    return {"success": True, "data": counts}


@router.get("/members")
def team_members(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    return {"success": True, "data": _members(user), "can_assign": _can_assign(user), "me": _user_id(user)}


@router.get("/conversations")
def list_conversations(folder: str = "all", search: str = "", stage: str = "", state: str = "open", limit: int = Query(200, ge=1, le=1000),
                       since: str = "", until: str = "", user: Dict[str, Any] = Depends(require_user)):
    """`since`/`until` (ISO times, e.g. the caller's local midnights): only threads active in that range; in a status
    view (`stage`), threads that moved into that status in the range."""
    _require_db()
    start, end = _when_param(since, "since"), _when_param(until, "until")
    in_range = lambda thread: (lambda at: isinstance(at, datetime) and (not start or at >= start) and (not end or at < end))(
        (thread.get("stage_at") or thread["last_at"]) if stage else thread["last_at"])
    if folder not in FOLDERS or folder == "mentioned":
        raise HTTPException(status_code=400, detail=f"Unknown folder {folder!r}")
    if stage and stage not in STAGE_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown status {stage!r}")
    if state not in CONVERSATION_STATES:
        raise HTTPException(status_code=400, detail=f"Unknown state {state!r}")
    needle = search.strip().lower()
    rows = [thread for thread in _threads() if _in_folder(thread, folder, _user_id(user)) and (not stage or thread["stage"] == stage)
            and (state == "all" or thread["closed"] == (state == "closed")) and (not (start or end) or in_range(thread))
            and (not needle or needle in " ".join(str(thread.get(field) or "") for field in ("name", "phone", "preview", "summary", "assigned_to_name")).lower())]
    logger.info("inbox: %s listed %s (%d %s threads, stage=%r, search=%r, since=%r, until=%r)", user.get("email"), folder, len(rows), state, stage, search, since, until)
    return {"success": True, "data": _serialize(rows[:limit])}


def _thread_name(key: str) -> str:
    return next((thread["name"] for thread in _threads() if thread["key"] == key), "a caller")


@router.post("/conversations/{key:path}/assign")
def assign_conversation(key: str, payload: AssignPayload, user: Dict[str, Any] = Depends(require_user)):
    """Admins assign, move and unassign; team members may claim an unassigned thread for themselves."""
    _require_db()
    admin_assignment = _can_assign(user)
    self_claim = not admin_assignment and payload.user_id == _user_id(user)
    if not admin_assignment and not self_claim:
        logger.warning("inbox: %s (role %s) tried to change the assignment of %s without permission", user.get("email"), user.get("role"), key)
        raise HTTPException(status_code=403, detail="Team members can only claim unassigned conversations for themselves")
    before = (mongo_client.inbox_threads.find_one({"_id": key}) or {}).get("assigned_to")
    if not mongo_client.calls.find_one(_calls_query(key), {"_id": 1}):
        logger.warning("inbox: %s tried to assign missing thread %s", user.get("email"), key)
        raise HTTPException(status_code=404, detail="Conversation not found")
    name = _thread_name(key)
    if payload.user_id is None:
        # This branch is admin-only because a member must never be able to make
        # someone else's work unassigned.
        mongo_client.inbox_threads.update_one({"_id": key}, {"$unset": {"assigned_to": "", "assigned_to_name": "", "assigned_at": ""},
                                                             "$set": {"assigned_by_name": user.get("name"), "updated_at": datetime.utcnow()}}, upsert=True)
        realtime.publish("assignment", key=key, workspace=user.get("workspace_id", "default"))
        logger.info("inbox: %s unassigned thread %s (was %s)", user.get("email"), key, before)
        _event(key, "unassigned", user, f"{user.get('name')} moved it back to Unassigned")
        sync_lead_owner(key, None)
        _notify_team_operation(user, "team_operation", "Conversation moved to Unassigned",
                               f"{user.get('name')} moved the conversation with {name} back to Unassigned.", key)
        return {"success": True}

    assignee = mongo_client.users.find_one({"_id": _object_id(payload.user_id, "user"), "workspace_id": user.get("workspace_id", "default"), "active": {"$ne": False}})
    if not assignee:
        logger.warning("inbox: %s tried to assign %s to unknown user %s", user.get("email"), key, payload.user_id)
        raise HTTPException(status_code=404, detail="Team member not found")
    if not has_permission(assignee, "handle_conversations") or _can_assign(assignee):
        logger.warning("inbox: %s tried to assign %s to %s, who is %s", user.get("email"), key, assignee.get("email"), assignee.get("role"))
        raise HTTPException(status_code=400, detail="Conversations are assigned to team members; admins hand them out")
    if before == str(assignee["_id"]):
        return {"success": True}  # already theirs; no duplicate notification
    now = datetime.utcnow()
    assignment = {"assigned_to": str(assignee["_id"]), "assigned_to_name": assignee.get("name"),
                  "assigned_by_name": user.get("name"), "assigned_at": now, "updated_at": now}
    if self_claim:
        # A member may only win the claim while the thread is still unassigned.
        # The conditional update prevents two agents clicking Claim together
        # from overwriting one another.
        try:
            result = mongo_client.inbox_threads.update_one(
                {"_id": key, "$or": [{"assigned_to": {"$exists": False}}, {"assigned_to": None}, {"assigned_to": ""}]},
                {"$set": assignment}, upsert=True,
            )
        except DuplicateKeyError:
            result = None
        if result is None or (result.matched_count == 0 and result.upserted_id is None):
            latest = mongo_client.inbox_threads.find_one({"_id": key}) or {}
            if latest.get("assigned_to") == _user_id(user):
                return {"success": True}
            raise HTTPException(status_code=409, detail="This conversation was already claimed by another team member")
    else:
        mongo_client.inbox_threads.update_one({"_id": key}, {"$set": assignment}, upsert=True)
    realtime.publish("assignment", key=key, workspace=user.get("workspace_id", "default"))
    action = "claimed" if self_claim else "assigned"
    logger.info("inbox: %s %s thread %s to %s (was %s)", user.get("email"), action, key, assignee.get("email"), before)
    _event(key, "assigned", user, f"{user.get('name')} {action} it")
    logger.info("inbox: %d lead(s) of %s now owned by %s", sync_lead_owner(key, str(assignee["_id"])), key, assignee.get("email"))
    verb = "claimed" if self_claim else f"assigned it to {assignee.get('name')}"
    _notify_team_operation(user, "team_operation", "Conversation assignment changed",
                           f"{user.get('name')} {verb} the conversation with {name}.", key)
    return {"success": True}


def sync_lead_owner(key: str, owner_id: Optional[str]) -> int:
    """The caller's lead follows the conversation: whoever is assigned the thread owns its lead (Leads page)."""
    lead_ids = [ObjectId(str(value)) for value in mongo_client.calls.distinct("lead_id", _calls_query(key)) if value and ObjectId.is_valid(str(value))]
    if not lead_ids:
        return 0
    update = {"$set": {"assigned_to": owner_id}} if owner_id else {"$unset": {"assigned_to": ""}}
    return mongo_client.leads.update_many({"_id": {"$in": lead_ids}}, update).modified_count


def _may_work_on(user: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Status and open/closed: admins change any conversation, a team member only the ones assigned to them."""
    return _can_assign(user) or state.get("assigned_to") == _user_id(user)


@router.post("/conversations/{key:path}/close")
def close_conversation(key: str, payload: ClosePayload, user: Dict[str, Any] = Depends(require_user)):
    """Close (done for now) or reopen a conversation. A new call from the caller reopens it by itself."""
    _require_db()
    state = mongo_client.inbox_threads.find_one({"_id": key}) or {}
    if not _may_work_on(user, state):
        logger.warning("inbox: %s tried to %s %s, which is not assigned to them", user.get("email"), "close" if payload.closed else "reopen", key)
        raise HTTPException(status_code=403, detail="Only admins or the person this conversation is assigned to can close or reopen it")
    if not mongo_client.calls.find_one(_calls_query(key), {"_id": 1}):
        raise HTTPException(status_code=404, detail="Conversation not found")
    if payload.closed:
        mongo_client.inbox_threads.update_one({"_id": key}, {"$set": {"closed_at": datetime.utcnow(), "closed_by_name": user.get("name")}}, upsert=True)
        _event(key, "closed", user, f"Conversation closed by {user.get('name')}")
        operation_title, operation_message = "Conversation closed", f"{user.get('name')} closed the conversation with {_thread_name(key)}."
    else:
        mongo_client.inbox_threads.update_one({"_id": key}, {"$unset": {"closed_at": "", "closed_by_name": ""}}, upsert=True)
        _event(key, "reopened", user, f"Conversation reopened by {user.get('name')}")
        operation_title, operation_message = "Conversation reopened", f"{user.get('name')} reopened the conversation with {_thread_name(key)}."
    _notify_team_operation(user, "team_operation", operation_title, operation_message, key)
    realtime.publish("close", key=key, workspace=user.get("workspace_id", "default"))
    logger.info("inbox: %s %s thread %s", user.get("email"), "closed" if payload.closed else "reopened", key)
    return {"success": True}


@router.post("/conversations/{key:path}/stage")
def set_stage(key: str, payload: StagePayload, user: Dict[str, Any] = Depends(require_user)):
    """Move a conversation to another pipeline stage. Admins can move any; a team member only the ones assigned to them."""
    _require_db()
    if payload.stage not in STAGE_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown status {payload.stage!r}")
    state = mongo_client.inbox_threads.find_one({"_id": key}) or {}
    if not _may_work_on(user, state):
        logger.warning("inbox: %s tried to change the status of %s, which is not assigned to them", user.get("email"), key)
        raise HTTPException(status_code=403, detail="Only admins or the person this conversation is assigned to can change its status")
    if not mongo_client.calls.find_one(_calls_query(key), {"_id": 1}):
        raise HTTPException(status_code=404, detail="Conversation not found")
    before = state.get("stage") or DEFAULT_STAGE
    if before == payload.stage:
        return {"success": True}
    now = datetime.utcnow()
    # stage_by marks a person's choice: the AI's automatic status (call_status.py) never overrides it.
    mongo_client.inbox_threads.update_one({"_id": key}, {"$set": {"stage": payload.stage, "stage_by": _user_id(user), "stage_by_name": user.get("name"), "stage_at": now, "updated_at": now},
                                                         "$push": {"stage_history": {"from": before, "to": payload.stage, "by": _user_id(user), "by_name": user.get("name"), "at": now}}}, upsert=True)
    realtime.publish("stage", key=key, workspace=user.get("workspace_id", "default"))
    logger.info("inbox: %s moved thread %s from %s to %s", user.get("email"), key, before, payload.stage)
    _notify_team_operation(user, "team_operation", "Conversation status changed",
                           f"{user.get('name')} moved the conversation with {_thread_name(key)} to {STAGE_LABELS[payload.stage]}.", key)
    return {"success": True}


@router.put("/conversations/{key:path}/profile")
def update_profile(key: str, payload: ProfilePayload, user: Dict[str, Any] = Depends(require_user)):
    """Edit the caller's contact details from the Contact panel. Writes the caller's lead and customer records."""
    _require_db()
    state = mongo_client.inbox_threads.find_one({"_id": key}) or {}
    if not _may_work_on(user, state):
        logger.warning("inbox: %s tried to edit the contact of %s, which is not assigned to them", user.get("email"), key)
        raise HTTPException(status_code=403, detail="Only admins or the person this conversation is assigned to can edit the contact")
    calls = list(mongo_client.calls.find(_calls_query(key), {"lead_id": 1, "phone_number": 1, "call_date": 1}).sort("call_date", -1))
    if not calls:
        raise HTTPException(status_code=404, detail="Conversation not found")
    changes = {field: value.strip() if isinstance(value, str) else value for field, value in payload.model_dump(exclude_none=True).items()}
    if "tags" in changes:
        changes["tags"] = [tag.strip() for tag in changes["tags"] if tag.strip()][:20]
    if not changes:
        return {"success": True}
    now = utc_now()
    lead_id = next((str(call["lead_id"]) for call in calls if call.get("lead_id") and ObjectId.is_valid(str(call["lead_id"]))), None)
    lead_fields = {field: changes[field] for field in ("name", "phone", "email", "company") if field in changes}
    if lead_id and lead_fields:
        mongo_client.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": {**lead_fields, "updated_at": now}})
    lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id)}, {"phone": 1}) if lead_id else None
    phone = (lead or {}).get("phone") or calls[0].get("phone_number")
    customer_fields = {"location" if field == "address" else field: value for field, value in changes.items() if field in ("name", "email", "address", "tags")}
    if phone and customer_fields:
        mongo_client.customers.update_one({"phone": phone}, {"$set": {**customer_fields, "updated_at": now}, "$setOnInsert": {"phone": phone, "created_at": now}}, upsert=True)
    _event(key, "profile", user, f"{user.get('name')} updated the contact ({', '.join(sorted(changes))})")
    realtime.publish("profile", key=key, workspace=user.get("workspace_id", "default"))
    logger.info("inbox: %s edited the contact of %s: %s", user.get("email"), key, sorted(changes))
    return {"success": True}


@router.get("/lead/{lead_id}")
def conversation_for_lead(lead_id: str, user: Dict[str, Any] = Depends(require_user)):
    """The inbox conversation a lead belongs to (its latest call's caller), for the lead page's "Open conversation" link."""
    _require_db()
    call = mongo_client.calls.find_one({"lead_id": {"$in": [lead_id, _object_id(lead_id, "lead")]}, "direction": {"$ne": "outbound"}}, sort=[("call_date", -1)])
    if not call:
        raise HTTPException(status_code=404, detail="This lead has no inbox conversation yet")
    key = _thread_key(call)
    logger.info("inbox: %s looked up the conversation for lead %s -> %s", user.get("email"), lead_id, key)
    return {"success": True, "data": {"key": key, "link": f"/inbox?open={quote(key, safe='')}"}}


@router.get("/conversations/{key:path}")
def get_conversation(key: str, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    calls = sorted(mongo_client.calls.find(_calls_query(key)), key=_when)
    if not calls:
        logger.warning("inbox: %s opened missing thread %s", user.get("email"), key)
        raise HTTPException(status_code=404, detail="Conversation not found")
    thread = next((item for item in _threads() if item["key"] == key), None) or {"key": key, "name": key}
    history = [{"id": str(call["_id"]), "at": _utc(_when(call)), "phone": caller_identity(call.get("phone_number") or ""), "status": call.get("status", "completed"), "live": _is_live(call),
                "failed": str(call.get("status", "")).lower() in FAILED_STATUSES, "provider": call.get("provider") or "voice",
                "channel_type": call.get("channel_type"), "duration": call.get("duration") or 0, "summary": _full_call_summary(call),
                "recording_url": call.get("recording_url"), "recording_sid": call.get("recording_sid"),
                "full_recording_url": call.get("full_recording_url"), "full_recording_sid": call.get("full_recording_sid"),
                "transfer_status": call.get("transfer_status"), "human_callback_requested": bool(call.get("human_callback_requested")),
                "disconnection_reason": call.get("disconnection_reason"),
                "messages": _call_messages(call)} for call in calls]
    discussion = list(mongo_client.team_messages.find({"workspace_id": user.get("workspace_id", "default"), "conversation_id": key, **_visible_query(user)}).sort("created_at", 1))
    read = _read_notifications(user, key)
    logger.info("inbox: %s opened thread %s (%d calls, live=%s, %d notifications read)", user.get("email"), key, len(history), thread.get("live"), read)
    state = mongo_client.inbox_threads.find_one({"_id": key}, {"stage_history": 1, "events": 1}) or {}
    events = (state.get("events") or [])[-100:] + [
        {"type": "stage", "text": f"{change.get('by_name') or 'Someone'} changed the status to {STAGE_LABELS.get(change.get('to'), change.get('to'))}"
                                 + (f": {change['reason']}" if change.get("reason") else ""),
         # Structured too, so the conversation can show "Moved to Outreach" with the reason underneath.
         "to": change.get("to"), "to_label": STAGE_LABELS.get(change.get("to"), change.get("to")), "reason": change.get("reason"),
         "by_name": change.get("by_name"), "at": change.get("at")} for change in (state.get("stage_history") or [])[-50:]]
    return {"success": True, "data": _serialize({**thread, "calls": history, "discussion": discussion, "stage_history": (state.get("stage_history") or [])[-50:],
                                                 "events": sorted(events, key=lambda event: event.get("at") or datetime.min), "profile": _profile(calls)})}


def _profile(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Contact details for the contact panel, from the caller's lead and customer records when there are any."""
    lead_id = next((str(call["lead_id"]) for call in calls if call.get("lead_id") and ObjectId.is_valid(str(call["lead_id"]))), None)
    lead = (mongo_client.leads.find_one({"_id": ObjectId(lead_id)}) if lead_id else None) or {}
    phone = lead.get("phone") or calls[-1].get("phone_number")
    customer = (mongo_client.customers.find_one({"phone": phone}) if phone else None) or {}
    return {"name": lead.get("name") or customer.get("name"), "phone": phone, "email": lead.get("email") or customer.get("email"), "company": lead.get("company"),
            "address": customer.get("location"), "tags": customer.get("tags") or [],
            "source": lead.get("source") or ("Web call" if str(phone or "").startswith("client:") else "Phone"),
            "lead_status": lead.get("status") or customer.get("lead_status"), "created_at": _utc(lead.get("created_at") or customer.get("created_at") or _when(calls[0])),
            "notes": lead.get("notes") or customer.get("notes"), "lead_id": lead_id}


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
    """Load a team message the user may change: authors edit/delete their own, admins may delete any they can see."""
    message = mongo_client.team_messages.find_one({"_id": _object_id(message_id, "message"), "workspace_id": user.get("workspace_id", "default")})
    if message and not mongo_client.team_messages.find_one({"_id": message["_id"], **_visible_query(user)}, {"_id": 1}):
        message = None  # a message mentioning other people does not exist for this user
    if not message:
        logger.warning("inbox: %s tried to %s missing message %s", user.get("email"), action, message_id)
        raise HTTPException(status_code=404, detail="Message not found")
    is_author = message.get("author_id") == _user_id(user)
    if not is_author and not (action == "delete" and _can_assign(user)):
        logger.warning("inbox: %s tried to %s message %s by %s", user.get("email"), action, message_id, message.get("author_name"))
        raise HTTPException(status_code=403, detail=f"You can only {action} your own messages")
    return message


@router.get("/channel")
def channel_messages(limit: int = Query(200, ge=1, le=500), user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    rows = list(mongo_client.team_messages.find({"workspace_id": user.get("workspace_id", "default"), **_visible_query(user)}).sort("created_at", -1).limit(limit))
    rows.reverse()
    logger.debug("inbox: %s read team channel (%d messages)", user.get("email"), len(rows))
    return {"success": True, "data": _serialize(rows)}


@router.post("/channel/read")
def mark_channel_read(user: Dict[str, Any] = Depends(require_user)):
    """Opening the team channel: its posts and the bell notifications about it count as read."""
    _require_db()
    messages = mongo_client.team_messages.update_many(_unread_channel_query(user), {"$addToSet": {"read_by": _user_id(user)}}).modified_count
    notes = _read_notifications(user, None)
    if messages or notes:
        logger.info("inbox: %s read the team channel (%d messages, %d notifications)", user.get("email"), messages, notes)
    return {"success": True, "messages": messages, "notifications": notes}


@router.post("/channel")
def post_channel_message(payload: ChannelMessagePayload, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    members, mentions = _resolve_mentions(payload.mentions, user)
    message = {"workspace_id": user.get("workspace_id", "default"), "author_id": _user_id(user), "author_name": user.get("name") or user.get("email"),
               "text": text, "mentions": mentions, "mention_names": [members[user_id]["name"] for user_id in mentions],
               "conversation_id": payload.conversation_id or None, "read_by": [_user_id(user)], "created_at": datetime.utcnow()}
    message["_id"] = mongo_client.team_messages.insert_one(message).inserted_id
    realtime.publish("messages", key=payload.conversation_id, workspace=user.get("workspace_id", "default"))
    logger.info("inbox: %s posted to team channel (message %s, %d mentions, thread %s, %s)", user.get("email"), message["_id"], len(mentions), payload.conversation_id,
                "visible to the mentioned people only" if mentions else "visible to the team")
    for user_id in mentions:
        if user_id != _user_id(user):
            _notify(members[user_id], "mention", f"{message['author_name']} mentioned you", text[:200], payload.conversation_id)
    if not mentions and not payload.conversation_id:
        # A message for the whole team: everyone else hears about it.
        for member in members.values():
            if member["id"] != _user_id(user):
                _notify(member, "channel_message", f"{message['author_name']} posted in Team channel", text[:200], None)
    elif not mentions:
        # A note on a conversation: tell whoever is working it.
        owner = (mongo_client.inbox_threads.find_one({"_id": payload.conversation_id}) or {}).get("assigned_to")
        if owner and owner != _user_id(user) and owner in members:
            _notify(members[owner], "note", f"{message['author_name']} added a note", text[:200], payload.conversation_id)
    return {"success": True, "data": _serialize(message)}


@router.put("/channel/{message_id}")
def edit_channel_message(message_id: str, payload: MessageEditPayload, user: Dict[str, Any] = Depends(require_user)):
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
    realtime.publish("messages", key=message.get("conversation_id"), workspace=user.get("workspace_id", "default"))
    logger.info("inbox: %s edited message %s (%d mentions, %d new)", user.get("email"), message_id, len(mentions), len(added))
    for user_id in added:
        if user_id != _user_id(user):
            _notify(members[user_id], "mention", f"{message.get('author_name')} mentioned you", text[:200], message.get("conversation_id"))
    return {"success": True, "data": _serialize({**message, **updates})}


@router.delete("/channel/{message_id}")
def delete_channel_message(message_id: str, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    message = _own_message(message_id, user, "delete")
    mongo_client.team_messages.delete_one({"_id": message["_id"]})
    realtime.publish("messages", key=message.get("conversation_id"), workspace=user.get("workspace_id", "default"))
    logger.info("inbox: %s deleted message %s by %s (thread %s)", user.get("email"), message_id, message.get("author_name"), message.get("conversation_id"))
    return {"success": True}


@router.get("/mentions")
def my_mentions(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    rows = list(mongo_client.team_messages.find(_mention_query(user)).sort("created_at", -1).limit(200))
    logger.info("inbox: %s listed mentions (%d)", user.get("email"), len(rows))
    return {"success": True, "data": _serialize(rows)}


@router.post("/mentions/read")
def mark_mentions_read(user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    result = mongo_client.team_messages.update_many({**_mention_query(user), "read_by": {"$ne": _user_id(user)}}, {"$addToSet": {"read_by": _user_id(user)}})
    logger.info("inbox: %s marked %d mentions read", user.get("email"), result.modified_count)
    return {"success": True, "updated": result.modified_count}
