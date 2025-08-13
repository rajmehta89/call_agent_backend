"""
Leads Management API Router - Leads and Calling Functionality
Converted from Flask to FastAPI for Render deployment
"""

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
import json
import os
import uuid
import csv
import io

router = APIRouter(
    prefix="/api/leads",
    tags=["Leads Management"]
)

# Pydantic models
class Lead(BaseModel):
    id: Optional[str] = None
    name: str
    phone: str
    email: Optional[str] = ""
    company: Optional[str] = ""
    notes: Optional[str] = ""
    status: str = "new"
    call_attempts: int = 0
    last_call: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class LeadUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None

# Simple OutboundCaller class
class OutboundCaller:
    def __init__(self):
        self.app_id = os.getenv("APP_ID")
        self.app_secret = os.getenv("APP_SECRET")
        self.caller_id = os.getenv("CALLER_ID")
        self.websocket_url = os.getenv("WEBSOCKET_URL")

        if not all([self.app_id, self.app_secret, self.caller_id, self.websocket_url]):
            print("⚠️ Warning: Piopiy credentials not configured. Call functionality will be simulated.")
            self.client = None
        else:
            # In production, initialize Piopiy client here
            self.client = None  # Placeholder
            print("📞 Outbound caller initialized.")

    def make_call(self, customer_number_str):
        """Initiates an outbound call"""
        if not self.client:
            print(f"[SIMULATED] Would call {customer_number_str}")
            return {"status": "simulated", "message": "Piopiy not configured"}

        # In production, implement actual Piopiy call logic here
        return {"status": "success", "message": "Call initiated"}

# Initialize outbound caller
outbound_caller = OutboundCaller()

# File storage functions
LEADS_FILE = "leads_data.json"

