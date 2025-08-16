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
from typing import Optional
import re
import httpx
from websocket import ABNF, WebSocketApp
from urllib.parse import parse_qs, urlparse
from piopiy import StreamAction

# Import project dependencies
from mongo_client import mongo_client
from routers.calls_api import log_call, update_lead_status_from_call
from qa_engine import RealEstateQA
from ai_services import AIServices

router = APIRouter(tags=["WebSocket"])

# Environment variables
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DG_API_KEY")

# TTS tuning (human-like + slower)
GOOGLE_TTS_VOICE = os.getenv("GOOGLE_TTS_VOICE", "en-IN-Neural2-A")  # good Indian English voice
GOOGLE_TTS_RATE = float(os.getenv("GOOGLE_TTS_RATE", "0.88"))        # 0.75–0.95 feels natural & slower
GOOGLE_TTS_PITCH = float(os.getenv("GOOGLE_TTS_PITCH", "-2.0"))      # in semitones, slightly deeper

# Constants
GOOGLE_TTS_URL = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
DG_WS_URL = (
    "wss://api.deepgram.com/v1/listen?"
    "sample_rate=8000&encoding=linear16&model=nova-2"
    "&language=en-IN&smart_format=true&vad_turnoff=1500&no_delay=true"
)

# Global variables
transcript_q = SimpleQueue()
tts_q = SimpleQueue()
ai_services = AIServices()
dg_ws_client = None
piopiy_ws = None
audio_buffer = bytearray()  # Buffer to accumulate audio chunks (from caller)

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

# ---------------------------
# Utilities
# ---------------------------
def _escape_ssml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def text_to_ssml(text: str) -> str:
    """
    Convert raw text into SSML with natural sentence breaks
    and slightly slower, deeper prosody for human-like telephony.
    """
    text = text.strip()
    if not text:
        return "<speak></speak>"

    # Split into sentences for controlled pauses
    parts = re.split(r'(?<=[.!?])\s+', text)
    segments = []
    for i, p in enumerate(parts):
        if not p:
            continue
        p_esc = _escape_ssml(p)
        # Slight pause after each sentence, longer after the first greeting
        if i == 0:
            segments.append(f"<s>{p_esc}</s><break time='350ms'/>")
        else:
            segments.append(f"<s>{p_esc}</s><break time='300ms'/>")

    body = " ".join(segments)
    # Global prosody: slow rate, slightly lower pitch (more relaxed & clear)
    return (
        "<speak>"
        f"<prosody rate='slow' pitch='{GOOGLE_TTS_PITCH:+.1f}st'>"
        f"{body}"
        "</prosody>"
        "</speak>"
    )

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

# ---------------------------
# TTS functions (slower, SSML)
# ---------------------------
async def ultra_fast_tts(text: str) -> Optional[bytes]:
    """Async TTS using Google TTS with SSML prosody for slower, human-like delivery"""
    cleaned = (text or "").strip()
    if not cleaned:
        print("🔇 TTS skipped: empty text")
        return None
    print(f"🗣️ TTS request: '{cleaned[:80]}'")

    ssml = text_to_ssml(cleaned)

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            payload = {
                "input": {"ssml": ssml},
                "voice": {"languageCode": "en-IN", "name": GOOGLE_TTS_VOICE},
                "audioConfig": {
                    "audioEncoding": "LINEAR16",
                    "sampleRateHertz": 8000,                 # telephony wideband
                    "speakingRate": GOOGLE_TTS_RATE,         # slightly slower than normal
                    "pitch": GOOGLE_TTS_PITCH,               # a touch deeper
                    "effectsProfileId": ["telephony-class-application"]
                }
            }
            response = await client.post(GOOGLE_TTS_URL, json=payload)
            if response.status_code == 200:
                audio_b64 = response.json().get("audioContent", "")
                if audio_b64:
                    raw = base64.b64decode(audio_b64)
                    print(f"✅ TTS HTTP 200, bytes: {len(raw)}")
                    # Optional debug dump:
                    # with open("tts_output.wav", "wb") as f: f.write(raw)
                    return raw
                print("⚠️ TTS success but empty audioContent")
                return None
            else:
                print(f"⚠️ TTS HTTP {response.status_code}: {response.text[:200]}")
                return None
        except Exception as e:
            print(f"❌ TTS request failed: {e}")
            return None

