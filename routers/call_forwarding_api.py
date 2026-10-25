"""Settings > Call forwarding (admins), and the Twilio callbacks that ring the team (see call_forwarding.py)."""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import call_forwarding
from auth_service import require_permission, require_user
from mongo_client import mongo_client
from routers.platform_api import _serialize
from twilio_security import verify_twilio_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/call-forwarding", tags=["call forwarding"])


class AgentPayload(BaseModel):
    name: str = ""
    phone: str = ""
    enabled: bool = True


class ForwardingPayload(BaseModel):
    enabled: bool = False
    agents: List[AgentPayload] = []
    ring_seconds: int = call_forwarding.DEFAULT_RING_SECONDS


def _require_db() -> None:
    if not mongo_client.is_connected():
        raise HTTPException(status_code=503, detail="The database is not connected")


@router.get("", dependencies=[Depends(require_user)])
def get_forwarding() -> Dict[str, Any]:
    _require_db()
    return {"success": True, "data": _serialize(call_forwarding.get_settings())}


@router.put("")
def save_forwarding(payload: ForwardingPayload, user: Dict[str, Any] = Depends(require_permission("manage_team"))) -> Dict[str, Any]:
    _require_db()
    try:
        saved = call_forwarding.save_settings(payload.model_dump(), user)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {"success": True, "data": _serialize(saved)}


def _xml(body: str) -> PlainTextResponse:
    return PlainTextResponse(body, media_type="text/xml")


@router.post("/screen", dependencies=[Depends(verify_twilio_request)])
async def agent_screen() -> PlainTextResponse:
    """An agent's phone picked up: ask them to press 1 (a voicemail or busy phone never does)."""
    return _xml(call_forwarding.screen_twiml())


@router.post("/screen-result", dependencies=[Depends(verify_twilio_request)])
async def agent_screen_result(request: Request) -> PlainTextResponse:
    form = dict(await request.form())
    digits, parent = str(form.get("Digits") or ""), str(form.get("ParentCallSid") or "")
    if digits.strip() == "1" and parent:
        call_forwarding.agent_accepted(parent, str(form.get("To") or ""))
    return _xml(call_forwarding.screen_result_twiml(digits))


@router.post("/dial-result", dependencies=[Depends(verify_twilio_request)])
async def dial_result(request: Request) -> PlainTextResponse:
    """The agents' phones are done: connected and finished, or nobody accepted (the caller hears the team will call back)."""
    form = dict(await request.form())
    status = str(form.get("DialCallStatus") or "")
    accepted = call_forwarding.dial_finished(str(form.get("CallSid") or ""), status, str(form.get("From") or ""),
                                              str(form.get("DialCallSid") or ""), str(form.get("DialCallDuration") or ""))
    return _xml(call_forwarding.dial_result_twiml(accepted))


@router.post("/recording-result", dependencies=[Depends(verify_twilio_request)])
async def recording_result(request: Request, background_tasks: BackgroundTasks) -> PlainTextResponse:
    form = dict(await request.form())
    call_forwarding.recording_finished(str(form.get("CallSid") or ""), str(form.get("RecordingSid") or ""),
                                       str(form.get("RecordingUrl") or ""), str(form.get("RecordingStatus") or ""))
    if str(form.get("RecordingStatus") or "") == "completed":
        from human_transcript import transcribe_human_recording
        background_tasks.add_task(transcribe_human_recording, str(form.get("CallSid") or ""), str(form.get("RecordingSid") or ""))
    return _xml('<?xml version="1.0" encoding="UTF-8"?><Response></Response>')


@router.post("/full-recording-result", dependencies=[Depends(verify_twilio_request)])
async def full_recording_result(request: Request, background_tasks: BackgroundTasks) -> PlainTextResponse:
    form = dict(await request.form())
    background_tasks.add_task(call_forwarding.full_recording_finished, str(form.get("CallSid") or ""),
                              str(form.get("RecordingSid") or ""), str(form.get("RecordingUrl") or ""),
                              str(form.get("RecordingStatus") or ""), str(form.get("RecordingDuration") or ""))
    return _xml('<?xml version="1.0" encoding="UTF-8"?><Response></Response>')
