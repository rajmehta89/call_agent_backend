from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from transfer_service import transfer_service
from routers.calls_api import update_call_from_twilio


router = APIRouter(prefix="/api/handoffs", tags=["handoffs"])


class AcceptTransferRequest(BaseModel):
    agent_name: str


class ResolveTransferRequest(BaseModel):
    notes: Optional[str] = None


@router.get("")
async def get_active_handoffs():
    return {"success": True, "data": transfer_service.get_active_handoffs()}


@router.get("/{session_id}")
async def get_handoff(session_id: str):
    record = transfer_service.get_call(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Handoff not found")
    return {"success": True, "data": record}


@router.post("/{session_id}/accept")
async def accept_handoff(session_id: str, request: AcceptTransferRequest):
    record = transfer_service.accept_transfer(session_id, request.agent_name.strip() or "Human Agent")
    if not record:
        raise HTTPException(status_code=404, detail="Handoff not found")
    update_call_from_twilio(session_id, transfer_status="accepted", transfer_destination=record.get("accepted_by"))
    return {"success": True, "data": record}


@router.post("/{session_id}/resolve")
async def resolve_handoff(session_id: str, request: ResolveTransferRequest):
    record = transfer_service.resolve_transfer(session_id, request.notes)
    if not record:
        raise HTTPException(status_code=404, detail="Handoff not found")
    update_call_from_twilio(session_id, transfer_status="completed", transfer_destination=record.get("handled_by"))
    return {"success": True, "data": record}
