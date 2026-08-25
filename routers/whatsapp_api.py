import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from twilio.rest import Client

from ai_services import AIServices
from mongo_client import mongo_client
from qa_engine import DynamicQA
from brain_service import brain_service


router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
WHATSAPP_AUTO_REPLY_ENABLED = os.getenv("WHATSAPP_AUTO_REPLY_ENABLED", "true").strip().lower() not in {"false", "0", "no"}


class SendWhatsAppRequest(BaseModel):
    to: str
    text: str
    conversation_id: Optional[str] = None


class ConversationControlRequest(BaseModel):
    ai_enabled: Optional[bool] = None
    assigned_to: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[List[str]] = None
    notes: Optional[str] = None


class TemplateRequest(BaseModel):
    name: str
    category: str = "UTILITY"
    language: str = "en"
    body: str
    variables: List[str] = Field(default_factory=list)


def _now() -> datetime:
    return datetime.utcnow()


def _serialize_document(document: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(document)
    result["_id"] = str(result["_id"])
    if "_id" in result and "id" not in result:
        result["id"] = result["_id"]
    for field in ("created_at", "updated_at", "last_message_at"):
        if isinstance(result.get(field), datetime):
            result[field] = result[field].isoformat() + "Z"
    return result


def _require_db() -> None:
    if not mongo_client.is_connected():
        raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})


def _get_lead_by_phone(phone_number: str) -> Optional[Dict[str, Any]]:
    if not phone_number or not mongo_client.is_connected():
        return None
    lead = mongo_client.leads.find_one({"phone": phone_number})
    if not lead:
        return None
    return {
        "lead_id": str(lead["_id"]),
        "name": lead.get("name", ""),
        "company": lead.get("company", ""),
        "email": lead.get("email", ""),
    }


def _ensure_conversation(customer_phone: str, customer_name: Optional[str] = None) -> Dict[str, Any]:
    _require_db()
    now = _now()
    mongo_client.customers.update_one(
        {"phone": customer_phone},
        {
            "$set": {"updated_at": now},
            "$addToSet": {"channels": "whatsapp"},
            "$setOnInsert": {
                "name": customer_name or "",
                "phone": customer_phone,
                "email": "",
                "location": "",
                "lead_status": "none",
                "tags": [],
                "notes": "",
                "archived": False,
                "created_at": now,
            },
        },
        upsert=True,
    )
    conversation = mongo_client.whatsapp_conversations.find_one({"customer_phone": customer_phone})
    lead = _get_lead_by_phone(customer_phone)
    if conversation:
      updates: Dict[str, Any] = {"updated_at": _now()}
      if customer_name and not conversation.get("customer_name"):
          updates["customer_name"] = customer_name
      if lead and not conversation.get("lead_id"):
          updates["lead_id"] = lead["lead_id"]
      if updates:
          mongo_client.whatsapp_conversations.update_one({"_id": conversation["_id"]}, {"$set": updates})
          conversation.update(updates)
      return conversation

    conversation = {
        "customer_phone": customer_phone,
        "customer_name": customer_name or "",
        "channel": "whatsapp",
        "status": "open",
        "source": "api",
        "lead_id": lead["lead_id"] if lead else None,
        "ai_enabled": True,
        "assigned_to": None,
        "tags": [],
        "notes": "",
        "unread_count": 0,
        "last_message": "",
        "last_message_direction": None,
        "last_message_at": _now(),
        "created_at": _now(),
        "updated_at": _now(),
    }
    result = mongo_client.whatsapp_conversations.insert_one(conversation)
    conversation["_id"] = result.inserted_id
    return conversation


