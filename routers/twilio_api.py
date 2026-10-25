import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime
from timeutil import utc_iso, utc_now
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.rest import Client

from ai_services import AIServices
from agent_config import agent_config
from auth_service import require_permission, require_user
from logging_setup import bind_call
from twilio_security import stream_token, valid_stream_token, verify_twilio_request
from env_loader import load_project_env
from qa_engine import DynamicQA
from routers.calls_api import log_call, record_failed_call, update_call_from_twilio, upsert_browser_lead
from brain_service import brain_service
from transfer_service import transfer_service
from notification_service import notify_recipients
from conversation_memory import ConversationMemory
from voice_languages import effective_language, language_config, language_for_call, language_greeting, language_enabled, normalize_language
import call_forwarding

load_project_env()

router = APIRouter(prefix="/api/twilio", tags=["twilio"])

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_CALLER_ID = os.getenv("TWILIO_CALLER_ID") or os.getenv("CALLER_ID")
TWILIO_HUMAN_AGENT_NUMBER = os.getenv("TWILIO_HUMAN_AGENT_NUMBER") or os.getenv("AGENT_NUMBER")
TWILIO_PUBLIC_BASE_URL = (
    os.getenv("TWILIO_PUBLIC_BASE_URL")
    or os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "http://localhost:8000"
)
TWILIO_CONVERSATION_LANG = os.getenv("TWILIO_CONVERSATION_LANG", "en-US")
TWILIO_BROWSER_API_KEY_SID = os.getenv("TWILIO_BROWSER_API_KEY_SID")
TWILIO_BROWSER_API_KEY_SECRET = os.getenv("TWILIO_BROWSER_API_KEY_SECRET")
TWILIO_BROWSER_TWIML_APP_SID = os.getenv("TWILIO_BROWSER_TWIML_APP_SID")
TWILIO_DEFAULT_COUNTRY_CODE = re.sub(
    r"\D", "", os.getenv("TWILIO_DEFAULT_COUNTRY_CODE", "91")
) or "91"
OUTBOUND_CALL_CONTEXT: Dict[str, Dict[str, Any]] = {}


def _get_ws_base_url(base_url: Optional[str] = None) -> str:
    base = (base_url or TWILIO_PUBLIC_BASE_URL).rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def _get_http_base_url(base_url: Optional[str] = None) -> str:
    return (base_url or TWILIO_PUBLIC_BASE_URL).rstrip("/")


def _get_request_base_url(request: Request) -> str:
    """Use the public proxy host for TwiML when running behind a tunnel."""
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    forwarded_proto = request.headers.get("x-forwarded-proto") or "https"
    if forwarded_host:
        return f"{forwarded_proto.split(',')[0].strip()}://{forwarded_host.split(',')[0].strip()}"
    return TWILIO_PUBLIC_BASE_URL


RELAY_TOKEN_SUBJECT = "conversation-relay"


def _build_conversation_twiml(public_base_url: Optional[str] = None, language: str = "en") -> str:
    ws_url = f"{_get_ws_base_url(public_base_url)}/api/twilio/ws"
    selected = language_config(language)
    greeting = html.escape(language_greeting(selected["code"], agent_config.get_greeting_message()), quote=True)
    language_nodes = "".join(
        f'<Language code="{item["locale"]}" ttsProvider="ElevenLabs" transcriptionProvider="Deepgram" />'
        for item in (language_config("en"), language_config("ar"), language_config("ur"))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<ConversationRelay url="{ws_url}" welcomeGreeting="{greeting}" '
        f'ttsProvider="ElevenLabs" transcriptionProvider="Deepgram" ttsLanguage="multi" transcriptionLanguage="multi">{language_nodes}'
        f'<Parameter name="preferred_language" value="{selected["code"]}" />'
        f'<Parameter name="stream_token" value="{stream_token(RELAY_TOKEN_SUBJECT)}" />'
        "</ConversationRelay>"
        "</Connect>"
        "</Response>"
    )


