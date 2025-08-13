# """
# ULTRA-FAST Voice Bot - Simple Logging
# ----------------------------------------
# • Shows user transcription
# • Shows Groq replies
# • Maximum speed optimization
# """
# import asyncio, base64, json, os, threading, time
# from queue import SimpleQueue
# import numpy as np
# import scipy.signal as sps
# import httpx
# from datetime import datetime
#
# import websockets
# import websocket
# from piopiy import StreamAction
#
# # ─── Project helpers ────────────────────────────────────────────────────────
# from qa_engine import RealEstateQA
# from ai_services import AIServices
#
# # Call logging and tracking
# from mongo_client import mongo_client
# from calls_api import log_call
# import httpx
#
# # Global call tracking
# current_call_data = {
#     "phone_number": None,
#     "lead_id": None,
#     "transcription": [],
#     "ai_responses": [],
#     "start_time": None,
#     "end_time": None
# }
#
# async def log_call_message(message_type, content, phone_number=None, lead_id=None):
#     """Log call message and track conversation"""
#     try:
#         # Print to terminal for immediate feedback
#         timestamp = datetime.now().strftime("%H:%M:%S")
#
#         if message_type == "user":
#             print(f" [{timestamp}] User: {content}")
#             # Add to transcription
#             current_call_data["transcription"].append({
#                 "type": "user",
#                 "content": content,
#                 "timestamp": datetime.now().isoformat()
#             })
#         elif message_type == "bot":
#             print(f"[{timestamp}] Bot: {content}")
#             # Add to AI responses
#             current_call_data["ai_responses"].append({
#                 "type": "bot",
#                 "content": content,
#                 "timestamp": datetime.now().isoformat()
#             })
#         elif message_type == "greeting":
#             print(f"[{timestamp}] Greeting: {content}")
#             # Add to AI responses
#             current_call_data["ai_responses"].append({
#                 "type": "greeting",
#                 "content": content,
#                 "timestamp": datetime.now().isoformat()
#             })
#         elif message_type == "exit":
#             print(f" [{timestamp}] Exit: {content}")
#             # Add to AI responses
#             current_call_data["ai_responses"].append({
#                 "type": "exit",
#                 "content": content,
#                 "timestamp": datetime.now().isoformat()
#             })
#         elif message_type == "system":
#             print(f"  [{timestamp}] System: {content}")
#
#         # Update call data
#         if phone_number:
#             current_call_data["phone_number"] = phone_number
#         if lead_id:
#             current_call_data["lead_id"] = lead_id
#
#     except Exception as e:
#         print(f" Call logging error: {e}")
#
# async def start_call_tracking(phone_number, lead_id=None, call_session_id: str | None = None):
#     """Start tracking a new call"""
#     global current_call_data
#     current_call_data = {
#         "phone_number": phone_number,
#         "lead_id": lead_id,
#         "transcription": [],
#         "ai_responses": [],
#         "start_time": datetime.now(),
#         "end_time": None,
#         "call_session_id": call_session_id or None
#     }
#     print(f" Started tracking call for {phone_number}")
#
# async def end_call_tracking():
#     """End call tracking and save to MongoDB"""
#     global current_call_data
#
#     # Only proceed if we have meaningful conversation data or proper context
#     if not current_call_data["phone_number"] and not current_call_data["transcription"] and not current_call_data["ai_responses"]:
#         print(" No meaningful call data to save")
#         return
#
#     # Skip logging if this is just an "unknown" phone with no conversation
#     if current_call_data["phone_number"] == "unknown" and not current_call_data["transcription"] and not current_call_data["ai_responses"]:
#         print(" Skipping log for unknown phone with no conversation")
#         return
#
#     try:
#         current_call_data["end_time"] = datetime.now()
#
#         # Calculate duration
#         duration = 0
#         if current_call_data["start_time"] and current_call_data["end_time"]:
#             duration = (current_call_data["end_time"] - current_call_data["start_time"]).total_seconds()
#
#         # Use actual phone number if available, otherwise keep as "unknown"
#         phone_to_log = current_call_data["phone_number"] or "unknown"
#
#         # Analyze conversation interest using LLM
#         interest_analysis = None
#         if current_call_data["transcription"] and current_call_data["ai_responses"]:
#             try:
#                 print(" Starting interest analysis...")
#                 # Create a temporary QA instance for analysis
#                 from qa_engine import RealEstateQA
#                 bot = RealEstateQA(ai_services)
#                 interest_analysis = bot.analyze_conversation_interest(
#                     current_call_data["transcription"],
#                     current_call_data["ai_responses"]
#                 )
#                 print(f"Interest analysis completed: {interest_analysis['interest_status']} ({interest_analysis['confidence']:.2f})")
#             except Exception as e:
#                 print(f" Interest analysis failed: {e}")
#                 # Provide fallback analysis
#                 interest_analysis = {
#                     "interest_status": "neutral",
#                     "confidence": 0.5,
#                     "reasoning": f"Analysis failed: {str(e)}",
#                     "key_indicators": []
#                 }
#
#         # Prepare call data for MongoDB
#         call_data = {
#             "duration": duration,
#             "transcription": current_call_data["transcription"],
#             "ai_responses": current_call_data["ai_responses"],
#             "summary": f"Call with {len(current_call_data['transcription'])} user messages and {len(current_call_data['ai_responses'])} AI responses",
#             "sentiment": "neutral",  # Could be enhanced with sentiment analysis
#             "interest_analysis": interest_analysis,  # Add interest analysis
#             "call_session_id": current_call_data.get("call_session_id"),
#             "status": "completed"  # Mark as completed
#         }
#
#         # Try to update existing call record if we have meaningful conversation data
#         if current_call_data["transcription"] or current_call_data["ai_responses"]:
#             # First, try to update via session_id if we have it
#             if current_call_data.get("call_session_id"):
#                 result = log_call(phone_to_log, current_call_data["lead_id"], call_data)
#                 if result["success"]:
#                     print(f" Call logged to MongoDB: {phone_to_log} (session: {current_call_data.get('call_session_id')})")
#                     if result.get("note") == "updated_existing_by_session":
#                         print(" Updated existing call record via session_id")
#                 else:
#                     print(f"Failed to log call: {result.get('error', 'Unknown error')}")
#
#             # If no session_id, try to find and update recent "initiated" call by phone number or lead_id
#             elif phone_to_log != "unknown" and mongo_client and mongo_client.is_connected():
#                 try:
#                     from datetime import timedelta
#                     five_minutes_ago = datetime.now() - timedelta(minutes=5)
#
#                     # Build query to find recent initiated call
#                     query = {
#                         "status": "initiated",
#                         "created_at": {"$gte": five_minutes_ago}
#                     }
#
#                     # Prefer matching by lead_id if available, otherwise by phone
#                     if current_call_data["lead_id"]:
#                         query["lead_id"] = current_call_data["lead_id"]
#                     else:
#                         query["phone_number"] = phone_to_log
#
#                     recent_call = mongo_client.calls.find_one(query, sort=[("created_at", -1)])
#
#                     if recent_call:
#                         # Update the existing initiated call record
#                         mongo_client.calls.update_one(
#                             {"_id": recent_call["_id"]},
#                             {"$set": {
#                                 "status": "completed",
#                                 "duration": call_data["duration"],
#                                 "transcription": call_data["transcription"],
#                                 "ai_responses": call_data["ai_responses"],
#                                 "call_summary": call_data["summary"],
#                                 "sentiment": call_data["sentiment"],
#                                 "interest_analysis": call_data["interest_analysis"],
#                                 "updated_at": datetime.now()
#                             }}
#                         )
#                         print(f"Updated existing initiated call record for {phone_to_log or current_call_data['lead_id']}")
#
#                         # Update lead status based on the completed call
#                         from calls_api import update_lead_status_from_call
#                         update_lead_status_from_call(phone_to_log, current_call_data["lead_id"], call_data)
#                     else:
#                         # No recent initiated call found, create new record
#                         result = log_call(phone_to_log, current_call_data["lead_id"], call_data)
#                         print(f" Created new call record for {phone_to_log}")
#                 except Exception as e:
#                     print(f"Error updating existing call: {e}")
#                     # Fallback to creating new record
#                     result = log_call(phone_to_log, current_call_data["lead_id"], call_data)
#
#             # Fallback: create new record if we have conversation data but no way to link
#             else:
#                 result = log_call(phone_to_log, current_call_data["lead_id"], call_data)
#                 print(f"Created fallback call record")
#         else:
#             print(" Skipping log - no conversation data to save")
#
#     except Exception as e:
#         print(f"Error ending call tracking: {e}")
#
#     # Reset call data
#     current_call_data = {
#         "phone_number": None,
#         "lead_id": None,
#         "transcription": [],
#         "ai_responses": [],
#         "start_time": None,
#         "end_time": None
#     }
#
# print("Call tracking enabled with MongoDB")
#
# # ─── Environment ────────────────────────────────────────────────────────────
# from dotenv import load_dotenv
# load_dotenv()
# GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# DEEPGRAM_API_KEY = os.getenv("DG_API_KEY")
#
# # ─── Constants ──────────────────────────────────────────────────────────────
# GOOGLE_TTS_URL = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
# DG_WS_URL = (
#     "wss://api.deepgram.com/v1/listen?"
#     "sample_rate=8000&encoding=linear16&model=nova-2"
#     "&language=en-IN&smart_format=true&vad_turnoff=1500"
# )
#
# # ─── Globals ────────────────────────────────────────────────────────────────
# transcript_q = SimpleQueue()
# tts_q = SimpleQueue()
# ai_services = AIServices()
# dg_ws_client = None
# piopiy_ws = None
# current_call_id = None  # Store current call ID for hangup
#
# async def trigger_call_hangup():
#     """Trigger call hangup by closing WebSocket connection"""
#     try:
#         await log_call_message("system", "Triggering call hangup due to exit intent")
#
#         # For streaming calls, we need to close the WebSocket connection
#         # This will cause Piopiy to hang up the call
#         global piopiy_ws
#         if piopiy_ws:
#             print("Closing WebSocket connection to hangup call")
#             await piopiy_ws.close()
#             await log_call_message("system", "WebSocket connection closed - call should end")
#         else:
#             print(" No active WebSocket connection to close")
#             await log_call_message("system", "No active WebSocket connection found")
#
#     except Exception as e:
#         print(f" Error triggering call hangup: {e}")
#         await log_call_message("system", f"Call hangup error: {str(e)}")
#
# # ─── Audio processing ───────────────────────────────────────────────────────
#
# def fast_audio_convert(raw_audio: bytes) -> bytes:
#     """Fastest possible audio conversion."""
#     samples = np.frombuffer(raw_audio, dtype=np.int16)
#     resampled = sps.resample_poly(samples, 8000, 22050)
#     normalized = np.clip(resampled * 0.8, -32767, 32767).astype(np.int16)
#     return normalized.tobytes()
#
# # ─── TTS ────────────────────────────────────────────────────────────────────
#
# async def ultra_fast_tts(text: str) -> bytes | None:
#     """Ultra-fast async TTS."""
#     if not text.strip():
#         print("TTS skipped: empty text")
#         return None
#     print(f"TTS request: '{text[:80]}'")
#
#     async with httpx.AsyncClient(timeout=10) as client:
#         try:
#             response = await client.post(GOOGLE_TTS_URL, json={
#                 "input": {"text": text},
#                 "voice": {"languageCode": "en-US", "name": "en-US-Standard-D", "ssmlGender": "MALE"},
#                 "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 22050, "speakingRate": 1.15}
#             })
#             if response.status_code == 200:
#                 audio_b64 = response.json().get("audioContent", "")
#                 if not audio_b64:
#                     print("TTS success but empty audioContent")
#                     return None
#                 raw = base64.b64decode(audio_b64)
#                 print(f"TTS HTTP 200, bytes: {len(raw)}")
#                 return raw
#             else:
#                 try:
#                     body = response.text[:200]
#                 except Exception:
#                     body = "<unreadable body>"
#                 print(f"TTS HTTP {response.status_code}: {body}")
#         except Exception as e:
#             print(f"TTS request failed: {e}")
#     return None
#
# async def send_audio_ultra_fast(audio_b64: str):
#     """Ultra-fast audio sending."""
#     global piopiy_ws
#     if piopiy_ws:
#         try:
#             action = StreamAction()
#             await piopiy_ws.send(action.playStream(audio_base64=audio_b64, audio_type="raw", sample_rate=8000))
#             print(f"Sent audio chunk, length={len(audio_b64)} base64 chars")
#         except Exception as e:
#             print(f"Error sending audio: {e}")
#     else:
#         print("No active WS when trying to send audio")
#
# # ─── Workers ────────────────────────────────────────────────────────────────
# async def ultra_fast_tts_worker():
#     """TTS processing worker."""
#     while True:
#         try:
#             if not tts_q.empty():
#                 text = tts_q.get()
#                 if text is None:
#                     break
#                 print(f"TTS worker dequeued text: '{text[:80]}'")
#
#                 raw_audio = await ultra_fast_tts(text)
#                 if raw_audio:
#                     processed = await asyncio.get_event_loop().run_in_executor(None, fast_audio_convert, raw_audio)
#                     audio_b64 = base64.b64encode(processed).decode()
#                     print(f" Processed audio bytes: in={len(raw_audio)} -> out={len(processed)}")
#                     await send_audio_ultra_fast(audio_b64)
#                 else:
#                     print(" No audio produced by TTS")
#
#             await asyncio.sleep(0.0005)
#         except Exception as e:
#             print(f"TTS worker error: {e}")
#             await asyncio.sleep(0.1)  # Brief pause on error
#
# async def ultra_fast_llm_worker():
#     """LLM processing worker."""
#     bot = RealEstateQA(ai_services)
#     history = []
#     session_started = False
#     has_sent_greeting = False
#
#     while True:
#         try:
#             if not transcript_q.empty():
#                 user_text = transcript_q.get()
#
#                 # Mark session started on first user input (do not skip processing)
#                 if not session_started:
#                     session_started = True
#                     await log_call_message("system", f"Session started. First user input: {user_text}")
#
#                 # Log user transcription FIRST (before any processing)
#                 await log_call_message("user", user_text)
#
#                 # If we haven't greeted yet, send the configured greeting (from agent_config) and skip LLM
#                 if not has_sent_greeting:
#                     try:
#                         greeting = bot.get_greeting_message()
#                         await log_call_message("greeting", greeting)
#                         tts_q.put(greeting)
#                         has_sent_greeting = True
#                         # Do not call LLM on the same turn to avoid duplicate greeting from model
#                         await asyncio.sleep(0)  # yield
#                         continue
#                     except Exception as e:
#                         print(f" Failed to send configured greeting: {e}")
#
#                 # Check for exit intent
#                 if bot.is_exit_intent(user_text):
#                     exit_message = bot.get_exit_message()
#                     await log_call_message("exit", exit_message)
#                     tts_q.put(exit_message)
#
#                     # End call tracking
#                     # await end_call_tracking()
#
#                     # Trigger call hangup after exit message
#                     await asyncio.sleep(3)  # Wait for exit message to be played
#                     await trigger_call_hangup()
#
#                     # Reset session
#                     session_started = False
#                     history = []
#                     has_sent_greeting = False
#                     continue
#
#                 # Generate response with timeout
#                 try:
#                     reply = await asyncio.wait_for(
#                         asyncio.get_event_loop().run_in_executor(None, bot.get_response, user_text, history),
#                         timeout=10.0  # 10 second timeout
#                     )
#
#                     # Log bot response
#                     await log_call_message("bot", reply)
#                 except asyncio.TimeoutError:
#                     print(" LLM response timeout, sending fallback")
#                     reply = "I apologize, but I'm having trouble processing your request right now. Could you please try again?"
#                     await log_call_message("bot", reply)
#                 except Exception as e:
#                     print(f" Error generating response: {e}")
#                     print(f" Error type: {type(e).__name__}")
#                     print(f" Full error details: {str(e)}")
#
#                     # Check if it's an API key issue
#                     if "api_key" in str(e).lower() or "authentication" in str(e).lower():
#                         reply = "I'm sorry, there's an authentication issue with my AI service. Please check the API configuration."
#                     elif "connection" in str(e).lower() or "timeout" in str(e).lower():
#                         reply = "I'm sorry, I'm having trouble connecting to my AI service. Please try again."
#                     else:
#                         reply = "I'm sorry, I encountered an error. Please try again."
#
#                     await log_call_message("bot", reply)
#
#                 # Queue TTS
#                 tts_q.put(reply)
#
#                 # Update history
#                 history.extend([
#                     {"role": "user", "content": user_text},
#                     {"role": "assistant", "content": reply}
#                 ])
#
#                 # Keep history manageable
#                 if len(history) > 8:
#                     history = history[-6:]
#
#             await asyncio.sleep(0.0001)  # Reduced sleep time for faster response
#         except Exception as e:
#             print(f"LLM worker error: {e}")
#             await asyncio.sleep(0.1)  # Brief pause on error
#
# # ─── Deepgram ───────────────────────────────────────────────────────────────
#
# def start_fast_deepgram():
#     """Minimal Deepgram client."""
#     global dg_ws_client
#
#     def on_open(ws):
#         def keep_alive():
#             while ws.sock and ws.sock.connected:
#                 try:
#                     ws.send('{"type":"KeepAlive"}')
#                     time.sleep(8)
#                 except:
#                     break
#         threading.Thread(target=keep_alive, daemon=True).start()
#
#     def on_message(ws, message):
#         try:
#             data = json.loads(message)
#             if data.get("type") == "Results":
#                 transcript = data.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
#                 # Robust final detection across Deepgram variants
#                 is_final = (
#                     bool(data.get("is_final"))
#                     or bool(data.get("speech_final"))
#                     or bool(data.get("final"))
#                     or bool(data.get("channel", {}).get("is_final"))
#                     or bool(data.get("channel", {}).get("alternatives", [{}])[0].get("final"))
#                 )
#                 if transcript:
#                     # Print ASR transcript for debugging
#                     print(f"ASR: {transcript}{' (final)' if is_final else ' (partial)'}")
#                 # Only enqueue final transcripts to reduce partials
#                 if transcript and is_final:
#                     transcript_q.put(transcript)
#         except Exception as e:
#             print(f" Deepgram message parse error: {e}")
#
#     def on_error(ws, error):
#         print(f" Deepgram WS error: {error}")
#
#     def on_close(ws, close_status_code, close_msg):
#         print(f"Deepgram WS closed: {close_status_code} {close_msg}")
#
#     headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
#     dg_ws_client = websocket.WebSocketApp(
#         DG_WS_URL,
#         header=headers,
#         on_open=on_open,
#         on_message=on_message,
#         on_close=on_close,
#         on_error=on_error
#     )
#     threading.Thread(target=dg_ws_client.run_forever, daemon=True).start()
#
# # ─── WebSocket handler ──────────────────────────────────────────────────────
#
# async def ultra_fast_client_handler(client_ws):
#     """Ultra-fast client handler."""
#     global piopiy_ws
#     piopiy_ws = client_ws
#
#     print(f" WebSocket client connected from: {client_ws.remote_address}")
#
#     # Extract session info from connection to link with existing call record
#     session_id = None
#     extracted_phone = None
#     extracted_lead_id = None
#
#     try:
#         # Extract from path/query params if present
#         path = getattr(client_ws, "path", "") or ""
#         if "?" in path:
#             from urllib.parse import parse_qs, urlparse
#             qs = parse_qs(urlparse(path).query)
#             session_id = (qs.get("session") or qs.get("sid") or [None])[0]
#             extracted_phone = (qs.get("phone_number") or qs.get("phone") or [None])[0]
#             extracted_lead_id = (qs.get("lead_id") or [None])[0]
#     except Exception as e:
#         print(f" Failed to extract connection params: {e}")
#         pass
#
#     # If we have a session_id, DON'T create a new call record - it should already exist
#     if session_id:
#         print(f" WebSocket connected with session_id: {session_id}")
#         # Set up tracking to update existing record
#         await start_call_tracking(
#             phone_number=extracted_phone or "unknown",
#             lead_id=extracted_lead_id,
#             call_session_id=session_id
#         )
#     else:
#         print(" WebSocket connected without session_id")
#         # Try to find recent initiated call to get context
#         recent_phone = "unknown"
#         recent_lead_id = None
#
#         if mongo_client and mongo_client.is_connected():
#             try:
#                 from datetime import timedelta
#                 two_minutes_ago = datetime.now() - timedelta(minutes=2)
#                 recent_call = mongo_client.calls.find_one({
#                     "status": "initiated",
#                     "created_at": {"$gte": two_minutes_ago}
#                 }, sort=[("created_at", -1)])
#
#                 if recent_call:
#                     recent_phone = recent_call.get("phone_number", "unknown")
#                     recent_lead_id = recent_call.get("lead_id")
#                     print(f" Found recent initiated call: phone={recent_phone}, lead={recent_lead_id}")
#             except Exception as e:
#                 print(f"Error finding recent call: {e}")
#
#         # Create new tracking with context from recent call if available
#         await start_call_tracking(phone_number=recent_phone, lead_id=recent_lead_id, call_session_id=None)
#
#     # Start workers
#     tts_task = asyncio.create_task(ultra_fast_tts_worker())
#     llm_task = asyncio.create_task(ultra_fast_llm_worker())
#
#     # Removed proactive greeting to avoid double greeting and to wait for user speech
#
#     try:
#         async for message in client_ws:
#             if isinstance(message, bytes):
#                 if dg_ws_client and dg_ws_client.sock and dg_ws_client.sock.connected:
#                     dg_ws_client.send(message, websocket.ABNF.OPCODE_BINARY)
#             else:
#                 # Try to parse text frames for control/metadata
#                 try:
#                     print(f"Received text message: {message[:200]}...")  # Log first 200 chars
#                     data = json.loads(message)
#                     print(f"Parsed JSON: {data}")
#
#                     # Accept either a wrapper or direct fields
#                     meta = data.get("meta") if isinstance(data, dict) else None
#                     if not meta and isinstance(data, dict):
#                         meta = data
#
#                     # Check for extra_params from Piopiy
#                     extra_params = data.get("extra_params")
#                     if extra_params:
#                         print(f" Found extra_params: {extra_params}")
#                         phone = extra_params.get("phone_number")
#                         lead_id = extra_params.get("lead_id")
#                         sess = extra_params.get("session")
#
#                         if phone:
#                             current_call_data["phone_number"] = str(phone)
#                         if lead_id:
#                             current_call_data["lead_id"] = str(lead_id)
#                         if sess:
#                             current_call_data["call_session_id"] = str(sess)
#                         await log_call_message("system", f"Call context from extra_params: phone={phone}, lead_id={lead_id}, session={sess}")
#
#                     # Also check meta format
#                     if isinstance(meta, dict):
#                         phone = meta.get("phone_number") or meta.get("phone")
#                         lead_id = meta.get("lead_id")
#                         sess = meta.get("session") or meta.get("sid") or meta.get("call_session_id") or session_id
#
#                         # Update call context with proper phone/lead info
#                         if phone:
#                             current_call_data["phone_number"] = str(phone)
#                         if lead_id:
#                             current_call_data["lead_id"] = str(lead_id)
#                         if sess:
#                             current_call_data["call_session_id"] = str(sess)
#                         await log_call_message("system", f"Call context from meta: phone={phone}, lead_id={lead_id}, session={sess}")
#
#                 except Exception as e:
#                     # Log but don't fail on non-JSON text frames
#                     print(f"Failed to parse text frame: {e}")
#                     pass
#     except Exception as e:
#         print(f" WebSocket connection error: {e}")
#     finally:
#         print(" WebSocket client disconnected - ending call tracking")
#         # Always end call tracking to save any conversation data
#         await end_call_tracking()
#
#         tts_task.cancel()
#         llm_task.cancel()
#         try:
#             await tts_task
#             await llm_task
#         except asyncio.CancelledError:
#             pass
#         piopiy_ws = None
#
# # ─── Main ───────────────────────────────────────────────────────────────────
#
# async def ultra_fast_main():
#     """Ultra-fast main entry point."""
#     start_fast_deepgram()
#     await asyncio.sleep(1)
#
#     server = await websockets.serve(ultra_fast_client_handler, "localhost", 8765)
#     print("Voice Bot running at ws://localhost:8765")
#     await server.wait_closed()
#
# if __name__ == "__main__":
#     try:
#         asyncio.run(ultra_fast_main())
#     except KeyboardInterrupt:
#         print("\n erver stopped.")
#
# # At startup, log presence of API keys
# print(f"[WebSocket Server] GOOGLE_API_KEY present: {'Yes' if os.getenv('GOOGLE_API_KEY') else 'No'}")
# print(f"[WebSocket Server] DG_API_KEY present: {'Yes' if os.getenv('DG_API_KEY') else 'No'}")
