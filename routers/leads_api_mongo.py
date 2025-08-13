#!/usr/bin/env python3
"""
MongoDB-based FastAPI for Leads Management
Provides endpoints for managing leads, CSV upload, and calling functionality
"""

from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Path
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import os
import csv
import io
import time
from dotenv import load_dotenv
from bson import ObjectId
from piopiy import RestClient, Action
from mongo_client import mongo_client
from routers.calls_api import log_call, update_lead_status_from_call

load_dotenv()

router = APIRouter(
    prefix="/api/leads",
    tags=["Leads Management"]
)

# ---------------------------
# Models
# ---------------------------
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

# ---------------------------
# Utilities
# ---------------------------
def is_valid_object_id(s: str) -> bool:
    return isinstance(s, str) and len(s) == 24 and all(c in "0123456789abcdefABCDEF" for c in s)

def clean_phone_number(phone_str):
    """Removes '+', '-', and spaces, then converts to integer."""
    if isinstance(phone_str, str):
        return int(phone_str.replace('+', '').replace('-', '').replace(' ', ''))
    return phone_str

# ---------------------------
# Piopiy Outbound Call
# ---------------------------
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
CALLER_ID = os.getenv("CALLER_ID")
WEBSOCKET_URL = os.getenv("WEBSOCKET_URL")

class OutboundCaller:
    def __init__(self):
        if not all([APP_ID, APP_SECRET, CALLER_ID, WEBSOCKET_URL]):
            print("⚠️ Warning: Piopiy credentials not configured. Call functionality will be simulated.")
            self.client = None
            return
        self.client = RestClient(int(APP_ID), APP_SECRET)
        print("🔧 Outbound caller initialized.")
        print(f"   - Caller ID: {CALLER_ID}")
        print(f"   - WebSocket URL: {WEBSOCKET_URL}")

    def make_call(self, customer_number_str, lead_id: str | None = None):
        """Initiates an outbound call and connects it to the WebSocket voice agent."""
        session_id = f"lead-{lead_id or 'unknown'}-{int(time.time()*1000)}"

        if not self.client:
            print(f"📞 [SIMULATED] Would call {customer_number_str}")
            return {
                "status": "simulated",
                "message": "Piopiy not configured",
                "session_id": session_id,
            }

        try:
            customer_number = clean_phone_number(customer_number_str)
            piopiy_number = clean_phone_number(CALLER_ID)

            action = Action()
            extra_params = {
                "phone_number": str(customer_number_str),
                "lead_id": str(lead_id) if lead_id else None,
            }
            extra_params = {k: v for k, v in extra_params.items() if v is not None}

            action.stream(
                WEBSOCKET_URL,
                {
                    "listen_mode": "callee",
                    "stream_on_answer": True,
                    "extra_params": {**extra_params, "session": session_id}
                }
            )

            print(f"\n📞 Placing call to {customer_number_str}...")
            response = self.client.voice.call(
                to=customer_number,
                piopiy_no=piopiy_number,
                to_or_array_pcmo=action.PCMO(),
                options={'record': True}
            )

            print("✅ Call initiated successfully!")
            return {
                "status": "initiated",
                "piopiy_response": response,
                "session_id": session_id,
            }

        except Exception as e:
            print(f"❌ Failed to make call: {e}")
            return {"error": str(e)}

outbound_caller = OutboundCaller()

# ---------------------------
# Data access helpers
# ---------------------------
def get_leads(filters: Dict = None, limit: int = 50, skip: int = 0):
    """Get leads from MongoDB with optional filters"""
    if not mongo_client.is_connected():
        raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})

    query = {}
    if filters:
        if filters.get("status"):
            query["status"] = filters["status"]
        if filters.get("search"):
            search_term = filters["search"]
            query["$or"] = [
                {"name": {"$regex": search_term, "$options": "i"}},
                {"phone": {"$regex": search_term, "$options": "i"}},
                {"email": {"$regex": search_term, "$options": "i"}},
                {"company": {"$regex": search_term, "$options": "i"}}
            ]

    leads = list(mongo_client.leads.find(query).sort("created_at", -1).skip(skip).limit(limit))
    for lead in leads:
        lead["_id"] = str(lead["_id"])

    total_count = mongo_client.leads.count_documents(query)

    return {
        "success": True,
        "data": leads,
        "total": total_count,
        "limit": limit,
        "skip": skip
    }

