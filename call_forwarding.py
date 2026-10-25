"""Live call forwarding to the human team.

An admin turns it on and lists the agents' phone numbers (Settings > Call forwarding). When it is on and a caller asks
to talk to a person, the AI says it is connecting them and the call is moved off the AI: every available agent's phone
rings at once, and the first phone to answer is connected. If nobody answers in time, the caller hears that the team will call back, and the call
is saved as a callback request (Outreach). When it is off, nothing changes: the AI handles the call and a request for a
person becomes a callback request.

Works for both voice paths: the custom stack knows the Twilio call itself; for Retell the Twilio call is remembered
when Retell is given the call (remember_retell_call), since Retell only tells us its own call id.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from functools import lru_cache
from datetime import datetime
from typing import Any, Dict, List, Optional

import phonenumbers

from mongo_client import mongo_client

logger = logging.getLogger(__name__)

SETTINGS_ID = "call_forwarding"
MODE = "ring_all_answer"  # transfer_mode on forwarded calls (calls_api keeps their outcome on later saves)
DEFAULT_RING_SECONDS = 20
MIN_RING_SECONDS, MAX_RING_SECONDS = 10, 60
MAX_AGENTS = 10

# What the caller hears. Business-neutral: the business name and details come from the knowledge, not from here.
CONNECTING_LINE = "Connecting you to the team now."
TEAM_BUSY_LINE = ("Sorry, no one on the team could answer right now. I've passed on your request. "
                  "If we have a phone number for you, the team can call you back. Otherwise, please contact us again with a number to reach you.")
AGENT_WHISPER = "A customer is waiting to speak with you. Press 1 now to accept and connect the call."
# Twilio <Say> voice for the lines spoken outside the AI voice (on hold, and to the agent). Change with FORWARDING_SAY_VOICE.
SAY_VOICE = os.getenv("FORWARDING_SAY_VOICE", "Polly.Joanna-Neural")


def _region() -> str:
    return (os.getenv("VOICE_DEFAULT_REGION") or "AU").strip().upper() or "AU"


def normalize_number(raw: str) -> str:
    """An agent's number in E.164 (+61412345678), or "" when it is not a valid phone number. Local numbers are read in
    the business's region (VOICE_DEFAULT_REGION)."""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        number = phonenumbers.parse(text, None if text.startswith("+") else _region())
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(number):
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _clean_agents(agents: Any) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in agents if isinstance(agents, list) else []:
        if not isinstance(item, dict):
            continue
        cleaned.append({"name": str(item.get("name") or "").strip()[:80], "phone": str(item.get("phone") or "").strip()[:32],
                        "enabled": bool(item.get("enabled", True))})
    return cleaned[:MAX_AGENTS]


def get_settings() -> Dict[str, Any]:
    """Saved settings (defaults when nothing is saved yet: off, no agents)."""
    saved: Dict[str, Any] = {}
    if mongo_client.is_connected():
        saved = mongo_client.platform_settings.find_one({"_id": SETTINGS_ID}) or {}
    ring = saved.get("ring_seconds")
    return {
        "enabled": bool(saved.get("enabled", False)),
        "agents": _clean_agents(saved.get("agents")),
        "ring_seconds": ring if isinstance(ring, int) and MIN_RING_SECONDS <= ring <= MAX_RING_SECONDS else DEFAULT_RING_SECONDS,
        "updated_by_name": saved.get("updated_by_name"),
        "updated_at": saved.get("updated_at"),
    }


def validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Check and normalise settings from the admin page. Raises ValueError with a message the admin can act on."""
    agents = []
    for index, agent in enumerate(_clean_agents(payload.get("agents")), start=1):
        if not agent["name"] and not agent["phone"]:
            continue  # an empty row left in the form
        number = normalize_number(agent["phone"])
        if not number:
            raise ValueError(f"Agent {index} ({agent['name'] or 'no name'}): '{agent['phone']}' is not a valid phone number")
        agents.append({"name": agent["name"] or f"Agent {index}", "phone": number, "enabled": agent["enabled"]})
    numbers = [agent["phone"] for agent in agents]
    if len(numbers) != len(set(numbers)):
        raise ValueError("The same phone number is listed twice")
    ring = payload.get("ring_seconds", DEFAULT_RING_SECONDS)
    try:
        ring = int(ring)
    except (TypeError, ValueError):
        raise ValueError("Ring time must be a number of seconds")
    if not MIN_RING_SECONDS <= ring <= MAX_RING_SECONDS:
        raise ValueError(f"Ring time must be between {MIN_RING_SECONDS} and {MAX_RING_SECONDS} seconds")
    enabled = bool(payload.get("enabled"))
    if enabled and not any(agent["enabled"] for agent in agents):
        raise ValueError("Add at least one active agent before turning call forwarding on")
    return {"enabled": enabled, "agents": agents, "ring_seconds": ring}


def save_settings(payload: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    settings = validate(payload)
    mongo_client.platform_settings.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {**settings, "updated_by": str(user.get("_id") or ""), "updated_by_name": user.get("name"), "updated_at": datetime.utcnow()}},
        upsert=True)
    _ON_CACHE.update(on=settings["enabled"] and bool(active_agents(settings)), at=time.monotonic())  # live calls see it next turn
    logger.info("call forwarding %s by %s: %d agent(s), %d active, ring %ss", "on" if settings["enabled"] else "off", user.get("email"),
                len(settings["agents"]), sum(agent["enabled"] for agent in settings["agents"]), settings["ring_seconds"])
    return get_settings()


def active_agents(settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    settings = settings or get_settings()
    return [agent for agent in settings["agents"] if agent["enabled"] and agent["phone"]]


_ON_CACHE: Dict[str, Any] = {"at": -1e9, "on": False, "refreshing": False}
ON_CACHE_SECONDS = 10  # re-read in the background at most this often; saving the settings page updates it at once


def _refresh_on() -> None:
    try:
        settings = get_settings()
        _ON_CACHE.update(on=settings["enabled"] and bool(active_agents(settings)), at=time.monotonic())
    except Exception as error:
        logger.debug("call forwarding: settings refresh failed (keeping %s): %s", _ON_CACHE["on"], error)
    finally:
        _ON_CACHE["refreshing"] = False


def forwarding_on() -> bool:
    """Forwarding is on and someone can take the call. Asked on every caller turn inside the live call, so it never
    waits for the database: it answers from the last reading and refreshes that in the background when stale (a slow
    database or network would otherwise hold up the call's audio)."""
    if time.monotonic() - _ON_CACHE["at"] > ON_CACHE_SECONDS and not _ON_CACHE["refreshing"]:
        _ON_CACHE["refreshing"] = True
        import threading
        threading.Thread(target=_refresh_on, name="forwarding-settings", daemon=True).start()
    return _ON_CACHE["on"]


def should_forward(caller_text: str) -> bool:
    """Forwarding is on and the caller asked for a person now. The meaning check runs only when forwarding is on;
    call it off the event loop (it embeds the text)."""
    try:
        return forwarding_on() and wants_person(caller_text)
    except Exception as error:
        logger.warning("call forwarding check failed (the AI carries on): %s", error)
        return False


def explicit_live_request(caller_text: str) -> bool:
    """Embedding-based live handoff check for a partial caller turn."""
    return handoff_intent(caller_text) == "live"


def followup_live_request(caller_text: str, earlier: str = "") -> bool:
    """Read a short reply together with the earlier team request."""
    return handoff_intent(caller_text, earlier) == "live"


def team_handoff_request(caller_text: str) -> bool:
    """An unclear handoff request, suitable for a now-or-later clarification."""
    return handoff_intent(caller_text) == "unclear"


def ambiguous_team_call(caller_text: str) -> bool:
    """A request to arrange a team call without clear live or later timing."""
    return handoff_intent(caller_text) == "unclear"


def handoff_intent(caller_text: str, earlier: str = "") -> str:
    """Classify the full turn, optionally with the preceding team request: live, callback, unclear or other."""
    from turn_meaning import HANDOFF_ACTION
    ranked = HANDOFF_ACTION.rank(caller_text, earlier)
    if not ranked:
        return ""
    scores = dict(ranked)
    live, callback = scores["live"], scores["callback"]
    unclear, other = scores["unclear"], scores["other"]
    # Embeddings sometimes put "arrange a call now" and "arrange a call tomorrow"
    # almost on top of each other. Ask a small language model to resolve only
    # that close live/callback tie, preserving the rest of the fast classifier.
    if max(live, callback) >= HANDOFF_ACTION.min_score and abs(live - callback) <= 0.02 and unclear <= max(live, callback) + 0.03:
        decided = _adjudicate_handoff(caller_text, earlier)
        if decided:
            return decided
        return "unclear"  # ask now-or-later instead of guessing after a provider error
    if live >= HANDOFF_ACTION.min_score and live >= callback + 0.025 and live >= unclear - 0.02 and live >= other + 0.02:
        result = "live"
    elif callback >= HANDOFF_ACTION.min_score and callback >= live + 0.02 and callback >= unclear + 0.02 and callback >= other + 0.02:
        result = "callback"
    elif unclear >= HANDOFF_ACTION.min_score and unclear >= other + 0.02:
        result = "unclear"
    else:
        result = ""
    logger.debug("handoff intent %r -> %s (%s)", caller_text[:80], result or "other", ", ".join(f"{k} {v:.2f}" for k, v in ranked))
    return result


@lru_cache(maxsize=256)
def _adjudicate_handoff(caller_text: str, earlier: str = "") -> str:
    """Resolve an embedding tie using the actual caller wording and context."""
    try:
        from ai_services import AIServices
        services = AIServices()
        model = os.getenv("HANDOFF_CLASSIFIER_MODEL") or ("openai/gpt-4.1-mini" if services.provider == "openrouter" else "gpt-4.1-mini")
        message = f"Earlier caller request: {earlier}\nCurrent caller turn: {caller_text}" if earlier else caller_text
        response = services.openai_client.with_options(timeout=6).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Classify whether the caller wants to speak with a human team member. "
                 "Reply with exactly one word: LIVE, CALLBACK, UNCLEAR, or OTHER. "
                 "LIVE means connect during this ongoing phone call. CALLBACK means the team should contact them later. "
                 "UNCLEAR means a human is requested but timing is not clear. OTHER means no human is requested. "
                 "Read the caller's meaning and timing, even when the wording is indirect."},
                {"role": "user", "content": message},
            ], max_tokens=8, temperature=0)
        answer = (response.choices[0].message.content or "").strip().lower()
        return answer if answer in {"live", "callback", "unclear", "other"} else ""
    except Exception as error:
        logger.warning("handoff tie check failed: %s", error)
        return ""


