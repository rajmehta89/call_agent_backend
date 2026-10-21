"""Live, in-order call transcripts for the team inbox.

Voice providers call save_live() after every turn, so a teammate watching the inbox sees the
conversation while the call is still running. The final save (calls_api.log_call) later marks
the same record completed; it is matched on call_session_id, so a call is always one record.

    messages: [{"role": "customer" | "agent", "text": "...", "at": "2026-10-19T15:01:02"}]
"""

import logging
from datetime import datetime, timezone
from timeutil import utc_now
from typing import Any, Dict, Iterable, List, Optional

from mongo_client import mongo_client
import realtime

logger = logging.getLogger(__name__)

LIVE_STATUS = "in_progress"
# Statuses written by the final save; a late live update must never undo them.
FINAL_STATUSES = {"completed", "failed", "transferred", "transfer_requested", "missed", "busy", "no-answer", "canceled"}


def ordered_from_lists(customer: Iterable[Dict[str, Any]], agent: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Merge separately stored caller and agent lines by their timestamps (custom voice stores real times)."""
    lines = [{"role": "customer", "text": str(item.get("content") or "").strip(), "at": str(item.get("timestamp") or "")} for item in customer]
    lines += [{"role": "agent", "text": str(item.get("content") or "").strip(), "at": str(item.get("timestamp") or "")} for item in agent]
    return sorted((line for line in lines if line["text"]), key=lambda line: line["at"])


def ordered_from_retell(transcript: Iterable[Dict[str, Any]], started_at: Optional[datetime] = None) -> List[Dict[str, str]]:
    """Retell transcripts are already in speaking order; word timings (seconds from start) give each line its time."""
    lines: List[Dict[str, str]] = []
    for item in transcript or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").lower()
        text = str(item.get("content") or "").strip()
        if not text or role not in {"user", "agent", "assistant"}:
            continue
        at = ""
        words = item.get("words") or []
        if started_at and words and isinstance(words[0], dict) and words[0].get("start") is not None:
            try:
                at = datetime.fromtimestamp(started_at.timestamp() + float(words[0]["start"]), timezone.utc).isoformat()
            except (TypeError, ValueError):
                at = ""
        lines.append({"role": "customer" if role == "user" else "agent", "text": text, "at": at})
    return lines


def save_live(session_id: str, phone_number: str, provider: str, messages: List[Dict[str, str]], channel_type: str = "phone_call") -> None:
    """Upsert the in-progress call record with the transcript so far. Safe to call after every turn."""
    if not session_id or not mongo_client.is_connected():
        return
    try:
        existing = mongo_client.calls.find_one({"call_session_id": session_id}, {"status": 1})
        if existing and str(existing.get("status", "")).lower() in FINAL_STATUSES:
            logger.debug("live transcript skipped: call %s already %s", session_id, existing.get("status"))
            return
        now = utc_now()
        mongo_client.calls.update_one(
            {"call_session_id": session_id},
            {"$set": {"messages": messages, "status": LIVE_STATUS, "updated_at": now, "phone_number": phone_number},
             "$setOnInsert": {"call_date": now, "created_at": now, "direction": "inbound", "provider": provider,
                              "provider_call_id": session_id, "channel_type": channel_type, "lead_id": None}},
            upsert=True,
        )
        logger.info("live transcript saved: call %s (%s) %d lines", session_id, provider, len(messages))
        realtime.publish("call", key=session_id)
    except Exception as error:  # never break a live call because the inbox could not be updated
        logger.warning("live transcript save failed for call %s: %s", session_id, error)


def close_orphaned_calls(provider: str = "custom_twilio") -> int:
    """Finish calls left "in progress" by a server restart. The custom voice pipeline runs inside this server, so at
    startup none of its calls can still be live; without this they would show as Live in the inbox forever.
    (Retell calls are not touched: Retell runs them and sends their end itself.)"""
    if not mongo_client.is_connected():
        return 0
    from call_status import schedule_auto_status  # imported here: call_status imports the routers
    closed = 0
    for call in mongo_client.calls.find({"status": LIVE_STATUS, "provider": provider}, {"created_at": 1, "updated_at": 1, "call_session_id": 1}):
        started, last = call.get("created_at"), call.get("updated_at")
        duration = max(0, int((last - started).total_seconds())) if started and last else 0
        mongo_client.calls.update_one({"_id": call["_id"]}, {"$set": {
            "status": "completed", "provider_call_status": "ended", "disconnection_reason": "server_restart",
            "duration": duration, "updated_at": utc_now()}})
        schedule_auto_status(str(call["_id"]), "completed")  # AI summary and status from what was said
        realtime.publish("call", key=call.get("call_session_id") or str(call["_id"]))
        closed += 1
    if closed:
        logger.warning("closed %d %s call(s) left in progress by a server restart", closed, provider)
    return closed
