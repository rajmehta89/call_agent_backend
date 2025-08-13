"""
Calls API Router - Complete FastAPI conversion from Flask
Handles call logging, retrieval, and linking with leads
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Dict, Any, Optional, List
from datetime import datetime
from bson import ObjectId
from mongo_client import mongo_client

router = APIRouter(prefix="/api/calls", tags=["calls"])

# Pydantic Models
class LogCallRequest(BaseModel):
    phone_number: str
    lead_id: Optional[str] = None
    call_data: Optional[Dict[str, Any]] = None

def update_lead_status_from_call(phone_number: str, lead_id: Optional[str], call_data: Dict[str, Any]) -> None:
    """
    Update lead status based on call activity and interest analysis

    Status Flow:
    - new -> called (when call is initiated)
    - called -> contacted (when call is answered and conversation happens)
    - contacted -> converted (when user shows interest)
    """
    try:
        if not mongo_client.is_connected():
            print("⚠️ Cannot update lead status: Database not connected")
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
            print(f"⚠️ No lead found for phone {phone_number} or lead_id {lead_id}")
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
                print(f"📞 Lead {lead['name']} moved to 'called' (call initiated)")

        elif call_status == "completed" and call_duration > 0:
            if current_status in ["new", "called"]:
                if has_conversation:
                    new_status = "contacted"
                    print(f"💬 Lead {lead['name']} moved to 'contacted' (conversation happened)")
                elif call_duration > 5:
                    new_status = "contacted"
                    print(f"📞 Lead {lead['name']} moved to 'contacted' (call answered)")
                else:
                    if current_status == "new":
                        new_status = "called"

        if interest_analysis and interest_analysis.get("interest_status") == "interested":
            confidence = interest_analysis.get("confidence", 0)
            if confidence > 0.7 and current_status in ["called", "contacted"]:
                new_status = "converted"
                print(f"🎯 Lead {lead['name']} moved to 'converted' (interested with {confidence:.0%} confidence)")

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
            print(f"✅ Lead {lead['name']} status updated: {current_status} -> {new_status}")
        else:
            print(f"📋 Lead {lead['name']} status unchanged: {current_status}")

    except Exception as e:
        print(f"❌ Error updating lead status: {e}")

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
                print(f"✅ Updated existing call record with session_id: {session_id}")
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
                print(f"✅ Updated lead {lead_id} last_call timestamp")
            except Exception as e:
                print(f"⚠️ Failed to update lead timestamps: {e}")

        if call_data:
            update_lead_status_from_call(phone_number, lead_id, call_data)

        return {"success": True, "data": call_record}

    except Exception as e:
        print(f"❌ Error logging call: {e}")
        return {"success": False, "error": str(e)}

def get_calls(filters: Dict[str, Any] = None, limit: int = 50, skip: int = 0) -> Dict[str, Any]:
    """Get calls with optional filters"""
    try:
        if not mongo_client.is_connected():
            return {"success": False, "error": "Database not connected"}

        query = {}
        if filters:
            if filters.get("phone_number"):
                query["phone_number"] = filters["phone_number"]
            if filters.get("lead_id"):
                query["lead_id"] = filters["lead_id"]
            if filters.get("status"):
                query["status"] = filters["status"]
            if filters.get("interest_status"):
                query["interest_analysis.interest_status"] = filters["interest_status"]
            if filters.get("date_from"):
                query["call_date"] = {"$gte": datetime.fromisoformat(filters["date_from"])}
            if filters.get("date_to"):
                if "call_date" in query:
                    query["call_date"]["$lte"] = datetime.fromisoformat(filters["date_to"])
                else:
                    query["call_date"] = {"$lte": datetime.fromisoformat(filters["date_to"])}

        calls = list(mongo_client.calls.find(query).sort("call_date", -1).skip(skip).limit(limit))

        lead_ids = list({c.get("lead_id") for c in calls if c.get("lead_id")})
        lead_map: Dict[str, Dict[str, Any]] = {}
        if lead_ids:
            try:
                obj_ids = [ObjectId(lid) for lid in lead_ids if lid]
                leads = list(mongo_client.leads.find({"_id": {"$in": obj_ids}}, {"name": 1, "email": 1, "company": 1}))
                for ld in leads:
                    lead_map[str(ld["_id"])] = {
                        "name": ld.get("name", ""),
                        "email": ld.get("email", ""),
                        "company": ld.get("company", "")
                    }
            except Exception as e:
                print(f"⚠️ Lead join failed: {e}")

        for call in calls:
            call["_id"] = str(call["_id"])
            if call.get("lead_id"):
                call["lead_id"] = str(call["lead_id"])
                lead_info = lead_map.get(call["lead_id"]) if isinstance(call["lead_id"], str) else None
                if lead_info:
                    call["lead"] = lead_info
            else:
                call["lead"] = call.get("lead") or None
            call["transcription"] = call.get("transcription") or []
            call["ai_responses"] = call.get("ai_responses") or []
            call["status"] = call.get("status", "completed")
            call["direction"] = call.get("direction", "outbound")
            cd = call.get("call_date")
            if isinstance(cd, datetime):
                call["call_date"] = cd.isoformat()
            elif not cd:
                ts = call.get("end_time") or call.get("start_time") or call.get("created_at") or datetime.now()
                if isinstance(ts, datetime):
                    call["call_date"] = ts.isoformat()
                else:
                    call["call_date"] = datetime.now().isoformat()
            if call.get("phone_number") is not None:
                call["phone_number"] = str(call["phone_number"])

        total_count = mongo_client.calls.count_documents(query)

        return {
            "success": True,
            "data": calls,
            "total": total_count,
            "limit": limit,
            "skip": skip
        }

    except Exception as e:
        print(f"❌ Error getting calls: {e}")
        return {"success": False, "error": str(e)}

def get_call_by_id(call_id: str) -> Dict[str, Any]:
    """Get a specific call by ID"""
    try:
        if not mongo_client.is_connected():
            return {"success": False, "error": "Database not connected"}

        try:
            call = mongo_client.calls.find_one({"_id": ObjectId(call_id)})
        except:
            return {"success": False, "error": f"'{call_id}' is not a valid ObjectId, it must be a 12-byte input or a 24-character hex string"}

        if not call:
            return {"success": False, "error": "Call not found"}

        call["_id"] = str(call["_id"])
        if call.get("lead_id"):
            call["lead_id"] = str(call["lead_id"])
            lead = mongo_client.leads.find_one({"_id": ObjectId(call["lead_id"])})
            if lead:
                lead["_id"] = str(lead["_id"])
                call["lead"] = lead

        return {"success": True, "data": call}

    except Exception as e:
        print(f"❌ Error getting call: {e}")
        return {"success": False, "error": str(e)}

# API Endpoints
@router.get("/stats")
async def get_call_stats():
    """Get comprehensive call statistics"""
    try:
        if not mongo_client.is_connected():
            raise HTTPException(status_code=500, detail="Database not connected")

        total_calls = mongo_client.calls.count_documents({})
        inbound_calls = mongo_client.calls.count_documents({"direction": "inbound"})
        outbound_calls = mongo_client.calls.count_documents({"direction": "outbound"})
        completed_calls = mongo_client.calls.count_documents({"status": "completed"})
        failed_calls = mongo_client.calls.count_documents({"status": "failed"})

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        calls_today = mongo_client.calls.count_documents({"call_date": {"$gte": today}})

        week_ago = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = week_ago.replace(day=week_ago.day - 7)
        calls_this_week = mongo_client.calls.count_documents({"call_date": {"$gte": week_ago}})

        pipeline = [
            {"$match": {"duration": {"$exists": True, "$ne": None}}},
            {"$group": {
                "_id": None,
                "total_duration": {"$sum": "$duration"},
                "avg_duration": {"$avg": "$duration"}
            }}
        ]
        duration_stats = list(mongo_client.calls.aggregate(pipeline))
        total_duration = duration_stats[0]["total_duration"] if duration_stats else 0
        avg_duration = duration_stats[0]["avg_duration"] if duration_stats else 0

        status_counts = {
            "completed": completed_calls,
            "failed": failed_calls,
            "missed": mongo_client.calls.count_documents({"status": "missed"})
        }

        interest_counts = {}
        for interest in ["interested", "not_interested", "neutral"]:
            interest_counts[interest] = mongo_client.calls.count_documents({"interest_analysis.interest_status": interest})

        calls_with_analysis = mongo_client.calls.count_documents({"interest_analysis": {"$exists": True, "$ne": None}})

        stats = {
            "total_calls": total_calls,
            "inbound_calls": inbound_calls,
            "outbound_calls": outbound_calls,
            "completed_calls": completed_calls,
            "failed_calls": failed_calls,
            "calls_today": calls_today,
            "calls_this_week": calls_this_week,
            "total_duration": total_duration,
            "average_duration": round(avg_duration, 2) if avg_duration else 0,
            "status_counts": status_counts,
            "interest_counts": interest_counts,
            "calls_with_analysis": calls_with_analysis
        }

        return {"success": True, "data": stats}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error getting call stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("")
async def get_calls_endpoint(
        phone_number: Optional[str] = Query(None),
        lead_id: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        interest_status: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        limit: int = Query(50),
        skip: int = Query(0)
):
    """Get calls with filters"""
    try:
        filters = {}
        if phone_number:
            filters["phone_number"] = phone_number
        if lead_id:
            filters["lead_id"] = lead_id
        if status:
            filters["status"] = status
        if interest_status:
            filters["interest_status"] = interest_status
        if date_from:
            filters["date_from"] = date_from
        if date_to:
            filters["date_to"] = date_to

        result = get_calls(filters, limit, skip)

        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "Failed to get calls"))

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/phone/{phone_number}")
async def get_calls_by_phone_endpoint(phone_number: str):
    """Get all calls for a specific phone number"""
    try:
        result = get_calls({"phone_number": phone_number})

        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error"))

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/lead/{lead_id}/stats")
async def get_lead_call_stats(lead_id: str):
    """Get call statistics for a specific lead"""
    try:
        if not mongo_client.is_connected():
            raise HTTPException(status_code=500, detail="Database not connected")

        calls = list(mongo_client.calls.find({"lead_id": lead_id}))

        total_calls = len(calls)
        inbound_calls = len([c for c in calls if c.get("direction") == "inbound"])
        outbound_calls = len([c for c in calls if c.get("direction") == "outbound"])
        total_duration = sum([c.get("duration", 0) for c in calls])
        avg_duration = total_duration / total_calls if total_calls > 0 else 0

        completed_calls = len([c for c in calls if c.get("status") == "completed"])
        failed_calls = len([c for c in calls if c.get("status") == "failed"])

        recent_calls = sorted(calls, key=lambda x: x.get("call_date", datetime.min), reverse=True)[:5]
        for call in recent_calls:
            call["_id"] = str(call["_id"])
            if isinstance(call.get("call_date"), datetime):
                call["call_date"] = call["call_date"].isoformat()

        stats = {
            "lead_id": lead_id,
            "total_calls": total_calls,
            "inbound_calls": inbound_calls,
            "outbound_calls": outbound_calls,
            "total_duration": total_duration,
            "average_duration": round(avg_duration, 2),
            "completed_calls": completed_calls,
            "failed_calls": failed_calls,
            "recent_calls": recent_calls
        }

        return {"success": True, "data": stats}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error getting lead call stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{call_id}")
async def get_call_endpoint(call_id: str):
    """Get a specific call by ID"""
    try:
        result = get_call_by_id(call_id)

        if not result.get("success"):
            if "not found" in result.get("error", "").lower():
                raise HTTPException(status_code=404, detail=result.get("error"))
            else:
                raise HTTPException(status_code=500, detail=result.get("error"))

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{lead_id}/messages")
async def get_call_messages(lead_id: str):
    """Retrieve conversation messages for a specific lead"""
    try:
        if not mongo_client.is_connected():
            raise HTTPException(status_code=500, detail={"success": False, "error": "Database not connected"})
        calls = list(mongo_client.calls.find({"lead_id": lead_id}).sort("created_at", -1).limit(10))
        messages = []
        for call in calls:
            call_id = str(call["_id"])
            for idx, msg in enumerate(call.get("transcription", []) + call.get("ai_responses", [])):
                messages.append({
                    "id": f"{call_id}-{idx}",
                    "type": msg["type"],
                    "content": msg["content"],
                    "timestamp": msg["timestamp"]
                })
        return {"success": True, "data": messages, "total": len(messages)}
    except Exception as e:
        print(f"❌ Error getting call messages: {e}")
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

@router.post("")
async def log_call_endpoint(request: LogCallRequest):
    """Log a new call"""
    try:
        phone_number = request.phone_number
        lead_id = request.lead_id
        call_data = request.call_data or {}

        if not phone_number:
            raise HTTPException(status_code=400, detail="Phone number is required")

        result = log_call(phone_number, lead_id, call_data)

        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Failed to log call"))

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))