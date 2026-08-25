import json
import os
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from twilio.rest import Client

from ai_services import AIServices
from agent_config import agent_config
from env_loader import load_project_env
from qa_engine import RealEstateQA
from routers.calls_api import log_call
from transfer_service import transfer_service

load_project_env()

router = APIRouter(prefix="/api/twilio", tags=["twilio"])

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_HUMAN_AGENT_NUMBER = os.getenv("TWILIO_HUMAN_AGENT_NUMBER") or os.getenv("AGENT_NUMBER")
TWILIO_PUBLIC_BASE_URL = (
    os.getenv("TWILIO_PUBLIC_BASE_URL")
    or os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "http://localhost:8000"
)
TWILIO_CONVERSATION_LANG = os.getenv("TWILIO_CONVERSATION_LANG", "en-US")


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


def _build_transfer_twiml() -> str:
    target = (TWILIO_HUMAN_AGENT_NUMBER or "").strip()
    if not target:
        raise RuntimeError("TWILIO_HUMAN_AGENT_NUMBER is not configured")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Dial>{target}</Dial>"
        "</Response>"
    )


def _get_twilio_client() -> Client | None:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return None
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


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


@router.post("/status")
async def status_callback(request: Request):
    payload = dict(await request.form())
    print(f"Twilio status callback: {payload}")
    return {"success": True}


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
                if client and session.get("call_sid") and TWILIO_HUMAN_AGENT_NUMBER:
                    try:
                        client.calls(session["call_sid"]).update(twiml=_build_transfer_twiml())
                    except Exception as exc:
                        print(f"Twilio transfer failed: {exc}")
                continue

            if bot.is_exit_intent(caller_text):
                exit_text = bot.get_exit_message()
                ai_responses.append({"content": exit_text, "timestamp": datetime.now().isoformat()})
                transfer_service.append_message(session["session_id"], "assistant", exit_text)
                await _send_text(websocket, exit_text)
                break

            reply = bot.get_response(caller_text, history)
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
        transfer_service.close_call(session.get("session_id"))
        duration = max((datetime.now() - session["started_at"]).total_seconds(), 0)
        if session.get("phone_number") and (user_transcript or ai_responses):
            handoff = transfer_service.get_call(session.get("session_id", "")) or {}
            log_call(
                session["phone_number"],
                session.get("lead_id"),
                {
                    "direction": "inbound",
                    "status": "completed",
                    "duration": duration,
                    "transcription": _serialize_messages(user_transcript, "user"),
                    "ai_responses": _serialize_messages(ai_responses, "bot"),
                    "summary": f"Twilio inbound call with {len(user_transcript)} caller turns",
                    "sentiment": "neutral",
                    "transfer_requested": handoff.get("transfer_requested", False),
                    "transfer_reason": handoff.get("transfer_reason"),
                    "transfer_mode": handoff.get("transfer_mode"),
                    "call_session_id": session.get("session_id"),
                },
            )
