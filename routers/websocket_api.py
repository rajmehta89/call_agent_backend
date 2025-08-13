"""
FastAPI Voice Bot - Complete Implementation
Converted from Flask to FastAPI with WebSocket support for real-time voice processing
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import asyncio
import base64
import json
import os
import threading
import time
import uuid
from queue import SimpleQueue
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import numpy as np
import scipy.signal as sps
import httpx
import websocket as ws_client
from urllib.parse import parse_qs, urlparse
from piopiy import StreamAction, RestAPI

# Import project dependencies (adjust paths as needed)
try:
    from mongo_client import mongo_client
    from routers.calls_api import log_call, update_lead_status_from_call
    from qa_engine import RealEstateQA
    from ai_services import AIServices
except ImportError as e:
    print(f"⚠️ Import error: {e}")
    # Create mock classes for testing
    class MockAIServices:
        pass
    class MockRealEstateQA:
        def __init__(self, ai_services):
            pass
        def get_greeting_message(self):
            return "Hello! I'm your real estate assistant. How can I help you today?"
        def get_exit_message(self):
            return "Thank you for calling. Have a great day!"
        def is_exit_intent(self, text):
            return any(word in text.lower() for word in ['goodbye', 'bye', 'end call', 'hang up', 'stop'])
        def get_response(self, text, history):
            return "Thank you for your message. This is a test response."
        def analyze_conversation_interest(self, transcription, ai_responses):
            return {
                "interest_status": "neutral",
                "confidence": 0.5,
                "reasoning": "Mock analysis",
                "key_indicators": []
            }

    AIServices = MockAIServices
    RealEstateQA = MockRealEstateQA
    mongo_client = None
    def log_call(phone, lead_id, data):
        return {"success": True}
    def update_lead_status_from_call(phone, lead_id, data):
        pass

app = FastAPI(title="Voice Bot API", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Environment variables
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DG_API_KEY")
PIOPIY_API_KEY = os.getenv("PIOPIY_API_KEY")
PIOPIY_API_SECRET = os.getenv("PIOPIY_API_SECRET")

# Constants
GOOGLE_TTS_URL = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
DG_WS_URL = (
    "wss://api.deepgram.com/v1/listen?"
    "sample_rate=8000&encoding=linear16&model=nova-2"
    "&language=en-IN&smart_format=true&vad_turnoff=1500"
)

# Global variables
transcript_q = SimpleQueue()
tts_q = SimpleQueue()
ai_services = AIServices()
dg_ws_client = None
active_connections: Dict[str, WebSocket] = {}
call_sessions: Dict[str, Dict] = {}

# Piopiy REST API instance
try:
    piopiy_rest = RestAPI(PIOPIY_API_KEY, PIOPIY_API_SECRET)
except:
    piopiy_rest = None
    print("⚠️ Piopiy REST API not initialized")

class CallManager:
    def __init__(self):
        self.active_calls = {}

    def start_call_session(self, session_id: str, phone_number: str = None, lead_id: str = None):
        """Start a new call session"""
        self.active_calls[session_id] = {
            "phone_number": phone_number or "unknown",
            "lead_id": lead_id,
            "transcription": [],
            "ai_responses": [],
            "start_time": datetime.now(),
            "end_time": None,
            "call_session_id": session_id,
            "status": "active"
        }
        print(f"📞 Started call session {session_id} for {phone_number}")
        return self.active_calls[session_id]

    def end_call_session(self, session_id: str):
        """End a call session and save to database"""
        if session_id not in self.active_calls:
            print(f"⚠️ Session {session_id} not found")
            return

        call_data = self.active_calls[session_id]
        call_data["end_time"] = datetime.now()
        call_data["status"] = "completed"

        # Calculate duration
        duration = 0
        if call_data["start_time"] and call_data["end_time"]:
            duration = (call_data["end_time"] - call_data["start_time"]).total_seconds()

        # Save to database
        if call_data["transcription"] or call_data["ai_responses"]:
            try:
                # Analyze conversation interest
                interest_analysis = None
                if call_data["transcription"] and call_data["ai_responses"]:
                    try:
                        bot = RealEstateQA(ai_services)
                        interest_analysis = bot.analyze_conversation_interest(
                            call_data["transcription"],
                            call_data["ai_responses"]
                        )
                    except Exception as e:
                        print(f"⚠️ Interest analysis failed: {e}")
                        interest_analysis = {
                            "interest_status": "neutral",
                            "confidence": 0.5,
                            "reasoning": f"Analysis failed: {str(e)}",
                            "key_indicators": []
                        }

                db_call_data = {
                    "duration": duration,
                    "transcription": call_data["transcription"],
                    "ai_responses": call_data["ai_responses"],
                    "summary": f"Call with {len(call_data['transcription'])} user messages and {len(call_data['ai_responses'])} AI responses",
                    "sentiment": "neutral",
                    "interest_analysis": interest_analysis,
                    "call_session_id": session_id,
                    "status": "completed"
                }

                result = log_call(call_data["phone_number"], call_data["lead_id"], db_call_data)
                if result["success"]:
                    print(f"✅ Call logged: {call_data['phone_number']} (session: {session_id})")
                    update_lead_status_from_call(call_data["phone_number"], call_data["lead_id"], db_call_data)
                else:
                    print(f"⚠️ Failed to log call: {result.get('error', 'Unknown error')}")

            except Exception as e:
                print(f"❌ Error saving call session: {e}")

        # Remove from active calls
        del self.active_calls[session_id]
        print(f"📞 Ended call session {session_id}")

    def log_message(self, session_id: str, message_type: str, content: str):
        """Log a message for a call session"""
        if session_id not in self.active_calls:
            print(f"⚠️ Session {session_id} not found for logging")
            return

        call_data = self.active_calls[session_id]
        timestamp = datetime.now()

        message_entry = {
            "type": message_type,
            "content": content,
            "timestamp": timestamp.isoformat()
        }

        if message_type == "user":
            call_data["transcription"].append(message_entry)
            print(f"🎤 [{timestamp.strftime('%H:%M:%S')}] User ({session_id[:8]}): {content}")
        elif message_type in ["bot", "greeting", "exit"]:
            call_data["ai_responses"].append(message_entry)
            emoji = "🤖" if message_type == "bot" else "👋" if message_type == "greeting" else "🚪"
            print(f"{emoji} [{timestamp.strftime('%H:%M:%S')}] {message_type.title()} ({session_id[:8]}): {content}")
        elif message_type == "system":
            print(f"⚙️ [{timestamp.strftime('%H:%M:%S')}] System ({session_id[:8]}): {content}")

# Global call manager
call_manager = CallManager()

# Audio processing functions
def fast_audio_convert(raw_audio: bytes) -> bytes:
    """Convert audio to Deepgram-compatible format"""
    try:
        samples = np.frombuffer(raw_audio, dtype=np.int16)
        resampled = sps.resample_poly(samples, 8000, 22050)
        normalized = np.clip(resampled * 0.8, -32767, 32767).astype(np.int16)
        return normalized.tobytes()
    except Exception as e:
        print(f"⚠️ Audio conversion error: {e}")
        return raw_audio

# TTS functions
async def generate_tts(text: str) -> Optional[bytes]:
    """Generate TTS audio using Google TTS"""
    if not text.strip():
        return None

    print(f"🗣️ TTS request: '{text[:80]}{'...' if len(text) > 80 else ''}'")

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(GOOGLE_TTS_URL, json={
                "input": {"text": text},
                "voice": {"languageCode": "en-US", "name": "en-US-Standard-D", "ssmlGender": "MALE"},
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 22050, "speakingRate": 1.15}
            })

            if response.status_code == 200:
                audio_b64 = response.json().get("audioContent", "")
                if audio_b64:
                    raw = base64.b64decode(audio_b64)
                    print(f"✅ TTS generated {len(raw)} bytes")
                    return raw
                print("⚠️ TTS success but empty audioContent")
            else:
                print(f"⚠️ TTS HTTP {response.status_code}: {response.text[:200]}")
        except Exception as e:
            print(f"❌ TTS request failed: {e}")

    return None

async def send_audio_to_websocket(websocket: WebSocket, audio_b64: str, session_id: str):
    """Send audio to WebSocket client"""
    try:
        action = StreamAction()
        audio_message = action.playStream(audio_base64=audio_b64, audio_type="raw", sample_rate=8000)
        await websocket.send_text(audio_message)
        print(f"📤 Sent audio to session {session_id[:8]}, length={len(audio_b64)} chars")
    except Exception as e:
        print(f"❌ Error sending audio to {session_id[:8]}: {e}")

# Worker functions
async def tts_worker():
    """TTS processing worker"""
    while True:
        try:
            if not tts_q.empty():
                task = tts_q.get()
                if task is None:
                    break

                text, session_id = task
                print(f"🔔 TTS worker processing for session {session_id[:8]}: '{text[:50]}{'...' if len(text) > 50 else ''}'")

                raw_audio = await generate_tts(text)
                if raw_audio and session_id in active_connections:
                    processed = await asyncio.get_event_loop().run_in_executor(None, fast_audio_convert, raw_audio)
                    audio_b64 = base64.b64encode(processed).decode()
                    websocket = active_connections[session_id]
                    await send_audio_to_websocket(websocket, audio_b64, session_id)
                else:
                    if not raw_audio:
                        print(f"⚠️ No audio generated for session {session_id[:8]}")
                    if session_id not in active_connections:
                        print(f"⚠️ Session {session_id[:8]} not in active connections")

            await asyncio.sleep(0.01)
        except Exception as e:
            print(f"⚠️ TTS worker error: {e}")
            await asyncio.sleep(0.1)

async def llm_worker():
    """LLM processing worker"""
    session_states = {}

    while True:
        try:
            if not transcript_q.empty():
                user_text, session_id = transcript_q.get()

                # Initialize session state if new
                if session_id not in session_states:
                    session_states[session_id] = {
                        "bot": RealEstateQA(ai_services),
                        "history": [],
                        "has_sent_greeting": False,
                        "last_activity": datetime.now()
                    }

                state = session_states[session_id]
                state["last_activity"] = datetime.now()

                call_manager.log_message(session_id, "user", user_text)

                # Send greeting if first interaction
                if not state["has_sent_greeting"]:
                    greeting = state["bot"].get_greeting_message()
                    call_manager.log_message(session_id, "greeting", greeting)
                    tts_q.put((greeting, session_id))
                    state["has_sent_greeting"] = True
                    continue

                # Check for exit intent
                if state["bot"].is_exit_intent(user_text):
                    exit_message = state["bot"].get_exit_message()
                    call_manager.log_message(session_id, "exit", exit_message)
                    tts_q.put((exit_message, session_id))

                    # Schedule call hangup
                    asyncio.create_task(hangup_call_after_delay(session_id, 3))
                    continue

                # Generate response
                try:
                    reply = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, state["bot"].get_response, user_text, state["history"]
                        ),
                        timeout=10.0
                    )
                    call_manager.log_message(session_id, "bot", reply)
                except asyncio.TimeoutError:
                    reply = "I apologize, but I'm having trouble processing your request right now. Could you please try again?"
                    call_manager.log_message(session_id, "bot", reply)
                except Exception as e:
                    print(f"⚠️ Error generating response for {session_id[:8]}: {e}")
                    if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                        reply = "I'm sorry, there's an authentication issue with my AI service."
                    else:
                        reply = "I'm sorry, I encountered an error. Please try again."
                    call_manager.log_message(session_id, "bot", reply)

                # Send response
                tts_q.put((reply, session_id))

                # Update history
                state["history"].extend([
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": reply}
                ])
                if len(state["history"]) > 10:
                    state["history"] = state["history"][-8:]

            # Clean up inactive sessions
            current_time = datetime.now()
            inactive_sessions = [
                sid for sid, state in session_states.items()
                if (current_time - state["last_activity"]).total_seconds() > 300  # 5 minutes
            ]
            for sid in inactive_sessions:
                print(f"🧹 Cleaning up inactive session {sid[:8]}")
                del session_states[sid]

            await asyncio.sleep(0.01)
        except Exception as e:
            print(f"⚠️ LLM worker error: {e}")
            await asyncio.sleep(0.1)

async def hangup_call_after_delay(session_id: str, delay: int):
    """Hangup call after specified delay"""
    await asyncio.sleep(delay)
    if session_id in active_connections:
        try:
            websocket = active_connections[session_id]
            call_manager.log_message(session_id, "system", "Ending call due to exit intent")
            await websocket.close()
        except Exception as e:
            print(f"❌ Error hanging up call {session_id[:8]}: {e}")

# Deepgram WebSocket client
def start_deepgram():
    """Start Deepgram WebSocket client"""
    global dg_ws_client

    def on_open(ws):
        print("✅ Deepgram WebSocket connected")
        def keep_alive():
            while ws.sock and ws.sock.connected:
                try:
                    ws.send('{"type":"KeepAlive"}')
                    time.sleep(8)
                except Exception as e:
                    print(f"⚠️ Deepgram keep-alive error: {e}")
                    break
        threading.Thread(target=keep_alive, daemon=True).start()

    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get("type") == "Results":
                transcript = data.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
                is_final = (
                        bool(data.get("is_final"))
                        or bool(data.get("speech_final"))
                        or bool(data.get("final"))
                        or bool(data.get("channel", {}).get("is_final"))
                        or bool(data.get("channel", {}).get("alternatives", [{}])[0].get("final"))
                )

                if transcript:
                    print(f"🎧 ASR: {transcript}{' (final)' if is_final else ' (partial)'}")

                # Only process final transcripts and distribute to all active sessions
                if transcript and is_final:
                    for session_id in active_connections:
                        transcript_q.put((transcript, session_id))

        except Exception as e:
            print(f"⚠️ Deepgram message parse error: {e}")

    def on_error(ws, error):
        print(f"⚠️ Deepgram WS error: {error}")

    def on_close(ws, close_status_code, close_msg):
        print(f"ℹ️ Deepgram WS closed: {close_status_code} {close_msg}")

    if DEEPGRAM_API_KEY:
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        dg_ws_client = ws_client.WebSocketApp(
            DG_WS_URL,
            header=headers,
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error
        )
        threading.Thread(target=dg_ws_client.run_forever, daemon=True).start()
    else:
        print("❌ Deepgram API key missing")

# API Routes

@app.post("/api/make-call")
async def make_call(request: Request):
    """Make an outbound call"""
    try:
        data = await request.json()
        phone_number = data.get("phone_number")
        lead_id = data.get("lead_id")

        if not phone_number:
            raise HTTPException(status_code=400, detail="phone_number is required")

        if not piopiy_rest:
            raise HTTPException(status_code=500, detail="Piopiy API not configured")

        # Generate session ID
        session_id = str(uuid.uuid4())

        # Create call session
        call_manager.start_call_session(session_id, phone_number, lead_id)

        # Prepare WebSocket URL
        base_url = str(request.base_url).replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{base_url}ws/{session_id}?phone_number={phone_number}&lead_id={lead_id or ''}"

        # Make call using Piopiy
        try:
            response = piopiy_rest.voice.call(
                from_=os.getenv("PIOPIY_FROM_NUMBER", "+1234567890"),
                to=phone_number,
                answer_url=ws_url,
                hangup_url=f"{base_url}api/call-hangup/{session_id}",
                extra_params={
                    "phone_number": phone_number,
                    "lead_id": lead_id,
                    "session": session_id
                }
            )

            return {
                "success": True,
                "session_id": session_id,
                "phone_number": phone_number,
                "lead_id": lead_id,
                "call_response": response
            }

        except Exception as e:
            call_manager.end_call_session(session_id)
            raise HTTPException(status_code=500, detail=f"Failed to make call: {str(e)}")

    except Exception as e:
        print(f"❌ Make call error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/call-hangup/{session_id}")
async def call_hangup(session_id: str):
    """Handle call hangup webhook"""
    print(f"📞 Call hangup webhook for session {session_id[:8]}")
    call_manager.end_call_session(session_id)
    return {"success": True}

@app.get("/api/call-status/{session_id}")
async def get_call_status(session_id: str):
    """Get call status"""
    if session_id in call_manager.active_calls:
        call_data = call_manager.active_calls[session_id]
        return {
            "session_id": session_id,
            "status": call_data["status"],
            "phone_number": call_data["phone_number"],
            "lead_id": call_data["lead_id"],
            "start_time": call_data["start_time"].isoformat(),
            "transcription_count": len(call_data["transcription"]),
            "response_count": len(call_data["ai_responses"])
        }
    else:
        raise HTTPException(status_code=404, detail="Call session not found")

@app.get("/api/active-calls")
async def get_active_calls():
    """Get all active calls"""
    active_calls = []
    for session_id, call_data in call_manager.active_calls.items():
        active_calls.append({
            "session_id": session_id,
            "phone_number": call_data["phone_number"],
            "lead_id": call_data["lead_id"],
            "status": call_data["status"],
            "start_time": call_data["start_time"].isoformat(),
            "duration": (datetime.now() - call_data["start_time"]).total_seconds()
        })

    return {"active_calls": active_calls, "count": len(active_calls)}

# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Main WebSocket endpoint for voice bot"""
    await websocket.accept()
    session_id = str(uuid.uuid4())
    active_connections[session_id] = websocket

    print(f"🔗 WebSocket connected: {session_id[:8]}")

    try:
        # Extract query parameters
        query_string = websocket.scope.get("query_string", b"").decode()
        phone_number = "unknown"
        lead_id = None

        if query_string:
            query_params = parse_qs(query_string)
            phone_number = query_params.get("phone_number", ["unknown"])[0]
            lead_id = query_params.get("lead_id", [None])[0]

        # Start call session
        call_manager.start_call_session(session_id, phone_number, lead_id)

        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break
            elif message["type"] == "websocket.receive":
                if "text" in message:
                    # Handle text messages (control/metadata)
                    try:
                        data = json.loads(message["text"])
                        call_manager.log_message(session_id, "system", f"Received metadata: {data}")

                        # Update call data with metadata
                        if "extra_params" in data:
                            extra = data["extra_params"]
                            if "phone_number" in extra:
                                call_manager.active_calls[session_id]["phone_number"] = str(extra["phone_number"])
                            if "lead_id" in extra:
                                call_manager.active_calls[session_id]["lead_id"] = str(extra["lead_id"])

                    except json.JSONDecodeError:
                        # Handle non-JSON text (maybe session ID)
                        text_content = message["text"]
                        if len(text_content) in [32, 36] and text_content.replace("-", "").replace("_", "").isalnum():
                            call_manager.log_message(session_id, "system", f"Received session ID: {text_content}")

                elif "bytes" in message:
                    # Handle audio data
                    audio_data = message["bytes"]
                    print(f"🎵 Received audio: {len(audio_data)} bytes for session {session_id[:8]}")

                    # Send to Deepgram if connected
                    if dg_ws_client and hasattr(dg_ws_client, 'sock') and dg_ws_client.sock and dg_ws_client.sock.connected:
                        try:
                            processed_audio = await asyncio.get_event_loop().run_in_executor(
                                None, fast_audio_convert, audio_data
                            )
                            dg_ws_client.send(processed_audio, opcode=ws_client.ABNF.OPCODE_BINARY)
                            print(f"📤 Sent audio to Deepgram for session {session_id[:8]}")
                        except Exception as e:
                            print(f"⚠️ Error sending audio to Deepgram: {e}")
                    else:
                        print("⚠️ Deepgram WebSocket not connected")

    except WebSocketDisconnect:
        print(f"🔌 WebSocket disconnected: {session_id[:8]}")
    except Exception as e:
        print(f"❌ WebSocket error for {session_id[:8]}: {e}")
    finally:
        # Cleanup
        if session_id in active_connections:
            del active_connections[session_id]
        call_manager.end_call_session(session_id)
        print(f"🧹 Cleaned up session {session_id[:8]}")

