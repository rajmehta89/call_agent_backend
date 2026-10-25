"""Self-managed Twilio voice path: Media Streams + Deepgram + ElevenLabs."""

from __future__ import annotations

import asyncio
import contextlib
import base64
import html
import json
import logging
import os
import re
from datetime import datetime
from urllib.parse import quote
from timeutil import utc_iso
from time import perf_counter
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from ai_services import AIServices, warm_model_connection
from voice_audio import LevelMatcher, adjust_ulaw_gain
from brain_service import brain_service
from auth_service import require_user
from mongo_client import mongo_client
from routers.retell_api import voice_turn_reminder, SpokenReplyGuard, _max_response_tokens, _max_spoken_words, _system_prompt, configure_voice_services, sounds_unfinished, unfinished_wait_seconds, voice_temperature, ACKNOWLEDGEMENT_OPENER
from conversation_memory import ConversationMemory, voice_history_token_budget
import call_forwarding
from voice_turns import caller_region, describe_caller_id, name_note, NumberSpeaker, known_numbers, reply_known_numbers, caller_number_digits, choose_voice_model, contact_check, last_agent_text, place_note, careful_voice_model, earlier_caller_texts, bridges_enabled, bridge_phrase, bridge_note, bridge_delay_seconds, status_after_seconds, status_phrase, ALL_BRIDGES, FAST_TURN_NOTE, call_is_over, yes_to_offer_note, search_text
from voice_keyterms import business_name, current_keyterms
import live_transcript
from logging_setup import bind_call, timed
from twilio_security import stream_token, valid_stream_token, verify_twilio_request
from routers.calls_api import log_call
from voice_provider_settings import custom_stack_enabled, custom_voice_engine, deepgram_voice, provider_snapshot
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
    # Words callers say that speech recognition could get wrong (business, brand and product names), taken from the
    # knowledge automatically (voice_keyterms.py) plus any extras in .env. Nova-3 listens for them ("keyterm").
    keyterms = current_keyterms()
    return (
        "wss://api.deepgram.com/v1/listen?"
        f"model={model}&encoding=mulaw&sample_rate=8000&channels=1"
        "&interim_results=true&smart_format=true&punctuate=true"
        f"&endpointing={os.getenv('CUSTOM_ENDPOINTING_MS', '500')}"
        f"&utterance_end_ms={os.getenv('CUSTOM_UTTERANCE_END_MS', '1000')}&vad_events=true"
        + "".join(f"&keyterm={quote(term)}" for term in keyterms[:50])
    )


def _use_flux() -> bool:
    """CUSTOM_STT: "flux" (default) = Deepgram Flux, which decides when the caller has finished their turn from what
    they say and how (built for voice agents); "nova" = Nova-3, which waits for a fixed silence (CUSTOM_ENDPOINTING_MS)."""
    return os.getenv("CUSTOM_STT", "flux").strip().lower() != "nova"


def _env_number(name: str, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


def _flux_url() -> str:
    """Deepgram Flux (/v2/listen) for phone audio. Turn ends are decided by the model: EndOfTurn once its confidence
    reaches CUSTOM_FLUX_EOT_THRESHOLD (0.5-1.0), EagerEndOfTurn earlier at CUSTOM_FLUX_EAGER_THRESHOLD (0.3-0.9, the
    signal to start preparing the reply), and an EndOfTurn anyway after CUSTOM_FLUX_EOT_TIMEOUT_MS of silence."""
    eot = _env_number("CUSTOM_FLUX_EOT_THRESHOLD", 0.7, 0.5, 1.0)
    eager = min(eot, _env_number("CUSTOM_FLUX_EAGER_THRESHOLD", 0.4, 0.3, 0.9))  # Flux rejects eager > eot
    timeout_ms = int(_env_number("CUSTOM_FLUX_EOT_TIMEOUT_MS", 3000, 500, 60000))
    model = os.getenv("CUSTOM_FLUX_MODEL", "flux-general-en").strip() or "flux-general-en"
    return (
        "wss://api.deepgram.com/v2/listen?"
        f"model={model}&encoding=mulaw&sample_rate=8000"
        f"&eot_threshold={eot}&eager_eot_threshold={eager}&eot_timeout_ms={timeout_ms}"
        + "".join(f"&keyterm={quote(term)}" for term in current_keyterms()[:50])
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


_ELEVENLABS_CLIENT: Optional[httpx.AsyncClient] = None
_ELEVENLABS_LOOP: Any = None


def _elevenlabs_client() -> httpx.AsyncClient:
    """One kept-alive HTTPS connection pool for ElevenLabs, reused by every sentence of every call: opening a new
    secure connection per sentence cost 0.1-0.3 s before any audio."""
    global _ELEVENLABS_CLIENT, _ELEVENLABS_LOOP
    loop = asyncio.get_running_loop()
    if _ELEVENLABS_CLIENT is None or _ELEVENLABS_LOOP is not loop:
        _ELEVENLABS_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0), limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120))
        _ELEVENLABS_LOOP = loop
    return _ELEVENLABS_CLIENT


def _first_audio_timeout() -> float:
    """CUSTOM_TTS_FIRST_AUDIO_TIMEOUT_MS: a voice request with no audio after this long is retried on a fresh connection
    (a kept-alive connection that had gone stale once left a caller in 10 s of silence)."""
    try:
        return max(0.5, float(os.getenv("CUSTOM_TTS_FIRST_AUDIO_TIMEOUT_MS", "2500")) / 1000)
    except ValueError:
        return 2.5


async def _reset_elevenlabs_client() -> None:
    global _ELEVENLABS_CLIENT
    client, _ELEVENLABS_CLIENT = _ELEVENLABS_CLIENT, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass


async def _elevenlabs_stream(text: str) -> AsyncIterator[bytes]:
    """Yield caller-ready mulaw/8k audio while ElevenLabs is still generating it. If no audio arrives within
    CUSTOM_TTS_FIRST_AUDIO_TIMEOUT_MS, the request is made once more on a fresh connection."""
    url, headers, model_id = _elevenlabs_request()
    for attempt in (1, 2):
        got_audio = False
        try:
            async with asyncio.timeout(_first_audio_timeout()) as deadline:
                async with _elevenlabs_client().stream("POST", url, headers=headers, json={"text": text, "model_id": model_id}) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(1600):
                        if not chunk:
                            continue
                        if not got_audio:
                            got_audio = True
                            deadline.reschedule(None)  # audio is flowing: no time limit for the rest
                        yield chunk
            return
        except (TimeoutError, httpx.TransportError) as error:
            if got_audio or attempt == 2:
                raise
            logger.warning("ElevenLabs sent no audio in %.1fs (%s); retrying on a fresh connection", _first_audio_timeout(), type(error).__name__)
            await _reset_elevenlabs_client()


async def _elevenlabs_speech(text: str) -> bytes:
    return b"".join([chunk async for chunk in _elevenlabs_stream(text)])