async def send_audio_ultra_fast(audio_b64: str, retry_count: int = 3):
    """Send audio to Piopiy WebSocket using StreamAction with retries"""
    global piopiy_ws
    if not piopiy_ws:
        print("⚠️ No active Piopiy WebSocket connection to send audio")
        return

    for attempt in range(retry_count):
        try:
            action = StreamAction()
            await piopiy_ws.send_text(action.playStream(audio_base64=audio_b64, audio_type="raw", sample_rate=8000))
            print(f"📤 Sent audio chunk to Piopiy, length={len(audio_b64)} base64 chars, attempt={attempt+1}")
            return
        except Exception as e:
            print(f"❌ Error sending audio to Piopiy (attempt {attempt+1}/{retry_count}): {e}")
            if attempt < retry_count - 1:
                await asyncio.sleep(1)
            else:
                print("⚠️ Failed to send audio to Piopiy after retries")

async def trigger_call_hangup():
    """Trigger call hangup by closing WebSocket connection"""
    try:
        await log_call_message("system", "Triggering call hangup due to exit intent")
        global piopiy_ws
        if piopiy_ws:
            print("🛑 Closing Piopiy WebSocket connection to hangup call")
            await piopiy_ws.close()
            await log_call_message("system", "Piopiy WebSocket connection closed - call should end")
        else:
            print("⚠️ No active Piopiy WebSocket connection to close")
            await log_call_message("system", "No active Piopiy WebSocket connection found")
    except Exception as e:
        print(f"❌ Error triggering call hangup: {e}")
        await log_call_message("system", f"Call hangup error: {str(e)}")

# ---------------------------
# Workers
# ---------------------------
async def ultra_fast_tts_worker():
    """TTS processing worker (keeps voice slow & natural)"""
    while True:
        try:
            if not tts_q.empty():
                text = tts_q.get()
                if text is None:
                    print("🔇 TTS worker received None, stopping")
                    break
                print(f"🔔 TTS worker dequeued text: '{text[:80]}'")
                raw_audio = await ultra_fast_tts(text)
                if raw_audio:
                    # Already LINEAR16 @ 8kHz from Google; send as-is for best quality
                    audio_b64 = base64.b64encode(raw_audio).decode()
                    print(f"🎼 TTS bytes ready: {len(raw_audio)}")
                    await send_audio_ultra_fast(audio_b64)
                else:
                    print(f"⚠️ No audio produced by TTS for text: '{text[:80]}'")
            await asyncio.sleep(0.0005)
        except Exception as e:
            print(f"⚠️ TTS worker error: {e}")
            await asyncio.sleep(0.1)

async def ultra_fast_llm_worker():
    """LLM processing worker"""
    bot = RealEstateQA(ai_services)
    history = []
    session_started = False
    last_activity = datetime.now()
    last_transcription_time = datetime.now()

    # Initial greeting (will be spoken slowly via SSML settings)
    greeting = bot.get_greeting_message()
    await log_call_message("greeting", greeting)
    tts_q.put(greeting)
    print("📢 Sent initial greeting to start conversation")

    # Optional gentle prompt to confirm audio path
    await asyncio.sleep(4)
    test_message = "Can you hear me clearly? Please say how I can help you with your real estate needs."
    await log_call_message("bot", test_message)
    tts_q.put(test_message)
    print("📢 Sent test message to ensure audio output")

    while True:
        try:
            # Nudge if silent
            if session_started and (datetime.now() - last_transcription_time).total_seconds() > 15:
                prompt = "I haven't heard from you. Are you still there? Please tell me how I can assist you."
                await log_call_message("bot", prompt)
                tts_q.put(prompt)
                print("📢 Sent prompt to encourage user speech")
                last_transcription_time = datetime.now()

            # End if no activity
            if session_started and (datetime.now() - last_activity).total_seconds() > 30:
                print("⏰ No activity for 30 seconds, ending session")
                exit_message = bot.get_exit_message()
                await log_call_message("exit", exit_message)
                tts_q.put(exit_message)
                await asyncio.sleep(3)
                await trigger_call_hangup()
                session_started = False
                history = []
                continue

            if not transcript_q.empty():
                user_text = transcript_q.get()
                last_activity = datetime.now()
                last_transcription_time = datetime.now()

                if not session_started:
                    session_started = True
                    await log_call_message("system", f"Session started. First user input: {user_text}")

                await log_call_message("user", user_text)

                if bot.is_exit_intent(user_text):
                    exit_message = bot.get_exit_message()
                    await log_call_message("exit", exit_message)
                    tts_q.put(exit_message)
                    await asyncio.sleep(3)
                    await trigger_call_hangup()
                    session_started = False
                    history = []
                    continue

                try:
                    reply = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, bot.get_response, user_text, history),
                        timeout=10.0
                    )
                    await log_call_message("bot", reply)
                    tts_q.put(reply)
                    print(f"📢 Queued bot response for TTS: '{reply[:80]}'")
                except asyncio.TimeoutError:
                    print("⚠️ LLM response timeout, sending fallback")
                    reply = "I apologize, but I'm having trouble processing your request right now. Could you please try again?"
                    await log_call_message("bot", reply)
                    tts_q.put(reply)
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

