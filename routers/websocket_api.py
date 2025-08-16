"""
Ultra-Fast Voice Bot WebSocket Router
Handles real-time voice bot functionality with Deepgram, Google TTS, and AI processing
(Optimized for proper question-answer sync, faster response, and natural voice)
"""
from fastapi import APIRouter, WebSocket
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
import numpy as np
import scipy.signal as sps
import scipy.io.wavfile

# Import project dependencies
from mongo_client import mongo_client
from routers.calls_api import log_call, update_lead_status_from_call
from qa_engine import RealEstateQA
from ai_services import AIServices

router = APIRouter(tags=["WebSocket"])

# ---------------------------
# Environment variables
# ---------------------------
from dotenv import load_dotenv
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DG_API_KEY")

# Voice tuning (slower & more natural)
GOOGLE_TTS_VOICE = os.getenv("GOOGLE_TTS_VOICE", "en-IN-Neural2-A")
GOOGLE_TTS_RATE = float(os.getenv("GOOGLE_TTS_RATE", "0.88"))          # 0.88 = slightly slower than natural
GOOGLE_TTS_PITCH = float(os.getenv("GOOGLE_TTS_PITCH", "-2.0"))        # Deeper, relaxed tone

# Turn-taking controls
LISTEN_HOLD_MS = int(os.getenv("LISTEN_HOLD_MS", "500"))               # Reduced to 500ms for faster response
BOT_SPEAKING_PAD_MS = int(os.getenv("BOT_SPEAKING_PAD_MS", "150"))     # Small pad after TTS
NUDGE_AFTER_SILENCE_S = int(os.getenv("NUDGE_AFTER_SILENCE_S", "15"))  # Nudge after 15s silence
END_AFTER_IDLE_S = int(os.getenv("END_AFTER_IDLE_S", "60"))            # End call after 60s idle

# Audio buffer size for Deepgram
AUDIO_BUFFER_SIZE = 8000  # ~500ms at 8kHz 16-bit mono

# ---------------------------
# Constants
# ---------------------------
GOOGLE_TTS_URL = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
DG_WS_URL = (
    "wss://api.deepgram.com/v1/listen?"
    "sample_rate=8000&encoding=linear16&model=nova-2"
    "&language=en-IN&smart_format=true&vad_turnoff=1000&no_delay=true"
)

# ---------------------------
# Globals
# ---------------------------
transcript_q = SimpleQueue()  # Final utterances from ASR
tts_q = SimpleQueue()         # Bot texts awaiting TTS
ai_services = AIServices()
dg_ws_client = None
piopiy_ws = None
audio_buffer = bytearray()    # Incoming caller audio
transcription_buffer = []     # Buffered transcriptions for processing

# Bot speaking window: ignore caller audio during this time (no barge-in)
bot_speaking_until: Optional[datetime] = None

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

# ===========================
# Utilities
# ===========================
def _escape_ssml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")

def text_to_ssml(text: str) -> str:
    """
    Convert raw text into SSML with natural sentence breaks
    and slightly slower, deeper prosody for human-like telephony.
    """
    text = (text or "").strip()
    if not text:
        return "<speak></speak>"

    parts = re.split(r'(?<=[.!?])\s+', text)
    segments = []
    for i, p in enumerate(parts):
        if not p:
            continue
        p_esc = _escape_ssml(p)
        pause = "350ms" if i == 0 else "300ms"
        segments.append(f"<s>{p_esc}</s><break time='{pause}'/>")

    body = " ".join(segments)
    return (
        "<speak>"
        f"<prosody rate='slow' pitch='{GOOGLE_TTS_PITCH:+.1f}st'>{body}</prosody>"
        "</speak>"
    )

def now() -> datetime:
    return datetime.now()