async def _deepgram_tts_stream(text: str) -> AsyncIterator[bytes]:
    """Deepgram Aura-2 voice in the caller's format (mulaw/8k, no container), streamed over the kept-alive pool."""
    api_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Deepgram voice is not configured")
    url = f"https://api.deepgram.com/v1/speak?model={deepgram_voice()}&encoding=mulaw&sample_rate=8000&container=none"
    try:
        gain_db = float(os.getenv("DEEPGRAM_TTS_GAIN_DB", "7"))
    except ValueError:
        gain_db = 7.0
    async with _elevenlabs_client().stream("POST", url, headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"}, json={"text": text}) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes(1600):
            if chunk:
                # Aura-2 comes out 5-8 dB quieter than ElevenLabs on the phone; bring it up to the same level.
                yield adjust_ulaw_gain(chunk, gain_db)


def _level_matcher() -> Optional[LevelMatcher]:
    """CUSTOM_VOICE_LEVEL_DBFS: the steady loudness every spoken piece is brought to (default -13; "off" disables)."""
    setting = os.getenv("CUSTOM_VOICE_LEVEL_DBFS", "-13").strip().lower()
    if setting in {"off", "false", "no", "none", ""}:
        return None
    try:
        return LevelMatcher(target_dbfs=float(setting))
    except ValueError:
        return LevelMatcher()


def _deepgram_gain_db() -> float:
    try:
        return float(os.getenv("DEEPGRAM_TTS_GAIN_DB", "7"))
    except ValueError:
        return 7.0


class _DeepgramSpeaker:
    """One live Deepgram Aura-2 connection per call (opened while the greeting plays). Each sentence is sent down the
    open line: about 0.6-0.7 s to first audio, steadily, where a separate web request per sentence took 1-2 s from
    here. If the caller interrupts mid-sentence the line is dropped and reopened, so stale audio never plays later."""

    def __init__(self) -> None:
        self.ws: Any = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def _ensure(self) -> Any:
        if self.ws is None:
            url = f"wss://api.deepgram.com/v1/speak?model={deepgram_voice()}&encoding=mulaw&sample_rate=8000"
            self.ws = await websockets.connect(url, additional_headers={"Authorization": f"Token {os.getenv('DEEPGRAM_API_KEY', '').strip()}"},
                                               open_timeout=10, ping_interval=20)
        return self.ws

    async def _drop(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def warm(self) -> None:
        try:
            async with self.lock:
                if not self.closed:
                    await self._ensure()
        except Exception as error:
            logger.debug("deepgram voice warm-up failed: %s", error)

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        gain_db = _deepgram_gain_db()
        async with self.lock:
            finished = False
            try:
                for attempt in (1, 2):
                    try:
                        ws = await self._ensure()
                        await ws.send(json.dumps({"type": "Speak", "text": text}))
                        await ws.send(json.dumps({"type": "Flush"}))
                        first = await ws.recv()
                        break
                    except websockets.ConnectionClosed:
                        await self._drop()  # Deepgram closed the idle line: reopen once and resend
                        if attempt == 2:
                            raise
                message = first
                while True:
                    if isinstance(message, (bytes, bytearray)):
                        yield adjust_ulaw_gain(bytes(message), gain_db)
                    else:
                        kind = json.loads(message).get("type")
                        if kind == "Flushed":
                            finished = True
                            return
                        if kind in {"Warning", "Error"}:
                            logger.warning("deepgram voice: %s", message[:200])
                    message = await ws.recv()
            finally:
                if not finished:
                    await self._drop()
                    # Reopen in the background so the next reply does not pay for a new connection (~1 s).
                    if not self.closed:
                        asyncio.get_running_loop().create_task(self.warm())

    async def close(self) -> None:
        self.closed = True
        await self._drop()


async def _warm_tts_connection(engine: str) -> None:
    """Open the kept-alive connection to the voice provider before the first sentence needs it."""
    url = "https://api.deepgram.com/v1/projects" if engine == "deepgram" else "https://api.elevenlabs.io/v1/models"
    headers = {"Authorization": f"Token {os.getenv('DEEPGRAM_API_KEY', '')}"} if engine == "deepgram" else {"xi-api-key": os.getenv("ELEVENLABS_API_KEY", "")}
    try:
        await _elevenlabs_client().get(url, headers=headers, timeout=10)
    except Exception as error:
        logger.debug("voice connection warm-up skipped: %s", error)


def _tts_stream(text: str, engine: str) -> AsyncIterator[bytes]:
    """Speech for one sentence from the voice chosen for this call ("deepgram" or "elevenlabs")."""
    return _deepgram_tts_stream(text) if engine == "deepgram" else _elevenlabs_stream(text)


async def _speech(text: str, engine: str) -> bytes:
    return b"".join([chunk async for chunk in _tts_stream(text, engine)])


# Greeting and filler audio never change, so synthesize each phrase once per process.
_cached_audio: Dict[str, bytes] = {}
# Said when the reply is slow to start, the way a person says "Yeah..." while thinking (CUSTOM_FILLER_AFTER_SECONDS).
_FILLERS = ("Yeah.", "Right.", "Sure.", "Okay.")


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


def _voice_services() -> AIServices:
    """One model client per call, on the voice model (shared with Retell: RETELL_OPENROUTER_MODEL, or CUSTOM_VOICE_MODEL)."""
    services = AIServices()
    return configure_voice_services(services, os.getenv("CUSTOM_VOICE_MODEL") or os.getenv("RETELL_OPENROUTER_MODEL") or services.config.OPENROUTER_MODEL)


_SENTENCE_BREAK = re.compile(r"[.!?](?=\s)")
_CLAUSE_BREAK = re.compile(r"[,;:—–](?=\s)")


def _speculate_after_seconds() -> float:
    """CUSTOM_SPECULATE_AFTER_MS: start preparing the reply once the caller's words have not changed for this long,
    before the end of the turn is confirmed (0 = only after the turn ends)."""
    try:
        return max(0.0, float(os.getenv("CUSTOM_SPECULATE_AFTER_MS", "250"))) / 1000
    except ValueError:
        return 0.25


def _turn_timeout_seconds() -> float:
    """CUSTOM_TURN_TIMEOUT_MS: end the caller's turn ourselves when their words have not changed for this long and
    Deepgram has not ended it (it sometimes drops short words). 0 = rely on Deepgram only."""
    try:
        return max(0.0, float(os.getenv("CUSTOM_TURN_TIMEOUT_MS", "2500"))) / 1000
    except ValueError:
        return 2.5


def _same_words(left: str, right: str) -> bool:
    """Same words, ignoring case and punctuation (partial and final transcripts differ in those)."""
    normal = lambda text: " ".join(re.findall(r"[a-z0-9']+", text.lower()))
    return normal(left) == normal(right)


def _first_chunk_words() -> int:
    """CUSTOM_FIRST_CHUNK_WORDS: the first words go to the voice at a comma once there are this many (0 = wait for
    the whole first sentence). Speech starts sooner; later chunks are whole sentences."""
    try:
        return max(0, int(os.getenv("CUSTOM_FIRST_CHUNK_WORDS", "7")))
    except ValueError:
        return 7


def _next_chunk(pending: str, first: bool) -> Optional[re.Match]:
    """Where the next spoken chunk ends: a sentence end, or for the very first chunk also a comma after enough words."""
    sentence = _SENTENCE_BREAK.search(pending)
    words = _first_chunk_words()
    if first and words:
        for clause in _CLAUSE_BREAK.finditer(pending):
            if sentence and clause.start() > sentence.start():
                break
            if len(pending[:clause.end()].split()) >= words:
                return clause
    return sentence


async def _voice_sentences(services: AIServices, text: str, history: List[Dict[str, str]], caller: str, summary: str,
                           bridge: str = "") -> AsyncIterator[str]:
    """Stream the reply and yield it one finished sentence at a time, so speech can start before the model is done.
    Uses the same voice prompt, knowledge search and reply guard as the Retell agent (routers/retell_api.py).
    bridge: the phrase being spoken right now ("Sure, let me check."); the reply continues from it."""
    phone = caller if caller.startswith("+") else "unknown"
    started = perf_counter()
    turn_model = choose_voice_model(text, last_agent_text(history))
    details_note, details_problem = contact_check(text, last_agent_text(history), earlier_caller_texts(history, text), caller_region(caller))
    if details_problem:
        turn_model = None  # a wrong number or email needs the careful model
    places = place_note(text, await asyncio.to_thread(brain_service.knowledge_text), last_agent_text(history))
    if places:
        turn_model = careful_voice_model(services.fallback_model) or None  # the main model tends to say "yes, covered"
    system = await asyncio.to_thread(_system_prompt, search_text(text, [*history, {"role": "user", "content": text}]), phone, summary, history, turn_model is None or bool(places))
    if call_forwarding.forwarding_on():
        system += ("\n\n# Live team transfer on this call\n"
                   "A live transfer to the human team is available. If the caller asks to speak with the team now, "
                   "do not say calls are unavailable or offer a callback instead. Do not ask for a name or phone number "
                   "before a live transfer. The call system connects them; a callback needs contact details only when "
                   "the caller explicitly wants the team to call later.")
    prompt_ms = (perf_counter() - started) * 1000
    first_token_ms = 0.0
    reminder_text = voice_turn_reminder()
    conversation = [*history, {"role": "user", "content": text}]
    messages = [{"role": "system", "content": system}, *conversation] + ([{"role": "system", "content": reminder_text}] if reminder_text else [])
    if details_note:
        messages.append({"role": "system", "content": details_note})
    if places:
        messages.append({"role": "system", "content": places})
    offered = yes_to_offer_note(text, last_agent_text(history))
    if offered:
        messages.append({"role": "system", "content": offered})
    misheard = name_note(text, business_name())
    if misheard:
        messages.append({"role": "system", "content": misheard})
    if bridge:
        messages.append({"role": "system", "content": bridge_note(bridge)})
    elif turn_model is not None and not places:
        messages.append({"role": "system", "content": FAST_TURN_NOTE})
    guard = SpokenReplyGuard(_max_spoken_words(fast=turn_model is not None and not places), strip_acknowledgement=bool(bridge), echo_of=bridge)
    caller_digits = caller_number_digits(conversation)
    numbers = NumberSpeaker(caller_digits, reply_known_numbers(brain_service.knowledge_text(), system, caller_digits))
    pending = ""
    yielded = False  # the first chunk may end at a comma (see _next_chunk)
    iterator = None
    try:
        iterator = services.stream_chat_completion(messages, _max_response_tokens(turn_model or services.config.OPENROUTER_MODEL), voice_temperature(), 1.0, model=turn_model)
        while True:
            chunk = await asyncio.to_thread(next, iterator, None)
            if chunk is None:
                break
            if not first_token_ms:
                first_token_ms = (perf_counter() - started) * 1000
                logger.info("custom voice timing: model=%s prompt=%.0fms model_first_words=%.0fms (after prompt %.0fms)",
                            turn_model or services.config.OPENROUTER_MODEL, prompt_ms, first_token_ms, first_token_ms - prompt_ms)
            pending += numbers.feed(guard.feed(chunk))
            while (match := _next_chunk(pending, first=not yielded)):
                sentence, pending = pending[:match.end()].strip(), pending[match.end():]
                if sentence:
                    yielded = True
                    yield sentence
            if guard.done:
                break
        pending += numbers.feed(guard.flush()) + numbers.flush()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error("custom voice model request failed: %s", error)
        if not pending.strip():
            pending = "Sorry, I'm having trouble answering right now. Could you say that again?"
    finally:
        close = getattr(iterator, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass
    if pending.strip():
        yield pending.strip()


def _hang_up(call_sid: str) -> None:
    """End the phone call (the caller's line) through Twilio."""
    from routers.twilio_api import _get_twilio_client
    client = _get_twilio_client()
    if client is None or not call_sid:
        raise RuntimeError("Twilio is not configured")
    client.calls(call_sid).update(status="completed")


def _log_voice_activity(request: str, reply: str, caller: str, seconds: float) -> None:
    """AI activity log for the dashboard, written after the caller has their answer (never in the way of it)."""
    try:
        if mongo_client.is_connected():
            mongo_client.ai_activity.insert_one({
                "channel": "voice", "customer_phone": caller, "agent": "Custom voice", "request": request, "response": reply,
                "success": bool(reply), "response_time_ms": round(seconds * 1000), "created_at": datetime.utcnow(),
            })
    except Exception as error:
        logger.debug("voice activity log failed: %s", error)


def _filler_after_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("CUSTOM_FILLER_AFTER_SECONDS", "0")))
    except ValueError:
        return 0.0


def _audio_cache_key(text: str, engine: str) -> str:
    voice = deepgram_voice() if engine == "deepgram" else os.getenv("ELEVENLABS_VOICE_ID", "")
    return f"{engine}:{voice}:{text}"


async def _warm_audio_cache(texts: tuple[str, ...], engine: str) -> None:
    for text in texts:
        key = _audio_cache_key(text, engine)
        if key not in _cached_audio:
            try:
                _cached_audio[key] = await _speech(text, engine)
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


def _active_custom(providers: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The custom-pipeline variant that is switched on (Deepgram voice or ElevenLabs voice)."""
    deepgram = providers.get("custom_deepgram") or {}
    return deepgram if deepgram.get("enabled") else providers["custom"]


@router.get("/config", dependencies=[Depends(require_user)])
async def custom_voice_config() -> Dict[str, Any]:
    """Readiness information only. API credentials never leave the server."""
    custom = _active_custom(provider_snapshot())
    return {
        "enabled": custom["enabled"],
        "ready": custom["ready"],
        "missing_configuration": custom["missing_configuration"],
        "inbound_webhook_path": "/api/custom-voice/inbound",
        "media_websocket_path": "/api/custom-voice/media",
        "stt_provider": "deepgram",
        "tts_provider": custom_voice_engine(),
    }


class _async_timed:
    """async-with version of logging_setup.timed for awaited provider calls."""

    def __init__(self, what: str) -> None:
        self._timer = timed(logger, what)

    async def __aenter__(self) -> None:
        self._timer.__enter__()

    async def __aexit__(self, *exc_info: Any) -> bool:
        return bool(self._timer.__exit__(*exc_info))


_RETELL_HTTP: Optional[httpx.AsyncClient] = None


def _retell_client() -> httpx.AsyncClient:
    """One kept-open connection to Retell's API for every call: a new connection per call spent ~0.5 s on the
    secure handshake before the caller heard anything."""
    global _RETELL_HTTP
    if _RETELL_HTTP is None or _RETELL_HTTP.is_closed:
        _RETELL_HTTP = httpx.AsyncClient(timeout=httpx.Timeout(10.0), limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300))
    return _RETELL_HTTP


async def warm_retell_connection() -> None:
    """Open the Retell connection before the first call (any reply will do; the connection is what is kept)."""
    if not os.getenv("RETELL_API_KEY"):
        return
    try:
        await _retell_client().get("https://api.retellai.com/", timeout=5)
    except Exception as exc:
        logger.debug("retell connection warm-up skipped: %s", exc)


async def _retell_sip_twiml(form: Dict[str, Any]) -> str:
    """Register the call with Retell, then bridge Twilio to Retell over SIP."""
    payload: Dict[str, Any] = {"agent_id": os.getenv("RETELL_AGENT_ID", "").strip(), "direction": "inbound"}
    if str(form.get("From") or "").startswith("+"):
        payload["from_number"] = form["From"]
    if str(form.get("To") or "").startswith("+"):
        payload["to_number"] = form["To"]
    async with _async_timed("Retell register-phone-call"):
        response = await _retell_client().post(
            "https://api.retellai.com/v2/register-phone-call",
            headers={"Authorization": f"Bearer {os.getenv('RETELL_API_KEY', '').strip()}"},
            json=payload,
        )
        response.raise_for_status()
    retell_call_id = response.json()["call_id"]
    logger.info("retell answering call %s (retell call %s)", form.get("CallSid"), retell_call_id)
    call_forwarding.remember_retell_call(retell_call_id, str(form.get("CallSid") or ""), str(form.get("From") or ""))
    sip_uri = html.escape(f"sip:{retell_call_id}@sip.retellai.com", quote=True)
    # Browser calls arrive From "client:..."; Twilio rejects that as a SIP caller ID (error 13247).
    caller_id = str(form.get("From") or "")
    if not caller_id.startswith("+"):
        caller_id = (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or "").strip()
    caller_attr = f' callerId="{html.escape(caller_id, quote=True)}"' if caller_id else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial{caller_attr}><Sip>{sip_uri}</Sip></Dial></Response>'


def _with_full_call_recording(twiml: str, base: str) -> str:
    """Start one parent-call recording before the AI or human bridge begins."""
    if not base.startswith(("https://", "http://")):
        return twiml
    callback = html.escape(f"{base.rstrip('/')}/api/call-forwarding/full-recording-result", quote=True)
    start = f'<Start><Recording track="both" channels="mono" trim="do-not-trim" recordingStatusCallback="{callback}" recordingStatusCallbackMethod="POST" /></Start>'
    return twiml.replace("<Response>", f"<Response>{start}", 1)


@router.post("/inbound", response_class=PlainTextResponse, dependencies=[Depends(verify_twilio_request)])
async def custom_inbound(request: Request) -> PlainTextResponse:
    """Single call router: Retell or the custom stack when selected, otherwise the default Twilio agent."""
    providers = provider_snapshot()
    bind_call((await request.form()).get("CallSid"))
    if providers["retell"]["enabled"] and providers["retell"]["ready"]:
        try:
            return PlainTextResponse(_with_full_call_recording(await _retell_sip_twiml(dict(await request.form())), _public_base(request)), media_type="text/xml")
        except Exception as error:
            logger.error("retell routing failed, using default Twilio agent: %s", error)
            return PlainTextResponse(_with_full_call_recording(_build_conversation_twiml(_get_request_base_url(request)), _public_base(request)), media_type="text/xml")

    custom = _active_custom(providers)
    if not custom["enabled"] or not custom["ready"]:
        # Default provider: never reject a caller, hand the call to the standard Twilio AI agent.
        logger.info(
            "custom voice unavailable (enabled=%s, missing=%s); using default Twilio ConversationRelay",
            custom["enabled"], custom["missing_configuration"],
        )
        return PlainTextResponse(_with_full_call_recording(_build_conversation_twiml(_get_request_base_url(request)), _public_base(request)), media_type="text/xml")

    form = dict(await request.form())
    base = _public_base(request)
    if not base.startswith(("https://", "http://")):
        raise HTTPException(status_code=503, detail="TWILIO_PUBLIC_BASE_URL is not configured")
    stream_url = f"{_to_ws_url(base)}/api/custom-voice/media"
    caller = html.escape(str(form.get("From") or "unknown"), quote=True)
    call_sid = html.escape(str(form.get("CallSid") or ""), quote=True)
    logger.info("custom voice stack answering call %s from %s", call_sid, caller)
    # Twilio opens the media stream about a second from now: have the greeting audio ready by then.
    asyncio.create_task(_warm_audio_cache((_greeting_text(),), custom_voice_engine()))
    return PlainTextResponse(_with_full_call_recording(
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect><Stream url=\"" + html.escape(stream_url, quote=True) + "\">"
        f'<Parameter name="caller" value="{caller}" /><Parameter name="call_sid" value="{call_sid}" />'
        f'<Parameter name="stream_token" value="{stream_token(str(form.get("CallSid") or ""))}" />'
        "</Stream></Connect></Response>", base),
        media_type="text/xml",
    )


@router.websocket("/media")
async def custom_media_stream(websocket: WebSocket) -> None:
    """Bridge Twilio mulaw/8k audio to Deepgram and ElevenLabs."""
    if not custom_stack_enabled():
        await websocket.close(code=1008, reason="Custom Twilio voice is disabled")
        return
    custom = _active_custom(provider_snapshot())
    engine = custom_voice_engine()  # chosen once per call: "deepgram" or "elevenlabs"
    speaker = _DeepgramSpeaker() if engine == "deepgram" else None  # one live voice line for the whole call
    leveller = _level_matcher()  # one steady voice loudness for the whole call
    if not custom["ready"]:
        await websocket.close(code=1011, reason="Custom Twilio voice is not configured")
        return

    await websocket.accept()
    deepgram_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    stream_sid = ""
    call_sid = ""
    caller = "unknown"
    # Keep the whole call in view: forgetting a number the caller already gave makes the agent ask again.
    memory = ConversationMemory(keep_recent_messages=60, max_messages=120, token_budget=voice_history_token_budget())
    transcript: List[Dict[str, str]] = []
    responses: List[Dict[str, str]] = []
    send_lock = asyncio.Lock()
    utterance: List[str] = []
    reply_task: Optional[asyncio.Task[Any]] = None
    transfer_pending = False  # Once promised, a new caller fragment must not cancel the handoff.
    earlier_handoff_text = ""  # keep an unclear team request so a short follow-up is read in context
    pending_mark = ""
    mark_count = 0
    filler_index = 0
    recent_bridges: List[str] = []  # acknowledgements said so far, so the last few are never repeated
    # Question currently being answered, until its reply starts playing.
    unanswered_text = ""
    active = True
    pending_saves: set[asyncio.Task[Any]] = set()
    services: Optional[AIServices] = None   # model client, made on the first answer and reused all call
    prefetched_at = 0.0
    call_started = perf_counter()
    heard_text = ""             # the caller's words so far in this turn (final parts + the latest partial)
    heard_changed_at = 0.0      # when those words last changed
    early_reply: Optional[Dict[str, Any]] = None  # reply started before the end of the turn (see speculate)
    flux = _use_flux()          # Deepgram Flux turn detection; falls back to Nova-3 if it cannot connect
    forced_turn = ""            # the words of a Flux turn we already asked Flux to end (asked once per turn)
    forced_at = 0.0             # ForceEndTurn can go unanswered; then finish from the partial transcript.
    forced_fallback_text = ""
    early_handoff_checked = ""
    flux_turn_started = 0.0     # when the current Flux turn began (for logging turns that stay open long)
    flux_turn_logged = False

    async def send_twilio(payload: Dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(payload))

    async def send_audio(chunk: bytes) -> None:
        await send_twilio({"event": "media", "streamSid": stream_sid, "media": {"payload": base64.b64encode(chunk).decode("ascii")}})

    def save_progress() -> None:
        """Write the transcript so far, so the inbox shows the call while it is happening."""
        if call_sid:
            lines = live_transcript.ordered_from_lists(transcript, responses)
            task = asyncio.create_task(asyncio.to_thread(live_transcript.save_live, call_sid, caller, "custom_twilio", lines))
            pending_saves.add(task)  # hold a reference until it finishes, or the task can be garbage-collected
            task.add_done_callback(pending_saves.discard)

    def bot_busy() -> bool:
        return bool(pending_mark) or bool(reply_task and not reply_task.done())

    async def interrupt() -> None:
        """Caller started talking: stop thinking/speaking and flush audio Twilio has buffered."""
        nonlocal reply_task, pending_mark
        if transfer_pending:
            return
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

    async def end_call_after_playback() -> None:
        """Hang up once the goodbye already sent to Twilio has finished playing (its playback mark came back)."""
        for _ in range(50):  # at most ~5 s
            if not pending_mark:
                break
            await asyncio.sleep(0.1)
        try:
            await asyncio.to_thread(_hang_up, call_sid)
        except Exception as error:
            logger.warning("custom voice: could not end call %s: %s", call_sid, error)

    async def forward_to_team(text: str, heard_at: float) -> None:
        """The caller asked for a person and call forwarding is on: say so, let it play, then ring the team
        (call_forwarding.py). If the transfer cannot start, the caller is told and the AI carries on."""
        nonlocal unanswered_text, transfer_pending
        bind_call(call_sid)
        unanswered_text = ""
        line = call_forwarding.CONNECTING_LINE
        spoke_at = utc_iso()
        await speak(line)
        memory.add(text, line)
        responses.append({"content": line, "timestamp": spoke_at})
        save_progress()
        for _ in range(80):  # the line has finished playing (its mark came back); at most ~8 s
            if not pending_mark:
                break
            await asyncio.sleep(0.1)
        if await asyncio.to_thread(call_forwarding.transfer, call_sid, caller):
            logger.info("custom voice: call=%s handed to the team %.2fs after the caller asked", call_sid, perf_counter() - heard_at)
            return
        transfer_pending = False
        fallback = "Sorry, I couldn't put you through just now. I've noted that you'd like to talk to someone, and our team will call you back."
        await speak(fallback)
        memory.add(text, fallback)
        responses.append({"content": fallback, "timestamp": utc_iso()})
        save_progress()

    async def answer_or_forward(text: str, heard_at: float, prepared: Optional[Dict[str, Any]]) -> None:
        """Forwarding is on: hand the call to the team if the caller asked for a person (a meaning check, off the
        event loop), otherwise answer as usual."""
        nonlocal transfer_pending, earlier_handoff_text, unanswered_text
        intent = await asyncio.to_thread(call_forwarding.handoff_intent, text, earlier_handoff_text)
        if intent == "live":
            earlier_handoff_text = ""
            transfer_pending = True
            drop_reply(prepared)
            await forward_to_team(text, heard_at)
        elif intent == "unclear":
            if len(text.split()) > 2:
                earlier_handoff_text = text
            unanswered_text = ""
            drop_reply(prepared)
            line = "Would you like me to connect you with the team now, or have someone call you back later?"
            spoke_at = utc_iso()
            await speak(line)
            memory.add(text, line)
            responses.append({"content": line, "timestamp": spoke_at})
            save_progress()
        else:
            earlier_handoff_text = ""
            await answer(text, heard_at, prepared)

    async def use_deepgram_voice(error: Exception) -> None:
        nonlocal engine, speaker
        # HTTPStatusError may carry a streaming response whose body was never read.
        # Accessing .text there raises ResponseNotRead and prevents the fallback.
        detail = f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else str(error)
        logger.warning("ElevenLabs voice failed (%s: %s); this call continues with the Deepgram voice", type(error).__name__, detail)
        engine, speaker = "deepgram", _DeepgramSpeaker()
        # Acknowledgements are only said from recorded audio: record the Deepgram versions in the background.
        asyncio.create_task(_warm_audio_cache(ALL_BRIDGES if bridges_enabled() else (), engine))

    async def speak(text: str, cache_key: str = "") -> float:
        """Stream speech to Twilio; returns seconds until the first audio chunk was sent."""
        nonlocal pending_mark, mark_count
        started = perf_counter()
        first_audio = 0.0
        cached = _cached_audio.get(cache_key) if cache_key else None
        if leveller:
            leveller.start()
        if cached:
            await send_audio(leveller.whole(cached) if leveller else cached)
            first_audio = perf_counter() - started
        else:
            chunks: List[bytes] = []
            try:
                # aclosing: when playback stops early (call ended, caller interrupted) the voice stream is closed at
                # once, which releases the live Deepgram line instead of holding it until garbage collection.
                audio = speaker.stream(text) if speaker else _tts_stream(text, engine)
                async with contextlib.aclosing(audio):
                    async for chunk in audio:
                        if not active:
                            return first_audio
                        if not first_audio:
                            first_audio = perf_counter() - started
                        chunks.append(chunk)  # the cache keeps the original audio; it is levelled when played
                        await send_audio(leveller.process(chunk) if leveller else chunk)
            except (httpx.HTTPError, RuntimeError) as error:
                if engine != "elevenlabs" or not os.getenv("DEEPGRAM_API_KEY", "").strip():
                    raise
                # ElevenLabs refused or cut off the sentence (out of credits, a dropped stream): the rest of the call
                # speaks with the Deepgram voice, starting with this whole sentence again, instead of going silent.
                await use_deepgram_voice(error)
                return await speak(text)
            if cache_key:
                _cached_audio[cache_key] = b"".join(chunks)
        mark_count += 1
        pending_mark = f"reply-{mark_count}"
        await send_twilio({"event": "mark", "streamSid": stream_sid, "mark": {"name": pending_mark}})
        return first_audio

    async def warm_call() -> None:
        """While the greeting plays: open the model and voice connections and pre-build the prompt, so the
        caller's first question is answered as fast as later ones (a cold first turn could take several seconds)."""
        nonlocal services
        try:
            if services is None:
                services = await asyncio.to_thread(_voice_services)
            phone = caller if caller.startswith("+") else "unknown"
            await asyncio.gather(
                asyncio.to_thread(warm_model_connection, services),
                asyncio.to_thread(_system_prompt, "hello", phone, ""),
                speaker.warm() if speaker else _warm_tts_connection(engine),
                return_exceptions=True,
            )
        except Exception as error:
            logger.debug("call warm-up failed: %s", error)

    def start_reply(text: str) -> Dict[str, Any]:
        """Start preparing the reply to `text` (knowledge search + model), without speaking anything yet."""
        history = memory.history()
        bridge = ""
        if bridges_enabled() and choose_voice_model(text, last_agent_text(history)) is None:
            # This turn needs the knowledge search and the main model (~1.5 s): a short, fitting phrase ("Sure, let me
            # check.") is said while they run, so the caller hears someone is there. Only recorded phrases (instant).
            candidate = bridge_phrase(text, recent_bridges[-3:], " ".join(earlier_caller_texts(history, text)[-8:]))
            if candidate and _audio_cache_key(candidate, engine) in _cached_audio:
                bridge = candidate
        sentences = _voice_sentences(services, text, history, caller, memory.summary, bridge).__aiter__()
        return {"text": text, "bridge": bridge, "sentences": sentences, "first": asyncio.ensure_future(sentences.__anext__()),
                "started": perf_counter()}

    def drop_reply(prepared: Optional[Dict[str, Any]]) -> None:
        """Throw away a reply that was prepared early but will not be used (the caller said something else)."""
        if not prepared:
            return
        if prepared["first"].done():
            asyncio.ensure_future(prepared["sentences"].aclose())
        else:
            prepared["first"].cancel()  # also stops the model request

    async def answer(text: str, heard_at: float, prepared: Optional[Dict[str, Any]] = None) -> None:
        """Think and speak at the same time: each sentence goes to ElevenLabs as soon as the model finishes it.
        `prepared`: the reply already started while the caller was finishing (see speculate)."""
        nonlocal filler_index, unanswered_text, services
        bind_call(call_sid)  # tasks copy their context when created, before the call id was known
        if services is None:
            services = await asyncio.to_thread(_voice_services)
        if prepared:
            logger.info("custom voice early start used call=%s: reply began %.2fs before the end of the turn", call_sid, heard_at - prepared["started"])
        # The model and the knowledge search start now (or already started), before anything is spoken.
        prepared = prepared or start_reply(text)
        bridge, sentences, first = prepared["bridge"], prepared["sentences"], prepared["first"]
        filler_after = _filler_after_seconds()
        spoken: List[str] = []
        spoke_at = ""
        try:
            if sounds_unfinished(text):
                # Probably a pause mid-sentence ("what are the popular ..."): give the caller a moment to finish. The
                # model is already working meanwhile; if they go on, finish_utterance cancels this and answers it all.
                waited = unfinished_wait_seconds()
                await asyncio.sleep(waited)
                if filler_after:
                    filler_after = max(0.05, filler_after - waited)
            filler_spoken = False
            if bridge:
                await asyncio.sleep(bridge_delay_seconds())  # caller carries on talking: this reply is cancelled, no bridge
                if not first.done():  # the answer is not ready yet (with an early start it often is: then say nothing)
                    recent_bridges.append(bridge)
                    filler_spoken = True
                    spoken.append(bridge)
                    spoke_at = utc_iso()
                    await speak(bridge, cache_key=_audio_cache_key(bridge, engine))  # the answer is being prepared meanwhile
                filler_after = 0.0
            status_wait = status_after_seconds() - (perf_counter() - heard_at)
            if bridges_enabled() and status_wait > 0 and not first.done():
                # A long wait: one real status update, said only if the answer still has not started.
                done, _ = await asyncio.wait({first}, timeout=status_wait)
                status = "" if done else status_phrase(recent_bridges[-3:])
                if status and _audio_cache_key(status, engine) in _cached_audio:
                    recent_bridges.append(status)
                    filler_spoken = True
                    spoken.append(status)
                    spoke_at = spoke_at or utc_iso()
                    await speak(status, cache_key=_audio_cache_key(status, engine))
            if filler_after:
                done, _ = await asyncio.wait({first}, timeout=filler_after)
                filler = _FILLERS[filler_index % len(_FILLERS)]
                if not done and _audio_cache_key(filler, engine) in _cached_audio:
                    # Acknowledge the caller instead of leaving dead air while the brain works.
                    filler_index += 1
                    filler_spoken = True
                    await speak(filler, cache_key=_audio_cache_key(filler, engine))
            try:
                sentence = await first
                if filler_spoken:
                    # "Yeah." was just said: don't follow it with the model's own "Yeah, ..." / "Sure, ...".
                    sentence = ACKNOWLEDGEMENT_OPENER.sub("", sentence).strip()
                    sentence = sentence[:1].upper() + sentence[1:]
                    if not sentence:
                        sentence = await sentences.__anext__()
            except StopAsyncIteration:
                sentence = "Sorry, I could not answer that just now."
            brain_seconds = perf_counter() - heard_at
            unanswered_text = ""  # committed: from here a new utterance is a new turn
            spoken.append(sentence)
            spoke_at = spoke_at or utc_iso()  # the transcript shows when the agent started speaking, not when it finished
            first_audio = await speak(sentence)
            logger.info("custom voice turn call=%s first_sentence=%.2fs first_audio=%.2fs total=%.2fs", call_sid, brain_seconds, first_audio, brain_seconds + first_audio)
            async for sentence in sentences:
                spoken.append(sentence)
                await speak(sentence)
        finally:
            if not first.done():
                first.cancel()  # interrupted before the first sentence: stop the model request too
            # Record what the caller actually heard, also when they interrupted part-way through.
            if spoken:
                reply = " ".join(spoken)
                memory.add(text, reply)
                responses.append({"content": reply, "timestamp": spoke_at or utc_iso()})
                save_progress()
                asyncio.get_running_loop().run_in_executor(None, _log_voice_activity, text, reply, caller, perf_counter() - heard_at)

    def log_reply_failure(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and task.exception():
            logger.error("custom voice reply failed: %s", task.exception())

    def heard_changed(text: str) -> None:
        """The caller's words so far changed: an early reply for different words is no longer any use."""
        nonlocal heard_text, heard_changed_at, early_reply
        if text != heard_text:
            heard_text, heard_changed_at = text, perf_counter()
            if early_reply and not _same_words(early_reply["text"], text):
                drop_reply(early_reply)
                early_reply = None

    def start_early() -> None:
        """Start preparing the reply to the caller's words so far (nothing is spoken until their turn ends)."""
        nonlocal early_reply
        idle = (reply_task is None or reply_task.done()) and not bot_busy()
        if early_reply or not heard_text or services is None or not idle:
            return
        try:
            early_reply = start_reply(heard_text)
        except Exception as error:
            logger.debug("early reply start failed: %s", error)

    async def speculate() -> None:
        """Start the reply when the caller's words stop changing for a moment, before Deepgram confirms the end of the
        turn. If the turn ends with the same words, the reply is already on its way; if the caller goes on, it is
        dropped. Nothing is spoken until the turn has really ended. With Nova-3 it also ends a turn Deepgram never
        ends (it sometimes drops short words)."""
        nonlocal forced_turn, forced_at, forced_fallback_text, early_handoff_checked
        wait = _speculate_after_seconds()
        stuck_after = _turn_timeout_seconds()
        while active:
            await asyncio.sleep(0.05)
            quiet_for = perf_counter() - heard_changed_at
            if flux and call_forwarding.forwarding_on() and heard_text and quiet_for >= 0.6 and not bot_busy() and early_handoff_checked != heard_text:
                early_handoff_checked = heard_text
                if await asyncio.to_thread(call_forwarding.wants_person, heard_text, earlier_handoff_text):
                    # A clear live request should not wait for Flux's often-missing EndOfTurn.
                    forced_fallback_text = heard_text
                    logger.info("custom voice: ending live handoff turn early: %s", heard_text[:120])
                    finish_utterance()
                    continue
            if flux and heard_text and stuck_after and quiet_for >= stuck_after and not bot_busy() and forced_turn == heard_text and forced_at and perf_counter() - forced_at >= 0.75:
                # Some Flux sessions acknowledge neither the request nor the turn. Do not leave the caller waiting.
                forced_fallback_text = heard_text
                logger.warning("custom voice: Flux did not end the requested turn; answering partial text: %s", heard_text[:120])
                finish_utterance()
                continue
            if flux and heard_text and stuck_after and quiet_for >= stuck_after and not bot_busy() and forced_turn != heard_text:
                # Flux keeps a turn open while someone reads out digits ("eight eight six ... six six three") and once
                # never ended one, leaving a caller giving their number unanswered. The words have stopped changing:
                # ask Flux to end the turn now (it then sends EndOfTurn with the final words, answered once as usual).
                forced_turn = heard_text
                forced_at = perf_counter()
                logger.info("custom voice: Flux turn still open %.1fs after the last words; ending it: %s", quiet_for, heard_text[:120])
                try:
                    await deepgram.send(json.dumps({"type": "ForceEndTurn"}))
                except Exception as error:
                    logger.warning("custom voice: could not end the Flux turn (%s); answering what was heard", error)
                    finish_utterance()
                continue
            # With Nova-3 the same safeguard ends the turn here directly. The ¼-second early start applies to both (on a
            # real call Flux's EagerEndOfTurn mostly came together with EndOfTurn, giving no head start on its own).
            if not flux and heard_text and stuck_after and quiet_for >= stuck_after and not bot_busy():
                # The caller's words have not changed for a while and Deepgram never said the turn ended: end it
                # ourselves rather than leave the caller waiting in silence.
                logger.info("custom voice: no end of turn from Deepgram after %.1fs; answering: %s", quiet_for, heard_text[:120])
                finish_utterance()
                continue
            if wait and quiet_for >= wait:
                start_early()

    def on_flux_turn(payload: Dict[str, Any]) -> None:
        """Deepgram Flux turn events. The transcript is the whole turn so far."""
        nonlocal early_reply
        nonlocal flux_turn_started, flux_turn_logged, forced_fallback_text
        event = payload.get("event")
        text = str(payload.get("transcript") or "").strip()
        if forced_fallback_text and _same_words(text, forced_fallback_text) and event in {"Update", "EagerEndOfTurn", "EndOfTurn"}:
            if event == "EndOfTurn":
                forced_fallback_text = ""
            return  # A delayed Flux update for a turn already handled locally.
        if text:
            heard_changed(text)
        if event == "StartOfTurn":
            flux_turn_started, flux_turn_logged = perf_counter(), False
        elif text and flux_turn_started and not flux_turn_logged and perf_counter() - flux_turn_started > 4:
            # So the log shows what Flux had heard if a turn ever stays open too long.
            flux_turn_logged = True
            logger.info("custom voice: Flux turn open for %.1fs so far: %s", perf_counter() - flux_turn_started, text[:160])
        if event == "EndOfTurn":
            flux_turn_started = 0.0
        if event == "EagerEndOfTurn":
            start_early()  # probably finished: prepare the reply now
        elif event == "TurnResumed" and early_reply:
            drop_reply(early_reply)  # they went on after all
            early_reply = None
        elif event == "EndOfTurn":
            if payload.get("trigger") == "timeout":
                logger.info("custom voice: Flux ended the turn after silence: %s", text[:120])
            utterance[:] = [text] if text else []
            finish_utterance()  # an empty transcript falls back to the words heard during the turn

    def finish_utterance() -> None:
        nonlocal reply_task, unanswered_text, early_reply, heard_text
        bind_call(call_sid)
        text = " ".join(utterance).strip()
        if not re.search(r"[A-Za-z0-9]", text):
            text = ""  # "." or "?" alone: no words were said
        if not re.search(r"[A-Za-z0-9]", heard_text):
            heard_text = ""
        if not text and heard_text:
            # No final words, but the partial transcript showed what the caller said: use that.
            logger.info("custom voice: Deepgram gave no final words; using what it heard: %s", heard_text[:120])
            text = heard_text
        utterance.clear()
        prepared, early_reply, heard_text = early_reply, None, ""
        if not text:
            drop_reply(prepared)
            return
        if transfer_pending:
            drop_reply(prepared)
            transcript.append({"content": text, "timestamp": utc_iso()})
            logger.info("custom voice heard call=%s during handoff: %s", call_sid, text[:120])
            save_progress()
            return
        if not unanswered_text and not (reply_task and not reply_task.done()) and call_is_over(text, last_agent_text(memory.history())):
            # We said goodbye and the caller only said bye back: hang up instead of another goodbye per fragment.
            drop_reply(prepared)
            transcript.append({"content": text, "timestamp": utc_iso()})
            save_progress()
            logger.info("custom voice: both said goodbye, ending call=%s: %s", call_sid, text[:120])
            asyncio.create_task(end_call_after_playback())
            return
        if prepared and not _same_words(prepared["text"], text):
            drop_reply(prepared)
            prepared = None
        if reply_task and not reply_task.done() and unanswered_text:
            # The caller kept talking before the previous part was answered (a mid-sentence pause):
            # answer the whole thing once instead of replying to each fragment.
            reply_task.cancel()
            text = f"{unanswered_text} {text}"
            drop_reply(prepared)  # prepared for the second part only
            prepared = None
            if transcript:
                transcript.pop()
        elif reply_task and not reply_task.done():
            reply_task.cancel()
        unanswered_text = text
        transcript.append({"content": text, "timestamp": utc_iso()})
        logger.info("custom voice heard call=%s: %s", call_sid, text[:120])
        save_progress()
        if call_sid and call_forwarding.forwarding_on():  # a cached flag: no waiting here
            reply_task = asyncio.create_task(answer_or_forward(text, perf_counter(), prepared))
            reply_task.add_done_callback(log_reply_failure)
            return
        reply_task = asyncio.create_task(answer(text, perf_counter(), prepared))
        reply_task.add_done_callback(log_reply_failure)

    deepgram: Any = None
    audio_backlog: List[bytes] = []   # caller audio that arrived before Deepgram finished connecting
    transcript_task: Optional[asyncio.Task[Any]] = None

    async def receive_transcripts(connection: Any) -> None:
        async for raw in connection:
            try:
                payload = json.loads(raw)
                kind = payload.get("type")
                if kind == "TurnInfo":  # Deepgram Flux
                    words = str(payload.get("transcript") or "").split()
                    # Real words (not line noise) while the agent is busy means the caller is barging in.
                    if bot_busy() and len(words) >= 2 and payload.get("event") in {"Update", "StartOfTurn"}:
                        await interrupt()
                    on_flux_turn(payload)
                    continue
                if kind in {"Error", "Warning"}:
                    logger.warning("custom voice: Deepgram %s %s: %s", kind.lower(), payload.get("code"), payload.get("description"))
                    continue
                if kind == "UtteranceEnd":
                    finish_utterance()
                    continue
                if kind != "Results":
                    continue
                alternative = ((payload.get("channel") or {}).get("alternatives") or [{}])[0]
                text = str(alternative.get("transcript") or "").strip()
                if not text:
                    # Deepgram can end a turn with an empty final transcript for a short word it showed in the partial
                    # one ("kitchen"): still end the turn, answering what it showed, instead of waiting forever.
                    if payload.get("is_final") and payload.get("speech_final") and heard_text and not bot_busy():
                        finish_utterance()
                    continue
                # Real words (not line noise) while the agent is busy means the caller is barging in.
                if bot_busy() and len(text.split()) >= 2:
                    await interrupt()
                # Start the knowledge search on what the caller has said so far (see brain_service.query_embedding).
                nonlocal prefetched_at
                if payload.get("is_final") or perf_counter() - prefetched_at > 0.4:
                    prefetched_at = perf_counter()
                    brain_service.prefetch_query_embedding(" ".join([*utterance, text]))
                if payload.get("is_final"):
                    utterance.append(text)
                    if payload.get("speech_final"):
                        finish_utterance()
                    else:
                        heard_changed(" ".join(utterance))
                else:
                    heard_changed(" ".join([*utterance, text]))
            except Exception as error:
                logger.error("custom voice transcript error: %s", error)

    async def open_deepgram() -> None:
        """Connect to Deepgram while the greeting plays, instead of making the caller wait for it first."""
        nonlocal deepgram, transcript_task, flux
        headers = {"Authorization": f"Token {deepgram_key}"}
        connection = None
        if flux:
            try:
                connection = await websockets.connect(_flux_url(), additional_headers=headers, open_timeout=12, ping_interval=20)
                logger.info("custom voice: listening with Deepgram Flux (turn detection by the model)")
            except Exception as error:
                # e.g. Flux not enabled on the Deepgram account: the call still works with Nova-3.
                logger.warning("custom voice: Deepgram Flux unavailable (%s); using Nova-3", error)
                flux = False
        if connection is None:
            connection = await websockets.connect(_deepgram_url(), additional_headers=headers, open_timeout=12, ping_interval=20)
        while audio_backlog:
            await connection.send(audio_backlog.pop(0))
        deepgram = connection
        transcript_task = asyncio.create_task(receive_transcripts(connection))
        asyncio.create_task(speculate())  # ends with the call (when `active` turns false)

    try:
        deepgram_ready = asyncio.create_task(open_deepgram())
        try:
                while True:
                    message = json.loads(await websocket.receive_text())
                    event = message.get("event")
                    if deepgram_ready.done() and deepgram_ready.exception():
                        raise deepgram_ready.exception()
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
                        logger.info("custom voice: greeting starts %.2fs after the call audio connected", perf_counter() - call_started)
                        responses.append({"content": greeting, "timestamp": utc_iso()})
                        memory.add_assistant(greeting)
                        save_progress()
                        reply_task = asyncio.create_task(speak(greeting, cache_key=_audio_cache_key(greeting, engine)))
                        asyncio.create_task(warm_call())
                        reply_task.add_done_callback(log_reply_failure)
                        # Record the bridge phrases and fillers in this call's voice while the greeting plays, so
                        # they can be said instantly.
                        phrases = (*(ALL_BRIDGES if bridges_enabled() else ()), *(_FILLERS if _filler_after_seconds() else ()))
                        if phrases:
                            asyncio.create_task(_warm_audio_cache(phrases, engine))
                    elif event == "media":
                        encoded = ((message.get("media") or {}).get("payload") or "")
                        if encoded:
                            if deepgram is None:
                                audio_backlog.append(base64.b64decode(encoded))
                            else:
                                await deepgram.send(base64.b64decode(encoded))
                    elif event == "mark":
                        if (message.get("mark") or {}).get("name") == pending_mark:
                            pending_mark = ""
                    elif event == "stop":
                        break
        finally:
                active = False
                drop_reply(early_reply)  # a reply started early for a turn that never ended
                if reply_task and not reply_task.done():
                    reply_task.cancel()
                deepgram_ready.cancel()
                if transcript_task:
                    transcript_task.cancel()
                    await asyncio.gather(transcript_task, return_exceptions=True)
                if deepgram is not None:
                    await deepgram.close()
    except WebSocketDisconnect:
        active = False
    except Exception as error:
        logger.error("custom Twilio voice websocket error: %s", error)
    finally:
        await memory.close()
        if speaker is not None:
            await speaker.close()
        if transcript or responses:
            log_call(caller, None, {
                "direction": "inbound",
                "status": "completed",
                "duration": int(round(perf_counter() - call_started)),
                "provider": "custom_twilio",
                "provider_call_id": call_sid or stream_sid,
                "channel_type": "phone_call",
                "provider_call_status": "ended",
                "transcription": [{"type": "user", **item} for item in transcript],
                "ai_responses": [{"type": "bot", **item} for item in responses],
                "messages": live_transcript.ordered_from_lists(transcript, responses),
                "summary": memory.summary or f"Custom Twilio voice call with {len(transcript)} caller turns and {len(responses)} AI responses.",
                "sentiment": "neutral",
                "call_session_id": call_sid or stream_sid,
            })
