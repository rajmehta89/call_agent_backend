#!/usr/bin/env python3
"""MongoDB-backed lead CRM API."""

from datetime import datetime
import csv
import io
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, File, HTTPException, Path, Query, UploadFile
from pydantic import BaseModel

from env_loader import load_project_env
from mongo_client import mongo_client
from routers.calls_api import log_call, update_lead_status_from_call
load_project_env()

router = APIRouter(prefix="/api/leads", tags=["Leads Management"])


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


def _serialize_lead(lead_doc: Dict[str, Any]) -> Dict[str, Any]:
    lead_doc["_id"] = str(lead_doc["_id"])
    return lead_doc


def get_leads(filters: Dict | None = None, limit: int = 50, skip: int = 0):
    _require_db()

    query: Dict[str, Any] = {}
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
    for lead in leads:
        _serialize_lead(lead)

    return {
        "success": True,
        "data": leads,
        "total": mongo_client.leads.count_documents(query),
        "limit": limit,
        "skip": skip,
    }


def add_lead(lead_data: Dict[str, Any]):
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
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    result = mongo_client.leads.insert_one(lead_doc)
    lead_doc["_id"] = str(result.inserted_id)
    return {"success": True, "data": lead_doc}


def update_lead(lead_id: str, lead_data: Dict[str, Any]):
    _require_db()
    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    existing_phone = lead_data.get("phone")
    if existing_phone:
        duplicate = mongo_client.leads.find_one({"phone": existing_phone, "_id": {"$ne": ObjectId(lead_id)}})
        if duplicate:
            raise HTTPException(status_code=400, detail={"success": False, "error": "Phone number already exists"})

    update_payload = {**lead_data, "updated_at": datetime.now()}
    result = mongo_client.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": update_payload})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

    updated = mongo_client.leads.find_one({"_id": ObjectId(lead_id)})
    return {"success": True, "data": _serialize_lead(updated)}


def delete_lead(lead_id: str):
    _require_db()
    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    result = mongo_client.leads.delete_one({"_id": ObjectId(lead_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})
    return {"success": True, "message": "Lead deleted successfully"}


def get_lead_by_id(lead_id: str):
    _require_db()
    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id)})
    if not lead:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})
    return {"success": True, "data": _serialize_lead(lead)}


def get_leads_stats():
    _require_db()
    total_leads = mongo_client.leads.count_documents({})
    status_counts = {status: mongo_client.leads.count_documents({"status": status}) for status in ["new", "called", "contacted", "converted"]}
    calls_result = list(mongo_client.leads.aggregate([{"$group": {"_id": None, "total_calls": {"$sum": "$call_attempts"}}}]))
    total_calls = calls_result[0]["total_calls"] if calls_result else 0
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
async def get_leads_endpoint(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
):
    filters: Dict[str, Any] = {}
    if status:
        filters["status"] = status
    if search:
        filters["search"] = search
    return get_leads(filters, limit, skip)


@router.post("/")
async def add_lead_endpoint(lead: Lead):
    return add_lead(lead.dict(exclude_unset=True))


@router.get("/stats")
async def get_leads_stats_endpoint():
    return get_leads_stats()


@router.post("/upload")
async def upload_leads_csv(file: UploadFile = File(...)):
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
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
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
async def health_check():
    return {
        "success": True,
        "message": "Leads API is running",
        "database": mongo_client.get_connection_status(),
        "timestamp": datetime.now().isoformat(),
    }


@router.post("/{lead_id}/call")
async def call_lead_endpoint(lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$")):
    raise HTTPException(status_code=410, detail="Outbound calling is disabled in this inbound-only project")


@router.get("/{lead_id}")
async def get_lead_endpoint(lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$")):
    return get_lead_by_id(lead_id)


@router.put("/{lead_id}")
async def update_lead_endpoint(lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$"), lead: LeadUpdate = None):
    lead_data = (lead or LeadUpdate()).dict(exclude_unset=True)
    return update_lead(lead_id, lead_data)


@router.delete("/{lead_id}")
async def delete_lead_endpoint(lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$")):
    return delete_lead(lead_id)
