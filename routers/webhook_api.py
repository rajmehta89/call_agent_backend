"""
Webhook API Router
Handles Piopiy call events and WebSocket streaming
"""

import os
from fastapi import APIRouter, Request, HTTPException
from typing import Dict, Any, Optional
import json
from datetime import datetime
from mongo_client import mongo_client

WS_URL = os.getenv("WEBSOCKET_URL")  # e.g. "ws://your_host:8765"

try:
    from piopiy import Action
except ImportError:
    print("⚠️ Piopiy not installed - using mock Action")
    class Action:
        def stream(self, ws_url, options):
            pass
        def PCMO(self):
            return {"action": "stream", "ws_url": ws_url}

router = APIRouter(
    prefix="/api",  # Updated prefix to match Flask routes
    tags=["Webhooks"]
)

# Global variable to track hangup state
pending_hangup = False

def log_call_event(event_type, data):
    """Log call events for debugging"""
    timestamp = datetime.now().isoformat()
    safe_event = (str(event_type).upper()) if event_type else "UNKNOWN"
    try:
        print(f"\n📞 [{timestamp}] {safe_event}")
        print(f"   Data: {json.dumps(data or {}, indent=2)}")
    except Exception:
        # Fallback if data isn't JSON serializable
        print(f"\n📞 [{timestamp}] {safe_event}")
        print(f"   Raw Data: {data}")

def clean_phone_number(phone_str):
    """Clean phone number for comparison"""
    if not phone_str:
        return ""
    return str(phone_str).replace('+', '').replace('-', '').replace(' ', '')

def update_lead_call_status(phone_number, status, call_data=None):
    """Update lead status based on call events (smart status preservation)"""
    try:
        if not mongo_client or not mongo_client.is_connected():
            print("❌ MongoDB not connected")
            return False

        # Clean phone number for comparison
        clean_phone = clean_phone_number(phone_number)
        if not clean_phone:
            print(f"❌ Invalid phone number: {phone_number}")
            return False

        # Find lead by phone number (exact match or contains)
        lead = mongo_client.leads.find_one({
            "$or": [
                {"phone": clean_phone},
                {"phone": f"+{clean_phone}"},
                {"phone": {"$regex": clean_phone, "$options": "i"}}
            ]
        })

        if not lead:
            print(f"❌ No lead found for phone: {phone_number} (cleaned: {clean_phone})")
            return False

        print(f"✅ Found lead: {lead.get('name', 'Unknown')} for phone: {phone_number}")

        current_status = lead.get("status", "new")

        # Smart status preservation - don't downgrade status
        # Status hierarchy: new < called < contacted < converted
        status_hierarchy = {"new": 0, "called": 1, "contacted": 2, "converted": 3}

        current_level = status_hierarchy.get(current_status, 0)
        new_level = status_hierarchy.get(status, 0)

        # Only update if the new status is higher in hierarchy, or if it's the same level
        if new_level > current_level:
            final_status = status
            print(f"📈 Upgrading lead status: {current_status} -> {final_status}")
        elif new_level == current_level:
            final_status = status
            print(f"📋 Maintaining lead status: {final_status}")
        else:
            # Don't downgrade - keep current higher status
            final_status = current_status
            print(f"🔒 Preserving higher status: {current_status} (not downgrading to {status})")

        # Update lead status and timestamps only (do not increment attempts here)
        update_doc = {
            "$set": {
                "status": final_status,
                "updated_at": datetime.now(),
                "last_call": datetime.now()
            }
        }

        result = mongo_client.leads.update_one(
            {"_id": lead["_id"]},
            update_doc
        )

        if result.modified_count > 0:
            print(f"✅ Updated lead {lead.get('name', 'Unknown')} status to {final_status}")
            return True
        else:
            print(f"⚠️ No changes made to lead {lead.get('name', 'Unknown')}")
            return False

    except Exception as e:
        print(f"❌ Error updating lead status: {e}")
        import traceback
        traceback.print_exc()
        return False

@router.post("/python/inbound")  # Updated route to match Flask exactly
async def inbound_call(request: Request):
    """Handle inbound calls from Piopiy"""
    global pending_hangup

    print(f"📞 Incoming call received")
    print(f"   WS_URL: {WS_URL}")
    print(f"   Pending hangup: {pending_hangup}")

    # Check if hangup is pending
    if pending_hangup:
        pending_hangup = False  # Reset the flag
        print("🛑 Hanging up call due to exit intent")
        return {"hangup": True}

    if not WS_URL:
        print("❌ WEBSOCKET_URL is undefined")
        raise HTTPException(status_code=500, detail="WEBSOCKET_URL undefined")

    print(f"📞 Incoming call → {WS_URL}")
    try:
        act = Action()

        # Forward context from request to WS via extra_params if present
        req_json = await request.json() if request.headers.get("content-type") == "application/json" else {}
        extra_params = {}
        if req_json.get("phone_number"):
            extra_params["phone_number"] = str(req_json["phone_number"])
        if req_json.get("lead_id"):
            extra_params["lead_id"] = str(req_json["lead_id"])

        act.stream(
            ws_url=WS_URL,
            options={
                "listen_mode": "both",
                "stream_on_answer": True,
                "voice_quality": 8000,
                "extra_params": extra_params
            }
        )
        pcmo = act.PCMO()
        print(f"✅ PCMO generated: {pcmo}")
        return pcmo
    except Exception as e:
        print(f"❌ Error generating PCMO: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/piopiy/events")  # Updated route to match Flask exactly
