#!/usr/bin/env python3
"""MongoDB-backed lead CRM API."""

from datetime import datetime
from timeutil import utc_iso, utc_now
import csv
import io
import logging
import mimetypes
import os
from pathlib import Path as FilePath
from uuid import uuid4
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import Depends, APIRouter, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from phonenumbers import NumberParseException, PhoneNumberFormat, format_number, is_valid_number, parse
from pydantic import BaseModel, Field
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from env_loader import load_project_env
from mongo_client import mongo_client
from routers.calls_api import log_call, update_lead_status_from_call
import realtime
from twilio_security import verify_twilio_request
load_project_env()

from auth_service import can_assign_conversations, require_permission, require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/leads", tags=["Leads Management"], dependencies=[Depends(require_permission("manage_assigned_leads"))])  # dashboard API: sign-in required for every route
sms_router = APIRouter(prefix="/api/twilio", tags=["SMS webhooks"])
public_router = APIRouter(prefix="/api/leads", tags=["Lead media"])
SMS_MEDIA_ROOT = FilePath(__file__).resolve().parents[1] / "media" / "sms"
SMS_MEDIA_TYPES = {"image/", "audio/", "video/", "application/pdf"}
MAX_SMS_MEDIA_BYTES = 5 * 1024 * 1024


class Lead(BaseModel):
    id: Optional[str] = None
    name: str
    phone: str
    email: Optional[str] = ""
    company: Optional[str] = ""
    notes: Optional[str] = ""
    status: str = "new"
    call_attempts: int = 0
    last_call: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LeadUpdate(BaseModel):
    assigned_to: Optional[str] = None  # admins only; "" takes the lead back
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class SmsMessage(BaseModel):
    body: str = Field(default="", max_length=1600)
    media_urls: List[str] = Field(default_factory=list, max_items=4)
    media_names: List[str] = Field(default_factory=list, max_items=4)


def is_valid_object_id(value: str) -> bool:
    return isinstance(value, str) and len(value) == 24 and all(c in "0123456789abcdefABCDEF" for c in value)


def clean_phone_number(phone_str):
    if isinstance(phone_str, str):
        return int(phone_str.replace("+", "").replace("-", "").replace(" ", ""))
    return phone_str


def _require_db():
    if not mongo_client.is_connected():
        raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})


def owner_scope(user: Dict[str, Any]) -> Dict[str, Any]:
    """Leads a user may see: admins (who assign work) see all; a team member sees the leads assigned to them."""
    return {} if can_assign_conversations(user) else {"assigned_to": str(user.get("_id") or user.get("id"))}


def _serialize_lead(lead_doc: Dict[str, Any]) -> Dict[str, Any]:
    lead_doc["_id"] = str(lead_doc["_id"])
    return lead_doc


def _lead_sms_number(value: str) -> str:
    """Normalize the lead's stored phone number; the API never accepts a separate recipient."""
    raw = str(value or "").strip()
    if not raw or raw.lower().startswith(("web:", "client:")):
        raise HTTPException(status_code=400, detail={"success": False, "error": "This lead does not have an SMS-capable phone number"})
    try:
        parsed = parse(raw, os.getenv("TWILIO_DEFAULT_REGION", "AU"))
    except NumberParseException as exc:
        raise HTTPException(status_code=400, detail={"success": False, "error": "The lead phone number is invalid"}) from exc
    if not is_valid_number(parsed):
        raise HTTPException(status_code=400, detail={"success": False, "error": "The lead phone number is invalid"})
    return format_number(parsed, PhoneNumberFormat.E164)


def _sms_media_public_base() -> str:
    return (os.getenv("TWILIO_PUBLIC_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")


@router.post("/{lead_id}/sms-media")
async def upload_lead_sms_media(
    lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$"),
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_user),
):
    """Store a small MMS attachment and return a public URL Twilio can fetch."""
    _require_db()
    lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id), **owner_scope(user)}, {"_id": 1})
    if not lead:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})
    content_type = str(file.content_type or "").lower().strip()
    if not any(content_type.startswith(prefix) for prefix in SMS_MEDIA_TYPES):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Attach an image, audio, video, or PDF file"})
    media_base = _sms_media_public_base()
    if not media_base.startswith("https://"):
        raise HTTPException(status_code=503, detail={"success": False, "error": "Media messaging needs a public HTTPS URL"})

    payload = await file.read(MAX_SMS_MEDIA_BYTES + 1)
    if len(payload) > MAX_SMS_MEDIA_BYTES:
        raise HTTPException(status_code=413, detail={"success": False, "error": "Attachments must be 5 MB or smaller"})
    extension = FilePath(file.filename or "").suffix.lower()[:10] or mimetypes.guess_extension(content_type) or ".bin"
    filename = f"{uuid4().hex}{extension}"
    SMS_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    (SMS_MEDIA_ROOT / filename).write_bytes(payload)
    mongo_client.db.sms_media.update_one(
        {"filename": filename},
        {"$set": {"filename": filename, "original_name": file.filename or filename, "content_type": content_type, "lead_id": lead_id, "created_at": utc_now()}},
        upsert=True,
    )
    return {"success": True, "data": {"url": f"{media_base}/api/leads/sms-media/{filename}", "name": file.filename or filename, "content_type": content_type}}


