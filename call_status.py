"""Automatic conversation status (the sidebar's STATUS group) from what the caller said.

When a call is stored as finished, the AI reads its transcript and summary and moves the caller's conversation to
the pipeline stage that fits (Outreach, Scheduling, Won, ...), recording the reason on the timeline. A status set by
a person is never changed by the AI. Runs in the background so saving a call is never slowed down.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId

from ai_services import AIServices
from mongo_client import mongo_client
import realtime

logger = logging.getLogger(__name__)

AI_ACTOR = "ai"
AI_NAME = "AI assistant"
FINAL_STATUSES = {"completed", "failed", "missed", "no-answer", "busy", "canceled", "transferred", "transfer_requested"}

# What each stage means, for the model. Generic on purpose: the business and its knowledge change.
STAGE_GUIDE = {
    "new_lead": "first contact or a general enquiry with no clear next step yet",
    "outreach": "the team needs to contact the caller: they asked for a callback, a quote, more information, or a person",
    "scheduling": "the caller wants to arrange a time: a meeting, visit, measure, installation, delivery or collection time",
    "servicing": "an existing customer with an order, delivery, installation, faulty product, return or exchange being handled",
    "payment_collection": "the call is about paying: an invoice, deposit, balance, payment method, or a refund owed",
    "follow_up": "the caller is interested but will decide later, is comparing options, or asked to be contacted again later",
    "won": "the caller committed: they placed or confirmed an order or purchase, or accepted a quote",
    "lost": "the caller is not interested, chose someone else, or declined",
    "unqualified": "not a real enquiry: wrong number, spam, silence, a test call, or something the business does not offer",
}


def _transcript(call: Dict[str, Any]) -> str:
    lines: List[str] = []
    for line in call.get("messages") or []:
        text = str(line.get("text") or "").strip()
        if text:
            lines.append(f"{'Caller' if line.get('role') == 'customer' else 'Agent'}: {text}")
    if not lines:
        for item in (call.get("transcription") or []) + (call.get("ai_responses") or []):
            text = str(item.get("content") or "").strip()
            if text:
                lines.append(f"{'Caller' if item.get('type') == 'user' else 'Agent'}: {text}")
    return "\n".join(lines[-60:])


def classify(call: Dict[str, Any], current_stage: str) -> Optional[Dict[str, Any]]:
    """Ask the model which stage the conversation belongs in now. None when there is nothing to judge."""
    transcript = _transcript(call)
    summary = str(call.get("call_summary") or call.get("summary") or "").strip()
    if not transcript and not summary:
        return None
    guide = "\n".join(f"- {stage}: {meaning}" for stage, meaning in STAGE_GUIDE.items())
    prompt = (
        "You file inbound phone calls for a business into a sales pipeline. Pick the ONE stage that best describes "
        "where this caller's conversation stands now, based on what the caller said and asked for.\n\n"
        f"Stages:\n{guide}\n\nThe conversation is currently in: {current_stage}. Keep it there if nothing in this call "
        "moves it. A request for a callback or quote is outreach; wanting a time arranged is scheduling.\n\n"
        f"Call summary: {summary or '(none)'}\n\nTranscript:\n{transcript or '(none)'}\n\n"
        'Answer with JSON only: {"stage": "<one stage id>", "reason": "<one short sentence for the team, quoting what the caller wanted>", '
        '"confidence": <0 to 1>, "summary": "<one or two plain sentences for the team: what the caller wanted and how the call ended; '
        'include any name, number or email the caller confirmed; no stage names>"}'
    )
    services = AIServices()
    model = os.getenv("CALL_STATUS_MODEL", "openai/gpt-4.1-mini").strip()
    response = services.openai_client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], max_tokens=200, temperature=0,
        response_format={"type": "json_object"},
        extra_body={"provider": {"order": ["openai", "azure"], "allow_fallbacks": True}},
    )
    result = json.loads(response.choices[0].message.content or "{}")
    if result.get("stage") not in STAGE_GUIDE:
        return None
    return {"stage": result["stage"], "reason": str(result.get("reason") or "").strip()[:240], "confidence": float(result.get("confidence") or 0),
            "summary": str(result.get("summary") or "").strip()[:600]}


def is_placeholder_summary(text: Any) -> bool:
    """Stand-in text written when a provider sent no summary; the AI summary replaces it."""
    text = str(text or "").strip()
    return not text or text == "Retell call completed." or text.startswith("Custom Twilio voice call with ")


def apply_auto_status(call_id: str) -> Optional[str]:
    """Classify a finished call and move its conversation if needed. Returns the new stage, or None."""
    from routers import inbox_api  # imported here: inbox_api imports the calls router
    if not mongo_client.is_connected():
        return None
    call = mongo_client.calls.find_one({"_id": ObjectId(call_id)}) if ObjectId.is_valid(call_id) else None
    if not call or str(call.get("status") or "").lower() not in FINAL_STATUSES or call.get("direction") == "outbound":
        return None
    key = inbox_api._thread_key(call)
    state = mongo_client.inbox_threads.find_one({"_id": key}) or {}
    person_set = bool(state.get("stage_by_name") and state.get("stage_by") != AI_ACTOR)
    needs_summary = is_placeholder_summary(call.get("call_summary") or call.get("summary"))
    if person_set and not needs_summary:
        logger.info("auto status: %s was set by %s; the AI leaves it", key, state.get("stage_by_name"))
        return None
    current = state.get("stage") if state.get("stage") in inbox_api.STAGE_LABELS else inbox_api.DEFAULT_STAGE
    try:
        verdict = classify(call, current)
    except Exception as error:
        logger.warning("auto status: classification failed for %s: %s", key, error)
        return None
    # Calls whose provider sent no summary ("Retell call completed.", "Custom Twilio voice call with ...")
    # get the AI's summary instead, so the inbox, dashboard and call history show what the caller wanted.
    if verdict and needs_summary and verdict.get("summary"):
        mongo_client.calls.update_one({"_id": call["_id"]}, {"$set": {"call_summary": verdict["summary"], "summary": verdict["summary"], "summary_by": AI_ACTOR}})
        realtime.publish("call", key=call.get("call_session_id") or str(call["_id"]))
    if person_set:
        return None
    try:
        minimum = float(os.getenv("CALL_STATUS_MIN_CONFIDENCE", "0.6"))
    except ValueError:
        minimum = 0.6
    if not verdict or verdict["stage"] == current or verdict["confidence"] < minimum:
        logger.info("auto status: %s stays %s (%s)", key, current, verdict)
        return None
    now = datetime.utcnow()
    stage = verdict["stage"]
    mongo_client.inbox_threads.update_one(
        {"_id": key},
        {"$set": {"stage": stage, "stage_by": AI_ACTOR, "stage_by_name": AI_NAME, "stage_at": now, "stage_reason": verdict["reason"], "updated_at": now},
         "$push": {"stage_history": {"from": current, "to": stage, "by": AI_ACTOR, "by_name": AI_NAME, "reason": verdict["reason"], "at": now}}},
        upsert=True,
    )
    realtime.publish("stage", key=key)
    logger.info("auto status: %s moved %s -> %s (%.2f): %s", key, current, stage, verdict["confidence"], verdict["reason"])
    assignee_id = state.get("assigned_to")
    if assignee_id and ObjectId.is_valid(assignee_id):
        assignee = mongo_client.users.find_one({"_id": ObjectId(assignee_id)})
        if assignee:
            inbox_api._notify(assignee, "status", "Conversation status changed",
                              f"The AI moved the conversation with {inbox_api._thread_name(key)} to {inbox_api.STAGE_LABELS[stage]}: {verdict['reason']}", key)
    return stage


def schedule_auto_status(call_id: str, status: str) -> None:
    """Called when a call is stored: classify it in the background once it has finished."""
    if os.getenv("CALL_STATUS_AUTO", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    if str(status or "").lower() not in FINAL_STATUSES:
        return
    threading.Thread(target=apply_auto_status, args=(str(call_id),), name="auto-status", daemon=True).start()
