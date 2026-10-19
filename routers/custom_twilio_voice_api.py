"""Self-managed Twilio voice path: Media Streams + Deepgram + ElevenLabs."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
from datetime import datetime
from time import perf_counter
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from brain_service import brain_service
from auth_service import require_user
from conversation_memory import ConversationMemory
from logging_setup import bind_call, timed
from twilio_security import stream_token, valid_stream_token, verify_twilio_request
from routers.calls_api import log_call
from voice_provider_settings import provider_enabled, provider_snapshot
from routers.twilio_api import _build_conversation_twiml, _get_request_base_url

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/custom-voice", tags=["custom voice"])


def _public_base(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    scheme = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip()
    if host:
        return f"{scheme}://{host.split(',')[0].strip()}"
    return os.getenv("TWILIO_PUBLIC_BASE_URL", "").rstrip("/")


def _to_ws_url(http_url: str) -> str:
    base = http_url.rstrip("/")
    if base.startswith("https://"):
        return f"wss://{base.removeprefix('https://')}"
    if base.startswith("http://"):
        return f"ws://{base.removeprefix('http://')}"
    return base


def _deepgram_url() -> str:
    model = os.getenv("CUSTOM_DEEPGRAM_MODEL", "nova-3").strip() or "nova-3"
    return (
        "wss://api.deepgram.com/v1/listen?"
        f"model={model}&encoding=mulaw&sample_rate=8000&channels=1"
        "&interim_results=true&smart_format=true&punctuate=true"
        f"&endpointing={os.getenv('CUSTOM_ENDPOINTING_MS', '500')}"
        f"&utterance_end_ms={os.getenv('CUSTOM_UTTERANCE_END_MS', '1000')}&vad_events=true"
    )


def _elevenlabs_request() -> tuple[str, Dict[str, str], str]:
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip() or "eleven_flash_v2_5"
    if not api_key or not voice_id:
        raise RuntimeError("ElevenLabs voice is not configured")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=ulaw_8000"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/basic"}
    return url, headers, model_id


async def _elevenlabs_stream(text: str) -> AsyncIterator[bytes]:
    """Yield caller-ready mulaw/8k audio while ElevenLabs is still generating it."""
    url, headers, model_id = _elevenlabs_request()
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0)) as client:
        async with client.stream("POST", url, headers=headers, json={"text": text, "model_id": model_id}) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(1600):
                if chunk:
                    yield chunk


async def _elevenlabs_speech(text: str) -> bytes:
    return b"".join([chunk async for chunk in _elevenlabs_stream(text)])


# Greeting and filler audio never change, so synthesize each phrase once per process.
_cached_audio: Dict[str, bytes] = {}
_FILLERS = ("Sure, one moment.", "Okay, let me check.", "Got it, one second.")


def _spoken_reply(text: str) -> str:
    """Keep phone answers short: whole sentences up to CUSTOM_MAX_SPOKEN_WORDS (always at least one)."""
    try:
        limit = max(10, int(os.getenv("CUSTOM_MAX_SPOKEN_WORDS", "35")))
    except ValueError:
        limit = 35
    # Drop stock openers ("Sure!", "Great,") that make every answer sound the same.
    text = re.sub(r"^(?:(?:sure|great|absolutely|of course|certainly|sure thing)\b[!,.]?\s*)+", "", text.strip(), flags=re.I)
    text = text[:1].upper() + text[1:]
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]
    kept: List[str] = []
    for sentence in sentences:
        if kept and len(" ".join(kept + [sentence]).split()) > limit:
            break
        kept.append(sentence)
    return " ".join(kept) or text


def _filler_after_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("CUSTOM_FILLER_AFTER_SECONDS", "0")))
    except ValueError:
        return 0.0


def _audio_cache_key(text: str) -> str:
    return f"{os.getenv('ELEVENLABS_VOICE_ID', '')}:{text}"


async def _warm_audio_cache(texts: tuple[str, ...]) -> None:
    for text in texts:
        key = _audio_cache_key(text)
        if key not in _cached_audio:
            try:
                _cached_audio[key] = await _elevenlabs_speech(text)
            except Exception as error:
                logger.warning("could not pre-generate voice phrase %r: %s", text, error)


def _greeting_text() -> str:
    configured = os.getenv("CUSTOM_VOICE_GREETING", "").strip()
    if configured:
        return configured
    try:
        return str(brain_service.channel_config("voice").get("greeting") or "").strip() or "Hello, how can I help you today?"
    except Exception:
        return "Hello, how can I help you today?"


@router.get("/config", dependencies=[Depends(require_user)])
async def custom_voice_config() -> Dict[str, Any]:
    """Readiness information only. API credentials never leave the server."""
    custom = provider_snapshot()["custom"]
    return {
        "enabled": custom["enabled"],
        "ready": custom["ready"],
        "missing_configuration": custom["missing_configuration"],
        "inbound_webhook_path": "/api/custom-voice/inbound",
        "media_websocket_path": "/api/custom-voice/media",
        "stt_provider": "deepgram",
        "tts_provider": "elevenlabs",
    }


class _async_timed:
    """async-with version of logging_setup.timed for awaited provider calls."""

    def __init__(self, what: str) -> None:
        self._timer = timed(logger, what)

    async def __aenter__(self) -> None:
        self._timer.__enter__()

    async def __aexit__(self, *exc_info: Any) -> bool:
        return bool(self._timer.__exit__(*exc_info))


async def _retell_sip_twiml(form: Dict[str, Any]) -> str:
    """Register the call with Retell, then bridge Twilio to Retell over SIP."""
    payload: Dict[str, Any] = {"agent_id": os.getenv("RETELL_AGENT_ID", "").strip(), "direction": "inbound"}
    if str(form.get("From") or "").startswith("+"):
        payload["from_number"] = form["From"]
    if str(form.get("To") or "").startswith("+"):
        payload["to_number"] = form["To"]
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client, _async_timed("Retell register-phone-call"):
        response = await client.post(
            "https://api.retellai.com/v2/register-phone-call",
            headers={"Authorization": f"Bearer {os.getenv('RETELL_API_KEY', '').strip()}"},
            json=payload,
        )
        response.raise_for_status()
    retell_call_id = response.json()["call_id"]
    logger.info("retell answering call %s (retell call %s)", form.get("CallSid"), retell_call_id)
    sip_uri = html.escape(f"sip:{retell_call_id}@sip.retellai.com", quote=True)
    # Browser calls arrive From "client:..."; Twilio rejects that as a SIP caller ID (error 13247).
    caller_id = str(form.get("From") or "")
    if not caller_id.startswith("+"):
        caller_id = (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or "").strip()
    caller_attr = f' callerId="{html.escape(caller_id, quote=True)}"' if caller_id else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial{caller_attr}><Sip>{sip_uri}</Sip></Dial></Response>'


@router.post("/inbound", response_class=PlainTextResponse, dependencies=[Depends(verify_twilio_request)])
async def custom_inbound(request: Request) -> PlainTextResponse:
    """Single call router: Retell or the custom stack when selected, otherwise the default Twilio agent."""
    providers = provider_snapshot()
    bind_call((await request.form()).get("CallSid"))
    if providers["retell"]["enabled"] and providers["retell"]["ready"]:
        try:
            return PlainTextResponse(await _retell_sip_twiml(dict(await request.form())), media_type="text/xml")
        except Exception as error:
            logger.error("retell routing failed, using default Twilio agent: %s", error)
            return PlainTextResponse(_build_conversation_twiml(_get_request_base_url(request)), media_type="text/xml")

    custom = providers["custom"]
    if not custom["enabled"] or not custom["ready"]:
        # Default provider: never reject a caller, hand the call to the standard Twilio AI agent.
        logger.info(
            "custom voice unavailable (enabled=%s, missing=%s); using default Twilio ConversationRelay",
            custom["enabled"], custom["missing_configuration"],
        )
        return PlainTextResponse(_build_conversation_twiml(_get_request_base_url(request)), media_type="text/xml")

    form = dict(await request.form())
    base = _public_base(request)
    if not base.startswith(("https://", "http://")):
        raise HTTPException(status_code=503, detail="TWILIO_PUBLIC_BASE_URL is not configured")
    stream_url = f"{_to_ws_url(base)}/api/custom-voice/media"
    caller = html.escape(str(form.get("From") or "unknown"), quote=True)
    call_sid = html.escape(str(form.get("CallSid") or ""), quote=True)
    logger.info("custom voice stack answering call %s from %s", call_sid, caller)
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect><Stream url=\"" + html.escape(stream_url, quote=True) + "\">"
        f'<Parameter name="caller" value="{caller}" /><Parameter name="call_sid" value="{call_sid}" />'
        f'<Parameter name="stream_token" value="{stream_token(str(form.get("CallSid") or ""))}" />'
        "</Stream></Connect></Response>",
        media_type="text/xml",
    )


@router.websocket("/media")
async def custom_media_stream(websocket: WebSocket) -> None:
    """Bridge Twilio mulaw/8k audio to Deepgram and ElevenLabs."""
    if not provider_enabled("custom"):
        await websocket.close(code=1008, reason="Custom Twilio voice is disabled")
        return
    custom = provider_snapshot()["custom"]
    if not custom["ready"]:
        await websocket.close(code=1011, reason="Custom Twilio voice is not configured")
        return

    await websocket.accept()
    deepgram_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    stream_sid = ""
    call_sid = ""
    caller = "unknown"
    memory = ConversationMemory()
    transcript: List[Dict[str, str]] = []
    responses: List[Dict[str, str]] = []
    send_lock = asyncio.Lock()
    utterance: List[str] = []
    reply_task: Optional[asyncio.Task[Any]] = None
    pending_mark = ""
    mark_count = 0
    filler_index = 0
    # Question currently being answered, until its reply starts playing.
    unanswered_text = ""
    active = True

    async def send_twilio(payload: Dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(payload))

    async def send_audio(chunk: bytes) -> None:
        await send_twilio({"event": "media", "streamSid": stream_sid, "media": {"payload": base64.b64encode(chunk).decode("ascii")}})

    def bot_busy() -> bool:
        return bool(pending_mark) or bool(reply_task and not reply_task.done())

    async def interrupt() -> None:
        """Caller started talking: stop thinking/speaking and flush audio Twilio has buffered."""
        nonlocal reply_task, pending_mark
        if unanswered_text:
            # Still thinking about a question nobody has heard an answer to: the caller is continuing
            # that sentence, not interrupting. finish_utterance() merges the two parts.
            return
        if reply_task and not reply_task.done():
            reply_task.cancel()
        reply_task = None
        if pending_mark and stream_sid:
            pending_mark = ""
            await send_twilio({"event": "clear", "streamSid": stream_sid})

    async def speak(text: str, cache_key: str = "") -> float:
        """Stream speech to Twilio; returns seconds until the first audio chunk was sent."""
        nonlocal pending_mark, mark_count
        started = perf_counter()
        first_audio = 0.0
        cached = _cached_audio.get(cache_key) if cache_key else None
        if cached:
            await send_audio(cached)
            first_audio = perf_counter() - started
        else:
            chunks: List[bytes] = []
            async for chunk in _elevenlabs_stream(text):
                if not active:
                    return first_audio
                if not first_audio:
                    first_audio = perf_counter() - started
                chunks.append(chunk)
                await send_audio(chunk)
            if cache_key:
                _cached_audio[cache_key] = b"".join(chunks)
        mark_count += 1
        pending_mark = f"reply-{mark_count}"
        await send_twilio({"event": "mark", "streamSid": stream_sid, "mark": {"name": pending_mark}})
        return first_audio

    async def answer(text: str, heard_at: float) -> None:
        nonlocal filler_index, unanswered_text
        bind_call(call_sid)  # tasks copy their context when created, before the call id was known
        thinking = asyncio.ensure_future(asyncio.to_thread(brain_service.respond, text, memory.history(), "voice", caller, memory.summary))
        filler_after = _filler_after_seconds()
        if filler_after:
            done, _ = await asyncio.wait({thinking}, timeout=filler_after)
            filler = _FILLERS[filler_index % len(_FILLERS)]
            if not done and _audio_cache_key(filler) in _cached_audio:
                # Acknowledge the caller instead of leaving dead air while the brain works.
                filler_index += 1
                await speak(filler, cache_key=_audio_cache_key(filler))
        reply = await thinking
        reply = _spoken_reply(reply or "Sorry, I could not answer that just now.")
        brain_seconds = perf_counter() - heard_at
        unanswered_text = ""  # committed: from here a new utterance is a new turn
        memory.add(text, reply)
        responses.append({"content": reply, "timestamp": datetime.now().isoformat()})
        first_audio = await speak(reply)
        logger.info("custom voice turn call=%s brain=%.2fs first_audio=%.2fs total=%.2fs", call_sid, brain_seconds, first_audio, brain_seconds + first_audio)

    def log_reply_failure(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and task.exception():
            logger.error("custom voice reply failed: %s", task.exception())

    def finish_utterance() -> None:
        nonlocal reply_task, unanswered_text
        bind_call(call_sid)
        text = " ".join(utterance).strip()
        utterance.clear()
        if not text:
            return
        if reply_task and not reply_task.done() and unanswered_text:
            # The caller kept talking before the previous part was answered (a mid-sentence pause):
            # answer the whole thing once instead of replying to each fragment.
            reply_task.cancel()
            text = f"{unanswered_text} {text}"
            if transcript:
                transcript.pop()
        elif reply_task and not reply_task.done():
            reply_task.cancel()
        unanswered_text = text
        transcript.append({"content": text, "timestamp": datetime.now().isoformat()})
        logger.info("custom voice heard call=%s: %s", call_sid, text[:120])
        reply_task = asyncio.create_task(answer(text, perf_counter()))
        reply_task.add_done_callback(log_reply_failure)

    try:
        async with websockets.connect(
            _deepgram_url(),
            additional_headers={"Authorization": f"Token {deepgram_key}"},
            open_timeout=12,
            ping_interval=20,
        ) as deepgram:
            async def receive_transcripts() -> None:
                async for raw in deepgram:
                    try:
                        payload = json.loads(raw)
                        kind = payload.get("type")
                        if kind == "UtteranceEnd":
                            finish_utterance()
                            continue
                        if kind != "Results":
                            continue
                        alternative = ((payload.get("channel") or {}).get("alternatives") or [{}])[0]
                        text = str(alternative.get("transcript") or "").strip()
                        if not text:
                            continue
                        # Real words (not line noise) while the agent is busy means the caller is barging in.
                        if bot_busy() and len(text.split()) >= 2:
                            await interrupt()
                        if payload.get("is_final"):
                            utterance.append(text)
                            if payload.get("speech_final"):
                                finish_utterance()
                    except Exception as error:
                        logger.error("custom voice transcript error: %s", error)

            transcript_task = asyncio.create_task(receive_transcripts())
            try:
                while True:
                    message = json.loads(await websocket.receive_text())
                    event = message.get("event")
                    if event == "start":
                        start = message.get("start") or {}
                        stream_sid = str(start.get("streamSid") or message.get("streamSid") or "")
                        call_sid = str(start.get("callSid") or "")
                        params = start.get("customParameters") or {}
                        caller = str(params.get("caller") or caller)
                        call_sid = str(params.get("call_sid") or call_sid)
                        bind_call(call_sid)
                        if not valid_stream_token(call_sid, str(params.get("stream_token") or "")):
                            logger.warning("closing media stream without a valid stream token (call %s)", call_sid)
                            await websocket.close(code=1008)
                            return
                        greeting = _greeting_text()
                        responses.append({"content": greeting, "timestamp": datetime.now().isoformat()})
                        memory.add_assistant(greeting)
                        reply_task = asyncio.create_task(speak(greeting, cache_key=_audio_cache_key(greeting)))
                        reply_task.add_done_callback(log_reply_failure)
                        if _filler_after_seconds():
                            asyncio.create_task(_warm_audio_cache(_FILLERS))
                    elif event == "media":
                        encoded = ((message.get("media") or {}).get("payload") or "")
                        if encoded:
                            await deepgram.send(base64.b64decode(encoded))
                    elif event == "mark":
                        if (message.get("mark") or {}).get("name") == pending_mark:
                            pending_mark = ""
                    elif event == "stop":
                        break
            finally:
                active = False
                if reply_task and not reply_task.done():
                    reply_task.cancel()
                transcript_task.cancel()
                await asyncio.gather(transcript_task, return_exceptions=True)
    except WebSocketDisconnect:
        active = False
    except Exception as error:
        logger.error("custom Twilio voice websocket error: %s", error)
    finally:
        await memory.close()
        if transcript or responses:
            log_call(caller, None, {
                "direction": "inbound",
                "status": "completed",
                "provider": "custom_twilio",
                "provider_call_id": call_sid or stream_sid,
                "channel_type": "phone_call",
                "provider_call_status": "ended",
                "transcription": [{"type": "user", **item} for item in transcript],
                "ai_responses": [{"type": "bot", **item} for item in responses],
                "summary": memory.summary or f"Custom Twilio voice call with {len(transcript)} caller turns and {len(responses)} AI responses.",
                "sentiment": "neutral",
                "call_session_id": call_sid or stream_sid,
            })