def _store_message(
    customer_phone: str,
    text: str,
    direction: str,
    provider: str,
    provider_message_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    customer_name: Optional[str] = None,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _require_db()

    conversation = None
    if conversation_id:
        try:
            conversation = mongo_client.whatsapp_conversations.find_one({"_id": ObjectId(conversation_id)})
        except Exception:
            conversation = None
    if not conversation:
        conversation = _ensure_conversation(customer_phone, customer_name)

    message = {
        "conversation_id": str(conversation["_id"]),
        "customer_phone": customer_phone,
        "text": text,
        "direction": direction,
        "provider": provider,
        "provider_message_id": provider_message_id,
        "status": "received" if direction == "inbound" else "sent",
        "raw_payload": raw_payload or {},
        "created_at": _now(),
        "updated_at": _now(),
    }
    result = mongo_client.whatsapp_messages.insert_one(message)
    message["_id"] = result.inserted_id

    mongo_client.whatsapp_conversations.update_one(
        {"_id": conversation["_id"]},
        {
            "$set": {
                "last_message": text,
                "last_message_direction": direction,
                "last_message_at": _now(),
                "updated_at": _now(),
                **({"unread_count": int(conversation.get("unread_count", 0)) + 1} if direction == "inbound" else {}),
            }
        },
    )
    return message


def _build_conversation_history(conversation_id: str) -> List[Dict[str, str]]:
    messages = list(
        mongo_client.whatsapp_messages.find({"conversation_id": conversation_id}).sort("created_at", 1).limit(20)
    )
    history: List[Dict[str, str]] = []
    for message in messages:
        role = "assistant" if message.get("direction") == "outbound" else "user"
        content = str(message.get("text") or "").strip()
        if content:
            history.append({"role": role, "content": content})
    return history[-8:]


def _send_via_twilio(to: str, text: str) -> Dict[str, Any]:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER):
        raise RuntimeError("Twilio WhatsApp is not configured")
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    message = client.messages.create(
        from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}",
        body=text,
    )
    return {"provider": "twilio", "provider_message_id": message.sid, "status": message.status}


