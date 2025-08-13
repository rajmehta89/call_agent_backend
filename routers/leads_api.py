from fastapi import APIRouter, UploadFile, File, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
import json
import os
import uuid
import csv
import io
from dotenv import load_dotenv
from piopiy import RestClient, Action

load_dotenv()

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

# File storage configuration
LEADS_FILE = "leads_data.json"

# Piopiy Configuration
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
CALLER_ID = os.getenv("CALLER_ID")
WEBSOCKET_URL = os.getenv("WEBSOCKET_URL")

def clean_phone_number(phone_str):
    """Removes '+', '-', and spaces, then converts to integer."""
    if isinstance(phone_str, str):
        return int(phone_str.replace('+', '').replace('-', '').replace(' ', ''))
    return phone_str

class OutboundCaller:
    def __init__(self):
        if not all([APP_ID, APP_SECRET, CALLER_ID, WEBSOCKET_URL]):
            print("⚠️ Warning: Piopiy credentials not configured. Call functionality will be simulated.")
            self.client = None
            return

        self.client = RestClient(int(APP_ID), APP_SECRET)
        print(f"🔧 Outbound caller initialized.")
        print(f"   - Caller ID: {CALLER_ID}")
        print(f"   - WebSocket URL: {WEBSOCKET_URL}")

    def make_call(self, customer_number_str):
        """Initiates an outbound call and connects it to the WebSocket voice agent."""
        if not self.client:
            print(f"📞 [SIMULATED] Would call {customer_number_str}")
            return {"status": "simulated", "message": "Piopiy not configured"}

        try:
            customer_number = clean_phone_number(customer_number_str)
            piopiy_number = clean_phone_number(CALLER_ID)

            action = Action()
            action.stream(
                WEBSOCKET_URL,
                {
                    "listen_mode": "callee",
                    "stream_on_answer": True
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
            print("   - Response from Piopiy:", response)
            return response

        except Exception as e:
            print(f"❌ Failed to make call: {e}")
            return {"error": str(e)}

# Initialize outbound caller
outbound_caller = OutboundCaller()

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
        return {
            "success": True,
            "data": leads,
            "count": len(leads)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

@router.post("/")
async def add_lead(lead: Lead):
    """Add a new lead manually"""
    try:
        if not lead.name or not lead.phone:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": f"Missing required field: {'name' if not lead.name else 'phone'}"}
            )

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

        if not save_leads(leads):
            raise HTTPException(status_code=500, detail={"success": False, "error": "Failed to save lead"})

        return {
            "success": True,
            "message": "Lead added successfully",
            "data": lead_data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

@router.put("/{lead_id}")
async def update_lead(lead_id: str, lead_update: LeadUpdate):
    """Update an existing lead"""
    try:
        leads = load_leads()
        lead_index = next((i for i, lead in enumerate(leads) if lead["id"] == lead_id), None)

        if lead_index is None:
            raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

        updatable_fields = ["name", "phone", "email", "company", "notes", "status"]
        for field in updatable_fields:
            value = getattr(lead_update, field)
            if value is not None:
                leads[lead_index][field] = value.strip() if isinstance(value, str) else value

        leads[lead_index]["updated_at"] = datetime.now().isoformat()

        if not save_leads(leads):
            raise HTTPException(status_code=500, detail={"success": False, "error": "Failed to update lead"})

        return {
            "success": True,
            "message": "Lead updated successfully",
            "data": leads[lead_index]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

@router.delete("/{lead_id}")
async def delete_lead(lead_id: str):
    """Delete a lead"""
    try:
        leads = load_leads()
        original_count = len(leads)
        leads = [lead for lead in leads if lead["id"] != lead_id]

        if len(leads) == original_count:
            raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

        if not save_leads(leads):
            raise HTTPException(status_code=500, detail={"success": False, "error": "Failed to delete lead"})

        return {
            "success": True,
            "message": "Lead deleted successfully"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

@router.post("/upload")
async def upload_leads_csv(file: UploadFile = File(...)):
    """Upload leads from CSV file"""
    try:
        if not file.filename.lower().endswith('.csv'):
            raise HTTPException(status_code=400, detail={"success": False, "error": "File must be a CSV"})

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
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "No valid leads found in CSV", "errors": errors}
            )

        existing_leads = load_leads()
        all_leads = existing_leads + new_leads

        if not save_leads(all_leads):
            raise HTTPException(status_code=500, detail={"success": False, "error": "Failed to save leads"})

        return {
            "success": True,
            "message": f"Successfully imported {len(new_leads)} leads",
            "imported_count": len(new_leads),
            "total_count": len(all_leads),
            "errors": errors
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

@router.post("/{lead_id}/call")
async def call_lead(lead_id: str):
    """Initiate a call to a specific lead using Piopiy"""
    try:
        leads = load_leads()
        lead_index = next((i for i, lead in enumerate(leads) if lead["id"] == lead_id), None)

        if lead_index is None:
            raise HTTPException(status_code=404, detail={"success": False, "error": "Lead not found"})

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

    except Exception as e:
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

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
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        return {
            "success": True,
            "message": "Leads API is running",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})