def add_lead(lead_data: Dict):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})

    if not lead_data.get("name") or not lead_data.get("phone"):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Name and phone are required"})

    existing_lead = mongo_client.leads.find_one({"phone": lead_data["phone"]})
    if existing_lead:
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
        "updated_at": datetime.now()
    }

    result = mongo_client.leads.insert_one(lead_doc)
    lead_doc["_id"] = str(result.inserted_id)
    return {"success": True, "data": lead_doc}

def update_lead(lead_id: str, lead_data: Dict):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})

    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    if not lead_data.get("name") or not lead_data.get("phone"):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Name and phone are required"})

    existing_lead = mongo_client.leads.find_one({
        "phone": lead_data["phone"],
        "_id": {"$ne": ObjectId(lead_id)}
    })
    if existing_lead:
        raise HTTPException(status_code=400, detail={"success": False, "error": "Phone number already exists"})

    update_data = {
        "name": lead_data["name"],
        "phone": lead_data["phone"],
        "email": lead_data.get("email", ""),
        "company": lead_data.get("company", ""),
        "notes": lead_data.get("notes", ""),
        "status": lead_data.get("status", "new"),
        "updated_at": datetime.now()
    }

    result = mongo_client.leads.update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

    updated_lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id)})
    updated_lead["_id"] = str(updated_lead["_id"])
    return {"success": True, "data": updated_lead}

def delete_lead(lead_id: str):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})

    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    result = mongo_client.leads.delete_one({"_id": ObjectId(lead_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})
    return {"success": True, "message": "Lead deleted successfully"}

def get_lead_by_id(lead_id: str):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})
    if not is_valid_object_id(lead_id):
        raise HTTPException(status_code=400, detail={"success": False, "error": "Invalid lead id"})

    lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id)})
    if not lead:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

    lead["_id"] = str(lead["_id"])
    return {"success": True, "data": lead}

def get_leads_stats():
    if not mongo_client.is_connected():
        raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})

    total_leads = mongo_client.leads.count_documents({})
    status_counts = {}
    for status in ["new", "called", "contacted", "converted"]:
        status_counts[status] = mongo_client.leads.count_documents({"status": status})

    pipeline = [{"$group": {"_id": None, "total_calls": {"$sum": "$call_attempts"}}}]
    calls_result = list(mongo_client.leads.aggregate(pipeline))
    total_calls = calls_result[0]["total_calls"] if calls_result else 0

    return {
        "success": True,
        "data": {
            "total": total_leads,
            "new": status_counts.get("new", 0),
            "called": status_counts.get("called", 0),
            "contacted": status_counts.get("contacted", 0),
            "converted": status_counts.get("converted", 0),
            "total_calls": total_calls
        }
    }

# ---------------------------
# Routes
# IMPORTANT: Non-parameter routes BEFORE "/{lead_id}"
# ---------------------------

@router.get("/")
async def get_leads_endpoint(
        status: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        skip: int = Query(0, ge=0),
):
    """Get leads with optional filters"""
    filters = {}
    if status:
        filters["status"] = status
    if search:
        filters["search"] = search
    return get_leads(filters, limit, skip)

@router.post("/")
async def add_lead_endpoint(lead: Lead):
    """Add a new lead"""
    lead_data = lead.dict(exclude_unset=True)
    return add_lead(lead_data)

@router.get("/stats")
async def get_leads_stats_endpoint():
    """Get leads statistics"""
    return get_leads_stats()