def _send_via_meta(to: str, text: str) -> Dict[str, Any]:
    if not (WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID):
        raise RuntimeError("Meta WhatsApp Cloud API is not configured")

    body = json.dumps(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Meta WhatsApp send failed: {error_body}") from exc

    message_id = (((payload.get("messages") or [{}])[0]).get("id"))
    return {"provider": "meta", "provider_message_id": message_id, "status": "sent"}


def _send_message(to: str, text: str) -> Dict[str, Any]:
    if TWILIO_WHATSAPP_NUMBER and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        return _send_via_twilio(to, text)
    return _send_via_meta(to, text)


def _generate_ai_reply(conversation_id: str, customer_text: str) -> Optional[str]:
    if not WHATSAPP_AUTO_REPLY_ENABLED:
        return None

    try:
        history = _build_conversation_history(conversation_id)
        conversation = mongo_client.whatsapp_conversations.find_one({"_id": ObjectId(conversation_id)})
        reply = brain_service.respond(
            customer_text,
            history,
            channel="whatsapp",
            customer_phone=conversation.get("customer_phone") if conversation else None,
        )
        reply = (reply or "").strip()
        return reply or None
    except Exception as exc:
        print(f"WhatsApp AI reply generation failed: {exc}")
        return None


def _parse_meta_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries = payload.get("entry") or []
    extracted: List[Dict[str, Any]] = []
    for entry in entries:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            contacts = value.get("contacts") or []
            messages = value.get("messages") or []
            for message in messages:
                text = ((message.get("text") or {}).get("body") or "").strip()
                if not text:
                    continue
                contact = contacts[0] if contacts else {}
                extracted.append(
                    {
                        "customer_phone": message.get("from", ""),
                        "customer_name": (contact.get("profile") or {}).get("name"),
                        "text": text,
                        "provider_message_id": message.get("id"),
                        "provider": "meta",
                        "raw_payload": payload,
                    }
                )
    return extracted


def _parse_twilio_form(form_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    body = str(form_data.get("Body") or "").strip()
    from_number = str(form_data.get("From") or "")
    if from_number.startswith("whatsapp:"):
        from_number = from_number.split(":", 1)[1]
    if not body or not from_number:
        return []
    return [
        {
            "customer_phone": from_number,
            "customer_name": str(form_data.get("ProfileName") or ""),
            "text": body,
            "provider_message_id": str(form_data.get("MessageSid") or ""),
            "provider": "twilio",
            "raw_payload": form_data,
        }
    ]


@router.get("/stats")
async def get_whatsapp_stats():
    _require_db()
    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "success": True,
        "data": {
            "conversations": mongo_client.whatsapp_conversations.count_documents({}),
            "open_conversations": mongo_client.whatsapp_conversations.count_documents({"status": "open"}),
            "messages": mongo_client.whatsapp_messages.count_documents({}),
            "inbound_messages": mongo_client.whatsapp_messages.count_documents({"direction": "inbound"}),
            "outbound_messages": mongo_client.whatsapp_messages.count_documents({"direction": "outbound"}),
            "messages_today": mongo_client.whatsapp_messages.count_documents({"created_at": {"$gte": today}}),
            "conversations_today": mongo_client.whatsapp_conversations.count_documents({"created_at": {"$gte": today}}),
            "unread_conversations": mongo_client.whatsapp_conversations.count_documents({"unread_count": {"$gt": 0}}),
            "ai_handled": mongo_client.whatsapp_conversations.count_documents({"ai_enabled": {"$ne": False}}),
            "human_handled": mongo_client.whatsapp_conversations.count_documents({"ai_enabled": False}),
            "average_response_time": "Tracked after provider delivery callbacks",
        },
    }


@router.get("/conversations")
async def get_conversations(limit: int = Query(50, ge=1, le=200), skip: int = Query(0, ge=0)):
    _require_db()
    conversations = list(
        mongo_client.whatsapp_conversations.find({})
        .sort("updated_at", -1)
        .skip(skip)
        .limit(limit)
    )
    data = []
    for conversation in conversations:
        item = _serialize_document(conversation)
        message_count = mongo_client.whatsapp_messages.count_documents({"conversation_id": item["_id"]})
        item["message_count"] = message_count
        lead = _get_lead_by_phone(item["customer_phone"])
        item["lead"] = lead
        data.append(item)

    return {
        "success": True,
        "data": data,
        "total": mongo_client.whatsapp_conversations.count_documents({}),
        "limit": limit,
        "skip": skip,
    }


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    _require_db()
    messages = list(
        mongo_client.whatsapp_messages.find({"conversation_id": conversation_id}).sort("created_at", 1)
    )
    data = [_serialize_document(message) for message in messages]
    return {"success": True, "data": data, "total": len(data)}


@router.put("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, payload: ConversationControlRequest):
    _require_db()
    try:
        object_id = ObjectId(conversation_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid conversation id") from exc
    updates = {key: value for key, value in payload.dict().items() if value is not None}
    updates["updated_at"] = _now()
    mongo_client.whatsapp_conversations.update_one({"_id": object_id}, {"$set": updates})
    row = mongo_client.whatsapp_conversations.find_one({"_id": object_id})
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "data": _serialize_document(row)}


@router.get("/templates")
async def get_templates():
    _require_db()
    rows = list(mongo_client.whatsapp_templates.find({}).sort("updated_at", -1))
    return {"success": True, "data": [_serialize_document(row) for row in rows]}


@router.post("/templates")
async def create_template(payload: TemplateRequest):
    _require_db()
    now = _now()
    row = {**payload.dict(), "status": "draft", "provider_template_id": None, "created_at": now, "updated_at": now}
    result = mongo_client.whatsapp_templates.insert_one(row)
    row["_id"] = result.inserted_id
    return {"success": True, "data": _serialize_document(row)}


@router.post("/messages")
async def send_message(request: SendWhatsAppRequest):
    _require_db()
    if not request.to.strip() or not request.text.strip():
        raise HTTPException(status_code=400, detail={"success": False, "error": "to and text are required"})

    try:
        provider_result = _send_message(request.to.strip(), request.text.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"success": False, "error": str(exc)})

    message = _store_message(
        customer_phone=request.to.strip(),
        text=request.text.strip(),
        direction="outbound",
        provider=provider_result["provider"],
        provider_message_id=provider_result.get("provider_message_id"),
        conversation_id=request.conversation_id,
        raw_payload=provider_result,
    )

    return {"success": True, "data": _serialize_document(message), "provider_result": provider_result}


@router.get("/webhook", response_class=PlainTextResponse)
async def verify_meta_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN and hub_challenge:
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("verification failed", status_code=403)


@router.post("/webhook")
async def receive_whatsapp_webhook(request: Request):
    _require_db()
    content_type = request.headers.get("content-type", "")

    records: List[Dict[str, Any]] = []
    if "application/json" in content_type:
        payload = await request.json()
        records = _parse_meta_payload(payload)
    else:
        form = dict(await request.form())
        records = _parse_twilio_form(form)

    stored = 0
    for record in records:
        try:
            inbound_message = _store_message(
                customer_phone=record["customer_phone"],
                text=record["text"],
                direction="inbound",
                provider=record["provider"],
                provider_message_id=record.get("provider_message_id"),
                customer_name=record.get("customer_name"),
                raw_payload=record.get("raw_payload"),
            )
            stored += 1

            ai_reply = _generate_ai_reply(inbound_message["conversation_id"], record["text"])
            if ai_reply:
                provider_result = _send_message(record["customer_phone"], ai_reply)
                _store_message(
                    customer_phone=record["customer_phone"],
                    text=ai_reply,
                    direction="outbound",
                    provider=provider_result["provider"],
                    provider_message_id=provider_result.get("provider_message_id"),
                    conversation_id=inbound_message["conversation_id"],
                    customer_name=record.get("customer_name"),
                    raw_payload=provider_result,
                )
        except Exception as exc:
            print(f"WhatsApp webhook store failed: {exc}")

    return {"success": True, "stored": stored}