@public_router.get("/sms-media/{filename}", include_in_schema=False)
def get_public_sms_media(filename: str):
    """Public, randomised media URL used by Twilio for MMS delivery."""
    safe_name = FilePath(filename).name
    path = SMS_MEDIA_ROOT / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")
    return FileResponse(path, media_type=mimetypes.guess_type(safe_name)[0] or "application/octet-stream")


def _call_counts(lead_ids: List[Any]) -> Dict[str, int]:
    """Calls linked to each lead (calls store lead_id as a string or an ObjectId)."""
    if not lead_ids:
        return {}
    keys = [str(lead_id) for lead_id in lead_ids] + [ObjectId(str(lead_id)) for lead_id in lead_ids if ObjectId.is_valid(str(lead_id))]
    rows = mongo_client.calls.aggregate([
        {"$match": {"lead_id": {"$in": keys}}},
        {"$group": {"_id": {"$toString": "$lead_id"}, "count": {"$sum": 1}}},
    ])
    return {row["_id"]: row["count"] for row in rows}


def get_leads(filters: Dict | None = None, limit: int = 50, skip: int = 0, scope: Dict[str, Any] | None = None):
    _require_db()

    query: Dict[str, Any] = dict(scope or {})
    if filters:
        if filters.get("status"):
            query["status"] = filters["status"]
        if filters.get("search"):
            search_term = filters["search"]
            query["$or"] = [
                {"name": {"$regex": search_term, "$options": "i"}},
                {"phone": {"$regex": search_term, "$options": "i"}},
                {"email": {"$regex": search_term, "$options": "i"}},
                {"company": {"$regex": search_term, "$options": "i"}},
            ]

    leads = list(mongo_client.leads.find(query).sort("created_at", -1).skip(skip).limit(limit))
    # The stored call_attempts counter was never increased when calls came in (every lead showed 0), so the
    # Calls column is counted from the calls actually linked to each lead, in one query for the page.
    counts = _call_counts([lead["_id"] for lead in leads])
    for lead in leads:
        lead["call_attempts"] = counts.get(str(lead["_id"]), 0)
        _serialize_lead(lead)

    return {
        "success": True,
        "data": leads,
        "total": mongo_client.leads.count_documents(query),
        "limit": limit,
        "skip": skip,
    }


def add_lead(lead_data: Dict[str, Any], owner: Optional[str] = None):
    _require_db()
    if not lead_data.get("name") or not lead_data.get("phone"):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Name and phone are required"})

    if mongo_client.leads.find_one({"phone": lead_data["phone"]}):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Phone number already exists"})

    lead_doc = {
        "name": lead_data["name"],
        "phone": lead_data["phone"],
        "email": lead_data.get("email", ""),
        "company": lead_data.get("company", ""),
        "notes": lead_data.get("notes", ""),
        "status": "new",
        "call_attempts": 0,
        "last_call": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        **({"assigned_to": owner} if owner else {}),
    }
    result = mongo_client.leads.insert_one(lead_doc)
    lead_doc["_id"] = str(result.inserted_id)
    return {"success": True, "data": lead_doc}


def update_lead(lead_id: str, lead_data: Dict[str, Any], scope: Dict[str, Any] | None = None):
    _require_db()
    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    existing_phone = lead_data.get("phone")
    if existing_phone:
        duplicate = mongo_client.leads.find_one({"phone": existing_phone, "_id": {"$ne": ObjectId(lead_id)}})
        if duplicate:
            raise HTTPException(status_code=400, detail={"success": False, "error": "Phone number already exists"})

    update: Dict[str, Any] = {"$set": {**lead_data, "updated_at": utc_now()}}
    if lead_data.get("assigned_to") == "":
        update["$set"].pop("assigned_to")
        update["$unset"] = {"assigned_to": ""}
    result = mongo_client.leads.update_one({"_id": ObjectId(lead_id), **(scope or {})}, update)
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

    updated = mongo_client.leads.find_one({"_id": ObjectId(lead_id)})
    return {"success": True, "data": _serialize_lead(updated)}