async def handle_call_events(request: Request):
    """Handle Piopiy call events (answer, hangup, etc.)"""
    try:
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        # Support multiple possible keys sent by provider
        event_type = (
                data.get("event")
                or data.get("event_type")
                or data.get("type")
                or data.get("status")
        )
        phone_number = (
                data.get("to")
                or data.get("to_number")
                or data.get("callee")
                or data.get("from")
                or data.get("from_number")
                or data.get("caller")
        )
        call_id = data.get("call_id") or data.get("id") or data.get("uuid")

        log_call_event(event_type, data)

        # If we still don't know the event, acknowledge and return 200 to avoid retries
        if not event_type:
            return {"status": "received", "note": "unknown event"}

        # Handle different call events
        if str(event_type).lower() == "answer":
            print(f"📞 Call answered: {phone_number}")

            # Find the lead for this phone number
            lead_id = None
            if mongo_client and mongo_client.is_connected() and phone_number:
                clean_phone = clean_phone_number(phone_number)
                lead = mongo_client.leads.find_one({
                    "$or": [
                        {"phone": clean_phone},
                        {"phone": f"+{clean_phone}"},
                        {"phone": {"$regex": clean_phone, "$options": "i"}}
                    ]
                })
                if lead:
                    lead_id = str(lead["_id"])
                    print(f"📋 Found lead: {lead.get('name', 'Unknown')} (ID: {lead_id})")

            # Update lead status
            if phone_number:
                update_lead_call_status(phone_number, "contacted", data)

        elif str(event_type).lower() == "hangup":
            print(f"📞 Call ended: {phone_number}")

            duration = data.get("duration", 0)
            if phone_number:
                if duration and duration > 10:
                    update_lead_call_status(phone_number, "contacted", data)
                else:
                    update_lead_call_status(phone_number, "called", data)

            if mongo_client and mongo_client.is_connected():
                try:
                    lead = None
                    if phone_number:
                        clean_phone = clean_phone_number(phone_number)
                        lead = mongo_client.leads.find_one({
                            "$or": [
                                {"phone": clean_phone},
                                {"phone": f"+{clean_phone}"},
                                {"phone": {"$regex": clean_phone, "$options": "i"}}
                            ]
                        })

                    # Only update last_call here; do not increment attempts
                    if lead:
                        mongo_client.leads.update_one(
                            {"_id": lead["_id"]},
                            {"$set": {"last_call": datetime.now()}}
                        )
                        print(f"✅ Updated lead call timestamp")
                except Exception as e:
                    print(f"❌ Error logging call to database: {e}")

        elif str(event_type).lower() in ("no-answer", "busy", "missed"):
            print(f"📞 Call not answered: {phone_number} ({event_type})")
            if phone_number:
                update_lead_call_status(phone_number, "called", data)

        return {"status": "received"}

    except Exception as e:
        print(f"❌ Error handling call event: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/hangup-call")
async def hangup_call(request: Request):
    """Handle call hangup requests from WebSocket server"""
    global pending_hangup

    try:
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        reason = data.get("reason", "user_request")
        call_id = data.get("call_id")

        log_call_event("hangup_request", {
            "reason": reason,
            "call_id": call_id,
            "timestamp": datetime.now().isoformat()
        })

        # Return hangup action to Piopiy
        # This will be sent as the next action in the call flow
        hangup_response = {"hangup": True}

        # Store hangup signal for any active Piopiy connections
        # In a production system, you'd store this in Redis or similar
        pending_hangup = True

        print(f"🛑 Call hangup requested - Reason: {reason}")

        return {
            "success": True,
            "message": "Call hangup initiated",
            "action": hangup_response
        }

    except Exception as e:
        print(f"Error in hangup_call: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/start-call")
async def start_call_session(request: Request):
    """Start a call session with current configuration"""
    try:
        if not WS_URL:
            raise HTTPException(status_code=500, detail="WEBSOCKET_URL undefined")

        print(f"📞 Starting call session → {WS_URL}")

        # You can extend this to accept phone numbers from frontend
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        phone_number = data.get("phone_number")

        if phone_number:
            # Here you would integrate with your calling service
            # For now, just return success
            return {
                "success": True,
                "message": f"Call session initiated to {phone_number}",
                "websocket_url": WS_URL
            }
        else:
            return {
                "success": True,
                "message": "WebSocket ready for incoming calls",
                "websocket_url": WS_URL
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