def _build_outbound_conversation_twiml() -> str:
    return _build_conversation_twiml()


def _build_transfer_twiml() -> str:
    target = (TWILIO_HUMAN_AGENT_NUMBER or "").strip()
    if not target:
        raise RuntimeError("TWILIO_HUMAN_AGENT_NUMBER is not configured")
    return _build_team_transfer_twiml([{"phone_number": target}])


def _normalize_agent(agent: Dict[str, Any]) -> Dict[str, Any]:
    aliases = agent.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [item.strip() for item in aliases.split(",") if item.strip()]
    preferred_languages = []
    for value in agent.get("preferred_languages") or agent.get("languages") or []:
        code = normalize_language(value)
        if str(value or "").strip().lower() in {"en", "ar", "ur"} and code not in preferred_languages:
            preferred_languages.append(code)
    return {
        "name": str(agent.get("name", "")).strip(),
        "phone_number": str(agent.get("phone_number", "")).strip(),
        "enabled": bool(agent.get("enabled", True)),
        "aliases": [str(item).strip().lower() for item in aliases if str(item).strip()],
        "preferred_languages": preferred_languages,
    }


def _get_transfer_settings() -> Dict[str, Any]:
    # The dedicated Call forwarding screen is the source of truth once it has
    # been saved. Keep the older Voice Agent config as a backwards-compatible
    # fallback for workspaces that have never opened that screen.
    if call_forwarding.has_saved_settings():
        forwarding = call_forwarding.get_settings()
        return {
            "enabled": forwarding.get("enabled", False),
            "agents": [
                {
                    "name": agent.get("name", ""),
                    "phone_number": agent.get("phone", ""),
                    "enabled": agent.get("enabled", True),
                    "aliases": agent.get("aliases", []),
                    "preferred_languages": agent.get("preferred_languages", []),
                }
                for agent in forwarding.get("agents", [])
            ],
        }
    channel_settings = brain_service.channel_config("voice")
    settings = channel_settings.get("human_transfer") or agent_config.get_human_transfer()
    if not isinstance(settings, dict):
        settings = agent_config.get_human_transfer()
    agents = [_normalize_agent(agent) for agent in settings.get("agents", []) if isinstance(agent, dict)]
    settings["agents"] = [agent for agent in agents if agent["phone_number"]]
    return settings


