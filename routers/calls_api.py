"""
Calls API Router - Direct conversion from Flask
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional
from datetime import datetime
from bson import ObjectId
from mongo_client import mongo_client

router = APIRouter(prefix="/api/calls", tags=["calls"])

class LogCallRequest(BaseModel):
    phone_number: str
    lead_id: Optional[str] = None
    call_data: Optional[Dict[str, Any]] = None

def update_lead_status_from_call(phone_number: str, lead_id: Optional[str], call_data: Dict[str, Any]) -> None:
    """Update lead status based on call activity and interest analysis"""
    try:
        if not mongo_client.is_connected():
            return

        lead = None
        if lead_id:
            try:
                lead = mongo_client.leads.find_one({"_id": ObjectId(lead_id)})
            except:
                pass

        if not lead and phone_number:
            lead = mongo_client.leads.find_one({"phone": phone_number})

        if not lead:
            return

        lead_id = str(lead["_id"])
        current_status = lead.get("status", "new")
        new_status = current_status

        call_status = call_data.get("status", "completed")
        call_duration = call_data.get("duration", 0)
        has_conversation = (
                len(call_data.get("transcription", [])) > 0 or
                len(call_data.get("ai_responses", [])) > 0
        )
        interest_analysis = call_data.get("interest_analysis")

        if call_status == "initiated":
            if current_status == "new":
                new_status = "called"

        elif call_status == "completed" and call_duration > 0:
            if current_status in ["new", "called"]:
                if has_conversation:
                    new_status = "contacted"
                elif call_duration > 5:
                    new_status = "contacted"
                else:
                    if current_status == "new":
                        new_status = "called"

        if interest_analysis and interest_analysis.get("interest_status") == "interested":
            confidence = interest_analysis.get("confidence", 0)
            if confidence > 0.7 and current_status in ["called", "contacted"]:
                new_status = "converted"

        if new_status != current_status:
            mongo_client.leads.update_one(
                {"_id": ObjectId(lead_id)},
                {
                    "$set": {
                        "status": new_status,
                        "updated_at": datetime.now(),
                        "status_reason": f"Auto-updated from call: {call_status}"
                    }
                }
            )

    except Exception:
        pass

def log_call(phone_number: str, lead_id: Optional[str] = None, call_data: Dict[str, Any] = None) -> Dict[str, Any]:
    """Log a new call"""
    try:
        if not mongo_client.is_connected():
            return {"success": False, "error": "Database not connected"}

        call_record = {
            "phone_number": phone_number,
            "lead_id": lead_id,
            "call_date": datetime.now(),
            "direction": call_data.get("direction", "outbound") if call_data else "outbound",
            "status": call_data.get("status", "completed") if call_data else "completed",
            "duration": call_data.get("duration", 0) if call_data else 0,
            "transcription": call_data.get("transcription", []) if call_data else [],
            "ai_responses": call_data.get("ai_responses", []) if call_data else [],
            "call_summary": call_data.get("summary", "") if call_data else "",
            "sentiment": call_data.get("sentiment", "neutral") if call_data else "neutral",
            "interest_analysis": call_data.get("interest_analysis", None) if call_data else None,
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }

        session_id = (call_data or {}).get("call_session_id")
        if session_id:
            existing = mongo_client.calls.find_one({"call_session_id": session_id})
            if existing:
                update_data = {
                    "phone_number": phone_number,
                    "lead_id": lead_id,
                    "direction": call_record["direction"],
                    "status": call_record["status"],
                    "duration": call_record["duration"],
                    "transcription": call_record["transcription"],
                    "ai_responses": call_record["ai_responses"],
                    "call_summary": call_record["call_summary"],
                    "sentiment": call_record["sentiment"],
                    "interest_analysis": call_record["interest_analysis"],
                    "updated_at": datetime.now(),
                    "call_date": call_record["call_date"]
                }
                mongo_client.calls.update_one(
                    {"_id": existing["_id"]},
                    {"$set": update_data}
                )
                updated_record = mongo_client.calls.find_one({"_id": existing["_id"]})
                updated_record["_id"] = str(updated_record["_id"])
                return {"success": True, "data": updated_record, "note": "updated_existing_by_session"}
            else:
                call_record["call_session_id"] = session_id

        result = mongo_client.calls.insert_one(call_record)
        call_record["_id"] = str(result.inserted_id)

        if lead_id:
            try:
                mongo_client.leads.update_one(
                    {"_id": ObjectId(lead_id)},
                    {
                        "$set": {
                            "last_call": datetime.now(),
                            "updated_at": datetime.now()
                        }
                    }
                )
            except Exception:
                pass

        if call_data:
            update_lead_status_from_call(phone_number, lead_id, call_data)

        return {"success": True, "data": call_record}

    except Exception as e:
        return {"success": False, "error": str(e)}

@router.post("/log")
async def log_call_endpoint(request: LogCallRequest):
    """Log a new call"""
    result = log_call(request.phone_number, request.lead_id, request.call_data)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to log call"))

    return result