def wants_person(caller_text: str, earlier: str = "") -> bool:
    return handoff_intent(caller_text, earlier) == "live"


def asks_for_team(caller_text: str) -> str:
    """Retell's live-person or later-callback decision from the same embedding model."""
    result = handoff_intent(caller_text)
    return "person" if result == "live" else result if result == "callback" else ""


# ---- Retell: its call id -> the Twilio call carrying it (Retell calls arrive as Twilio calls bridged over SIP).
_RETELL_CALLS: Dict[str, Dict[str, Any]] = {}
_RETELL_KEEP_SECONDS = 4 * 3600


def remember_retell_call(retell_call_id: str, call_sid: str, caller: str) -> None:
    now = time.monotonic()
    for key in [key for key, item in _RETELL_CALLS.items() if now - item["at"] > _RETELL_KEEP_SECONDS]:
        _RETELL_CALLS.pop(key, None)
    if retell_call_id and call_sid:
        _RETELL_CALLS[retell_call_id] = {"call_sid": call_sid, "caller": caller, "at": now}


def twilio_call_for_retell(retell_call_id: str) -> Optional[Dict[str, Any]]:
    return _RETELL_CALLS.get(retell_call_id)


# ---- TwiML
def _base_url() -> str:
    return (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("TWILIO_PUBLIC_BASE_URL") or "").rstrip("/")