def set_bot_speaking_for_seconds(seconds: float):
    """Mark bot as speaking for a computed duration (+ small pad)."""
    global bot_speaking_until
    pad = BOT_SPEAKING_PAD_MS / 1000.0
    bot_speaking_until = now() + timedelta(seconds=max(0.0, seconds) + pad)
    print(f"⏳ Bot speaking window set for {seconds + pad:.2f}s")

def bot_is_speaking() -> bool:
    return bool(bot_speaking_until and now() < bot_speaking_until)

def fast_audio_convert(raw_audio: bytes) -> bytes:
    """Convert audio to Deepgram-compatible format with noise filtering"""
    try:
        samples = np.frombuffer(raw_audio, dtype=np.int16)
        b, a = sps.butter(4, 100.0 / (8000 / 2), btype='high', analog=False)
        filtered = sps.filtfilt(b, a, samples)
        normalized = np.clip(filtered * 0.8, -32767, 32767).astype(np.int16)
        scipy.io.wavfile.write(f"input_audio_sample_{int(time.time())}.wav", 8000, normalized)
        print(f"🎵 Saved audio sample to input_audio_sample_{int(time.time())}.wav")
        return normalized.tobytes()
    except Exception as e:
        print(f"⚠️ Audio conversion error: {e}")
        return raw_audio

# ===========================
# Logging & Call Tracking
# ===========================
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

# ===========================
# TTS (slow, SSML) + sender
# ===========================
async def ultra_fast_tts(text: str) -> Optional[bytes]:
    """Async TTS using Google TTS with SSML prosody for slower, human-like delivery"""
    cleaned = (text or "").strip()
    if not cleaned:
        print("🔇 TTS skipped: empty text")
        return None
    print(f"🗣️ TTS request: '{cleaned[:80]}'")

    ssml = text_to_ssml(cleaned)

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            payload = {
                "input": {"ssml": ssml},
                "voice": {"languageCode": "en-IN", "name": GOOGLE_TTS_VOICE},
                "audioConfig": {
                    "audioEncoding": "LINEAR16",
                    "sampleRateHertz": 8000,
                    "speakingRate": GOOGLE_TTS_RATE,
                    "pitch": GOOGLE_TTS_PITCH,
                    "effectsProfileId": ["telephony-class-application"]
                }
            }
            response = await client.post(GOOGLE_TTS_URL, json=payload)
            if response.status_code == 200:
                audio_b64 = response.json().get("audioContent", "")
                if audio_b64:
                    raw = base64.b64decode(audio_b64)
                    scipy.io.wavfile.write(f"tts_output_{int(time.time())}.wav", 8000, np.frombuffer(raw, dtype=np.int16))
                    print(f"✅ TTS HTTP 200, bytes: {len(raw)}, saved to tts_output_{int(time.time())}.wav")
                    return raw
                print("⚠️ TTS success but empty audioContent")
                return None
            else:
                print(f"⚠️ TTS HTTP {response.status_code}: {response.text[:200]}")
                return None
        except Exception as e:
            print(f"❌ TTS request failed: {e}")
            return None

