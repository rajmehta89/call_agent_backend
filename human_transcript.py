"""Transcribe the two channels of a forwarded call after Twilio finishes recording."""

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from mongo_client import mongo_client
import realtime


logger = logging.getLogger(__name__)
RECORDING_SID = re.compile(r"^RE[0-9a-fA-F]{32}$")
BRIEF_UTTERANCE = re.compile(r"(?:(?:hello|hi|hey|what|yes|yeah|okay|ok|done|can you hear me|are you there|thank you|thanks)\b[?.!\s]*)+", re.I)


def lines_from_utterances(payload: dict[str, Any], started_at: datetime | None = None) -> list[dict[str, str]]:
    lines = []
    for utterance in (payload.get("results") or {}).get("utterances") or []:
        text = str(utterance.get("transcript") or "").strip()
        if not text:
            continue
        role = "customer" if utterance.get("channel") == 0 else "human_agent"
        at = ""
        if started_at is not None:
            try:
                at = (started_at + timedelta(seconds=float(utterance.get("start") or 0))).isoformat()
            except (TypeError, ValueError):
                pass
        lines.append({"role": role, "text": text, "at": at})
    return sorted(lines, key=lambda line: line["at"])


def summarize_human_segment(lines: list[dict[str, str]]) -> str:
    """Summarize only what was heard after the human answered, without inventing a conversation."""
    if not lines:
        return ""
    if all(BRIEF_UTTERANCE.fullmatch(str(line.get("text") or "").strip()) for line in lines):
        if any(line.get("role") == "human_agent" for line in lines):
            return "The human agent and caller exchanged only brief words. No substantive request or next step was captured."
        return "Only brief caller speech was captured. No human agent reply or substantive discussion was transcribed."
    from ai_services import AIServices
    transcript = "\n".join(
        f"{'Human agent' if line.get('role') == 'human_agent' else 'Caller'}: {str(line.get('text') or '').strip()}"
        for line in lines if str(line.get("text") or "").strip()
    )
    reply = AIServices().chat_completion([
        {"role": "system", "content": "Summarize only the human agent portion of a phone call in one or two plain sentences for the call history. State what was discussed and any agreed next step. Do not infer details from the earlier AI portion. If the speech is unclear or contains no substantive discussion, say so. Do not invent a promise, name, product, or outcome. Use plain ASCII punctuation."},
        {"role": "user", "content": transcript},
    ], max_tokens=160, temperature=0)
    return str(reply or "").strip()[:600]


def transcribe_human_recording(call_sid: str, recording_sid: str) -> None:
    """Background job; failures are recorded and never hold Twilio's callback open."""
    if not RECORDING_SID.fullmatch(recording_sid):
        return
    api_key = os.getenv("DEEPGRAM_API_KEY") or os.getenv("DG_API_KEY")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    media_user = os.getenv("TWILIO_API_KEY_SID") or account_sid
    media_password = os.getenv("TWILIO_API_KEY_SECRET") or os.getenv("TWILIO_AUTH_TOKEN")
    if not (api_key and account_sid and media_user and media_password):
        return
    if not mongo_client.is_connected():
        logger.warning("human recording transcription deferred for %s: database unavailable", call_sid)
        return
    call = mongo_client.calls.find_one({"$or": [{"call_session_id": call_sid}, {"twilio_call_sid": call_sid}]})
    if not call or (call.get("human_recording_sid") or call.get("recording_sid")) != recording_sid:
        return
    where = {"_id": call["_id"], "recording_sid": recording_sid}
    if call.get("human_transcription_status") == "complete":
        return
    mongo_client.calls.update_one(where, {"$set": {"human_transcription_status": "processing"}})
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.wav?RequestedChannels=2"
        media = _retry_connection(lambda: requests.get(url, auth=(media_user, media_password), timeout=(5, 45)))
        media.raise_for_status()
        response = _retry_connection(lambda: requests.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-3", "multichannel": "true", "utterances": "true", "smart_format": "true"},
            headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"},
            data=media.content,
            timeout=(5, 90),
        ))
        response.raise_for_status()
        ended = call.get("human_call_ended_at")
        duration = call.get("human_call_duration")
        started = ended - timedelta(seconds=duration) if isinstance(ended, datetime) and isinstance(duration, (int, float)) else None
        if started and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        lines = lines_from_utterances(response.json(), started)
        human_summary = ""
        human_summary_status = "no_speech" if not lines else "complete"
        if lines:
            try:
                human_summary = summarize_human_segment(lines)
                if not human_summary:
                    human_summary_status = "error"
            except Exception as exc:
                human_summary_status = "error"
                logger.warning("human summary failed for %s: %s", call_sid, type(exc).__name__)
        mongo_client.calls.update_one(where, {"$set": {
            "human_transcript": lines,
            "human_transcription_status": "complete" if lines else "no_speech",
            "human_summary": human_summary,
            "human_summary_status": human_summary_status,
            "updated_at": datetime.now(timezone.utc),
        }})
        realtime.publish("call", key=call_sid)
        if lines:
            from call_status import schedule_auto_status
            schedule_auto_status(str(call["_id"]), str(call.get("status") or ""))
        logger.info("human recording transcribed for %s (%d lines)", call_sid, len(lines))
    except Exception as exc:
        mongo_client.calls.update_one(where, {"$set": {"human_transcription_status": "error"}})
        logger.warning("human recording transcription failed for %s: %s", call_sid, type(exc).__name__)


def _retry_connection(request):
    for attempt in range(3):
        try:
            return request()
        except requests.ConnectionError:
            if attempt == 2:
                raise
            time.sleep(2)