def _caller_id(caller: str) -> str:
    """Shown to the agent: the customer's own number when there is one (browser test calls have none: the business
    number is shown instead)."""
    if str(caller or "").startswith("+"):
        return caller
    return (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or "").strip()


def transfer_twiml(caller: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """Ring every active agent at once and bridge the first answer to the caller."""
    settings = settings or get_settings()
    base = _base_url()
    agents = active_agents(settings)
    if not agents or not base:
        raise RuntimeError("call forwarding has no active agents" if not agents else "TWILIO_PUBLIC_BASE_URL is not set")
    numbers = "".join(f'<Number>{html.escape(agent["phone"])}</Number>' for agent in agents)
    caller_id = _caller_id(caller)
    caller_attr = f' callerId="{html.escape(caller_id, quote=True)}"' if caller_id else ""
    result = html.escape(f"{base}/api/call-forwarding/dial-result", quote=True)
    recording_result = html.escape(f"{base}/api/call-forwarding/recording-result", quote=True)
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Dial{caller_attr} answerOnBridge="true" '
            f'record="record-from-answer-dual" recordingStatusCallback="{recording_result}" '
            f'recordingStatusCallbackMethod="POST" timeout="{settings["ring_seconds"]}" action="{result}" method="POST">{numbers}</Dial>'
            "</Response>")


def recording_finished(call_sid: str, recording_sid: str, recording_url: str, status: str) -> None:
    """Twilio sends this only when the bridge recording is available (or absent)."""
    if not call_sid or not _find_call(call_sid):
        logger.warning("call forwarding: recording callback has no matching call %s", call_sid)
        return
    if status == "completed" and recording_sid.startswith("RE") and recording_url.startswith("https://"):
        _record(call_sid, {"recording_sid": recording_sid, "recording_url": recording_url,
                           "human_recording_sid": recording_sid, "human_recording_status": "completed",
                           "human_transcription_status": "pending" if (os.getenv("DEEPGRAM_API_KEY") or os.getenv("DG_API_KEY")) else "unavailable"})
        logger.info("call forwarding: recording ready for %s", call_sid)
    elif status == "absent":
        _record(call_sid, {"human_recording_status": "absent"})
        logger.warning("call forwarding: Twilio recording absent for %s", call_sid)


def full_recording_finished(call_sid: str, recording_sid: str, recording_url: str, status: str, duration: str = "") -> None:
    """Save the parent-call audio that starts before the AI greeting."""
    if not call_sid:
        return
    # Retell's final webhook and Twilio's recording callback can arrive in either order.
    for attempt in range(6):
        if _find_call(call_sid):
            break
        if attempt < 5:
            time.sleep(2)
    else:
        logger.warning("call forwarding: full recording callback has no matching call %s", call_sid)
        return
    if status == "completed" and recording_sid.startswith("RE") and recording_url.startswith("https://"):
        fields = {"full_recording_sid": recording_sid, "full_recording_url": recording_url,
                  "full_recording_status": "completed"}
        if str(duration).isdigit():
            fields["full_recording_duration"] = int(duration)
            current = _find_call(call_sid) or {}
            fields["duration"] = max(int(duration), int(current.get("duration") or 0))
        _record(call_sid, fields)
        logger.info("call forwarding: full recording ready for %s", call_sid)
    elif status == "absent":
        _record(call_sid, {"full_recording_status": "absent"})
        logger.warning("call forwarding: full recording absent for %s", call_sid)


def screen_twiml() -> str:
    """Played to an agent who picked up: accept with 1, otherwise their leg hangs up and the others keep ringing."""
    action = html.escape(f"{_base_url()}/api/call-forwarding/screen-result", quote=True)
    say = html.escape(AGENT_WHISPER)
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Gather numDigits="1" timeout="15" action="{action}" method="POST"><Say voice="{SAY_VOICE}">{say}</Say>'
            f'<Say voice="{SAY_VOICE}">Press 1 now to connect.</Say></Gather>'
            "<Hangup/></Response>")