async def send_audio_ultra_fast(audio_b64: str, raw_len_bytes: int):
    """Send audio to Piopiy WebSocket using StreamAction and set speaking window"""
    global piopiy_ws
    if not piopiy_ws:
        print("⚠️ No active Piopiy WebSocket connection to send audio")
        return

    bytes_per_second = 8000 * 2
    duration_s = raw_len_bytes / float(bytes_per_second)
    set_bot_speaking_for_seconds(duration_s)

    for attempt in range(3):
        try:
            action = StreamAction()
            await piopiy_ws.send_text(action.playStream(audio_base64=audio_b64, audio_type="raw", sample_rate=8000))
            print(f"📤 Sent audio chunk to Piopiy (≈{duration_s:.2f}s, {len(audio_b64)} b64 chars, attempt={attempt+1})")
            return
        except Exception as e:
            print(f"❌ Error sending audio to Piopiy (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(0.5)
            else:
                print("⚠️ Failed to send audio to Piopiy after retries")

async def trigger_call_hangup():
    """Trigger call hangup by closing WebSocket connection"""
    try:
        await log_call_message("system", "Triggering call hangup due to exit intent")
        global piopiy_ws
        if piopiy_ws:
            print("🛑 Closing Piopiy WebSocket connection")
            await piopiy_ws.close()
            await log_call_message("system", "Piopiy WebSocket closed - call should end")
        else:
            print("⚠️ No active Piopiy WebSocket connection")
            await log_call_message("system", "No active Piopiy WebSocket found")
    except Exception as e:
        print(f"❌ Error triggering call hangup: {e}")
        await log_call_message("system", f"Call hangup error: {str(e)}")

# ===========================
# Workers
# ===========================
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
                    audio_b64 = base64.b64encode(raw_audio).decode()
                    print(f"🎼 TTS bytes ready: {len(raw_audio)}")
                    await send_audio_ultra_fast(audio_b64, raw_len_bytes=len(raw_audio))
                else:
                    print(f"⚠️ No audio produced by TTS for text: '{text[:80]}'")
            await asyncio.sleep(0.001)  # Reduced sleep for faster processing
        except Exception as e:
            print(f"⚠️ TTS worker error: {e}")
            await asyncio.sleep(0.1)

async def ultra_fast_llm_worker():
    """
    LLM worker with improved turn-taking and sync:
    - Waits briefly after user speech to catch add-ons.
    - Ignores user inputs during bot speech to prevent overlap.
    - Processes transcriptions in order to maintain question-answer sync.
    """
    bot = RealEstateQA(ai_services)
    history = []
    session_started = False
    last_activity = now()
    last_transcription_time = now()

    greeting = bot.get_greeting_message()
    await log_call_message("greeting", greeting)
    tts_q.put(greeting)
    print("📢 Sent initial greeting")

    while True:
        try:
            if (now() - last_transcription_time).total_seconds() > NUDGE_AFTER_SILENCE_S and not bot_is_speaking():
                prompt = "Are you still there? How can I assist you with real estate today?"
                await log_call_message("bot", prompt)
                tts_q.put(prompt)
                print("📢 Sent prompt to encourage user speech")
                last_transcription_time = now()

            if (now() - last_activity).total_seconds() > END_AFTER_IDLE_S and session_started:
                print("⏰ No activity for too long, ending session")
                exit_message = bot.get_exit_message()
                await log_call_message("exit", exit_message)
                tts_q.put(exit_message)
                await asyncio.sleep(3)
                await trigger_call_hangup()
                session_started = False
                history = []
                continue

            if not transcript_q.empty():
                if bot_is_speaking():
                    print("🔇 Deferring transcription processing while bot is speaking")
                    await asyncio.sleep(0.1)
                    continue

                first = transcript_q.get()
                transcription_buffer.append((first, now()))
                hold_until = now() + timedelta(milliseconds=LISTEN_HOLD_MS)

                while now() < hold_until:
                    if not transcript_q.empty():
                        transcription_buffer.append((transcript_q.get(), now()))
                        hold_until = now() + timedelta(milliseconds=LISTEN_HOLD_MS // 2)
                    await asyncio.sleep(0.01)

                user_text = " ".join(t[0].strip() for t in transcription_buffer if t[0] and t[0].strip())
                transcription_buffer.clear()
                if not user_text:
                    continue

                last_activity = now()
                last_transcription_time = now()
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
                    start_time = now()
                    reply = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, bot.get_response, user_text, history),
                        timeout=12.0
                    )
                    response_time = (now() - start_time).total_seconds()
                    print(f"⏱️ LLM response generated in {response_time:.2f}s")
                    await log_call_message("bot", reply)
                    tts_q.put(reply)
                    print(f"📢 Queued bot response: '{reply[:80]}'")
                except asyncio.TimeoutError:
                    print("⚠️ LLM response timeout, sending fallback")
                    reply = "I'm sorry, I'm taking a bit longer. Could you please repeat or clarify what you'd like help with?"
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

            await asyncio.sleep(0.001)  # Reduced sleep for faster processing
        except Exception as e:
            print(f"⚠️ LLM worker error: {e}")
            await asyncio.sleep(0.1)

