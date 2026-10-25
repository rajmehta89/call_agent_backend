#!/usr/bin/env python3
"""MongoDB-backed lead CRM API."""

from datetime import datetime
from timeutil import utc_iso, utc_now
import csv
import io
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import Depends, APIRouter, File, HTTPException, Path, Query, UploadFile
from pydantic import BaseModel

from env_loader import load_project_env
from mongo_client import mongo_client
from routers.calls_api import log_call, update_lead_status_from_call
load_project_env()

from auth_service import can_assign_conversations, require_permission, require_user
router = APIRouter(prefix="/api/leads", tags=["Leads Management"], dependencies=[Depends(require_permission("manage_assigned_leads"))])  # dashboard API: sign-in required for every route


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