def screen_result_twiml(digits: str) -> str:
    """1: an empty response connects the agent to the caller. Anything else: this agent's leg ends."""
    if str(digits or "").strip() == "1":
        return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def dial_result_twiml(accepted: bool) -> str:
    """After the agents' phones: hang up when an agent took the call (they talked and it is over), otherwise tell the
    caller the team will call back."""
    if accepted:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{SAY_VOICE}">{html.escape(TEAM_BUSY_LINE)}</Say>'
            "<Hangup/></Response>")


# ---- Moving a live call to the team, and recording what happened
def transfer(call_sid: str, caller: str, session_id: str = "") -> bool:
    """Take the live call away from the AI and ring the team. False when it could not be started (the AI carries on).
    `session_id`: the id the call is stored under when it is not the Twilio call (Retell)."""
    from routers.twilio_api import _get_twilio_client
    client = _get_twilio_client()
    if client is None or not call_sid:
        logger.warning("call forwarding: Twilio is not configured; cannot transfer %s", call_sid)
        return False
    settings = get_settings()
    try:
        twiml = transfer_twiml(caller, settings)
        client.calls(call_sid).update(twiml=twiml)
    except Exception as error:
        logger.warning("call forwarding: could not transfer %s: %s", call_sid, error)
        return False
    targets = [{"name": agent["name"], "phone": agent["phone"], "status": "requested"} for agent in active_agents(settings)]
    _record(call_sid, {"transfer_requested": True, "transfer_status": "ringing", "transfer_mode": MODE, "twilio_call_sid": call_sid,
                       "transfer_targets": targets,
                       "status": "transfer_requested", "transfer_destination": "Human team", "transfer_started_at": datetime.utcnow()},
            session_id or call_sid)
    _timeline(call_sid, caller, f"Caller asked for a person: ringing {len(active_agents(settings))} team member(s)")
    logger.info("call forwarding: %s moved to the team (%d agent(s), %ss)", call_sid, len(active_agents(settings)), settings["ring_seconds"])
    return True


def agent_accepted(parent_call_sid: str, agent_number: str) -> None:
    """An agent pressed 1: remember who took the call."""
    name = next((agent["name"] for agent in get_settings()["agents"] if agent["phone"] == agent_number), agent_number)
    _record(parent_call_sid, {"transfer_status": "connected", "transfer_destination": name, "handled_by": name,
                              "transfer_succeeded": True, "status": "transferred", "accepted_by": name})
    _timeline(parent_call_sid, "", f"Connected to {name}")
    logger.info("call forwarding: %s accepted by %s", parent_call_sid, name)