# ===========================
# Deepgram WebSocket client
# ===========================
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
            print(f"📥 Deepgram message: {json.dumps(data)[:200]}")
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
                    if bot_is_speaking():
                        print(f"🔇 Ignored transcript while bot speaking: '{transcript}'")
                        return
                    print(f"🎧 ASR (final, confidence={confidence:.2f}): {transcript}")
                    transcript_q.put(transcript)
                    print(f"📢 Transcription queued: '{transcript[:80]}'")
                else:
                    print(f"⚠️ No valid transcript (transcript='{transcript}', is_final={is_final}, confidence={confidence:.2f})")
            else:
                print(f"ℹ️ Non-results Deepgram message: {data.get('type')}")
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

# ===========================
# WebSocket endpoint
# ===========================
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket client handler with improved question-answer sync"""
    global piopiy_ws, audio_buffer
    piopiy_ws = websocket

    await websocket.accept()
    ws_url = get_websocket_url()
    env_type = "Production (Render)" if os.getenv("RENDER_EXTERNAL_URL") else "Development"
    print(f"🔗 WebSocket available at: {ws_url} [{env_type}]")
    print(f"📞 WebSocket client connected from: {websocket.client}")

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

    await start_call_tracking(
        phone_number=extracted_phone,
        lead_id=extracted_lead_id,
        call_session_id=session_id
    )

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

    tts_task = asyncio.create_task(ultra_fast_tts_worker())
    llm_task = asyncio.create_task(ultra_fast_llm_worker())

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
                        incoming = message["bytes"]
                        print(f"🎵 Received audio data: {len(incoming)} bytes")
                        if bot_is_speaking():
                            print(f"🔇 Dropped {len(incoming)} bytes while bot speaking")
                            audio_buffer.clear()
                            continue

                        audio_buffer.extend(incoming)
                        while len(audio_buffer) >= AUDIO_BUFFER_SIZE:
                            chunk = audio_buffer[:AUDIO_BUFFER_SIZE]
                            del audio_buffer[:AUDIO_BUFFER_SIZE]
                            if dg_ws_client and dg_ws_client.sock and dg_ws_client.sock.connected:
                                try:
                                    processed_audio = await asyncio.get_event_loop().run_in_executor(None, fast_audio_convert, bytes(chunk))
                                    dg_ws_client.send(processed_audio, opcode=ABNF.OPCODE_BINARY)
                                    print(f"📤 Sent {len(processed_audio)} bytes of buffered audio to Deepgram")
                                except Exception as e:
                                    print(f"⚠️ Failed to send chunk to Deepgram: {e}")
                                    audio_buffer.clear()
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
        try:
            if audio_buffer and dg_ws_client and dg_ws_client.sock and dg_ws_client.sock.connected:
                processed_audio = await asyncio.get_event_loop().run_in_executor(None, fast_audio_convert, bytes(audio_buffer))
                dg_ws_client.send(processed_audio, opcode=ABNF.OPCODE_BINARY)
                print(f"📤 Flushed {len(processed_audio)} bytes to Deepgram on close")
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
        transcription_buffer.clear()

# ---------------------------
# Startup logs
# ---------------------------
print(f"[WebSocket Server] 🔧 GOOGLE_API_KEY present: {'Yes' if GOOGLE_API_KEY else 'No'}")
print(f"[WebSocket Server] 🔧 DG_API_KEY present: {'Yes' if DEEPGRAM_API_KEY else 'No'}")
print("📞 Call tracking enabled with MongoDB")