def load_leads():
    """Load leads from JSON file"""
    try:
        if os.path.exists(LEADS_FILE):
            with open(LEADS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []
    except Exception as e:
        print(f"Error loading leads: {e}")
        return []

def save_leads(leads):
    """Save leads to JSON file"""
    try:
        with open(LEADS_FILE, 'w', encoding='utf-8') as f:
            json.dump(leads, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving leads: {e}")
        return False

@router.get("/")
async def get_leads():
    """Get all leads"""
    try:
        leads = load_leads()
        return {"success": True, "data": leads, "count": len(leads)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/")
async def add_lead(lead: Lead):
    """Add a new lead manually"""
    try:
        lead_data = {
            "id": str(uuid.uuid4()),
            "name": lead.name.strip(),
            "phone": lead.phone.strip(),
            "email": lead.email.strip() if lead.email else "",
            "company": lead.company.strip() if lead.company else "",
            "notes": lead.notes.strip() if lead.notes else "",
            "status": "new",
            "call_attempts": 0,
            "last_call": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }

        leads = load_leads()
        leads.append(lead_data)

        if save_leads(leads):
            return {"success": True, "message": "Lead added successfully", "data": lead_data}
        else:
            raise HTTPException(status_code=500, detail="Failed to save lead")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/{lead_id}")
async def update_lead(lead_id: str, lead_update: LeadUpdate):
    """Update an existing lead"""
    try:
        leads = load_leads()
        lead_index = next((i for i, lead in enumerate(leads) if lead["id"] == lead_id), None)

        if lead_index is None:
            raise HTTPException(status_code=404, detail="Lead not found")

        updatable_fields = ["name", "phone", "email", "company", "notes", "status"]
        for field in updatable_fields:
            value = getattr(lead_update, field)
            if value is not None:
                leads[lead_index][field] = value

        leads[lead_index]["updated_at"] = datetime.now().isoformat()

        if save_leads(leads):
            return {"success": True, "message": "Lead updated successfully", "data": leads[lead_index]}
        else:
            raise HTTPException(status_code=500, detail="Failed to update lead")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{lead_id}")
async def delete_lead(lead_id: str):
    """Delete a lead"""
    try:
        leads = load_leads()
        original_count = len(leads)
        leads = [lead for lead in leads if lead["id"] != lead_id]

        if len(leads) == original_count:
            raise HTTPException(status_code=404, detail="Lead not found")

        if save_leads(leads):
            return {"success": True, "message": "Lead deleted successfully"}
        else:
            raise HTTPException(status_code=500, detail="Failed to delete lead")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/upload")
async def upload_leads_csv(file: UploadFile = File(...)):
    """Upload leads from CSV file"""
    try:
        if not file.filename.lower().endswith('.csv'):
            raise HTTPException(status_code=400, detail="File must be a CSV")

        content = await file.read()
        stream = io.StringIO(content.decode("utf-8"))
        csv_reader = csv.DictReader(stream)

        new_leads, errors = [], []

        for row_num, row in enumerate(csv_reader, start=2):
            try:
                name, phone = row.get('name', '').strip(), row.get('phone', '').strip()
                if not name or not phone:
                    errors.append(f"Row {row_num}: Missing name or phone")
                    continue

                lead_data = {
                    "id": str(uuid.uuid4()),
                    "name": name,
                    "phone": phone,
                    "email": row.get('email', '').strip(),
                    "company": row.get('company', '').strip(),
                    "notes": row.get('notes', '').strip(),
                    "status": "new",
                    "call_attempts": 0,
                    "last_call": None,
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat()
                }
                new_leads.append(lead_data)

            except Exception as e:
                errors.append(f"Row {row_num}: {str(e)}")

        if not new_leads:
            raise HTTPException(status_code=400, detail="No valid leads found in CSV")

        existing_leads = load_leads()
        all_leads = existing_leads + new_leads

        if save_leads(all_leads):
            return {
                "success": True,
                "message": f"Successfully imported {len(new_leads)} leads",
                "imported_count": len(new_leads),
                "total_count": len(all_leads),
                "errors": errors
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to save leads")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{lead_id}/call")
async def call_lead(lead_id: str):
    """Initiate a call to a specific lead using Piopiy"""
    try:
        leads = load_leads()
        lead_index = next((i for i, lead in enumerate(leads) if lead["id"] == lead_id), None)

        if lead_index is None:
            raise HTTPException(status_code=404, detail="Lead not found")

        lead = leads[lead_index]
        print(f"📞 Initiating call to {lead['name']} at {lead['phone']}")
        call_response = outbound_caller.make_call(lead["phone"])

        if call_response:
            if "error" in call_response:
                call_status, success = f"Call failed: {call_response['error']}", False
            elif call_response.get("status") == "simulated":
                call_status, success = "Call simulated (Piopiy not configured)", True
            else:
                call_status, success = "Call initiated successfully via Piopiy", True
        else:
            call_status, success = "Call failed - no response", False

        if success:
            leads[lead_index]["call_attempts"] += 1
            leads[lead_index]["last_call"] = datetime.now().isoformat()
            leads[lead_index]["status"] = "called"
            leads[lead_index]["updated_at"] = datetime.now().isoformat()
            save_leads(leads)

        return {
            "success": success,
            "message": f"Call {'initiated' if success else 'failed'} to {lead['name']}",
            "data": {
                "lead": leads[lead_index],
                "call_status": call_status,
                "phone_number": lead["phone"],
                "piopiy_response": call_response
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/stats")
async def get_leads_stats():
    """Get leads statistics"""
    try:
        leads = load_leads()
        stats = {
            "total": len(leads),
            "new": len([l for l in leads if l["status"] == "new"]),
            "called": len([l for l in leads if l["status"] == "called"]),
            "contacted": len([l for l in leads if l["status"] == "contacted"]),
            "converted": len([l for l in leads if l["status"] == "converted"]),
            "total_calls": sum(l["call_attempts"] for l in leads)
        }
        return {"success": True, "data": stats}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