def dial_finished(call_sid: str, dial_status: str, caller: str, dial_call_sid: str = "", dial_duration: str = "") -> bool:
    """Record a completed bridge, or request a callback when nobody answered.

    Old press-1 transfers still require the screen-result webhook to have marked them connected.
    """
    status = str(dial_status or "").lower()
    leg = {"human_call_status": status, "human_call_ended_at": datetime.utcnow()}
    if dial_call_sid:
        leg["human_call_sid"] = dial_call_sid
    if str(dial_duration).isdigit():
        leg["human_call_duration"] = int(dial_duration)
    call = _find_call(call_sid)
    if call and call.get("transfer_mode") == MODE:
        try:
            from routers.twilio_api import _get_twilio_client
            client = _get_twilio_client()
            legs = client.calls.list(parent_call_sid=call_sid, limit=MAX_AGENTS) if client else []
            by_number = {str(leg.to): leg for leg in legs}
            targets = call.get("transfer_targets") or [
                {"name": agent["name"], "phone": agent["phone"]} for agent in active_agents(get_settings())
            ]
            targets = [
                {**target, "status": str(by_number[target["phone"]].status), "call_sid": str(by_number[target["phone"]].sid)}
                if target["phone"] in by_number else {**target, "status": "not-started"}
                for target in targets
            ]
            leg["transfer_targets"] = targets
        except Exception as error:
            logger.warning("call forwarding: could not fetch team call legs for %s: %s", call_sid, error)
    if call and call.get("transfer_status") in {"connected", "completed"}:
        _record(call_sid, {"transfer_status": "completed", **leg})
        return True
    if call and call.get("transfer_mode") == MODE and status == "completed":
        name = "Human team"
        if dial_call_sid:
            try:
                from routers.twilio_api import _get_twilio_client
                client = _get_twilio_client()
                answered_number = str(client.calls(dial_call_sid).fetch().to or "") if client else ""
                name = next((agent["name"] for agent in get_settings()["agents"] if agent["phone"] == answered_number), name)
            except Exception as error:
                logger.warning("call forwarding: could not identify answering agent for %s: %s", call_sid, error)
        _record(call_sid, {"transfer_status": "completed", "transfer_destination": name, "handled_by": name, **leg,
                           "accepted_by": name, "transfer_succeeded": True, "status": "transferred", "transfer_error": None})
        _timeline(call_sid, caller, f"Connected to {name}")
        logger.info("call forwarding: %s connected to %s", call_sid, name)
        return True
    _record(call_sid, {"transfer_status": "no-answer" if status in {"", "completed", "answered"} else status, "transfer_succeeded": False, **leg,
                       "transfer_destination": "Callback queue", "human_callback_requested": True, "status": "transfer_requested",
                       "transfer_error": f"No team member accepted ({status or 'no-answer'})"})
    _timeline(call_sid, caller, "No team member was free: the caller was told the team will call back")
    try:
        from notification_service import notify_human_assignment
        number = caller or (call or {}).get("phone_number") or ""
        notify_human_assignment(channel="voice", customer=number, recipient="Human team", call_session_id=call_sid,
                                message=f"{number or 'A caller'} asked for a person but nobody was free: please call them back.")
    except Exception as error:
        logger.warning("call forwarding: could not notify the team about %s: %s", call_sid, error)
    logger.info("call forwarding: %s not accepted (%s); callback requested", call_sid, status)
    return False


def _find_call(call_sid: str) -> Optional[Dict[str, Any]]:
    """The stored call for a Twilio call: custom calls are stored under it, Retell calls under Retell's id with the
    Twilio one saved when the transfer started (twilio_call_sid)."""
    if not mongo_client.is_connected() or not call_sid:
        return None
    found = mongo_client.calls.find_one({"$or": [{"call_session_id": call_sid}, {"twilio_call_sid": call_sid}]})
    if found:
        return found
    retell_id = next((key for key, value in _RETELL_CALLS.items() if value.get("call_sid") == call_sid), "")
    return mongo_client.calls.find_one({"call_session_id": retell_id}) if retell_id else None


def _record(call_sid: str, fields: Dict[str, Any], session_id: str = "") -> None:
    if not mongo_client.is_connected() or not call_sid:
        return
    found = _find_call(call_sid)
    where = {"call_session_id": session_id} if session_id else ({"_id": found["_id"]} if found.get("_id") else {"call_session_id": found["call_session_id"]}) if found else None
    if where is None:
        return
    mongo_client.calls.update_one(where, {"$set": {**fields, "updated_at": datetime.utcnow()}})
    try:
        import realtime
        realtime.publish("call", key=call_sid)
    except Exception:
        pass


def _timeline(call_sid: str, caller: str, text: str) -> None:
    """A line on the caller's conversation timeline (the inbox shows it between the calls)."""
    if not mongo_client.is_connected():
        return
    try:
        from routers import inbox_api
        call = _find_call(call_sid) or ({"_id": call_sid, "phone_number": caller} if caller else None)
        if not call:
            return
        key = inbox_api._thread_key(call)
        mongo_client.inbox_threads.update_one({"_id": key}, {"$push": {"events": {"type": "transfer", "text": text, "by_name": "AI assistant",
                                                                                  "at": datetime.utcnow()}}}, upsert=True)
    except Exception as error:
        logger.debug("call forwarding: timeline note skipped: %s", error)
