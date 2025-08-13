from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
import os
import csv
import json
from datetime import datetime
from typing import List, Optional
import io

# Pydantic models
class SMSRequest(BaseModel):
    to: str
    message: str

class Lead(BaseModel):
    name: str
    phone: str
    project: str
    timestamp: Optional[str] = None

router = APIRouter(prefix="/api/sms", tags=["sms"])

# Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PIOPIY_API_KEY = os.getenv("PIOPIY_API_KEY")

# Data storage
DATA_FILE = "leads.json"
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump([], f)

# Initialize Twilio client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        from twilio.rest import Client
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except ImportError:
        print("Twilio not installed")

def send_sms(to: str, message: str):
    """Send SMS via Twilio or Piopiy"""
    if twilio_client:
        try:
            twilio_client.messages.create(
                body=message,
                from_=TWILIO_PHONE_NUMBER,
                to=to
            )
            return True, "SMS sent via Twilio"
        except Exception as e:
            return False, str(e)
    elif PIOPIY_API_KEY:
        try:
            import requests
            resp = requests.get(
                "https://sms.piopiy.com/api/send",
                params={
                    "key": PIOPIY_API_KEY,
                    "phone": to,
                    "message": message
                }
            )
            return True, resp.text
        except Exception as e:
            return False, str(e)
    else:
        print(f"Simulated SMS to {to}: {message}")
        return True, "Simulated"

def save_lead(name: str, phone: str, project: str):
    """Save lead to local JSON"""
    try:
        with open(DATA_FILE, "r+") as f:
            leads = json.load(f)
            leads.append({
                "name": name,
                "phone": phone,
                "project": project,
                "timestamp": datetime.now().isoformat()
            })
            f.seek(0)
            json.dump(leads, f, indent=2)
            f.truncate()
    except Exception as e:
        print(f"Error saving lead: {e}")

@router.post("/send")
async def send_single_sms(sms_request: SMSRequest):
    """Send a single SMS"""
    success, message = send_sms(sms_request.to, sms_request.message)
    if success:
        return {"status": "success", "message": message}
    else:
        raise HTTPException(status_code=400, detail=message)

@router.post("/upload_csv")
async def upload_csv(file: UploadFile = File(...)):
    """Upload and process CSV file"""
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    try:
        contents = await file.read()
        csv_data = contents.decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(csv_data))

        processed_count = 0
        for row in csv_reader:
            name = row.get("name", "")
            phone = row.get("phone", "")
            project = row.get("project", "")

            if phone:
                message = f"Hello {name}, thanks for showing interest in {project}"
                send_sms(phone, message)
                save_lead(name, phone, project)
                processed_count += 1

        return {"message": f"CSV processed successfully. {processed_count} leads processed."}

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing CSV: {str(e)}")

@router.get("/leads")
async def get_leads():
    """Get all leads"""
    try:
        with open(DATA_FILE, "r") as f:
            leads = json.load(f)
        return leads
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading leads: {str(e)}")

@router.get("/")
async def sms_status():
    """SMS service status"""
    return {"status": "running", "message": "SMS Management API"}
