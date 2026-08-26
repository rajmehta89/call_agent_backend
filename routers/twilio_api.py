import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from twilio.rest import Client

from ai_services import AIServices
from agent_config import agent_config
from env_loader import load_project_env
from qa_engine import RealEstateQA
from routers.calls_api import log_call, update_call_from_twilio
from brain_service import brain_service
from transfer_service import transfer_service
from notification_service import notify_recipients

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
OUTBOUND_CALL_CONTEXT: Dict[str, Dict[str, Any]] = {}


def _get_ws_base_url() -> str:
    base = TWILIO_PUBLIC_BASE_URL.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def _get_http_base_url() -> str:
    return TWILIO_PUBLIC_BASE_URL.rstrip("/")


def _build_conversation_twiml() -> str:
    ws_url = f"{_get_ws_base_url()}/api/twilio/ws"
    greeting = agent_config.get_greeting_message().replace("&", "&amp;").replace('"', "&quot;")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<ConversationRelay url="{ws_url}" welcomeGreeting="{greeting}" language="{TWILIO_CONVERSATION_LANG}" />'
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
    return {
        "name": str(agent.get("name", "")).strip(),
        "phone_number": str(agent.get("phone_number", "")).strip(),
        "enabled": bool(agent.get("enabled", True)),
        "aliases": [str(item).strip().lower() for item in aliases if str(item).strip()],
    }


def _get_transfer_settings() -> Dict[str, Any]:
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