def delete_lead(lead_id: str, scope: Dict[str, Any] | None = None):
    _require_db()
    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    result = mongo_client.leads.delete_one({"_id": ObjectId(lead_id), **(scope or {})})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})
    return {"success": True, "message": "Lead deleted successfully"}


def get_lead_by_id(lead_id: str, scope: Dict[str, Any] | None = None):
    _require_db()
    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id), **(scope or {})})
    if not lead:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})
    return {"success": True, "data": _serialize_lead(lead)}


def get_leads_stats(scope: Dict[str, Any] | None = None):
    _require_db()
    scope = scope or {}
    total_leads = mongo_client.leads.count_documents(scope)
    status_counts = {status: mongo_client.leads.count_documents({**scope, "status": status}) for status in ["new", "called", "contacted", "converted"]}
    lead_ids = [row["_id"] for row in mongo_client.leads.find(scope, {"_id": 1})]
    total_calls = sum(_call_counts(lead_ids).values())
    return {
        "success": True,
        "data": {
            "total": total_leads,
            "new": status_counts["new"],
            "called": status_counts["called"],
            "contacted": status_counts["contacted"],
            "converted": status_counts["converted"],
            "total_calls": total_calls,
        },
    }


@router.get("/")
def get_leads_endpoint(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    user: Dict[str, Any] = Depends(require_user),
):
    filters: Dict[str, Any] = {}
    if status:
        filters["status"] = status
    if search:
        filters["search"] = search
    return get_leads(filters, limit, skip, owner_scope(user))


@router.post("/")
def add_lead_endpoint(lead: Lead, user: Dict[str, Any] = Depends(require_user)):
    # A team member's new lead is theirs; an admin's starts unassigned.
    return add_lead(lead.dict(exclude_unset=True), owner=owner_scope(user).get("assigned_to"))


@router.get("/stats")
def get_leads_stats_endpoint(user: Dict[str, Any] = Depends(require_user)):
    return get_leads_stats(owner_scope(user))


@router.post("/upload")
async def upload_leads_csv(file: UploadFile = File(...), user: Dict[str, Any] = Depends(require_user)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail={"success": False, "error": "File must be a CSV"})

    _require_db()
    content = await file.read()
    stream = io.StringIO(content.decode("utf-8"))
    csv_reader = csv.DictReader(stream)

    imported_count = 0
    errors = []
    for row_num, row in enumerate(csv_reader, start=2):
        try:
            if not row.get("name") or not row.get("phone"):
                errors.append(f"Row {row_num}: Missing name or phone")
                continue

            if mongo_client.leads.find_one({"phone": row["phone"]}):
                errors.append(f"Row {row_num}: Phone number {row['phone']} already exists")
                continue

            lead_doc = {
                    "name": row["name"].strip(),
                    "phone": row["phone"].strip(),
                    "email": row.get("email", "").strip(),
                    "company": row.get("company", "").strip(),
                    "notes": row.get("notes", "").strip(),
                    "status": "new",
                    "call_attempts": 0,
                    "last_call": None,
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    **owner_scope(user),  # a team member's import is theirs
                }
            result = mongo_client.leads.insert_one(lead_doc)
            imported_count += 1
        except Exception as exc:
            errors.append(f"Row {row_num}: {str(exc)}")

    return {
        "success": True,
        "imported_count": imported_count,
        "errors": errors,
        "message": f"Successfully imported {imported_count} leads",
    }


@router.get("/health")
def health_check():
    return {
        "success": True,
        "message": "Leads API is running",
        "database": mongo_client.get_connection_status(),
        "timestamp": utc_iso(),
    }


