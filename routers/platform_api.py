import csv
import io
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_config import agent_config
from brain_service import DEFAULT_TOOLS, brain_service
from mongo_client import mongo_client
from shopify_service import shopify_service


router = APIRouter(prefix="/api/platform", tags=["platform"])

ROLE_PERMISSIONS = {
    "owner": ["*"],
    "admin": ["manage_workspace", "manage_team", "manage_agents", "manage_integrations", "manage_data", "view_analytics"],
    "manager": ["manage_agents", "manage_data", "assign_leads", "view_analytics"],
    "agent": ["handle_conversations", "handle_calls", "manage_assigned_leads", "view_customers"],
    "viewer": ["view_dashboard", "view_analytics"],
}


class ValuePayload(BaseModel):
    value: Dict[str, Any]


class AutomationPayload(BaseModel):
    name: str
    description: str = ""
    trigger: str
    conditions: List[Dict[str, Any]] = Field(default_factory=list)
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True


class TeamMemberPayload(BaseModel):
    name: str
    email: str
    role: str = "agent"
    active: bool = True


class CustomerPayload(BaseModel):
    name: str
    phone: str
    email: str = ""
    location: str = ""
    tags: List[str] = Field(default_factory=list)
    notes: str = ""


def _require_db() -> None:
    if not mongo_client.is_connected():
        raise HTTPException(status_code=503, detail={"success": False, "error": "Database not connected"})


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