# ---------------------------
# Deepgram WebSocket client
# ---------------------------
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
            # print(f"📥 Deepgram message: {json.dumps(data)[:200]}")
            if data.get("type") == "Results":
                alt = data.get("channel", {}).get("alternatives", [{}])[0]
                transcript = alt.get("transcript", "")
                confidence = alt.get("confidence", 0.0)
                is_final = (
                        bool(data.get("is_final")) or
                        bool(data.get("speech_final")) or
                        bool(data.get("final")) or
                        bool(data.get("channel", {}).get("is_final")) or
                        bool(alt.get("final"))
                )
                if transcript and is_final:
                    print(f"🎧 ASR: {transcript} (final, conf={confidence:.2f})")
                    transcript_q.put(transcript)
                # else: partials are ignored for now
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
            dg_ws_client = WebSocketApp(
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
        dg_ws_client = WebSocketApp(
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

# ---------------------------
# WebSocket endpoint
# ---------------------------
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket client handler"""
    global piopiy_ws, audio_buffer
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
        extracted_phone = query_params.get("phone_number", ["unknown"])[0] or query_params.get("phone", ["unknown"])[0]
        extracted_lead_id = query_params.get("lead_id", [None])[0]
        session_id = query_params.get("session", [None])[0] or query_params.get("sid", [None])[0]
        print(f"📋 Extracted from query params: phone={extracted_phone}, lead_id={extracted_lead_id}, session={session_id}")
    except Exception as e:
        print(f"⚠️ Error parsing query parameters: {e}")

    # Start call tracking
    await start_call_tracking(
        phone_number=extracted_phone,
        lead_id=extracted_lead_id,
        call_session_id=session_id
    )

    # Update call context from MongoDB
    if mongo_client and mongo_client.is_connected() and session_id:
        try:
            recent_call = mongo_client.calls.find_one({
                "call_session_id": session_id,
                "status": "initiated",
                "created_at": {"$gte": datetime.now() - timedelta(minutes=5)}
            }, sort=[("created_at", -1)])
            if recent_call:
                current_call_data["phone_number"] = recent_call.get("phone_number", extracted_phone)
                current_call_data["lead_id"] = recent_call.get("lead_id", extracted_lead_id)
                print(f"📋 Updated call context from MongoDB: phone={current_call_data['phone_number']}, lead_id={current_call_data['lead_id']}")
        except Exception as e:
            print(f"⚠️ Error updating call context from MongoDB: {e}")

    # Start workers
    tts_task = asyncio.create_task(ultra_fast_tts_worker())
    llm_task = asyncio.create_task(ultra_fast_llm_worker())

    # Start Deepgram if not already started
    if not dg_ws_client and DEEPGRAM_API_KEY:
        start_fast_deepgram()
        await asyncio.sleep(1)

    # Stream loop
    CHUNK_SIZE = 3200  # ~200 ms at 8kHz 16-bit mono
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
                        if message["text"].replace("-", "").isalnum() and len(message["text"]) == 36:
                            print(f"📋 Treating text as session_id: {message['text']}")
                            current_call_data["call_session_id"] = message["text"]
                            await log_call_message("system", f"Updated session_id: {message['text']}")
                            if mongo_client and mongo_client.is_connected():
                                try:
                                    recent_call = mongo_client.calls.find_one({
                                        "call_session_id": message["text"],
                                        "status": "initiated",
                                        "created_at": {"$gte": datetime.now() - timedelta(minutes=5)}
                                    }, sort=[("created_at", -1)])
                                    if recent_call:
                                        current_call_data["phone_number"] = recent_call.get("phone_number", "unknown")
                                        current_call_data["lead_id"] = recent_call.get("lead_id")
                                        print(f"📋 Matched recent call: phone={current_call_data['phone_number']}, lead_id={current_call_data['lead_id']}")
                                except Exception as e:
                                    print(f"⚠️ Error finding recent call by session_id: {e}")
                            continue
                        data = json.loads(message["text"])
                        print(f"📋 Parsed JSON: {data}")
                        extra_params = data.get("extra_params")
                        if extra_params:
                            phone = extra_params.get("phone_number") or extra_params.get("phone")
                            lead_id = extra_params.get("lead_id")
                            sess = extra_params.get("session") or extra_params.get("sid")
                            if phone:
                                current_call_data["phone_number"] = str(phone)
                            if lead_id:
                                current_call_data["lead_id"] = str(lead_id)
                            if sess:
                                current_call_data["call_session_id"] = str(sess)
                            await log_call_message("system", f"Call context: phone={phone}, lead_id={lead_id}, session={sess}")
                        meta = data.get("meta", data)
                        if isinstance(meta, dict):
                            phone = meta.get("phone_number") or meta.get("phone")
                            lead_id = meta.get("lead_id")
                            sess = meta.get("session") or meta.get("sid") or session_id
                            if phone:
                                current_call_data["phone_number"] = str(phone)
                            if lead_id:
                                current_call_data["lead_id"] = str(lead_id)
                            if sess:
                                current_call_data["call_session_id"] = str(sess)
                            await log_call_message("system", f"Call context from meta: phone={phone}, lead_id={lead_id}, session={sess}")
                    except json.JSONDecodeError as e:
                        print(f"⚠️ Non-JSON text message received: {message['text'][:200]}, error: {e}")
                    except Exception as e:
                        print(f"⚠️ Error processing text message: {e}, message: {message['text'][:200]}")
                elif "bytes" in message:
                    try:
                        # Incoming audio from caller (assumed PCM16, 8kHz, mono)
                        incoming = message["bytes"]
                        print(f"🎵 Received audio: {len(incoming)} bytes")
                        audio_buffer.extend(incoming)

                        # Stream to Deepgram in ~200 ms chunks for low latency & better ASR
                        while len(audio_buffer) >= CHUNK_SIZE:
                            chunk = audio_buffer[:CHUNK_SIZE]
                            del audio_buffer[:CHUNK_SIZE]
                            if dg_ws_client and dg_ws_client.sock and dg_ws_client.sock.connected:
                                try:
                                    dg_ws_client.send(chunk, opcode=ABNF.OPCODE_BINARY)
                                    # print(f"📤 Sent {len(chunk)} bytes to Deepgram")
                                except Exception as e:
                                    print(f"⚠️ Failed to send chunk to Deepgram: {e}")
                                    break
                            else:
                                print("⚠️ Deepgram WS not connected; dropping chunk")
                                audio_buffer.clear()
                                break
                    except Exception as e:
                        print(f"⚠️ Error processing audio data: {e}")
                        audio_buffer.clear()
    except Exception as e:
        print(f"❌ WebSocket connection error: {e}")
    finally:
        # Flush any remaining audio once (best-effort) on disconnect
        try:
            if audio_buffer and dg_ws_client and dg_ws_client.sock and dg_ws_client.sock.connected:
                dg_ws_client.send(bytes(audio_buffer), opcode=ABNF.OPCODE_BINARY)
                print(f"📤 Flushed {len(audio_buffer)} bytes to Deepgram on close")
        except Exception as e:
            print(f"⚠️ Flush error: {e}")

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
        audio_buffer.clear()

# Log API key presence at startup
print(f"[WebSocket Server] 🔧 GOOGLE_API_KEY present: {'Yes' if GOOGLE_API_KEY else 'No'}")
print(f"[WebSocket Server] 🔧 DG_API_KEY present: {'Yes' if DEEPGRAM_API_KEY else 'No'}")
print("Call tracking enabled with MongoDB")