@router.post("/{lead_id}/call")
def call_lead_endpoint(lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$")):
    raise HTTPException(status_code=410, detail="Outbound calling is disabled in this inbound-only project")


@router.get("/{lead_id}/sms")
def get_lead_sms(
    lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$"),
    user: Dict[str, Any] = Depends(require_user),
):
    """Return the lead's outbound SMS thread for the lead detail chat window."""
    _require_db()
    lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id), **owner_scope(user)}, {"_id": 1})
    if not lead:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

    messages = list(
        mongo_client.sms_messages.find(
            {"lead_id": lead_id, "workspace_id": user.get("workspace_id", "default")}
        ).sort("created_at", 1).limit(100)
    )
    for message in messages:
        message["_id"] = str(message["_id"])
        message["direction"] = message.get("direction") or "outbound"
        message["media_urls"] = list(message.get("media_urls") or [])
        message["media_names"] = list(message.get("media_names") or [])
        message["media_attachments"] = [
            {"url": url, "name": message["media_names"][index] if index < len(message["media_names"]) and message["media_names"][index] else url.rsplit("/", 1)[-1] or "Attachment"}
            for index, url in enumerate(message["media_urls"])
        ]
    return {"success": True, "data": messages}


@router.post("/{lead_id}/sms")
def send_lead_sms(
    lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$"),
    payload: SmsMessage = None,
    user: Dict[str, Any] = Depends(require_user),
):
    """Send through Twilio to the lead's stored number, never to a team member number."""
    _require_db()
    if payload is None or (not payload.body.strip() and not payload.media_urls):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Add a message or attachment"})

    lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id), **owner_scope(user)})
    if not lead:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = (os.getenv("TWILIO_SMS_FROM") or os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or "").strip()
    if not account_sid or not auth_token or not from_number:
        raise HTTPException(status_code=503, detail={"success": False, "error": "SMS is not configured. Add Twilio credentials and TWILIO_SMS_FROM."})

    to_number = _lead_sms_number(lead.get("phone"))
    body = payload.body.strip()
    media_urls = [str(url).strip() for url in payload.media_urls if str(url).strip()]
    media_names = [str(name).strip()[:180] for name in payload.media_names[:len(media_urls)]]
    media_names.extend(["" for _ in range(len(media_names), len(media_urls))])
    for index, url in enumerate(media_urls):
        if media_names[index] and media_names[index].lower() != "attachment":
            continue
        metadata = mongo_client.db.sms_media.find_one({"filename": FilePath(url.split("/", 1)[-1]).name}, {"original_name": 1})
        media_names[index] = str((metadata or {}).get("original_name") or "Attachment")[:180]
    media_base = _sms_media_public_base()
    if any(not media_base or not url.startswith(f"{media_base}/api/leads/sms-media/") for url in media_urls):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Attachment URL is not valid for this workspace"})

    try:
        client = Client(account_sid, auth_token)
        create_kwargs = {"body": body, "from_": from_number, "to": to_number}
        if media_urls:
            create_kwargs["media_url"] = media_urls
        status_base = _sms_media_public_base()
        if status_base.startswith("https://"):
            create_kwargs["status_callback"] = f"{status_base}/api/twilio/sms/status"
        message = client.messages.create(**create_kwargs)
    except TwilioRestException as exc:
        logger.exception("Twilio could not send lead SMS for %s", lead_id)
        raise HTTPException(status_code=502, detail={"success": False, "error": "Twilio could not send the message"}) from exc

    now = utc_now()
    mongo_client.sms_messages.insert_one({
        "workspace_id": user.get("workspace_id", "default"),
        "lead_id": lead_id,
        "to": to_number,
        "from": from_number,
        "body": body,
        "media_urls": media_urls,
        "media_names": media_names,
        "direction": "outbound",
        "provider": "twilio",
        "provider_message_id": message.sid,
        "num_segments": message.num_segments,
        "price": message.price,
        "status": message.status or "queued",
        "created_by": str(user.get("_id")),
        "created_at": now,
    })
    mongo_client.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": {"last_sms_at": now, "updated_at": now}})
    realtime.publish("sms", key=lead.get("browser_identity") or f"lead:{lead_id}", workspace=user.get("workspace_id", "default"))
    logger.info("Lead SMS sent by %s to lead %s via Twilio (%s)", user.get("email"), lead_id, message.sid)
    return {"success": True, "data": {"message_id": message.sid, "status": message.status, "num_segments": message.num_segments, "to": to_number}}