def create_outbound_call(customer_number: str, lead_id: Optional[str] = None, lead_name: Optional[str] = None) -> Dict[str, Any]:
    client = _get_twilio_client()
    if not client:
        return {"error": "Twilio credentials are not configured"}
    if not TWILIO_CALLER_ID:
        return {"error": "TWILIO_CALLER_ID is not configured"}

    status_callback = f"{_get_http_base_url()}/api/twilio/status"
    outbound_twiml = _build_outbound_conversation_twiml()

    try:
        outbound_call = client.calls.create(
            to=customer_number,
            from_=TWILIO_CALLER_ID,
            twiml=outbound_twiml,
            status_callback=status_callback,
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )
        print(
            f"Twilio outbound call created for {customer_number} "
            f"using ConversationRelay websocket {_get_ws_base_url()}/api/twilio/ws"
        )
        OUTBOUND_CALL_CONTEXT[outbound_call.sid] = {
            "phone_number": customer_number,
            "lead_id": lead_id,
            "lead_name": lead_name,
            "direction": "outbound",
            "created_at": datetime.now().isoformat(),
        }
        transfer_service.register_call(outbound_call.sid, customer_number, lead_id, direction="outbound")
        return {
            "status": "initiated",
            "provider": "twilio",
            "call_sid": outbound_call.sid,
            "session_id": outbound_call.sid,
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_outbound_call_context(call_sid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not call_sid:
        return None
    return OUTBOUND_CALL_CONTEXT.get(call_sid)


async def _send_text(websocket: WebSocket, text: str, last: bool = True):
    await websocket.send_text(json.dumps({
        "type": "text",
        "token": text,
        "last": last,
        "interruptible": True,
        "preemptible": True,
    }))


def _serialize_messages(messages: List[Dict[str, Any]], speaker: str) -> List[Dict[str, str]]:
    return [
        {
            "type": speaker,
            "content": item["content"],
            "timestamp": item["timestamp"],
        }
        for item in messages
    ]


@router.post("/inbound", response_class=PlainTextResponse)
async def inbound_call():
    return PlainTextResponse(_build_conversation_twiml(), media_type="text/xml")


@router.post("/outbound/connect", response_class=PlainTextResponse)
async def outbound_connect():
    return PlainTextResponse(_build_outbound_conversation_twiml(), media_type="text/xml")


@router.post("/status")
async def status_callback(request: Request):
    payload = dict(await request.form())
    print(f"Twilio status callback: {payload}")
    update_call_from_twilio(payload.get("CallSid"), call_status=payload.get("CallStatus"))
    return {"success": True}


@router.post("/transfer-status")
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
    print(f"Twilio human transfer callback: {payload}")
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
    bot = RealEstateQA(ai_services)
    history: List[Dict[str, str]] = []
    user_transcript: List[Dict[str, str]] = []
    ai_responses: List[Dict[str, str]] = []
    session: Dict[str, Any] = {
        "session_id": None,
        "call_sid": None,
        "phone_number": "unknown",
        "direction": "inbound",
        "lead_id": None,
        "started_at": datetime.now(),
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
                session["session_id"] = data.get("sessionId") or data.get("callSid")
                session["call_sid"] = data.get("callSid")
                session["phone_number"] = data.get("from") or "unknown"
                session["direction"] = data.get("direction") or "inbound"
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

            timestamp = datetime.now().isoformat()
            user_transcript.append({"content": caller_text, "timestamp": timestamp})
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
                    "A human agent request has been created. Please hold while we arrange a callback."
                )
                ai_responses.append({"content": handoff_text, "timestamp": datetime.now().isoformat()})
                transfer_service.append_message(session["session_id"], "assistant", handoff_text)
                await _send_text(websocket, handoff_text)

                client = _get_twilio_client()
                transfer_settings = _get_transfer_settings()
                active_agents = [agent for agent in transfer_settings.get("agents", []) if agent.get("enabled")]
                dedicated_agent = _find_dedicated_agent(caller_text, active_agents)
                transfer_destination = None

                if dedicated_agent:
                    transfer_destination = dedicated_agent.get("name") or "Human agent"
                    transfer_service.append_message(
                        session["session_id"],
                        "system",
                        f"Dedicated human agent matched: {dedicated_agent['name']}",
                    )
                    transfer_twiml = _build_team_transfer_twiml([dedicated_agent])
                elif active_agents:
                    transfer_destination = "Human team"
                    transfer_service.append_message(
                        session["session_id"],
                        "system",
                        f"Routing to human team: {', '.join(agent['name'] for agent in active_agents if agent.get('name'))}",
                    )
                    transfer_twiml = _build_team_transfer_twiml(active_agents)
                elif TWILIO_HUMAN_AGENT_NUMBER:
                    transfer_destination = "Human agent"
                    transfer_twiml = _build_transfer_twiml()
                else:
                    transfer_twiml = None

                session["transfer_destination"] = transfer_destination
                notification_recipients = (
                    [dedicated_agent.get("name") or "Human agent"]
                    if dedicated_agent else
                    [agent.get("name") or "Human team" for agent in active_agents]
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
                        print(f"Twilio transfer failed: {exc}")
                        session["transfer_status"] = "failed"
                        session["transfer_error"] = str(exc)
                        transfer_service.update_transfer(session["session_id"], "failed", transfer_destination, str(exc))
                elif transfer_twiml:
                    session["transfer_status"] = "unconfigured"
                    session["transfer_error"] = "Twilio credentials or call SID are not available"
                    transfer_service.update_transfer(session["session_id"], "unconfigured", transfer_destination, session["transfer_error"])
                else:
                    session["transfer_status"] = "unconfigured"
                    session["transfer_error"] = "No enabled human transfer number is configured"
                    transfer_service.update_transfer(session["session_id"], "unconfigured", transfer_destination, session["transfer_error"])
                continue

            if bot.is_exit_intent(caller_text):
                exit_text = bot.get_exit_message()
                ai_responses.append({"content": exit_text, "timestamp": datetime.now().isoformat()})
                transfer_service.append_message(session["session_id"], "assistant", exit_text)
                await _send_text(websocket, exit_text)
                break

            reply = brain_service.respond(
                caller_text,
                history,
                channel="voice",
                customer_phone=session.get("phone_number"),
            ) or "I'm sorry, the AI service is not available right now."
            history.extend([
                {"role": "user", "content": caller_text},
                {"role": "assistant", "content": reply},
            ])
            history = history[-8:]
            ai_responses.append({"content": reply, "timestamp": datetime.now().isoformat()})
            transfer_service.append_message(session["session_id"], "assistant", reply)
            await _send_text(websocket, reply)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"Twilio ConversationRelay websocket error: {exc}")
    finally:
        handoff = transfer_service.get_call(session.get("session_id", "")) or {}
        transfer_service.close_call(session.get("session_id"))
        duration = max((datetime.now() - session["started_at"]).total_seconds(), 0)
        if session.get("phone_number") and (user_transcript or ai_responses):
            transfer_requested = bool(session.get("transfer_attempted") or handoff.get("transfer_requested"))
            transfer_status = handoff.get("transfer_status") or session.get("transfer_status") or handoff.get("status")
            if transfer_status == "awaiting_human":
                transfer_status = "requested"
            if transfer_requested:
                if transfer_status in {"connected", "completed", "accepted"}:
                    call_status = "transferred"
                elif transfer_status in {"failed", "busy", "no-answer", "canceled", "disabled", "unconfigured"}:
                    call_status = "failed"
                else:
                    call_status = "transfer_requested"
                destination = session.get("transfer_destination") or handoff.get("transfer_destination") or "Human team"
                summary = (
                    f"Human handoff to {destination} ({transfer_status}). "
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