@router.post("/upload")
async def upload_leads_csv(file: UploadFile = File(...)):
    """Upload leads from CSV file"""
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail={"success": False, "error": "File must be a CSV"})

    content = await file.read()
    stream = io.StringIO(content.decode("utf-8"))
    csv_reader = csv.DictReader(stream)

    imported_count = 0
    errors = []

    for row_num, row in enumerate(csv_reader, start=2):
        try:
            if not row.get('name') or not row.get('phone'):
                errors.append(f"Row {row_num}: Missing name or phone")
                continue

            existing_lead = mongo_client.leads.find_one({"phone": row['phone']})
            if existing_lead:
                errors.append(f"Row {row_num}: Phone number {row['phone']} already exists")
                continue

            lead_data = {
                "name": row['name'].strip(),
                "phone": row['phone'].strip(),
                "email": row.get('email', '').strip(),
                "company": row.get('company', '').strip(),
                "notes": row.get('notes', '').strip(),
                "status": "new",
                "call_attempts": 0,
                "last_call": None,
                "created_at": datetime.now(),
                "updated_at": datetime.now()
            }

            mongo_client.leads.insert_one(lead_data)
            imported_count += 1

        except Exception as e:
            errors.append(f"Row {row_num}: {str(e)}")

    return {
        "success": True,
        "imported_count": imported_count,
        "errors": errors,
        "message": f"Successfully imported {imported_count} leads"
    }

@router.post("/{lead_id}/call")
async def call_lead_endpoint(
        lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$")
):
    """Initiate a call to a lead"""
    lead_result = get_lead_by_id(lead_id)
    if not lead_result["success"]:
        raise HTTPException(status_code=404, detail=lead_result)

    lead = lead_result["data"]
    call_result = outbound_caller.make_call(lead["phone"], lead_id)

    if call_result.get("error"):
        raise HTTPException(status_code=500, detail={"success": False, "error": call_result["error"]})

    call_data = {
        "direction": "outbound",
        "status": "initiated",
        "duration": 0,
        "summary": f"Outbound call initiated to {lead['name']}",
        "piopiy_response": call_result.get("piopiy_response", call_result),
        "call_session_id": call_result.get("session_id")
    }

    log_result = log_call(lead["phone"], lead_id, call_data)
    if log_result.get("success"):
        print(f"✅ Call logged: {log_result['data']['_id']}")

    mongo_client.leads.update_one(
        {"_id": ObjectId(lead_id)},
        {
            "$inc": {"call_attempts": 1},
            "$set": {
                "last_call": datetime.now(),
                "updated_at": datetime.now()
            }
        }
    )

    update_lead_status_from_call(lead["phone"], lead_id, call_data)

    updated_lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id)})
    updated_lead["_id"] = str(updated_lead["_id"])

    return {
        "success": True,
        "message": f"Call initiated to {lead['name']}",
        "data": {
            "lead": updated_lead,
            "call": call_result,
            "call_log": log_result.get("data")
        }
    }

# Put the parameterized routes LAST to avoid collisions with /stats, /upload, etc.
@router.get("/{lead_id}")
async def get_lead_endpoint(
        lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$")
):
    """Get a specific lead by ID"""
    return get_lead_by_id(lead_id)

@router.put("/{lead_id}")
async def update_lead_endpoint(
        lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$"),
        lead: LeadUpdate = None
):
    """Update a lead"""
    lead_data = (lead or LeadUpdate()).dict(exclude_unset=True)
    return update_lead(lead_id, lead_data)

@router.delete("/{lead_id}")
async def delete_lead_endpoint(
        lead_id: str = Path(..., pattern=r"^[0-9a-fA-F]{24}$")
):
    """Delete a lead"""
    return delete_lead(lead_id)

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "success": True,
        "message": "Leads API is running",
        "database_connected": mongo_client.is_connected(),
        "timestamp": datetime.now().isoformat()
    }