@app.websocket("/ws/{session_id}")
async def websocket_with_session(websocket: WebSocket, session_id: str):
    """WebSocket endpoint with specific session ID"""
    await websocket.accept()
    active_connections[session_id] = websocket

    print(f"🔗 WebSocket connected with session: {session_id[:8]}")

    try:
        # Extract query parameters
        query_string = websocket.scope.get("query_string", b"").decode()
        phone_number = "unknown"
        lead_id = None

        if query_string:
            query_params = parse_qs(query_string)
            phone_number = query_params.get("phone_number", ["unknown"])[0]
            lead_id = query_params.get("lead_id", [None])[0]

        # Start or update call session
        if session_id not in call_manager.active_calls:
            call_manager.start_call_session(session_id, phone_number, lead_id)

        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break
            elif message["type"] == "websocket.receive":
                if "bytes" in message:
                    # Handle audio data
                    audio_data = message["bytes"]
                    print(f"🎵 Audio received: {len(audio_data)} bytes for {session_id[:8]}")

                    # Send to Deepgram
                    if dg_ws_client and hasattr(dg_ws_client, 'sock') and dg_ws_client.sock and dg_ws_client.sock.connected:
                        try:
                            processed_audio = await asyncio.get_event_loop().run_in_executor(
                                None, fast_audio_convert, audio_data
                            )
                            dg_ws_client.send(processed_audio, opcode=ws_client.ABNF.OPCODE_BINARY)
                        except Exception as e:
                            print(f"⚠️ Deepgram send error: {e}")

    except WebSocketDisconnect:
        print(f"🔌 WebSocket disconnected: {session_id[:8]}")
    except Exception as e:
        print(f"❌ WebSocket error for {session_id[:8]}: {e}")
    finally:
        if session_id in active_connections:
            del active_connections[session_id]
        call_manager.end_call_session(session_id)

