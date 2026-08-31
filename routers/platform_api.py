import csv
import io
import os
import secrets
from fastapi import BackgroundTasks
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_config import agent_config
from brain_service import DEFAULT_TOOLS, brain_service
from auth_service import ALL_PERMISSIONS, DEFAULT_ROLES, has_permission, invitation_hash, require_permission, require_user, role_document
from email_service import email_service
from mongo_client import mongo_client
from shopify_service import shopify_service


router = APIRouter(prefix="/api/platform", tags=["platform"])

ROLE_PERMISSIONS = {
    "owner": ["*"],
    "admin": ["manage_workspace", "manage_team", "manage_roles", "manage_agents", "manage_integrations", "manage_data", "view_analytics"],
    "manager": ["manage_agents", "manage_data", "assign_leads", "view_analytics"],
    "agent": ["handle_conversations", "handle_calls", "manage_assigned_leads", "view_customers"],
    "viewer": ["view_dashboard", "view_analytics"],
}


class ValuePayload(BaseModel):
    value: Dict[str, Any]


class BrainUrlPayload(BaseModel):
    url: str


class ShopifyConfigPayload(BaseModel):
    store_domain: str
    access_token: str = ""
    api_version: str = "2025-10"
    test_connection: bool = True


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
    permissions: List[str] = Field(default_factory=list)


