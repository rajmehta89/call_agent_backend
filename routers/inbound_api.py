from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from piopiy import Action
import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/python", tags=["inbound"])

# Environment variables
WEBSOCKET_URL = os.getenv("WEBSOCKET_URL")
AGENT_NUMBER = os.getenv("AGENT_NUMBER")
PIOPIY_NUMBER = os.getenv("PIOPIY_NUMBER")

@router.post("/inbound")
async def inbound_call(request: Request):
    """Handle inbound calls by streaming to WebSocket and connecting to agent"""
    action = Action()

    # Stream the call to WebSocket server
    action.stream(WEBSOCKET_URL, {
        "listen_mode": "caller",
        "voice_quality": "8000",
        "stream_on_answer": True
    })

    # Connect to human agent
    action.call(AGENT_NUMBER, PIOPIY_NUMBER, {
        "duration": 300,
        "timeout": 20,
        "loop": 2
    })

    return JSONResponse(content=action.PCMO())