def _upsert_setting(key: str, value: Dict[str, Any]) -> Dict[str, Any]:
    _require_db()
    now = datetime.utcnow()
    mongo_client.platform_settings.update_one(
        {"key": key},
        {"$set": {"value": value, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return {"key": key, "value": value, "updated_at": now}


def _count(collection: Any, query: Optional[Dict[str, Any]] = None) -> int:
    return collection.count_documents(query or {}) if mongo_client.is_connected() else 0


def _audit(action: str, resource: str, before: Any = None, after: Any = None, actor: str = "workspace-owner") -> None:
    if mongo_client.is_connected():
        mongo_client.audit_logs.insert_one({"actor": actor, "action": action, "resource": resource, "before": before, "after": after, "created_at": datetime.utcnow()})


@router.get("/permissions")
async def permissions():
    return {"success": True, "data": ROLE_PERMISSIONS}


@router.get("/audit")
async def audit_logs(limit: int = Query(100, ge=1, le=500)):
    _require_db()
    return {"success": True, "data": _serialize(list(mongo_client.audit_logs.find({}).sort("created_at", -1).limit(limit)))}


@router.get("/notifications")
async def notifications(unread_only: bool = False):
    _require_db()
    query = {"read": False} if unread_only else {}
    return {"success": True, "data": _serialize(list(mongo_client.notifications.find(query).sort("created_at", -1).limit(100)))}


@router.get("/dashboard")
async def dashboard(date_from: Optional[str] = None, date_to: Optional[str] = None, channel: Optional[str] = None):
    _require_db()
    calls = _count(mongo_client.calls)
    whatsapp = _count(mongo_client.whatsapp_conversations)
    leads = _count(mongo_client.leads)
    customers = _count(mongo_client.customers)
    handoffs = _count(mongo_client.calls, {"transfer_status": {"$in": ["requested", "accepted", "completed"]}})
    ai_total = _count(mongo_client.ai_activity)
    ai_success = _count(mongo_client.ai_activity, {"success": True})
    errors = _count(mongo_client.ai_activity, {"success": False})
    qualified = _count(mongo_client.leads, {"status": {"$in": ["qualified", "hot", "converted"]}})
    recent = list(mongo_client.ai_activity.find({}).sort("created_at", -1).limit(8))
    return {"success": True, "data": {
        "metrics": {
            "total_conversations": whatsapp + calls,
            "whatsapp_conversations": whatsapp,
            "voice_calls": calls,
            "total_leads": leads,
            "qualified_leads": qualified,
            "customers": customers,
            "ai_resolution_percent": round((ai_success / ai_total) * 100, 1) if ai_total else 0,
            "human_handoff_percent": round((handoffs / max(calls + whatsapp, 1)) * 100, 1),
            "shopify_product_enquiries": _count(mongo_client.ai_activity, {"shopify_lookup": True}),
            "orders_influenced": 0,
            "revenue_influenced": 0,
            "ai_usage": ai_total,
            "error_count": errors,
        },
        "channels": {
            "whatsapp": {"connected": bool(os.getenv("TWILIO_WHATSAPP_NUMBER") or os.getenv("WHATSAPP_PHONE_NUMBER_ID")), "agent": brain_service.channel_config("whatsapp")},
            "voice": {"connected": bool(os.getenv("TWILIO_ACCOUNT_SID") and (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or os.getenv("CALLER_ID"))), "agent": brain_service.channel_config("voice")},
        },
        "shopify": shopify_service.status(),
        "recent_activity": _serialize(recent),
    }}


@router.get("/brain")
async def get_brain():
    return {"success": True, "data": brain_service.brain_config()}


@router.put("/brain")
async def update_brain(payload: ValuePayload):
    stored = _upsert_setting("brain", payload.value)
    knowledge = {
        "company_information": payload.value.get("company_information", ""),
        "business_description": payload.value.get("business_description", ""),
        "locations": payload.value.get("locations", []),
        "working_hours": payload.value.get("working_hours", ""),
        "services": payload.value.get("services", []),
        "faqs": payload.value.get("faqs", []),
        "policies": payload.value.get("policies", []),
        "website_content": payload.value.get("website_content", ""),
        "custom_knowledge": payload.value.get("custom_knowledge", ""),
    }
    agent_config.set_knowledge_base(knowledge)
    agent_config.set_knowledge_base_enabled(True)
    _audit("update", "brain", after=payload.value)
    return {"success": True, "data": _serialize(stored)}


@router.get("/agents/{channel}")
async def get_channel_agent(channel: str):
    if channel not in {"voice", "whatsapp"}:
        raise HTTPException(status_code=404, detail="Unknown channel")
    return {"success": True, "data": brain_service.channel_config(channel)}


@router.put("/agents/{channel}")
async def update_channel_agent(channel: str, payload: ValuePayload):
    if channel not in {"voice", "whatsapp"}:
        raise HTTPException(status_code=404, detail="Unknown channel")
    stored = _upsert_setting(f"{channel}_agent", payload.value)
    _audit("update", f"{channel}_agent", after=payload.value)
    return {"success": True, "data": _serialize(stored)}


@router.get("/tools")
async def get_tools():
    return {"success": True, "data": brain_service.tools()}


@router.put("/tools")
async def update_tools(payload: ValuePayload):
    tools = {**DEFAULT_TOOLS, **{key: bool(value) for key, value in payload.value.items() if key in DEFAULT_TOOLS}}
    stored = _upsert_setting("ai_tools", tools)
    _audit("update", "ai_tools", after=tools)
    return {"success": True, "data": _serialize(stored)}


@router.get("/activity")
async def get_activity(search: str = "", channel: str = "", success: Optional[bool] = None, limit: int = Query(100, ge=1, le=500)):
    _require_db()
    query: Dict[str, Any] = {}
    if channel:
        query["channel"] = channel
    if success is not None:
        query["success"] = success
    if search:
        query["$or"] = [{"request": {"$regex": search, "$options": "i"}}, {"response": {"$regex": search, "$options": "i"}}, {"customer_phone": {"$regex": search, "$options": "i"}}]
    rows = list(mongo_client.ai_activity.find(query).sort("created_at", -1).limit(limit))
    return {"success": True, "data": _serialize(rows), "total": mongo_client.ai_activity.count_documents(query)}


@router.get("/customers")
async def get_customers(search: str = "", status: str = "", limit: int = Query(100, ge=1, le=500)):
    _require_db()
    query: Dict[str, Any] = {}
    if search:
        query["$or"] = [{field: {"$regex": search, "$options": "i"}} for field in ("name", "phone", "email")]
    if status:
        query["lead_status"] = status
    rows = list(mongo_client.customers.find(query).sort("updated_at", -1).limit(limit))
    return {"success": True, "data": _serialize(rows), "total": mongo_client.customers.count_documents(query)}


@router.post("/customers")
async def create_customer(payload: CustomerPayload):
    _require_db()
    now = datetime.utcnow()
    data = {**payload.dict(), "channels": [], "lead_status": "none", "created_at": now, "updated_at": now, "archived": False}
    result = mongo_client.customers.insert_one(data)
    data["_id"] = result.inserted_id
    _audit("create", "customer", after=_serialize(data))
    return {"success": True, "data": _serialize(data)}


@router.get("/customers/{customer_id}")
async def get_customer(customer_id: str):
    _require_db()
    try:
        customer = mongo_client.customers.find_one({"_id": ObjectId(customer_id)})
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid customer id") from exc
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    phone = customer.get("phone")
    detail = _serialize(customer)
    detail["whatsapp"] = _serialize(list(mongo_client.whatsapp_conversations.find({"customer_phone": phone}).sort("updated_at", -1)))
    detail["calls"] = _serialize(list(mongo_client.calls.find({"phone_number": phone}).sort("call_date", -1)))
    detail["ai_activity"] = _serialize(list(mongo_client.ai_activity.find({"customer_phone": phone}).sort("created_at", -1).limit(50)))
    detail["lead"] = _serialize(mongo_client.leads.find_one({"phone": phone}))
    return {"success": True, "data": detail}


@router.put("/customers/{customer_id}")
async def update_customer(customer_id: str, payload: ValuePayload):
    _require_db()
    mongo_client.customers.update_one({"_id": ObjectId(customer_id)}, {"$set": {**payload.value, "updated_at": datetime.utcnow()}})
    return await get_customer(customer_id)


@router.get("/automations")
async def get_automations():
    _require_db()
    rows = list(mongo_client.automations.find({}).sort("updated_at", -1))
    return {"success": True, "data": _serialize(rows)}


@router.post("/automations")
async def create_automation(payload: AutomationPayload):
    _require_db()
    now = datetime.utcnow()
    data = {**payload.dict(), "runs": 0, "errors": 0, "version": 1, "created_at": now, "updated_at": now}
    result = mongo_client.automations.insert_one(data)
    data["_id"] = result.inserted_id
    _audit("create", "automation", after=_serialize(data))
    return {"success": True, "data": _serialize(data)}


@router.put("/automations/{automation_id}")
async def update_automation(automation_id: str, payload: ValuePayload):
    _require_db()
    mongo_client.automations.update_one({"_id": ObjectId(automation_id)}, {"$set": {**payload.value, "updated_at": datetime.utcnow()}, "$inc": {"version": 1}})
    row = mongo_client.automations.find_one({"_id": ObjectId(automation_id)})
    return {"success": True, "data": _serialize(row)}


@router.get("/team")
async def get_team():
    _require_db()
    return {"success": True, "data": _serialize(list(mongo_client.team_members.find({}).sort("name", 1)))}


@router.post("/team")
async def invite_team_member(payload: TeamMemberPayload):
    _require_db()
    now = datetime.utcnow()
    data = {**payload.dict(), "invitation_status": "pending", "created_at": now, "updated_at": now}
    result = mongo_client.team_members.insert_one(data)
    data["_id"] = result.inserted_id
    _audit("invite", "team_member", after=_serialize(data))
    return {"success": True, "data": _serialize(data)}


@router.get("/settings")
async def get_settings():
    defaults = {"business": {}, "ai": {"default_model": os.getenv("OPENAI_MODEL", "")}, "notifications": {}, "security": {}, "billing": {"plan": "Development"}}
    stored = brain_service._setting("workspace_settings", {})
    return {"success": True, "data": {**defaults, **stored}}


@router.put("/settings")
async def update_settings(payload: ValuePayload):
    stored = _upsert_setting("workspace_settings", payload.value)
    _audit("update", "workspace_settings", after=payload.value)
    return {"success": True, "data": _serialize(stored)}


@router.get("/shopify/status")
async def shopify_status():
    data = shopify_service.status()
    if shopify_service.configured:
        try:
            data["products_count"] = len(shopify_service.products(limit=100))
            data["api_status"] = "connected"
        except Exception as exc:
            data["api_status"] = "error"
            data["error"] = str(exc)
    return {"success": True, "data": data}


@router.get("/shopify/products")
async def shopify_products(search: str = "", limit: int = Query(25, ge=1, le=100)):
    try:
        return {"success": True, "data": shopify_service.products(search, limit)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/shopify/orders")
async def shopify_orders(search: str = "", limit: int = Query(25, ge=1, le=100)):
    try:
        return {"success": True, "data": shopify_service.orders(search, limit)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/shopify/sync")
async def shopify_sync(scope: str = "all"):
    _require_db()
    if not shopify_service.configured:
        raise HTTPException(status_code=400, detail="Shopify is not configured")
    started = datetime.utcnow()
    try:
        counts = {
            "products": len(shopify_service.products(limit=100)) if scope in {"all", "products", "inventory"} else 0,
            "orders": len(shopify_service.orders(limit=100)) if scope in {"all", "orders"} else 0,
            "customers": len(shopify_service.customers(limit=100)) if scope in {"all", "customers"} else 0,
        }
        record = {"scope": scope, "status": "success", "counts": counts, "created_at": started, "completed_at": datetime.utcnow()}
        mongo_client.shopify_sync_history.insert_one(record)
        return {"success": True, "data": _serialize(record)}
    except Exception as exc:
        mongo_client.shopify_sync_history.insert_one({"scope": scope, "status": "error", "error": str(exc), "created_at": started})
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/search")
async def global_search(q: str = Query(..., min_length=2)):
    _require_db()
    regex = {"$regex": q, "$options": "i"}
    customers = list(mongo_client.customers.find({"$or": [{"name": regex}, {"phone": regex}, {"email": regex}]}).limit(10))
    leads = list(mongo_client.leads.find({"$or": [{"name": regex}, {"phone": regex}, {"email": regex}, {"notes": regex}]}).limit(10))
    conversations = list(mongo_client.whatsapp_conversations.find({"$or": [{"customer_name": regex}, {"customer_phone": regex}, {"last_message": regex}]}).limit(10))
    calls = list(mongo_client.calls.find({"$or": [{"phone_number": regex}, {"call_summary": regex}]}).limit(10))
    return {"success": True, "data": _serialize({"customers": customers, "leads": leads, "conversations": conversations, "calls": calls})}


@router.get("/export/{resource}.csv")
async def export_csv(resource: str):
    _require_db()
    collections = {"customers": mongo_client.customers, "leads": mongo_client.leads, "calls": mongo_client.calls, "activity": mongo_client.ai_activity, "automations": mongo_client.automations}
    collection = collections.get(resource)
    if collection is None:
        raise HTTPException(status_code=404, detail="Unknown export resource")
    rows = [_serialize(row) for row in collection.find({}).limit(10000)]
    fields = sorted({key for row in rows for key in row.keys() if not isinstance(row.get(key), (dict, list))}) or ["id"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{resource}.csv"'
    return response
