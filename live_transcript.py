"""Live, in-order call transcripts for the team inbox.

Voice providers call save_live() after every turn, so a teammate watching the inbox sees the
conversation while the call is still running. The final save (calls_api.log_call) later marks
the same record completed; it is matched on call_session_id, so a call is always one record.

    messages: [{"role": "customer" | "agent", "text": "...", "at": "2026-10-19T15:01:02"}]
"""

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from mongo_client import mongo_client

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
                at = datetime.fromtimestamp(started_at.timestamp() + float(words[0]["start"])).isoformat()
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
        now = datetime.now()
        mongo_client.calls.update_one(
            {"call_session_id": session_id},
            {"$set": {"messages": messages, "status": LIVE_STATUS, "updated_at": now, "phone_number": phone_number},
             "$setOnInsert": {"call_date": now, "created_at": now, "direction": "inbound", "provider": provider,
                              "provider_call_id": session_id, "channel_type": channel_type, "lead_id": None}},
            upsert=True,
        )
        logger.info("live transcript saved: call %s (%s) %d lines", session_id, provider, len(messages))
    except Exception as error:  # never break a live call because the inbox could not be updated
        logger.warning("live transcript save failed for call %s: %s", session_id, error)