class TeamMemberUpdatePayload(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    permissions: Optional[List[str]] = None


class RolePayload(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    label: str = Field(min_length=2, max_length=80)
    description: str = ""
    permissions: List[str] = Field(default_factory=list)


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


def _whatsapp_status() -> Dict[str, Any]:
    meta_token = os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN")
    meta_phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    provider_mode = (os.getenv("WHATSAPP_PROVIDER") or "auto").strip().lower()
    twilio_ready = bool(
        os.getenv("TWILIO_ACCOUNT_SID")
        and os.getenv("TWILIO_AUTH_TOKEN")
        and os.getenv("TWILIO_WHATSAPP_NUMBER")
    )
    meta_ready = bool(meta_token and meta_phone_id)
    return {
        "connected": meta_ready or twilio_ready,
        "provider_mode": provider_mode,
        "provider": provider_mode if provider_mode in {"meta", "twilio"} and ((provider_mode == "meta" and meta_ready) or (provider_mode == "twilio" and twilio_ready)) else "meta" if meta_ready else "twilio" if twilio_ready else "not_configured",
        "meta_ready": meta_ready,
        "meta_access_token_configured": bool(meta_token),
        "meta_phone_number_id_configured": bool(meta_phone_id),
    }


def _audit(action: str, resource: str, before: Any = None, after: Any = None, actor: str = "workspace-owner") -> None:
    if mongo_client.is_connected():
        mongo_client.audit_logs.insert_one({"actor": actor, "action": action, "resource": resource, "before": before, "after": after, "created_at": datetime.utcnow()})


def _apply_saved_shopify_config() -> None:
    if not mongo_client.is_connected():
        return
    saved = mongo_client.platform_settings.find_one({"key": "shopify"})
    values = saved.get("value", {}) if saved else {}
    if values.get("store_domain") and values.get("access_token"):
        shopify_service.configure(values["store_domain"], values["access_token"], values.get("api_version", "2025-10"))


_apply_saved_shopify_config()


@router.get("/permissions")
async def permissions():
    return {"success": True, "data": {"available": ALL_PERMISSIONS, "roles": ROLE_PERMISSIONS}}


@router.get("/audit")
async def audit_logs(limit: int = Query(100, ge=1, le=500)):
    _require_db()
    return {"success": True, "data": _serialize(list(mongo_client.audit_logs.find({}).sort("created_at", -1).limit(limit)))}


@router.get("/notifications")
async def notifications(unread_only: bool = False, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    query: Dict[str, Any] = {"read": False} if unread_only else {}
    if user.get("role") not in {"owner", "admin"}:
        recipients = [str(user.get("name") or ""), str(user.get("email") or ""), "Human team", "team"]
        query = {"$and": [query, {"$or": [{"recipient": {"$in": recipients}}, {"recipient": {"$exists": False}}]}]} if query else {"$or": [{"recipient": {"$in": recipients}}, {"recipient": {"$exists": False}}]}
    return {"success": True, "data": _serialize(list(mongo_client.notifications.find(query).sort("created_at", -1).limit(100)))}


@router.post("/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, user: Dict[str, Any] = Depends(require_user)):
    _require_db()
    if not ObjectId.is_valid(notification_id):
        raise HTTPException(status_code=400, detail="Invalid notification id")
    notification_query: Dict[str, Any] = {"_id": ObjectId(notification_id)}
    if user.get("role") not in {"owner", "admin"}:
        notification_query["recipient"] = {"$in": [str(user.get("name") or ""), str(user.get("email") or ""), "Human team", "team"]}
    result = mongo_client.notifications.update_one(
        notification_query,
        {"$set": {"read": True, "read_at": datetime.utcnow()}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"success": True}


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
    workload_names = set()
    for member in mongo_client.team_members.find({}, {"name": 1}):
        if member.get("name"):
            workload_names.add(str(member["name"]))
    for member in mongo_client.users.find({}, {"name": 1}):
        if member.get("name"):
            workload_names.add(str(member["name"]))
    for row in mongo_client.calls.find({"handled_by": {"$exists": True}}, {"handled_by": 1}):
        if row.get("handled_by"):
            workload_names.add(str(row["handled_by"]))
    for row in mongo_client.whatsapp_conversations.find({"assigned_to": {"$exists": True}}, {"assigned_to": 1}):
        if row.get("assigned_to"):
            workload_names.add(str(row["assigned_to"]))
    human_workload = []
    for name in sorted(workload_names):
        call_count = _count(mongo_client.calls, {"$or": [{"handled_by": name}, {"accepted_by": name}]})
        whatsapp_count = _count(mongo_client.whatsapp_conversations, {"assigned_to": name})
        converted_count = _count(mongo_client.leads, {"assigned_to": name, "status": "converted"})
        human_workload.append({
            "name": name,
            "voice_calls": call_count,
            "whatsapp_conversations": whatsapp_count,
            "total_handled": call_count + whatsapp_count,
            "converted_leads": converted_count,
        })
    human_workload.sort(key=lambda row: (row["total_handled"], row["converted_leads"]), reverse=True)
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
            "whatsapp": {**_whatsapp_status(), "agent": brain_service.channel_config("whatsapp")},
            "voice": {"connected": bool(os.getenv("TWILIO_ACCOUNT_SID") and (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or os.getenv("CALLER_ID"))), "agent": brain_service.channel_config("voice")},
        },
        "shopify": shopify_service.status(),
        "human_workload": human_workload[:50],
        "recent_activity": _serialize(recent),
    }}


@router.get("/brain")
async def get_brain():
    config = brain_service.brain_config()
    if config.get("index", {}).get("documents_count", 0) == 0 and any(config.get(key) for key in ("company_information", "business_description", "locations", "working_hours", "services", "faqs", "policies", "website_content", "custom_knowledge")):
        config["index"] = brain_service.reindex_knowledge(config)
        config["sources"] = brain_service.source_list()
    return {"success": True, "data": config}


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
    stored["index"] = brain_service.reindex_knowledge(payload.value)
    _audit("update", "brain", after=payload.value)
    return {"success": True, "data": _serialize(stored)}


@router.post("/brain/upload")
async def upload_brain_source(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required")
    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Files must be 25 MB or smaller")
    suffix = os.path.splitext(file.filename)[1].lower()
    if suffix != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")
    try:
        result = brain_service.ingest_pdf(content, file.filename)
        return {"success": True, "data": result}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/brain/scrape")
async def scrape_brain_source(payload: BrainUrlPayload):
    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Use an http or https URL")
    try:
        return {"success": True, "data": brain_service.ingest_url(payload.url)}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/brain/shopify")
async def ingest_brain_shopify():
    try:
        return {"success": True, "data": brain_service.ingest_shopify_catalog()}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/brain/resync")
async def resync_brain_sources():
    try:
        return {"success": True, "data": brain_service.resync_static_sources()}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/brain/resync/{source_id:path}")
async def resync_brain_source(source_id: str):
    try:
        return {"success": True, "data": brain_service.resync_source(source_id)}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/brain/sources")
async def get_brain_sources():
    return {"success": True, "data": brain_service.source_list()}


@router.get("/agents/{channel}")
async def get_channel_agent(channel: str, user: Dict[str, Any] = Depends(require_permission("manage_agents"))):
    if channel not in {"voice", "whatsapp"}:
        raise HTTPException(status_code=404, detail="Unknown channel")
    return {"success": True, "data": brain_service.channel_config(channel)}


@router.put("/agents/{channel}")
async def update_channel_agent(channel: str, payload: ValuePayload, user: Dict[str, Any] = Depends(require_permission("manage_agents"))):
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
    total = mongo_client.ai_activity.count_documents(query)
    success_count = mongo_client.ai_activity.count_documents({**query, "success": True})
    error_count = mongo_client.ai_activity.count_documents({**query, "success": False})
    latency = list(mongo_client.ai_activity.aggregate([{"$match": query}, {"$group": {"_id": None, "average": {"$avg": "$response_time_ms"}}}]))
    return {"success": True, "data": _serialize(rows), "total": total, "stats": {"total": total, "success": success_count, "errors": error_count, "average_latency_ms": round(latency[0].get("average", 0), 1) if latency else 0}}


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
    data = []
    for row in rows:
        item = _serialize(row)
        latest = mongo_client.automation_runs.find_one({"automation_id": str(row["_id"])}, sort=[("created_at", -1)])
        item["last_run"] = _serialize(latest) if latest else None
        data.append(item)
    return {"success": True, "data": data}


@router.get("/automations/runs")
async def get_automation_runs(limit: int = Query(50, ge=1, le=200)):
    _require_db()
    rows = list(mongo_client.automation_runs.find({}).sort("created_at", -1).limit(limit))
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


@router.get("/roles")
async def get_roles(user: Dict[str, Any] = Depends(require_permission("manage_team"))):
    _require_db()
    for role in DEFAULT_ROLES:
        mongo_client.roles.update_one({"name": role["name"]}, {"$setOnInsert": {**role, "system": True, "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}}, upsert=True)
    return {"success": True, "data": _serialize(list(mongo_client.roles.find({}).sort([("system", -1), ("label", 1)])))}


@router.post("/roles")
async def create_role(payload: RolePayload, user: Dict[str, Any] = Depends(require_permission("manage_roles"))):
    _require_db()
    name = payload.name.strip().lower().replace(" ", "-")
    if mongo_client.roles.find_one({"name": name}):
        raise HTTPException(status_code=409, detail="A role with this name already exists")
    invalid = [permission for permission in payload.permissions if permission not in ALL_PERMISSIONS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown permissions: {', '.join(invalid)}")
    now = datetime.utcnow()
    data = {"name": name, "label": payload.label.strip(), "description": payload.description.strip(), "permissions": list(dict.fromkeys(payload.permissions)), "system": False, "created_at": now, "updated_at": now}
    result = mongo_client.roles.insert_one(data)
    data["_id"] = result.inserted_id
    _audit("create", "role", after=_serialize(data), actor=str(user.get("email", "workspace-owner")))
    return {"success": True, "data": _serialize(data)}


@router.put("/roles/{role_id}")
async def update_role(role_id: str, payload: RolePayload, user: Dict[str, Any] = Depends(require_permission("manage_roles"))):
    _require_db()
    if not ObjectId.is_valid(role_id):
        raise HTTPException(status_code=400, detail="Invalid role id")
    role = mongo_client.roles.find_one({"_id": ObjectId(role_id)})
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.get("system") and payload.name.strip().lower().replace(" ", "-") != role.get("name"):
        raise HTTPException(status_code=400, detail="System role names cannot be changed")
    invalid = [permission for permission in payload.permissions if permission not in ALL_PERMISSIONS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown permissions: {', '.join(invalid)}")
    values = {"label": payload.label.strip(), "description": payload.description.strip(), "permissions": list(dict.fromkeys(payload.permissions)), "updated_at": datetime.utcnow()}
    mongo_client.roles.update_one({"_id": role["_id"]}, {"$set": values})
    return {"success": True, "data": _serialize(mongo_client.roles.find_one({"_id": role["_id"]}))}


@router.delete("/roles/{role_id}")
async def delete_role(role_id: str, user: Dict[str, Any] = Depends(require_permission("manage_roles"))):
    _require_db()
    if not ObjectId.is_valid(role_id):
        raise HTTPException(status_code=400, detail="Invalid role id")
    role = mongo_client.roles.find_one({"_id": ObjectId(role_id)})
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.get("system"):
        raise HTTPException(status_code=400, detail="System roles cannot be deleted")
    if mongo_client.users.count_documents({"role": role["name"]}):
        raise HTTPException(status_code=409, detail="Reassign users before deleting this role")
    mongo_client.roles.delete_one({"_id": role["_id"]})
    return {"success": True, "data": {"deleted": True}}


@router.get("/team")
async def get_team(user: Dict[str, Any] = Depends(require_permission("manage_team"))):
    _require_db()
    workspace_id = user.get("workspace_id", "default")
    members = list(mongo_client.users.find({"workspace_id": workspace_id}).sort("name", 1))
    pending = list(mongo_client.invitations.find({"workspace_id": workspace_id, "status": "pending"}).sort("created_at", -1))
    rows = [{**member, "member_type": "user", "invitation_status": "accepted"} for member in members]
    rows.extend({**invite, "member_type": "invitation", "active": False, "invitation_status": "pending"} for invite in pending if not any(member.get("email") == invite.get("email") for member in members))
    return {"success": True, "data": _serialize(rows)}


@router.post("/team")
async def invite_team_member(payload: TeamMemberPayload, background_tasks: BackgroundTasks, user: Dict[str, Any] = Depends(require_permission("manage_team"))):
    _require_db()
    email = payload.email.strip().lower()
    role_name = payload.role.strip().lower()
    role = role_document(role_name)
    if not role:
        raise HTTPException(status_code=400, detail="Choose an existing role")
    if mongo_client.users.find_one({"email": email, "workspace_id": user.get("workspace_id", "default")}) or mongo_client.invitations.find_one({"email": email, "workspace_id": user.get("workspace_id", "default"), "status": "pending"}):
        raise HTTPException(status_code=409, detail="This email already belongs to the workspace or has a pending invitation")
    now = datetime.utcnow()
    token = secrets.token_urlsafe(32)
    workspace_id = user.get("workspace_id", "default")
    invite = {"name": payload.name.strip(), "email": email, "role": role_name, "permissions": payload.permissions, "workspace_id": workspace_id, "workspace_name": user.get("workspace_name", "My workspace"), "token_hash": invitation_hash(token), "status": "pending", "created_by": user.get("_id"), "expires_at": now + timedelta(days=7), "created_at": now, "updated_at": now}
    result = mongo_client.invitations.insert_one(invite)
    invite["_id"] = result.inserted_id
    mongo_client.team_members.update_one({"email": email}, {"$set": {"name": payload.name.strip(), "email": email, "role": role_name, "permissions": payload.permissions, "active": False, "invitation_status": "pending", "updated_at": now}, "$setOnInsert": {"created_at": now}}, upsert=True)
    _audit("invite", "team_member", after={"email": email, "role": role_name}, actor=str(user.get("email", "workspace-owner")))
    origin = os.getenv("PUBLIC_APP_URL") or os.getenv("FRONTEND_ORIGIN_ALT") or os.getenv("FRONTEND_ORIGIN_2") or os.getenv("FRONTEND_ORIGIN") or "http://127.0.0.1:3000"
    invite_url = f"{origin.rstrip('/')}/invite/accept?token={token}"
    delivery = "queued" if email_service.configured else "manual_link_required"
    if email_service.configured:
        background_tasks.add_task(email_service.send_invitation, email, payload.name.strip(), user.get("workspace_name", "My workspace"), invite_url, role.get("label", role_name))
    return {"success": True, "data": {**_serialize(invite), "invite_token": token, "invite_url": invite_url, "delivery": delivery}}


@router.put("/team/{member_id}")
async def update_team_member(member_id: str, payload: TeamMemberUpdatePayload, user: Dict[str, Any] = Depends(require_permission("manage_team"))):
    _require_db()
    if not ObjectId.is_valid(member_id):
        raise HTTPException(status_code=400, detail="Invalid member id")
    member = mongo_client.users.find_one({"_id": ObjectId(member_id), "workspace_id": user.get("workspace_id", "default")})
    if not member:
        raise HTTPException(status_code=404, detail="Team member not found")
    if str(member["_id"]) == str(user["_id"]) and (payload.role is not None or payload.active is not None):
        raise HTTPException(status_code=400, detail="Use another Admin account to change your own role or status")
    if payload.role is not None and str(user.get("role", "")).lower() not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only an Owner or Admin can change member roles")
    updates: Dict[str, Any] = {"updated_at": datetime.utcnow()}
    if payload.role is not None:
        role = role_document(payload.role.strip().lower())
        if not role:
            raise HTTPException(status_code=400, detail="Choose an existing role")
        updates["role"] = role["name"]
    if payload.active is not None:
        updates["active"] = payload.active
    if payload.permissions is not None:
        invalid = [permission for permission in payload.permissions if permission not in ALL_PERMISSIONS]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Unknown permissions: {', '.join(invalid)}")
        updates["permissions"] = list(dict.fromkeys(payload.permissions))
    mongo_client.users.update_one({"_id": member["_id"]}, {"$set": updates})
    return {"success": True, "data": _serialize(mongo_client.users.find_one({"_id": member["_id"]}))}


@router.delete("/team/{member_id}")
async def deactivate_team_member(member_id: str, user: Dict[str, Any] = Depends(require_permission("manage_team"))):
    _require_db()
    if not ObjectId.is_valid(member_id):
        raise HTTPException(status_code=400, detail="Invalid member id")
    if str(member_id) == str(user.get("_id")):
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    result = mongo_client.users.update_one({"_id": ObjectId(member_id), "workspace_id": user.get("workspace_id", "default"), "role": {"$ne": "owner"}}, {"$set": {"active": False, "updated_at": datetime.utcnow()}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Team member not found or cannot be deactivated")
    return {"success": True, "data": {"deactivated": True}}


@router.get("/invitations")
async def get_invitations(user: Dict[str, Any] = Depends(require_permission("manage_team"))):
    _require_db()
    rows = list(mongo_client.invitations.find({"workspace_id": user.get("workspace_id", "default")}).sort("created_at", -1))
    return {"success": True, "data": _serialize(rows)}


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
    _apply_saved_shopify_config()
    data = shopify_service.status()
    if shopify_service.configured:
        try:
            data["products_count"] = len(shopify_service.products(limit=100))
            data["api_status"] = "connected"
        except Exception as exc:
            data["api_status"] = "error"
            data["error"] = str(exc)
    return {"success": True, "data": data}


@router.put("/shopify/configure")
async def configure_shopify(payload: ShopifyConfigPayload):
    _require_db()
    before = shopify_service.status()
    saved = mongo_client.platform_settings.find_one({"key": "shopify"}) or {}
    saved_values = saved.get("value", {}) if saved else {}
    token = payload.access_token.strip() or str(saved_values.get("access_token") or shopify_service.access_token or "")
    shopify_service.configure(payload.store_domain.strip(), token, payload.api_version.strip() or "2025-10")
    stored = {"store_domain": shopify_service.store_domain, "access_token": token, "api_version": shopify_service.api_version}
    _upsert_setting("shopify", stored)
    data = shopify_service.status()
    if payload.test_connection and shopify_service.configured:
        try:
            shopify_service.products(limit=1)
            data["api_status"] = "connected"
        except Exception as exc:
            data["api_status"] = "error"
            data["error"] = str(exc)
    _audit("update", "shopify", before=before, after={"store_domain": shopify_service.store_domain, "api_version": shopify_service.api_version, "access_token_configured": bool(token)})
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