@sms_router.post("/sms/status", response_class=PlainTextResponse, dependencies=[Depends(verify_twilio_request)])
async def receive_sms_status(request: Request):
    """Update the message tick state from Twilio delivery callbacks."""
    _require_db()
    form = dict(await request.form())
    provider_id = str(form.get("MessageSid") or "").strip()
    status = str(form.get("MessageStatus") or form.get("SmsStatus") or "").strip().lower()
    if not provider_id or not status:
        return PlainTextResponse("<Response></Response>", media_type="text/xml")
    result = mongo_client.sms_messages.update_one(
        {"provider_message_id": provider_id},
        {"$set": {"status": status, "error_code": str(form.get("ErrorCode") or ""), "updated_at": utc_now()}},
    )
    if result.matched_count:
        message = mongo_client.sms_messages.find_one({"provider_message_id": provider_id}, {"lead_id": 1}) or {}
        lead = mongo_client.leads.find_one({"_id": ObjectId(str(message.get("lead_id")))}, {"browser_identity": 1}) if ObjectId.is_valid(str(message.get("lead_id"))) else None
        realtime.publish("sms", key=(lead or {}).get("browser_identity") or f"lead:{message.get('lead_id', '')}")
    return PlainTextResponse("<Response></Response>", media_type="text/xml")


@sms_router.post("/sms/inbound", response_class=PlainTextResponse, dependencies=[Depends(verify_twilio_request)])
async def receive_inbound_sms(request: Request):
    """Persist an inbound SMS/MMS reply in the same one-to-one lead timeline."""
    _require_db()
    form = dict(await request.form())
    raw_from = str(form.get("From") or "").strip()
    if not raw_from:
        return PlainTextResponse("<Response></Response>", media_type="text/xml")
    try:
        from_number = _lead_sms_number(raw_from)
    except HTTPException:
        from_number = raw_from
    lead = mongo_client.leads.find_one({"phone": {"$in": [from_number, raw_from]}})
    now = utc_now()
    if not lead:
        lead = {
            "name": str(form.get("ProfileName") or from_number),
            "phone": from_number,
            "email": "",
            "company": "",
            "notes": "Created from an inbound SMS.",
            "status": "new",
            "source": "sms",
            "call_attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
        lead["_id"] = mongo_client.leads.insert_one(lead).inserted_id
    lead_id = str(lead["_id"])
    media_urls = [str(form.get(f"MediaUrl{index}") or "").strip() for index in range(4)]
    media_urls = [url for url in media_urls if url]
    media_names = [str(form.get(f"MediaContentType{index}") or "Attachment").split("/")[-1].upper() + " attachment" for index in range(len(media_urls))]
    mongo_client.sms_messages.insert_one({
        "workspace_id": lead.get("workspace_id", "default"),
        "lead_id": lead_id,
        "to": str(form.get("To") or ""),
        "from": from_number,
        "body": str(form.get("Body") or "").strip(),
        "media_urls": media_urls,
        "media_names": media_names,
        "direction": "inbound",
        "provider": "twilio",
        "provider_message_id": str(form.get("MessageSid") or ""),
        "status": "received",
        "created_at": now,
    })
    mongo_client.leads.update_one({"_id": lead["_id"]}, {"$set": {"last_sms_at": now, "updated_at": now}})
    key = lead.get("browser_identity") or f"lead:{lead_id}"
    linked_call = mongo_client.calls.find_one({"lead_id": {"$in": [lead_id, lead["_id"]]}}, {"phone_number": 1})
    if linked_call:
        from routers.calls_api import caller_identity
        key = caller_identity(linked_call.get("phone_number") or "") or key
    realtime.publish("sms", key=key)
    return PlainTextResponse("<Response></Response>", media_type="text/xml")


@router.get("/{lead_id}")
def get_lead_endpoint(lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$"), user: Dict[str, Any] = Depends(require_user)):
    return get_lead_by_id(lead_id, owner_scope(user))


@router.put("/{lead_id}")
def update_lead_endpoint(lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$"), lead: LeadUpdate = None, user: Dict[str, Any] = Depends(require_user)):
    lead_data = (lead or LeadUpdate()).dict(exclude_unset=True)
    if "assigned_to" in lead_data and not can_assign_conversations(user):
        raise HTTPException(status_code=403, detail={"success": False, "error": "Only admins can change who owns a lead"})
    if lead_data.get("assigned_to") and not mongo_client.users.find_one({"_id": ObjectId(lead_data["assigned_to"])} if is_valid_object_id(lead_data["assigned_to"]) else {"_id": None}):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Team member not found"})
    return update_lead(lead_id, lead_data, owner_scope(user))


@router.delete("/{lead_id}")
def delete_lead_endpoint(lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$"), user: Dict[str, Any] = Depends(require_user)):
    return delete_lead(lead_id, owner_scope(user))