def _find_dedicated_agent(caller_text: str, agents: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    normalized_text = re.sub(r"[^a-z0-9\s]", " ", caller_text.lower())
    for agent in agents:
        if not agent.get("enabled"):
            continue
        candidate_terms = [agent["name"].lower(), *agent.get("aliases", [])]
        for term in candidate_terms:
            if term and re.search(rf"\b{re.escape(term)}\b", normalized_text):
                return agent
    return None


def _build_team_transfer_twiml(agents: List[Dict[str, Any]]) -> str:
    active_agents = [agent for agent in agents if agent.get("enabled", True) and agent.get("phone_number")]
    if not active_agents:
        raise RuntimeError("No enabled human agents are configured")

    number_nodes = "".join(
        f'<Number>{agent["phone_number"]}</Number>'
        for agent in active_agents
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial answerOnBridge="true" timeout="20" action="{_get_http_base_url()}/api/twilio/transfer-status" method="POST">{number_nodes}</Dial>'
        "</Response>"
    )


def _get_twilio_client() -> Client | None:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return None
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def _normalize_twilio_phone(phone_number: str) -> str:
    """Return a phone number in E.164 format for Twilio."""
    raw = str(phone_number or "").strip()
    if not raw:
        raise ValueError("Lead phone number is empty")

    compact = re.sub(r"[^\d+]", "", raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]

    digits = re.sub(r"\D", "", compact)
    if not compact.startswith("+"):
        if len(digits) == 10:
            digits = f"{TWILIO_DEFAULT_COUNTRY_CODE}{digits}"
        elif len(digits) == 11 and digits.startswith("0"):
            digits = f"{TWILIO_DEFAULT_COUNTRY_CODE}{digits[1:]}"

    if not 8 <= len(digits) <= 15:
        raise ValueError(
            "Lead phone number must include a valid country code, for example +919979172234"
        )
    return f"+{digits}"


def create_outbound_call(customer_number: str, lead_id: Optional[str] = None, lead_name: Optional[str] = None) -> Dict[str, Any]:
    return {"error": "Outbound calling is disabled in this inbound-only project"}


def get_outbound_call_context(call_sid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not call_sid:
        return None
    return OUTBOUND_CALL_CONTEXT.get(call_sid)


@router.get("/config", dependencies=[Depends(require_user)])
async def twilio_config() -> Dict[str, Any]:
    """Safe local setup diagnostics; never return Twilio credentials."""
    public_base_url = _get_http_base_url()
    return {
        "success": True,
        "data": {
            "provider": "twilio",
            "account_configured": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
            "voice_number_configured": bool(TWILIO_CALLER_ID),
            "human_transfer_configured": bool(TWILIO_HUMAN_AGENT_NUMBER),
            "public_base_url": public_base_url,
            "inbound_webhook": f"{public_base_url}/api/twilio/inbound",
            "status_webhook": f"{public_base_url}/api/twilio/status",
            "websocket_url": f"{_get_ws_base_url()}/api/twilio/ws",
            "conversation_relay_ready": bool(
                TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_CALLER_ID
                and public_base_url.startswith("https://")
            ),
        },
    }


async def _send_text(websocket: WebSocket, text: str, last: bool = True, language: str = "en"):
    locale = language_config(language)["locale"]
    await websocket.send_text(json.dumps({
        "type": "text",
        "token": text,
        "last": last,
        "lang": locale,
        "interruptible": True,
        "preemptible": True,
    }))


def _serialize_messages(messages: List[Dict[str, Any]], speaker: str) -> List[Dict[str, str]]:
    return [
        {
            "type": speaker,
            "content": item["content"],
            "timestamp": item["timestamp"],
            **({"language": item["language"]} if item.get("language") else {}),
        }
        for item in messages
    ]


async def _routed_twiml(request: Request) -> PlainTextResponse:
    """Follow the voice provider switch: custom stack when it is on, otherwise this default agent."""
    # Imported here because the custom voice router imports this module.
    from routers.custom_twilio_voice_api import custom_inbound
    return await custom_inbound(request)


@router.post("/inbound", response_class=PlainTextResponse, dependencies=[Depends(verify_twilio_request)])
async def inbound_call(request: Request):
    return await _routed_twiml(request)


@router.post("/browser-connect", response_class=PlainTextResponse, dependencies=[Depends(verify_twilio_request)])
async def browser_connect(request: Request):
    """Voice SDK TwiML entry point for a browser-only inbound-call test."""
    return await _routed_twiml(request)


logger = logging.getLogger(__name__)
BROWSER_TOKEN_TTL_SECONDS = 3600
_clock_offset = {"seconds": 0.0, "checked_at": 0.0}


async def _twilio_clock_offset() -> float:
    """Seconds to add to this machine's clock to match Twilio's.

    Access tokens carry absolute timestamps, so a PC clock that is a few minutes off makes Twilio
    reject them (error 20104). Twilio's Date header gives the true time; re-check every 30 minutes."""
    if time.time() - _clock_offset["checked_at"] < 1800:
        return _clock_offset["seconds"]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            sent = time.time()
            response = await client.head("https://api.twilio.com")
            local_mid = (sent + time.time()) / 2
        offset = parsedate_to_datetime(response.headers["date"]).timestamp() - local_mid
        _clock_offset.update(seconds=offset, checked_at=time.time())
        if abs(offset) > 30:
            logger.warning("this computer's clock is off by %.0fs from Twilio; correcting browser tokens. Sync the Windows clock to fix it permanently.", -offset)
    except Exception as error:
        logger.warning("could not read Twilio time, using the local clock: %s", error)
    return _clock_offset["seconds"]


@router.get("/browser-token")
async def browser_token(request: Request, user: Dict[str, Any] = Depends(require_permission("handle_calls"))):
    """Issue a short-lived Voice SDK token without exposing Twilio credentials."""
    missing = [
        name for name, value in {
            "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
            "TWILIO_BROWSER_API_KEY_SID": TWILIO_BROWSER_API_KEY_SID,
            "TWILIO_BROWSER_API_KEY_SECRET": TWILIO_BROWSER_API_KEY_SECRET,
            "TWILIO_BROWSER_TWIML_APP_SID": TWILIO_BROWSER_TWIML_APP_SID,
        }.items() if not value
    ]
    if missing:
        raise HTTPException(status_code=503, detail=f"Browser calling is not configured: {', '.join(missing)}")

    user_id = str(user.get("_id", "user"))[-12:]
    identity = f"browser_{user_id}_{secrets.token_hex(4)}"
    await asyncio.to_thread(
        upsert_browser_lead,
        f"client:{identity}",
        request.query_params.get("caller_name", ""),
        request.query_params.get("caller_email", ""),
        request.query_params.get("caller_phone", ""),
        request.query_params.get("caller_language", "en"),
    )
    twilio_now = int(time.time() + await _twilio_clock_offset())
    token = AccessToken(
        TWILIO_ACCOUNT_SID,
        TWILIO_BROWSER_API_KEY_SID,
        TWILIO_BROWSER_API_KEY_SECRET,
        identity=identity,
        nbf=twilio_now - 30,
        valid_until=twilio_now + BROWSER_TOKEN_TTL_SECONDS,
    )
    token.add_grant(VoiceGrant(outgoing_application_sid=TWILIO_BROWSER_TWIML_APP_SID))
    return {"success": True, "data": {"token": token.to_jwt(), "expires_in": BROWSER_TOKEN_TTL_SECONDS}}


@router.post("/outbound/connect", response_class=PlainTextResponse, dependencies=[Depends(verify_twilio_request)])
async def outbound_connect(request: Request):
    return PlainTextResponse("Outbound calling is disabled", status_code=410)


@router.post("/status", dependencies=[Depends(verify_twilio_request)])
async def status_callback(request: Request):
    payload = dict(await request.form())
    bind_call(payload.get("CallSid"))
    logger.info("Twilio call status: %s (duration %ss)", payload.get("CallStatus"), payload.get("CallDuration", "-"))
    updated = update_call_from_twilio(payload.get("CallSid"), call_status=payload.get("CallStatus"), call_duration=payload.get("CallDuration"))
    if not updated:
        # Busy / no answer / failed calls never reached a voice agent, so no record exists yet: create one.
        record_failed_call(payload.get("CallSid"), payload.get("From") or payload.get("Caller") or "unknown", payload.get("CallStatus"),
                           direction=payload.get("Direction") or "inbound", error_code=payload.get("ErrorCode"))
    return {"success": True}


@router.post("/transfer-status", dependencies=[Depends(verify_twilio_request)])
async def transfer_status_callback(request: Request):
    """Persist the result of the human leg created by <Dial>."""
    payload = dict(await request.form())
    parent_sid = payload.get("CallSid")
    dial_status = str(payload.get("DialCallStatus") or "").lower()
    status_map = {
        "answered": "connected",
        "completed": "completed",
        "busy": "busy",
        "no-answer": "no-answer",
        "failed": "failed",
        "canceled": "canceled",
    }
    transfer_status = status_map.get(dial_status, dial_status or "failed")
    logger.info("Twilio human transfer status: %s", payload.get("DialCallStatus"))
    record = transfer_service.update_transfer(parent_sid, transfer_status)
    update_call_from_twilio(
        parent_sid,
        transfer_status=transfer_status,
        transfer_error=(f"Human leg ended with {dial_status}" if transfer_status in {"busy", "no-answer", "failed", "canceled"} else None),
    )
    return {"success": True, "transfer_status": transfer_status, "tracked": bool(record)}


@router.websocket("/ws")
async def twilio_conversation_ws(websocket: WebSocket):
    await websocket.accept()

    ai_services = AIServices()
    bot = DynamicQA(ai_services)
    memory = ConversationMemory()
    # ConversationRelay plays this greeting itself; record it so "yes"/"okay" replies have context.
    memory.add_assistant(agent_config.get_greeting_message())
    history: List[Dict[str, str]] = []
    user_transcript: List[Dict[str, str]] = []
    ai_responses: List[Dict[str, str]] = []
    session: Dict[str, Any] = {
        "session_id": None,
        "call_sid": None,
        "phone_number": "unknown",
        "direction": "inbound",
        "lead_id": None,
        "preferred_language": "en",
        "started_at": utc_now(),
        "transfer_attempted": False,
        "transfer_succeeded": False,
        "transfer_status": None,
        "transfer_destination": None,
        "transfer_error": None,
    }

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            message_type = data.get("type")

            if message_type == "setup":
                if not valid_stream_token(RELAY_TOKEN_SUBJECT, str((data.get("customParameters") or {}).get("stream_token") or "")):
                    logger.warning("closing ConversationRelay socket without a valid stream token")
                    await websocket.close(code=1008)
                    return
                session["session_id"] = data.get("sessionId") or data.get("callSid")
                session["call_sid"] = data.get("callSid")
                bind_call(session["call_sid"])
                session["phone_number"] = data.get("from") or "unknown"
                session["direction"] = data.get("direction") or "inbound"
                custom_parameters = data.get("customParameters") or {}
                session["preferred_language"] = normalize_language(
                    custom_parameters.get("preferred_language") or language_for_call({"From": session["phone_number"]})
                )
                outbound_context = get_outbound_call_context(session["call_sid"])
                if outbound_context:
                    session["phone_number"] = outbound_context.get("phone_number") or session["phone_number"]
                    session["lead_id"] = outbound_context.get("lead_id")
                    session["direction"] = "outbound"
                transfer_service.register_call(
                    session["session_id"],
                    session["phone_number"],
                    session.get("lead_id"),
                    session["direction"],
                )
                continue

            if message_type == "interrupt":
                transfer_service.append_message(
                    session["session_id"],
                    "system",
                    f"Caller interrupted agent playback after {data.get('durationUntilInterruptMs', 0)} ms",
                )
                continue

            if message_type != "prompt" or not data.get("last", True):
                continue

            caller_text = (data.get("voicePrompt") or "").strip()
            if not caller_text:
                continue

            detected_language = str(data.get("lang") or "").strip()
            if detected_language:
                detected_code = normalize_language(detected_language)
                if language_enabled(detected_code):
                    session["preferred_language"] = effective_language(detected_code)
                await websocket.send_text(json.dumps({
                    "type": "language",
                    "ttsLanguage": language_config(session["preferred_language"])["locale"],
                    "transcriptionLanguage": "multi",
                }))

            timestamp = utc_iso()
            user_transcript.append({"content": caller_text, "timestamp": timestamp, "language": session.get("preferred_language", "en")})
            transfer_service.append_message(session["session_id"], "user", caller_text)

            transfer_intent = bot.analyze_transfer_intent(caller_text, history)
            if transfer_intent.get("should_transfer"):
                session["transfer_attempted"] = True
                transfer_service.request_transfer(
                    session["session_id"],
                    transfer_intent.get("reason", "Human handoff requested"),
                    transfer_intent.get("mode", "intent_analyzer"),
                    transfer_intent.get("confidence", 0.75),
                    phone_number=session["phone_number"],
                    lead_id=session.get("lead_id"),
                )

                handoff_text = (
                    "I am transferring you to a human agent now. They will have the context from this call."
                    if TWILIO_HUMAN_AGENT_NUMBER else
                    "I have recorded your request for a human follow-up. Our team will call you back using the number associated with this call."
                )
                ai_responses.append({"content": handoff_text, "timestamp": utc_iso(), "language": session.get("preferred_language", "en")})
                transfer_service.append_message(session["session_id"], "assistant", handoff_text)
                await _send_text(websocket, handoff_text, language=session.get("preferred_language", "en"))

                client = _get_twilio_client()
                transfer_settings = _get_transfer_settings()
                active_agents = [agent for agent in transfer_settings.get("agents", []) if agent.get("enabled")]
                dedicated_agent = _find_dedicated_agent(caller_text, active_agents)
                transfer_destination = None

                if dedicated_agent:
                    call_forwarding.assign_lead_to_requested_agent(
                        session.get("phone_number") or "",
                        caller_text,
                        transfer_settings,
                    )
                    transfer_destination = dedicated_agent.get("name") or "Human agent"
                    transfer_service.append_message(
                        session["session_id"],
                        "system",
                        f"Dedicated human agent matched: {dedicated_agent['name']}",
                    )
                    transfer_twiml = _build_team_transfer_twiml([dedicated_agent])
                elif active_agents:
                    routed_agents = call_forwarding.language_matched_agents(active_agents, session.get("preferred_language", "en"))
                    transfer_destination = "Human team"
                    transfer_service.append_message(
                        session["session_id"],
                        "system",
                        f"Routing to {session.get('preferred_language', 'en')} team: {', '.join(agent['name'] for agent in routed_agents if agent.get('name'))}",
                    )
                    transfer_twiml = _build_team_transfer_twiml(routed_agents)
                elif TWILIO_HUMAN_AGENT_NUMBER:
                    transfer_destination = "Human agent"
                    transfer_twiml = _build_transfer_twiml()
                else:
                    transfer_twiml = None

                session["transfer_destination"] = transfer_destination
                notification_recipients = (
                    [dedicated_agent.get("name") or "Human agent"]
                    if dedicated_agent else
                    [agent.get("name") or "Human team" for agent in (routed_agents if active_agents else [])]
                    if active_agents else
                    ["Human team"]
                )
                notify_recipients(
                    channel="voice",
                    customer=session.get("phone_number") or "caller",
                    recipients=notification_recipients,
                    call_session_id=session.get("session_id"),
                    message=f"A voice caller is waiting for human assistance ({transfer_intent.get('reason', 'handoff requested')}).",
                )
                if not transfer_settings.get("enabled", True):
                    session["transfer_status"] = "disabled"
                    session["transfer_error"] = "Human transfer is disabled in the voice agent settings"
                    transfer_service.update_transfer(session["session_id"], "disabled", transfer_destination, session["transfer_error"])
                elif client and session.get("call_sid") and transfer_twiml:
                    try:
                        client.calls(session["call_sid"]).update(twiml=transfer_twiml)
                        session["transfer_succeeded"] = True
                        session["transfer_status"] = "dialing"
                        transfer_service.update_transfer(session["session_id"], "dialing", transfer_destination)
                    except Exception as exc:
                        logger.error(f"Twilio transfer failed: {exc}")
                        session["transfer_status"] = "failed"
                        session["transfer_error"] = str(exc)
                        transfer_service.update_transfer(session["session_id"], "failed", transfer_destination, str(exc))
                elif transfer_twiml:
                    session["transfer_status"] = "unconfigured"
                    session["transfer_error"] = "Twilio credentials or call SID are not available"
                    transfer_service.update_transfer(session["session_id"], "unconfigured", transfer_destination, session["transfer_error"])
                else:
                    session["transfer_status"] = "requested"
                    session["transfer_error"] = "No enabled human transfer number is configured; callback request recorded"
                    transfer_service.update_transfer(session["session_id"], "requested", transfer_destination, session["transfer_error"])
                continue

            if bot.is_exit_intent(caller_text):
                exit_text = bot.get_exit_message()
                ai_responses.append({"content": exit_text, "timestamp": utc_iso(), "language": session.get("preferred_language", "en")})
                transfer_service.append_message(session["session_id"], "assistant", exit_text)
                await _send_text(websocket, exit_text, language=session.get("preferred_language", "en"))
                break

            reply = brain_service.respond(
                caller_text,
                memory.history(),
                channel="voice",
                customer_phone=session.get("phone_number"),
                summary=memory.summary,
                    language=session.get("preferred_language"),
            ) or "I'm sorry, the AI service is not available right now."
            memory.add(caller_text, reply)
            history = memory.history()
            ai_responses.append({"content": reply, "timestamp": utc_iso(), "language": session.get("preferred_language", "en")})
            transfer_service.append_message(session["session_id"], "assistant", reply)
            await _send_text(websocket, reply, language=session.get("preferred_language", "en"))

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error(f"Twilio ConversationRelay websocket error: {exc}")
    finally:
        handoff = transfer_service.get_call(session.get("session_id", "")) or {}
        transfer_service.close_call(session.get("session_id"))
        duration = max((utc_now() - session["started_at"]).total_seconds(), 0)
        if session.get("phone_number") and (user_transcript or ai_responses):
            transfer_requested = bool(session.get("transfer_attempted") or handoff.get("transfer_requested"))
            transfer_status = handoff.get("transfer_status") or session.get("transfer_status") or handoff.get("status")
            if transfer_status == "awaiting_human":
                transfer_status = "requested"
            if transfer_requested:
                if transfer_status in {"connected", "completed", "accepted"}:
                    call_status = "transferred"
                elif transfer_status in {"failed", "busy", "no-answer", "canceled", "disabled"}:
                    call_status = "failed"
                else:
                    call_status = "transfer_requested"
                destination = session.get("transfer_destination") or handoff.get("transfer_destination") or "Human team"
                summary = (
                    f"Human follow-up requested for {destination} ({transfer_status}). "
                    f"Twilio {session.get('direction', 'inbound')} call with {len(user_transcript)} caller turns."
                )
            else:
                call_status = "completed"
                destination = None
                summary = f"Twilio {session.get('direction', 'inbound')} call with {len(user_transcript)} caller turns"
            log_call(
                session["phone_number"],
                session.get("lead_id"),
                {
                    "direction": session.get("direction", "inbound"),
                    "status": call_status,
                    "duration": duration,
                    "transcription": _serialize_messages(user_transcript, "user"),
                    "ai_responses": _serialize_messages(ai_responses, "bot"),
                    "summary": summary,
                    "preferred_language": session.get("preferred_language", "en"),
                    "languages_used": sorted({item.get("language", "en") for item in [*user_transcript, *ai_responses]}),
                    "summary_language": "en",
                    "sentiment": "neutral",
                    "transfer_requested": transfer_requested,
                    "transfer_status": transfer_status,
                    "transfer_destination": destination,
                    "transfer_error": session.get("transfer_error") or handoff.get("transfer_error"),
                    "transfer_succeeded": bool(session.get("transfer_succeeded") or transfer_status in {"connected", "completed", "accepted"}),
                    "accepted_by": handoff.get("accepted_by"),
                    "handled_by": handoff.get("accepted_by") or destination,
                    "transfer_reason": handoff.get("transfer_reason"),
                    "transfer_mode": handoff.get("transfer_mode"),
                    "call_session_id": session.get("session_id"),
                },
            )
        if session.get("call_sid"):
            OUTBOUND_CALL_CONTEXT.pop(session["call_sid"], None)
