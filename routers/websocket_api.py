"""
Ultra-Fast Voice Bot WebSocket Router
Handles real-time voice bot functionality with Deepgram, Google TTS, and AI processing
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import base64
import json
import os
import threading
import time
from queue import SimpleQueue
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import numpy as np
import scipy.signal as sps
import httpx
import websocket
from urllib.parse import parse_qs, urlparse

# Import project dependencies
from mongo_client import mongo_client
from routers.calls_api import log_call, update_lead_status_from_call
from qa_engine import RealEstateQA
from ai_services import AIServices

router = APIRouter(tags=["WebSocket"])

# Environment variables
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DG_API_KEY")

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
piopiy_ws = None

# Global call tracking
current_call_data = {
    "phone_number": None,
    "lead_id": None,
    "transcription": [],
    "ai_responses": [],
    "start_time": None,
    "end_time": None,
    "call_session_id": None
}

async def log_call_message(message_type: str, content: str, phone_number: Optional[str] = None, lead_id: Optional[str] = None):
    """Log call message and track conversation"""
    try:
        timestamp = datetime.now().strftime("%H:%M:%S")
        if message_type == "user":
            print(f"🎤 [{timestamp}] User: {content}")
            current_call_data["transcription"].append({
                "type": "user",
                "content": content,
                "timestamp": datetime.now().isoformat()
            })
        elif message_type == "bot":
            print(f"🤖 [{timestamp}] Bot: {content}")
            current_call_data["ai_responses"].append({
                "type": "bot",
                "content": content,
                "timestamp": datetime.now().isoformat()
            })
        elif message_type == "greeting":
            print(f"👋 [{timestamp}] Greeting: {content}")
            current_call_data["ai_responses"].append({
                "type": "greeting",
                "content": content,
                "timestamp": datetime.now().isoformat()
            })
        elif message_type == "exit":
            print(f"👋 [{timestamp}] Exit: {content}")
            current_call_data["ai_responses"].append({
                "type": "exit",
                "content": content,
                "timestamp": datetime.now().isoformat()
            })
        elif message_type == "system":
            print(f"⚙️ [{timestamp}] System: {content}")

        if phone_number:
            current_call_data["phone_number"] = phone_number
        if lead_id:
            current_call_data["lead_id"] = lead_id

    except Exception as e:
        print(f"⚠️ Call logging error: {e}")

async def start_call_tracking(phone_number: str, lead_id: Optional[str] = None, call_session_id: Optional[str] = None):
    """Start tracking a new call"""
    global current_call_data
    current_call_data = {
        "phone_number": phone_number,
        "lead_id": lead_id,
        "transcription": [],
        "ai_responses": [],
        "start_time": datetime.now(),
        "end_time": None,
        "call_session_id": call_session_id
    }
    print(f"📞 Started tracking call for {phone_number}")

async def end_call_tracking():
    """End call tracking and save to MongoDB"""
    global current_call_data

    if not current_call_data["phone_number"] and not current_call_data["transcription"] and not current_call_data["ai_responses"]:
        print("📞 No meaningful call data to save")
        return

    if current_call_data["phone_number"] == "unknown" and not current_call_data["transcription"] and not current_call_data["ai_responses"]:
        print("📞 Skipping log for unknown phone with no conversation")
        return

    try:
        current_call_data["end_time"] = datetime.now()
        duration = (current_call_data["end_time"] - current_call_data["start_time"]).total_seconds() if current_call_data["start_time"] and current_call_data["end_time"] else 0
        phone_to_log = current_call_data["phone_number"] or "unknown"

        # Analyze conversation interest
        interest_analysis = None
        if current_call_data["transcription"] and current_call_data["ai_responses"]:
            try:
                bot = RealEstateQA(ai_services)
                interest_analysis = bot.analyze_conversation_interest(
                    current_call_data["transcription"],
                    current_call_data["ai_responses"]
                )
                print(f"✅ Interest analysis: {interest_analysis['interest_status']} ({interest_analysis['confidence']:.2f})")
            except Exception as e:
                print(f"⚠️ Interest analysis failed: {e}")
                interest_analysis = {
                    "interest_status": "neutral",
                    "confidence": 0.5,
                    "reasoning": f"Analysis failed: {str(e)}",
                    "key_indicators": []
                }

        call_data = {
            "duration": duration,
            "transcription": current_call_data["transcription"],
            "ai_responses": current_call_data["ai_responses"],
            "summary": f"Call with {len(current_call_data['transcription'])} user messages and {len(current_call_data['ai_responses'])} AI responses",
            "sentiment": "neutral",
            "interest_analysis": interest_analysis,
            "call_session_id": current_call_data.get("call_session_id"),
            "status": "completed"
        }

        if current_call_data["transcription"] or current_call_data["ai_responses"]:
            if current_call_data.get("call_session_id"):
                result = log_call(phone_to_log, current_call_data["lead_id"], call_data)
                if result["success"]:
                    print(f"✅ Call logged to MongoDB: {phone_to_log} (session: {current_call_data.get('call_session_id')})")
                    update_lead_status_from_call(phone_to_log, current_call_data["lead_id"], call_data)
                else:
                    print(f"⚠️ Failed to log call: {result.get('error', 'Unknown error')}")
            elif phone_to_log != "unknown" and mongo_client and mongo_client.is_connected():
                try:
                    five_minutes_ago = datetime.now() - timedelta(minutes=5)
                    query = {"status": "initiated", "created_at": {"$gte": five_minutes_ago}}
                    if current_call_data["lead_id"]:
                        query["lead_id"] = current_call_data["lead_id"]
                    else:
                        query["phone_number"] = phone_to_log

                    recent_call = mongo_client.calls.find_one(query, sort=[("created_at", -1)])
                    if recent_call:
                        mongo_client.calls.update_one(
                            {"_id": recent_call["_id"]},
                            {"$set": {
                                "status": "completed",
                                "duration": call_data["duration"],
                                "transcription": call_data["transcription"],
                                "ai_responses": call_data["ai_responses"],
                                "call_summary": call_data["summary"],
                                "sentiment": call_data["sentiment"],
                                "interest_analysis": call_data["interest_analysis"],
                                "updated_at": datetime.now()
                            }}
                        )
                        print(f"✅ Updated existing initiated call record for {phone_to_log}")
                        update_lead_status_from_call(phone_to_log, current_call_data["lead_id"], call_data)
                    else:
                        result = log_call(phone_to_log, current_call_data["lead_id"], call_data)
                        print(f"✅ Created new call record for {phone_to_log}")
                except Exception as e:
                    print(f"⚠️ Error updating existing call: {e}")
                    result = log_call(phone_to_log, current_call_data["lead_id"], call_data)
            else:
                result = log_call(phone_to_log, current_call_data["lead_id"], call_data)
                print(f"✅ Created fallback call record")
        else:
            print("📞 Skipping log - no conversation data to save")

    except Exception as e:
        print(f"❌ Error ending call tracking: {e}")

    current_call_data = {
        "phone_number": None,
        "lead_id": None,
        "transcription": [],
        "ai_responses": [],
        "start_time": None,
        "end_time": None,
        "call_session_id": None
    }

# Audio processing
def fast_audio_convert(raw_audio: bytes) -> bytes:
    """Fastest possible audio conversion to Deepgram-compatible format"""
    try:
        samples = np.frombuffer(raw_audio, dtype=np.int16)
        resampled = sps.resample_poly(samples, 8000, 22050)
        normalized = np.clip(resampled * 0.8, -32767, 32767).astype(np.int16)
        return normalized.tobytes()
    except Exception as e:
        print(f"⚠️ Audio conversion error: {e}")
        return raw_audio

# TTS functions
async def ultra_fast_tts(text: str) -> Optional[bytes]:
    """Ultra-fast async TTS using Google TTS"""
    if not text.strip():
        print("🔇 TTS skipped: empty text")
        return None
    print(f"🗣️ TTS request: '{text[:80]}'")

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
                    print(f"✅ TTS HTTP 200, bytes: {len(raw)}")
                    return raw
                print("⚠️ TTS success but empty audioContent")
            else:
                print(f"⚠️ TTS HTTP {response.status_code}: {response.text[:200]}")
        except Exception as e:
            print(f"❌ TTS request failed: {e}")
    return None

async def send_audio_ultra_fast(audio_b64: str):
    """Send audio to Piopiy WebSocket"""
    global piopiy_ws
    if piopiy_ws:
        try:
            await piopiy_ws.send_text(json.dumps({
                "type": "audio",
                "audio_base64": audio_b64,
                "audio_type": "raw",
                "sample_rate": 8000
            }))
            print(f"📤 Sent audio chunk, length={len(audio_b64)} base64 chars")
        except Exception as e:
            print(f"❌ Error sending audio: {e}")
    else:
        print("⚠️ No active WebSocket connection to send audio")

async def trigger_call_hangup():
    """Trigger call hangup by closing WebSocket connection"""
    try:
        await log_call_message("system", "Triggering call hangup due to exit intent")
        global piopiy_ws
        if piopiy_ws:
            print("🛑 Closing WebSocket connection to hangup call")
            await piopiy_ws.close()
            await log_call_message("system", "WebSocket connection closed - call should end")
        else:
            print("⚠️ No active WebSocket connection to close")
            await log_call_message("system", "No active WebSocket connection found")
    except Exception as e:
        print(f"❌ Error triggering call hangup: {e}")
        await log_call_message("system", f"Call hangup error: {str(e)}")

# Worker functions
async def ultra_fast_tts_worker():
    """TTS processing worker"""
    while True:
        try:
            if not tts_q.empty():
                text = tts_q.get()
                if text is None:
                    break
                print(f"🔔 TTS worker dequeued text: '{text[:80]}'")
                raw_audio = await ultra_fast_tts(text)
                if raw_audio:
                    processed = await asyncio.get_event_loop().run_in_executor(None, fast_audio_convert, raw_audio)
                    audio_b64 = base64.b64encode(processed).decode()
                    print(f"🎼 Processed audio bytes: in={len(raw_audio)} -> out={len(processed)}")
                    await send_audio_ultra_fast(audio_b64)
                else:
                    print("⚠️ No audio produced by TTS")
            await asyncio.sleep(0.0005)
        except Exception as e:
            print(f"⚠️ TTS worker error: {e}")
            await asyncio.sleep(0.1)

async def ultra_fast_llm_worker():
    """LLM processing worker"""
    bot = RealEstateQA(ai_services)
    history = []
    session_started = False
    has_sent_greeting = False
    last_activity = datetime.now()

    while True:
        try:
            if session_started and (datetime.now() - last_activity).total_seconds() > 30:
                print("⏰ No activity for 30 seconds, ending session")
                exit_message = bot.get_exit_message()
                await log_call_message("exit", exit_message)
                tts_q.put(exit_message)
                await asyncio.sleep(3)
                await trigger_call_hangup()
                session_started = False
                history = []
                has_sent_greeting = False
                continue

            if not transcript_q.empty():
                user_text = transcript_q.get()
                last_activity = datetime.now()

                if not session_started:
                    session_started = True
                    await log_call_message("system", f"Session started. First user input: {user_text}")

                await log_call_message("user", user_text)

                if not has_sent_greeting:
                    greeting = bot.get_greeting_message()
                    await log_call_message("greeting", greeting)
                    tts_q.put(greeting)
                    has_sent_greeting = True
                    continue

                if bot.is_exit_intent(user_text):
                    exit_message = bot.get_exit_message()
                    await log_call_message("exit", exit_message)
                    tts_q.put(exit_message)
                    await asyncio.sleep(3)
                    await trigger_call_hangup()
                    session_started = False
                    history = []
                    has_sent_greeting = False
                    continue

                try:
                    reply = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, bot.get_response, user_text, history),
                        timeout=10.0
                    )
                    await log_call_message("bot", reply)
                except asyncio.TimeoutError:
                    print("⚠️ LLM response timeout, sending fallback")
                    reply = "I apologize, but I'm having trouble processing your request right now. Could you please try again?"
                    await log_call_message("bot", reply)
                except Exception as e:
                    print(f"⚠️ Error generating response: {e}, type: {type(e).__name__}")
                    if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                        reply = "I'm sorry, there's an authentication issue with my AI service. Please check the API configuration."
                    elif "connection" in str(e).lower() or "timeout" in str(e).lower():
                        reply = "I'm sorry, I'm having trouble connecting to my AI service. Please try again."
                    else:
                        reply = "I'm sorry, I encountered an error. Please try again."
                    await log_call_message("bot", reply)

                tts_q.put(reply)
                history.extend([
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": reply}
                ])
                if len(history) > 8:
                    history = history[-6:]

            await asyncio.sleep(0.0001)
        except Exception as e:
            print(f"⚠️ LLM worker error: {e}")
            await asyncio.sleep(0.1)

# Deepgram WebSocket client
def start_fast_deepgram():
    """Start Deepgram WebSocket client with reconnection logic"""
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
                if transcript and is_final:
                    transcript_q.put(transcript)
        except Exception as e:
            print(f"⚠️ Deepgram message parse error: {e}")

    def on_error(ws, error):
        print(f"⚠️ Deepgram WS error: {error}")
        reconnect_deepgram()

    def on_close(ws, close_status_code, close_msg):
        print(f"ℹ️ Deepgram WS closed: {close_status_code} {close_msg}")
        reconnect_deepgram()

    def reconnect_deepgram():
        global dg_ws_client
        print("🔄 Attempting to reconnect Deepgram WebSocket...")
        time.sleep(5)
        if DEEPGRAM_API_KEY:
            headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
            dg_ws_client = websocket.WebSocketApp(
                DG_WS_URL,
                header=headers,
                on_open=on_open,
                on_message=on_message,
                on_close=on_close,
                on_error=on_error
            )
            threading.Thread(target=dg_ws_client.run_forever, daemon=True).start()

    if DEEPGRAM_API_KEY:
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        dg_ws_client = websocket.WebSocketApp(
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

def get_websocket_url():
    """Get WebSocket URL based on environment"""
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    websocket_url = os.getenv("WEBSOCKET_URL")
    if websocket_url and not websocket_url.startswith("ws://localhost"):
        return websocket_url
    elif render_url:
        ws_url = render_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_url}/ws"
    return "ws://localhost:8000/ws"

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Ultra-fast WebSocket client handler"""
    global piopiy_ws
    piopiy_ws = websocket

    await websocket.accept()
    ws_url = get_websocket_url()
    env_type = "Production (Render)" if os.getenv("RENDER_EXTERNAL_URL") else "Development"
    print(f"🔗 WebSocket available at: {ws_url} [{env_type}]")
    print(f"📞 WebSocket client connected from: {websocket.client}")

    # Extract session info from query parameters
    session_id = None
    extracted_phone = "unknown"
    extracted_lead_id = None
    try:
        query_params = parse_qs(urlparse(websocket.scope["path"]).query)
        extracted_phone = query_params.get("phone_number", ["unknown"])[0]
        extracted_lead_id = query_params.get("lead_id", [None])[0]
        session_id = query_params.get("session", [None])[0]
        print(f"📋 Extracted from query params: phone={extracted_phone}, lead_id={extracted_lead_id}, session={session_id}")
    except Exception as e:
        print(f"⚠️ Error parsing query parameters: {e}")

    # Start call tracking
    await start_call_tracking(
        phone_number=extracted_phone,
        lead_id=extracted_lead_id,
        call_session_id=session_id
    )

    # Start workers
    tts_task = asyncio.create_task(ultra_fast_tts_worker())
    llm_task = asyncio.create_task(ultra_fast_llm_worker())

    # Start Deepgram if not already started
    if not dg_ws_client and DEEPGRAM_API_KEY:
        start_fast_deepgram()
        await asyncio.sleep(1)

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                print("🔌 WebSocket client disconnected")
                break
            elif message["type"] == "websocket.receive":
                if "text" in message:
                    try:
                        print(f"📝 Received text message: {message['text'][:200]}...")
                        data = json.loads(message["text"])
                        print(f"📋 Parsed JSON: {data}")

                        extra_params = data.get("extra_params")
                        if extra_params:
                            phone = extra_params.get("phone_number")
                            lead_id = extra_params.get("lead_id")
                            sess = extra_params.get("session")
                            if phone:
                                current_call_data["phone_number"] = str(phone)
                            if lead_id:
                                current_call_data["lead_id"] = str(lead_id)
                            if sess:
                                current_call_data["call_session_id"] = str(sess)
                            await log_call_message("system", f"Call context: phone={phone}, lead_id={lead_id}, session={sess}")

                        meta = data.get("meta")
                        if isinstance(meta, dict):
                            phone = meta.get("phone_number") or meta.get("phone")
                            lead_id = meta.get("lead_id")
                            sess = meta.get("session") or meta.get("sid")
                            if phone:
                                current_call_data["phone_number"] = str(phone)
                            if lead_id:
                                current_call_data["lead_id"] = str(lead_id)
                            if sess:
                                current_call_data["call_session_id"] = str(sess)
                            await log_call_message("system", f"Call context from meta: phone={phone}, lead_id={lead_id}")
                    except json.JSONDecodeError as e:
                        print(f"⚠️ Non-JSON text message received: {message['text'][:200]}, error: {e}")
                    except Exception as e:
                        print(f"⚠️ Error processing text message: {e}, message: {message['text'][:200]}")
                elif "bytes" in message:
                    try:
                        print(f"🎵 Received audio data: {len(message['bytes'])} bytes")
                        if dg_ws_client and dg_ws_client.sock and dg_ws_client.sock.connected:
                            processed_audio = await asyncio.get_event_loop().run_in_executor(None, fast_audio_convert, message["bytes"])
                            dg_ws_client.send(processed_audio, opcode=websocket.ABNF.OPCODE_BINARY)
                            print("📤 Sent audio to Deepgram")
                        else:
                            print("⚠️ Deepgram WebSocket not connected")
                    except Exception as e:
                        print(f"⚠️ Error processing audio data: {e}")
    except Exception as e:
        print(f"❌ WebSocket connection error: {e}")
    finally:
        print("🔌 WebSocket client disconnected - ending call tracking")
        await end_call_tracking()
        tts_task.cancel()
        llm_task.cancel()
        try:
            await tts_task
            await llm_task
        except asyncio.CancelledError:
            pass
        piopiy_ws = None

# Log API key presence at startup
print(f"[WebSocket Server] 🔧 GOOGLE_API_KEY present: {'Yes' if GOOGLE_API_KEY else 'No'}")
print(f"[WebSocket Server] 🔧 DG_API_KEY present: {'Yes' if DEEPGRAM_API_KEY else 'No'}")