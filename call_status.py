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
from pymongo.errors import DuplicateKeyError

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
    "outreach": "the team needs to contact a prospective caller about a callback, quote, more information, or a person, with no existing order issue as the main reason",
    "scheduling": "the caller wants to arrange a time: a meeting, visit, measure, installation, delivery or collection time",
    "servicing": "an existing customer needs help with an order, missing or late delivery, installation, faulty product, return or exchange; includes an order already paid for but not received",
    "payment_collection": "the money itself needs action: an unpaid invoice, deposit or balance, failed payment, payment method, disputed charge, or refund owed",
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
    for line in call.get("human_transcript") or []:
        text = str(line.get("text") or "").strip()
        if text:
            lines.append(f"{'Human agent' if line.get('role') == 'human_agent' else 'Caller'}: {text}")
    return "\n".join(lines[-60:])


def classify(call: Dict[str, Any], current_stage: str) -> Optional[Dict[str, Any]]:
    """Ask the model which stage the conversation belongs in now. None when there is nothing to judge."""
    transcript = _transcript(call)
    summary = str(call.get("call_summary") or call.get("summary") or "").strip()
    human_summary = str(call.get("human_summary") or "").strip()
    contact_phone = str(call.get("contact_phone") or "").strip()
    if not transcript and not summary:
        return None
    guide = "\n".join(f"- {stage}: {meaning}" for stage, meaning in STAGE_GUIDE.items())
    prompt = (
        "You file inbound phone calls for a business into a sales pipeline. Pick the ONE stage that best describes "
        "where this caller's conversation stands now, based on what the caller said and asked for.\n\n"
        f"Stages:\n{guide}\n\nThe conversation is currently in: {current_stage}. Keep it there if nothing in this call "
        "moves it: an earlier request (a callback, a time, an order) stays open even if this call only asked a general "
        "question. A request for a callback or quote is outreach; wanting a time arranged is scheduling. "
        "Classify by the main unresolved reason for the latest call. A team callback about an existing order does not "
        "turn an order or delivery problem into outreach. If the caller says they paid and their order has not arrived, "
        "choose servicing; choose payment_collection only when the payment, charge, balance or refund itself needs resolution.\n\n"
        f"AI call summary: {summary or '(none)'}\nHuman segment summary: {human_summary or '(none)'}\n"
        f"System-validated contact number: {contact_phone or '(none)'}. If present, it is valid and overrides any agent guess that it was short or incomplete.\n"
        f"System transfer result: requested={bool(call.get('transfer_requested'))}, status={call.get('transfer_status') or 'not attempted'}, "
        f"connected={bool(call.get('transfer_succeeded'))}. This system result is authoritative; an AI promise to transfer is not proof of a connection. "
        "When requested=false, no dial was initiated: say 'no live transfer was attempted' if describing the outcome. "
        "Never say a transfer was attempted or completed unless the system result confirms it.\n"
        "If the caller asked to speak with someone now, describe it as a live transfer request, even if the agent incorrectly offered a later callback.\n\n"
        f"Transcript:\n{transcript or '(none)'}\n\n"
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


def may_move(current: str, stage: str) -> bool:
    """New lead is only where a conversation starts ("no next step yet"). Once a caller has asked for something
    (a callback, a time, an order), a later call with just a general question must not undo that by sending the
    conversation back to New lead. Moves between the other stages are the model's judgement (e.g. Follow-up ->
    Scheduling when the caller comes back to book a time)."""
    return stage != "new_lead" or current == "new_lead"


def is_placeholder_summary(text: Any) -> bool:
    """Stand-in text written when a provider sent no summary; the AI summary replaces it."""
    text = str(text or "").strip()
    # Our own stand-ins: the Retell webhook's, the Retell end-of-call backup save's (it was missing, so Retell calls
    # whose webhook never arrived kept "Retell inbound AI call completed."), and the custom stack's.
    return (not text or text in {"Retell call completed.", "Retell inbound AI call completed.", "Human callback requested from the Retell AI call."}
            or text.startswith("Custom Twilio voice call with "))


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
    person_set = bool((state.get("stage_by") or state.get("stage_by_name")) and state.get("stage_by") != AI_ACTOR)
    needs_summary = is_placeholder_summary(call.get("call_summary") or call.get("summary")) or bool(
        call.get("provider") == "custom_twilio" and call.get("summary_by") in (None, AI_ACTOR))
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
        # Only if the summary is still the one read above: classifying takes seconds, and Retell's own summary
        # (webhook or fetch) may have been stored meanwhile; it must not be replaced by ours.
        written = mongo_client.calls.update_one({"_id": call["_id"], "call_summary": call.get("call_summary")},
                                                {"$set": {"call_summary": verdict["summary"], "summary": verdict["summary"], "summary_by": AI_ACTOR}})
        if written.modified_count:
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
    if not may_move(current, verdict["stage"]):
        logger.info("auto status: %s stays %s: a later call never sends it back to %s (%s)", key, current, verdict["stage"], verdict["reason"])
        return None
    now = datetime.utcnow()
    stage = verdict["stage"]
    try:
        written = mongo_client.inbox_threads.update_one(
            {"_id": key, "stage_revision": state.get("stage_revision"), "stage_by": state.get("stage_by"), "stage_at": state.get("stage_at")},
            {"$set": {"stage": stage, "stage_by": AI_ACTOR, "stage_by_name": AI_NAME, "stage_at": now, "stage_reason": verdict["reason"], "updated_at": now},
             "$inc": {"stage_revision": 1},
             "$push": {"stage_history": {"from": current, "to": stage, "by": AI_ACTOR, "by_name": AI_NAME, "reason": verdict["reason"], "at": now}}},
            upsert=True,
        )
    except DuplicateKeyError:  # another writer created/changed the thread meanwhile
        return None
    if written.matched_count == 0 and written.upserted_id is None:
        return None
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
