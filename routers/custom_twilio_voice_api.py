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
from routers.retell_api import _SemanticPrefetch
from voice_turns import prepare_turn_meaning, needs_facts
from voice_turns import pause_request, pause_reply, STATUS_PHRASES, LOOKUP_BRIDGES, contact_email_followup_note, contact_email_from_messages
from conversation_memory import ConversationMemory, voice_history_token_budget
from voice_wait import with_wait_bridge
from voice_turns import wait_bridge_phrase, complete_nova_turn
import call_forwarding
from voice_turns import caller_region, describe_caller_id, name_note, NumberSpeaker, known_numbers, reply_known_numbers, caller_number_digits, choose_voice_model, contact_check, provided_contact_phone, last_agent_text, place_note, careful_voice_model, earlier_caller_texts, bridges_enabled, bridge_phrase, bridge_note, bridge_delay_seconds, status_after_seconds, status_phrase, ALL_BRIDGES, FAST_TURN_NOTE, call_is_over, yes_to_offer_note, search_text
from voice_keyterms import business_name, current_keyterms
import live_transcript
from logging_setup import bind_call, timed
from twilio_security import stream_token, valid_stream_token, verify_twilio_request
from routers.calls_api import log_call
from voice_provider_settings import custom_stack_enabled, custom_voice_engine, deepgram_voice, provider_snapshot
from routers.twilio_api import _browser_caller_phone, _build_conversation_twiml, _get_request_base_url
from voice_languages import effective_language, language_enabled, language_for_call, language_greeting, normalize_language

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


def _deepgram_url(language: str = "en") -> str:
    code = normalize_language(language)
    auto_language = (os.getenv("CUSTOM_AUTO_LANGUAGE", "true").strip().lower() not in {"0", "false", "no", "off"}
                     and code != "ur")  # Urdu is supported by the dedicated Nova model, not Nova-3 multi.
    model = os.getenv("CUSTOM_DEEPGRAM_MODEL_MULTI", "").strip() or os.getenv(f"CUSTOM_DEEPGRAM_MODEL_{code.upper()}", "").strip() or os.getenv("CUSTOM_DEEPGRAM_MODEL", "nova-3").strip() or "nova-3"
    # Words callers say that speech recognition could get wrong (business, brand and product names), taken from the
    # knowledge automatically (voice_keyterms.py) plus any extras in .env. Nova-3 listens for them ("keyterm").
    keyterms = current_keyterms()
    return (
        "wss://api.deepgram.com/v1/listen?"
        f"model={model}&language={quote('multi' if auto_language else {'en': 'en-US', 'ar': 'ar', 'ur': 'ur'}.get(code, 'en-US'))}&encoding=mulaw&sample_rate=8000&channels=1"
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


def _flux_url(language: str = "en") -> str:
    """Deepgram Flux (/v2/listen) for phone audio. Turn ends are decided by the model: EndOfTurn once its confidence
    reaches CUSTOM_FLUX_EOT_THRESHOLD (0.5-1.0), EagerEndOfTurn earlier at CUSTOM_FLUX_EAGER_THRESHOLD (0.3-0.9, the
    signal to start preparing the reply), and an EndOfTurn anyway after CUSTOM_FLUX_EOT_TIMEOUT_MS of silence."""
    eot = _env_number("CUSTOM_FLUX_EOT_THRESHOLD", 0.7, 0.5, 1.0)
    eager = min(eot, _env_number("CUSTOM_FLUX_EAGER_THRESHOLD", 0.4, 0.3, 0.9))  # Flux rejects eager > eot
    # End the turn promptly after a real pause while retaining room for callers who
    # pause briefly between clauses. Override for callers who need longer pauses.
    timeout_ms = int(_env_number("CUSTOM_FLUX_EOT_TIMEOUT_MS", 1800, 500, 60000))
    code = normalize_language(language)
    model = os.getenv(f"CUSTOM_FLUX_MODEL_{code.upper()}", "").strip() or os.getenv("CUSTOM_FLUX_MODEL", "flux-general-en").strip() or "flux-general-en"
    return (
        "wss://api.deepgram.com/v2/listen?"
        f"model={model}&encoding=mulaw&sample_rate=8000"
        f"&eot_threshold={eot}&eager_eot_threshold={eager}&eot_timeout_ms={timeout_ms}"
        + "".join(f"&keyterm={quote(term)}" for term in current_keyterms()[:50])
    )


def _elevenlabs_voice(language: str = "en") -> str:
    code = normalize_language(language)
    voice_variable = {"ar": "ELEVENLABS_VOICE_AR", "ur": "ELEVENLABS_VOICE_UR"}.get(code, "ELEVENLABS_VOICE_ID")
    return os.getenv(voice_variable, "").strip() or os.getenv("ELEVENLABS_VOICE_ID", "").strip()


def _elevenlabs_request(language: str = "en") -> tuple[str, Dict[str, str], str]:
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id = _elevenlabs_voice(language)
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


async def _elevenlabs_stream(text: str, language: str = "en") -> AsyncIterator[bytes]:
    """Yield caller-ready mulaw/8k audio while ElevenLabs is still generating it. If no audio arrives within
    CUSTOM_TTS_FIRST_AUDIO_TIMEOUT_MS, the request is made once more on a fresh connection."""
    url, headers, model_id = _elevenlabs_request(language)
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


async def _elevenlabs_speech(text: str, language: str = "en") -> bytes:
    return b"".join([chunk async for chunk in _elevenlabs_stream(text, language)])


async def _deepgram_tts_stream(text: str, language: str = "en") -> AsyncIterator[bytes]:
    """Stream an Aura REST response or a Flux TTS WebSocket response in Twilio's format."""
    api_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Deepgram voice is not configured")
    try:
        gain_db = float(os.getenv("DEEPGRAM_TTS_GAIN_DB", "7"))
    except ValueError:
        gain_db = 7.0
    model = deepgram_voice(language)
    if model.lower().startswith("flux-"):
        url = f"wss://api.deepgram.com/v2/speak?model={model}&encoding=mulaw&sample_rate=8000"
        async with websockets.connect(url, additional_headers={"Authorization": f"Token {api_key}"}, open_timeout=10, ping_interval=20) as ws:
            await ws.send(json.dumps({"type": "Speak", "text": text}))
            await ws.send(json.dumps({"type": "Flush"}))
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    yield adjust_ulaw_gain(bytes(message), gain_db)
                    continue
                payload = json.loads(message)
                kind = payload.get("type")
                if kind in {"SpeechMetadata", "Flushed"}:
                    break
                if kind in {"Error", "Warning"}:
                    raise RuntimeError(f"Deepgram Flux TTS {kind}: {str(payload)[:300]}")
            try:
                await ws.send(json.dumps({"type": "Close"}))
            except Exception:
                pass
        return
    url = f"https://api.deepgram.com/v1/speak?model={model}&encoding=mulaw&sample_rate=8000&container=none"
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
    """One live Deepgram TTS connection per call (opened while the greeting plays). Each sentence is sent down the
    open line: about 0.6-0.7 s to first audio, steadily, where a separate web request per sentence took 1-2 s from
    here. If the caller interrupts mid-sentence the line is dropped and reopened, so stale audio never plays later."""

    def __init__(self, language: str = "en") -> None:
        self.language = normalize_language(language)
        self.ws: Any = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def _ensure(self) -> Any:
        if self.ws is None:
            model = deepgram_voice(self.language)
            version = "v2" if model.lower().startswith("flux-") else "v1"
            url = f"wss://api.deepgram.com/{version}/speak?model={model}&encoding=mulaw&sample_rate=8000"
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

    async def set_language(self, language: str) -> None:
        code = normalize_language(language)
        if code == self.language:
            return
        self.language = code
        await self._drop()

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
                        if kind in {"Flushed", "SpeechMetadata"}:
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


def _tts_engine_for_language(engine: str, language: str = "en") -> str:
    """Use ElevenLabs automatically for Arabic when Deepgram has no Arabic TTS model configured."""
    if engine == "deepgram" and normalize_language(language) == "ar" and not os.getenv("DEEPGRAM_TTS_VOICE_AR", "").strip():
        if os.getenv("ELEVENLABS_API_KEY", "").strip() and _elevenlabs_voice(language):
            return "elevenlabs"
    return engine


def _tts_stream(text: str, engine: str, language: str = "en") -> AsyncIterator[bytes]:
    """Speech for one sentence from the provider selected for this language."""
    selected_engine = _tts_engine_for_language(engine, language)
    return _deepgram_tts_stream(text, language) if selected_engine == "deepgram" else _elevenlabs_stream(text, language)


async def _speech(text: str, engine: str, language: str = "en") -> bytes:
    return b"".join([chunk async for chunk in _tts_stream(text, engine, language)])


# Greeting and filler audio never change, so synthesize each phrase once per process.
_cached_audio: Dict[str, bytes] = {}
# Said when the reply is slow to start, the way a person says "Yeah..." while thinking (CUSTOM_FILLER_AFTER_SECONDS).


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
    normal = lambda text: " ".join(re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.casefold()))
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
                           bridge: str = "", verified_phone: str = "", language: str = "en",
                           timings: Optional[Dict[str, Any]] = None) -> AsyncIterator[str]:
    """Stream the reply and yield it one finished sentence at a time, so speech can start before the model is done.
    Uses the same voice prompt, knowledge search and reply guard as the Retell agent (routers/retell_api.py).
    bridge: the phrase being spoken right now ("Sure, let me check."); the reply continues from it."""
    phone = caller if caller.startswith("+") else "unknown"
    started = perf_counter()
    agent_text = last_agent_text(history)
    earlier_text = " ".join(" ".join(earlier_caller_texts(history, text)[-8:]).split()[-25:])
    await asyncio.to_thread(prepare_turn_meaning, text, agent_text, earlier_text)

    def check_places():
        return place_note(text, brain_service.knowledge_text(), agent_text)

    def prepare_question():
        if needs_facts(text):
            return _system_prompt(search_text(text, [*history, {"role": "user", "content": text}]), phone, summary, history, True, language)
        return None

    turn_model, (details_note, details_problem), places, prepared_prompt = await asyncio.gather(
        asyncio.to_thread(choose_voice_model, text, agent_text, earlier_text),
        asyncio.to_thread(contact_check, text, agent_text, earlier_caller_texts(history, text), caller_region(caller)),
        asyncio.to_thread(check_places),
        asyncio.to_thread(prepare_question),
    )
    if details_problem:
        turn_model = None  # a wrong number or email needs the careful model
    if places:
        turn_model = careful_voice_model(services.fallback_model) or None  # the main model tends to say "yes, covered"
    if prepared_prompt is not None:
        # The fast model handles non-product questions too, with the same relevant business facts as the main model.
        system = prepared_prompt
    else:
        def prepare_selected_prompt():
            return _system_prompt(search_text(text, [*history, {"role": "user", "content": text}]), phone, summary, history, turn_model is None or bool(places), language)
        system = await asyncio.to_thread(prepare_selected_prompt)
    if call_forwarding.forwarding_on():
        system += ("\n\n# Live team transfer on this call\n"
                   "A live transfer to the human team is available. If the caller asks to speak with the team now, "
                   "do not say calls are unavailable or offer a callback instead. Do not ask for a name or phone number "
                   "before a live transfer. The call system connects them; a callback needs contact details only when "
                   "the caller explicitly wants the team to call later. The call system announces and starts a live "
                   "transfer separately. In an ordinary AI reply, never claim a transfer is already happening.")
    prompt_ms = (perf_counter() - started) * 1000
    requested_model = turn_model or services.config.OPENROUTER_MODEL
    if timings is not None:
        timings.update(requested_model=requested_model, preparation_ms=round(prompt_ms))
    first_token_ms = 0.0
    reminder_text = voice_turn_reminder()
    conversation = [*history, {"role": "user", "content": text}]
    messages = [{"role": "system", "content": system}, *conversation] + ([{"role": "system", "content": reminder_text}] if reminder_text else [])
    if details_note:
        messages.append({"role": "system", "content": details_note})
    email_note = contact_email_followup_note([*history, {"role": "user", "content": text}], text)
    if email_note:
        messages.append({"role": "system", "content": email_note})
    if verified_phone:
        messages.append({"role": "system", "content": f"The caller has already provided a valid contact number: {verified_phone}. Keep it for this call. Do not call it incomplete or ask for it again."})
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
    if bridges_enabled():
        messages.append({"role": "system", "content": "The call system handles any short wait acknowledgement. "
                         "Start directly with the actual answer; do not add or repeat a wait/checking phrase."})
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
                            requested_model, prompt_ms, first_token_ms, first_token_ms - prompt_ms)
                if timings is not None:
                    timings.update(first_model_text_ms=round(first_token_ms),
                                   model_wait_ms=round(max(0.0, first_token_ms - prompt_ms)))
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
    """Legacy wait override; still uses the one eligible bridge policy rather than extra random fillers."""
    try:
        return max(0.0, float(os.getenv("CUSTOM_FILLER_AFTER_SECONDS", "0")))
    except ValueError:
        return 0.0


def _audio_cache_key(text: str, engine: str, language: str = "en") -> str:
    selected_engine = _tts_engine_for_language(engine, language)
    voice = deepgram_voice(language) if selected_engine == "deepgram" else _elevenlabs_voice(language)
    return f"{selected_engine}:{normalize_language(language)}:{voice}:{text}"


async def _warm_audio_cache(texts: tuple[str, ...], engine: str, language: str = "en") -> None:
    for text in texts:
        key = _audio_cache_key(text, engine, language)
        if key not in _cached_audio:
            try:
                _cached_audio[key] = await _speech(text, engine, language)
            except Exception as error:
                logger.warning("could not pre-generate voice phrase %r: %s", text, error)


def _greeting_text(language: str = "en") -> str:
    configured = os.getenv("CUSTOM_VOICE_GREETING", "").strip()
    if configured:
        return configured if normalize_language(language) == "en" else language_greeting(language, configured)
    try:
        configured = str(brain_service.channel_config("voice").get("greeting") or "").strip() or "Hello, how can I help you today?"
        return language_greeting(language, configured)
    except Exception:
        return language_greeting(language, "Hello, how can I help you today?")


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
    caller_phone = _browser_caller_phone(form.get("caller_phone"))
    source_phone = str(form.get("From") or "") if str(form.get("From") or "").startswith("+") else caller_phone
    if source_phone:
        payload["from_number"] = source_phone
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
    call_forwarding.remember_retell_call(retell_call_id, str(form.get("CallSid") or ""), source_phone or str(form.get("From") or ""))
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
    providers = await asyncio.to_thread(provider_snapshot)
    form = dict(await request.form())
    call_language = language_for_call(form)
    bind_call(form.get("CallSid"))
    call_forwarding.forwarding_on()  # refresh the cached switch before the first caller turn
    if providers["retell"]["enabled"] and providers["retell"]["ready"]:
        try:
            return PlainTextResponse(_with_full_call_recording(await _retell_sip_twiml(form), _public_base(request)), media_type="text/xml")
        except Exception as error:
            logger.error("retell routing failed, using default Twilio agent: %s", error)
            return PlainTextResponse(_with_full_call_recording(_build_conversation_twiml(_get_request_base_url(request), call_language, _browser_caller_phone(form.get("caller_phone"))), _public_base(request)), media_type="text/xml")

    custom = _active_custom(providers)
    if not custom["enabled"] or not custom["ready"]:
        # Default provider: never reject a caller, hand the call to the standard Twilio AI agent.
        logger.info(
            "custom voice unavailable (enabled=%s, missing=%s); using default Twilio ConversationRelay",
            custom["enabled"], custom["missing_configuration"],
        )
        return PlainTextResponse(_with_full_call_recording(_build_conversation_twiml(_get_request_base_url(request), call_language, _browser_caller_phone(form.get("caller_phone"))), _public_base(request)), media_type="text/xml")

    base = _public_base(request)
    if not base.startswith(("https://", "http://")):
        raise HTTPException(status_code=503, detail="TWILIO_PUBLIC_BASE_URL is not configured")
    stream_url = f"{_to_ws_url(base)}/api/custom-voice/media"
    browser_phone = _browser_caller_phone(form.get("caller_phone"))
    caller = html.escape(browser_phone or str(form.get("From") or "unknown"), quote=True)
    call_sid = html.escape(str(form.get("CallSid") or ""), quote=True)
    logger.info("custom voice stack answering call %s from %s", call_sid, caller)
    # Twilio opens the media stream about a second from now: have the greeting audio ready by then.
    async def warm_greeting() -> None:
        greeting = await asyncio.to_thread(_greeting_text, call_language)
        await _warm_audio_cache((greeting,), custom_voice_engine(), call_language)

    asyncio.create_task(warm_greeting())
    return PlainTextResponse(_with_full_call_recording(
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect><Stream url=\"" + html.escape(stream_url, quote=True) + "\">"
        f'<Parameter name="caller" value="{caller}" /><Parameter name="call_sid" value="{call_sid}" />'
        f'<Parameter name="preferred_language" value="{html.escape(call_language, quote=True)}" />'
        f'<Parameter name="stream_token" value="{stream_token(str(form.get("CallSid") or ""))}" />'
        "</Stream></Connect></Response>", base),
        media_type="text/xml",
    )


@router.websocket("/media")
async def custom_media_stream(websocket: WebSocket) -> None:
    """Bridge Twilio mulaw/8k audio to Deepgram and ElevenLabs."""
    if not await asyncio.to_thread(custom_stack_enabled):
        await websocket.close(code=1008, reason="Custom Twilio voice is disabled")
        return
    custom = _active_custom(await asyncio.to_thread(provider_snapshot))
    preferred_language = normalize_language(websocket.query_params.get("preferred_language"))
    languages_used = {preferred_language}
    engine = custom_voice_engine()  # chosen once per call: "deepgram" or "elevenlabs"
    speaker = _DeepgramSpeaker(preferred_language) if _tts_engine_for_language(engine, preferred_language) == "deepgram" else None  # one live voice line for the whole call
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
    finish_lock = asyncio.Lock()
    utterance: List[str] = []
    reply_task: Optional[asyncio.Task[Any]] = None
    silence_reminder_task: Optional[asyncio.Task[Any]] = None
    transfer_pending = False  # Once promised, a new caller fragment must not cancel the handoff.
    earlier_handoff_text = ""  # keep an unclear team request so a short follow-up is read in context
    contact_phone = ""  # valid number spoken on a browser call, which has no phone caller ID
    contact_tasks: set[asyncio.Task[Any]] = set()
    pending_mark = ""
    mark_count = 0
    recent_bridges: List[str] = []  # acknowledgements said so far, so the last few are never repeated
    # Question currently being answered, until its reply starts playing.
    unanswered_text = ""
    active = True
    stream_failure = ""
    callback_requested = False
    pending_saves: set[asyncio.Task[Any]] = set()
    services: Optional[AIServices] = None   # model client, made on the first answer and reused all call
    prefetched_at = 0.0
    semantic_prefetch = _SemanticPrefetch(prepare_turn_meaning)
    voice_turn_timings: List[Dict[str, Any]] = []
    call_started = perf_counter()
    heard_text = ""             # the caller's words so far in this turn (final parts + the latest partial)
    heard_changed_at = 0.0      # when those words last changed
    early_reply: Optional[Dict[str, Any]] = None  # reply started before the end of the turn (see speculate)
    auto_language = os.getenv("CUSTOM_AUTO_LANGUAGE", "true").strip().lower() not in {"0", "false", "no", "off"}
    flux = _use_flux() and not auto_language  # Nova-3 multilingual handles turn audio when callers can switch languages.
    forced_turn = ""            # the words of a Flux turn we already asked Flux to end (asked once per turn)
    forced_at = 0.0             # ForceEndTurn can go unanswered; then finish from the partial transcript.
    forced_fallback_text = ""
    early_handoff_checked = ""
    early_handoff_task: Optional[asyncio.Task[Any]] = None
    completion_task: Optional[asyncio.Task[Any]] = None
    completion_text = ""
    nova_speech_final_text = ""
    speculation_task: Optional[asyncio.Task[Any]] = None
    flux_turn_started = 0.0     # when the current Flux turn began (for logging turns that stay open long)
    flux_turn_logged = False
    audio_received_seconds = 0.0
    stt_result_events = 0
    stt_final_events = 0
    stt_speech_final_events = 0
    stt_flux_end_of_turn_events = 0
    latest_word_end = 0.0
    turn_word_end = 0.0

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

    def cancel_silence_reminder() -> None:
        nonlocal silence_reminder_task
        if silence_reminder_task and not silence_reminder_task.done():
            silence_reminder_task.cancel()
        silence_reminder_task = None

    def schedule_silence_reminder() -> None:
        nonlocal silence_reminder_task
        cancel_silence_reminder()

        async def remind_if_still_silent() -> None:
            try:
                while active and pending_mark:
                    await asyncio.sleep(0.05)
                if not active:
                    return
                trigger_ms = int(_env_number("VOICE_REMINDER_TRIGGER_MS", 7000, 1000, 60000))
                await asyncio.sleep(trigger_ms / 1000)
                if not active or bot_busy():
                    return
                line = "Is there anything else I can help you with?"
                await speak(line)
                memory.add_assistant(line)
                responses.append({"content": line, "timestamp": utc_iso(), "language": preferred_language})
                save_progress()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("custom voice silence reminder failed: %s", type(error).__name__)

        silence_reminder_task = asyncio.create_task(remind_if_still_silent())

    async def interrupt(force: bool = False) -> None:
        """Caller started talking: stop thinking/speaking and flush audio Twilio has buffered."""
        nonlocal reply_task, pending_mark, unanswered_text
        if transfer_pending:
            return
        if unanswered_text and not force:
            # Still thinking about a question nobody has heard an answer to: the caller is continuing
            # that sentence, not interrupting. finish_utterance() merges the two parts.
            return
        if force:
            unanswered_text = ""
        if reply_task and not reply_task.done():
            reply_task.cancel()
        reply_task = None
        if stream_sid:
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

    async def pause_for_caller(text: str) -> None:
        line = pause_reply(text)
        await speak(line)
        memory.add(text, line)
        responses.append({"content": line, "timestamp": utc_iso(), "language": preferred_language})
        save_progress()

    async def control_reply(text: str, line: str, heard_at: float, turn_timing: Optional[Dict[str, Any]], action: str) -> None:
        """Speak a routing/control result directly, without another model request."""
        nonlocal unanswered_text
        unanswered_text = ""
        spoke_at = utc_iso()
        routing_ms = round((perf_counter() - heard_at) * 1000)
        first_audio = await speak(line)
        first_audio_ms = routing_ms + round(first_audio * 1000)
        timing = {"pipeline": "custom_twilio", "tts_engine": engine, **(turn_timing or {}),
                  "action": action, "routing_ms": routing_ms, "tts_first_audio_ms": round(first_audio * 1000),
                  "first_audio_ms": first_audio_ms, "first_audible_ms": first_audio_ms}
        if "turn_detection_ms" in timing:
            timing["speech_end_to_first_audible_ms"] = timing["turn_detection_ms"] + first_audio_ms
        voice_turn_timings.append(timing)
        spoken_line = " ".join(part for part in ((turn_timing or {}).get("bridge_text", ""), line) if part)
        memory.add(text, spoken_line)
        responses.append({"content": spoken_line, "timestamp": spoke_at, "language": preferred_language})
        save_progress()
        if active and not transfer_pending:
            schedule_silence_reminder()

    async def forward_to_team(text: str, heard_at: float, turn_timing: Optional[Dict[str, Any]] = None) -> None:
        """The caller asked for a person and call forwarding is on: say so, let it play, then ring the team
        (call_forwarding.py). If the transfer cannot start, the caller is told and the AI carries on."""
        nonlocal unanswered_text, transfer_pending, callback_requested
        bind_call(call_sid)
        unanswered_text = ""
        line = call_forwarding.CONNECTING_LINE
        await control_reply(text, line, heard_at, turn_timing, "live_transfer")
        for _ in range(80):  # the line has finished playing (its mark came back); at most ~8 s
            if not pending_mark:
                break
            await asyncio.sleep(0.1)
        if await asyncio.to_thread(call_forwarding.transfer, call_sid, caller, "", text, preferred_language):
            logger.info("custom voice: call=%s handed to the team %.2fs after the caller asked", call_sid, perf_counter() - heard_at)
            return
        transfer_pending = False
        callback_requested = True
        fallback = "Sorry, I couldn't put you through just now. I've noted that you'd like to talk to someone, and our team will call you back."
        await speak(fallback)
        memory.add(text, fallback)
        responses.append({"content": fallback, "timestamp": utc_iso(), "language": preferred_language})
        save_progress()

    async def answer_or_forward(text: str, heard_at: float, prepared: Optional[Dict[str, Any]], turn_timing: Optional[Dict[str, Any]] = None) -> None:
        """Resolve semantic handoff intent and conversation state, otherwise answer as usual."""
        nonlocal transfer_pending, earlier_handoff_text, unanswered_text, callback_requested
        agent_text = last_agent_text(memory.history())
        turn_timing = dict(turn_timing or {})
        bridge_diagnostics = prepared["bridge_diagnostics"] if prepared else {}
        bridge_ready = prepared["bridge_ready"] if prepared else asyncio.create_task(
            prepare_bridge(text, memory.history(), bridge_diagnostics))

        async def resolve():
            intent = await asyncio.to_thread(call_forwarding.contextual_handoff_intent, text, agent_text, callback_requested)
            turn_timing["handoff_source"] = "semantic_context" if intent is not None else "semantic"
            if intent is None:
                await asyncio.to_thread(prepare_turn_meaning, text, agent_text)
                intent = await asyncio.to_thread(call_forwarding.handoff_intent, text, earlier_handoff_text)
            yield intent

        async def routing_notice():
            line = await asyncio.shield(bridge_ready)
            return STATUS_PHRASES[0] if line in LOOKUP_BRIDGES else line

        bridge_pending = False
        def record_notice(line):
            nonlocal bridge_pending
            bridge_pending = True
            turn_timing.update(bridge_text=line, bridge_ms=round((perf_counter() - heard_at) * 1000))

        deadline = (_filler_after_seconds() or max(bridge_delay_seconds(), status_after_seconds())) if bridges_enabled() else 0
        try:
            async with contextlib.aclosing(with_wait_bridge(resolve(), routing_notice, "", deadline, heard_at, record_notice)) as routed:
                async for item in routed:
                    if bridge_pending:
                        bridge_pending = False
                        recent_bridges.append(item.strip())
                        delay = await speak(item.strip())
                        turn_timing["bridge_first_audio_ms"] = turn_timing["bridge_ms"] + round(delay * 1000)
                    else:
                        intent = item
        except BaseException:
            bridge_ready.cancel()
            drop_reply(prepared)
            raise
        turn_timing.update(bridge_diagnostics)
        if not intent:
            prepared = prepared or start_reply(text, bridge_ready, bridge_diagnostics)
        else:
            bridge_ready.cancel()
        if intent in {"callback_ack", "declined"}:
            earlier_handoff_text = ""
            drop_reply(prepared)
            line = "You're welcome." if intent == "callback_ack" else "Of course."
            await control_reply(text, line, heard_at, turn_timing, intent)
        elif intent == "live" and call_forwarding.forwarding_on():
            earlier_handoff_text = ""
            callback_requested = False
            transfer_pending = True
            drop_reply(prepared)
            await forward_to_team(text, heard_at, turn_timing)
        elif intent in {"live", "callback"}:
            earlier_handoff_text = ""
            drop_reply(prepared)
            if callback_requested:
                await control_reply(text, "Your callback request is already with the team.", heard_at, turn_timing, "callback_ack")
                return
            email = contact_email_from_messages(memory.history())
            callback_number = contact_phone or caller
            await asyncio.to_thread(
                call_forwarding.create_callback_outreach,
                callback_number,
                text,
                call_sid,
                "",
                "The caller requested a callback. Please follow up using the caller's phone number.",
                contact_email=email,
            )
            callback_requested = True
            known_phone = bool(re.fullmatch(r"\+?[0-9 ()-]{8,}", callback_number))
            line = ("I've noted your callback request. The team will call you on this number." if known_phone
                    else "I've passed on your callback request. What phone number should the team use?")
            if intent == "live":
                line = "A live transfer isn't available right now. " + line
            await control_reply(text, line, heard_at, turn_timing, "callback_requested")
        elif intent in {"callback", "unclear"} and await asyncio.to_thread(call_forwarding.requested_agent, text):
            # A named request should never disappear into a generic callback queue, even when a live transfer
            # is unavailable or the caller did not clearly say "now". Create the outreach for that person.
            earlier_handoff_text = ""
            drop_reply(prepared)
            destination = await asyncio.to_thread(
                call_forwarding.create_callback_outreach,
                caller,
                text,
                call_sid,
                "",
                "The caller asked to speak with a specific team member. Please follow up with this caller.",
                contact_email=contact_email_from_messages(memory.history()),
            )
            callback_requested = True
            line = f"I have noted your request for {destination}. They or the team will contact you using this number."
            await control_reply(text, line, heard_at, turn_timing, "callback_requested")
        elif intent == "unclear":
            if len(text.split()) > 2:
                earlier_handoff_text = text
            unanswered_text = ""
            drop_reply(prepared)
            line = "Would you like me to connect you with the team now, or have someone call you back later?"
            await control_reply(text, line, heard_at, turn_timing, "handoff_clarification")
        else:
            earlier_handoff_text = ""
            await answer(text, heard_at, prepared, turn_timing)

    async def use_deepgram_voice(error: Exception) -> None:
        nonlocal engine, speaker
        # HTTPStatusError may carry a streaming response whose body was never read.
        # Accessing .text there raises ResponseNotRead and prevents the fallback.
        detail = f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else str(error)
        logger.warning("ElevenLabs voice failed (%s: %s); this call continues with the Deepgram voice", type(error).__name__, detail)
        engine, speaker = "deepgram", _DeepgramSpeaker(preferred_language)
        # Acknowledgements are only said from recorded audio: record the Deepgram versions in the background.
        asyncio.create_task(_warm_audio_cache(ALL_BRIDGES if bridges_enabled() else (), engine, preferred_language))

    async def speak(text: str, cache_key: str = "", forced_engine: Optional[str] = None) -> float:
        """Stream speech to Twilio; returns seconds until the first audio chunk was sent."""
        nonlocal pending_mark, mark_count
        selected_engine = forced_engine or _tts_engine_for_language(engine, preferred_language)
        selected_speaker = speaker if selected_engine == "deepgram" else None
        if selected_speaker:
            await selected_speaker.set_language(preferred_language)
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
                audio = selected_speaker.stream(text) if selected_speaker else _tts_stream(text, selected_engine, preferred_language)
                async with contextlib.aclosing(audio):
                    async for chunk in audio:
                        if not active:
                            return first_audio
                        if not first_audio:
                            first_audio = perf_counter() - started
                        chunks.append(chunk)  # the cache keeps the original audio; it is levelled when played
                        await send_audio(leveller.process(chunk) if leveller else chunk)
            except (httpx.HTTPError, RuntimeError) as error:
                if selected_engine != "elevenlabs" or not os.getenv("DEEPGRAM_API_KEY", "").strip():
                    raise
                # ElevenLabs refused or cut off the sentence (out of credits, a dropped stream): the rest of the call
                # speaks with the Deepgram voice, starting with this whole sentence again, instead of going silent.
                await use_deepgram_voice(error)
                return await speak(text, forced_engine="deepgram")
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
                asyncio.to_thread(prepare_turn_meaning, "", last_agent_text(memory.history())),
                asyncio.to_thread(_system_prompt, "hello", phone, "", None, True, preferred_language),
                speaker.warm() if speaker else _warm_tts_connection(engine),
                return_exceptions=True,
            )
        except Exception as error:
            logger.debug("call warm-up failed: %s", error)

    async def prepare_bridge(text, history, diagnostics=None):
        diagnostics = diagnostics if diagnostics is not None else {}
        started = perf_counter()
        if not bridges_enabled():
            diagnostics.update(bridge_candidate=False, bridge_ready_ms=round((perf_counter() - started) * 1000))
            return ""
        eligibility_started = perf_counter()
        earlier_text = " ".join(" ".join(earlier_caller_texts(history, text)[-8:]).split()[-25:])
        await asyncio.to_thread(prepare_turn_meaning, text, last_agent_text(history), earlier_text)
        candidate = await asyncio.to_thread(wait_bridge_phrase, text, recent_bridges[-3:],
                                           " ".join(earlier_caller_texts(history, text)[-8:]))
        diagnostics.update(bridge_candidate=bool(candidate),
                           bridge_eligibility_ms=round((perf_counter() - eligibility_started) * 1000))
        if not candidate:
            diagnostics.update(bridge_selector="none", bridge_ready_ms=round((perf_counter() - started) * 1000))
            return ""
        # wait_bridge_phrase already selected an approved, semantically appropriate line. A second Llama call only
        # chooses between approved phrases and can consume most of the time the bridge is meant to save.
        diagnostics.update(bridge_selector="semantic_candidate", bridge_ready_ms=round((perf_counter() - started) * 1000))
        return candidate

    def start_reply(text: str, bridge_ready=None, bridge_diagnostics=None) -> Dict[str, Any]:
        """Prepare the real answer independently of semantic bridge selection."""
        history = memory.history()
        diagnostics = bridge_diagnostics if bridge_diagnostics is not None else {}
        bridge_ready = bridge_ready if bridge_ready is not None else asyncio.create_task(prepare_bridge(text, history, diagnostics))
        reply_timings: Dict[str, Any] = {}

        async def stream():
            # Bridge eligibility and the real answer run independently. A slow classifier must never hold up
            # the knowledge/model request, and a missing audio-cache entry must never suppress an eligible bridge.
            async with contextlib.aclosing(_voice_sentences(services, text, history, caller, memory.summary, "", contact_phone,
                                                              preferred_language, timings=reply_timings)) as source:
                async for sentence in source:
                    yield sentence

        sentences = stream().__aiter__()
        return {"text": text, "bridge_ready": bridge_ready, "bridge_diagnostics": diagnostics,
                "reply_timings": reply_timings, "sentences": sentences,
                "first": asyncio.ensure_future(sentences.__anext__()), "started": perf_counter()}

    def drop_reply(prepared: Optional[Dict[str, Any]]) -> None:
        """Throw away a reply that was prepared early but will not be used (the caller said something else)."""
        if not prepared:
            return
        prepared["bridge_ready"].cancel()
        if prepared["first"].done():
            asyncio.ensure_future(prepared["sentences"].aclose())
        else:
            prepared["first"].cancel()  # also stops the model request

    async def answer(text: str, heard_at: float, prepared: Optional[Dict[str, Any]] = None,
                     turn_timing: Optional[Dict[str, Any]] = None) -> None:
        """Think and speak at the same time: each sentence goes to ElevenLabs as soon as the model finishes it.
        `prepared`: the reply already started while the caller was finishing (see speculate)."""
        nonlocal unanswered_text, services, contact_phone
        bind_call(call_sid)  # tasks copy their context when created, before the call id was known
        contact_history = memory.history()
        contact_question = last_agent_text(contact_history)
        contact_earlier = earlier_caller_texts(contact_history, text)
        async def capture_contact() -> None:
            nonlocal contact_phone
            try:
                spoken = await asyncio.to_thread(
                    provided_contact_phone, text, contact_question, contact_earlier, caller_region(caller))
                if spoken:
                    contact_phone = spoken
            except Exception as error:
                logger.warning("could not capture a spoken contact number: %s", error)

        contact_task = asyncio.create_task(capture_contact())
        contact_tasks.add(contact_task)
        contact_task.add_done_callback(contact_tasks.discard)
        if services is None:
            services = await asyncio.to_thread(_voice_services)
        if prepared:
            logger.info("custom voice early start used call=%s: reply began %.2fs before the end of the turn", call_sid, heard_at - prepared["started"])
        # The model and the knowledge search start now (or already started), before anything is spoken.
        prepared = prepared or start_reply(text)
        sentences, first = prepared["sentences"], prepared["first"]
        prefix = (turn_timing or {}).get("bridge_text", "")
        spoken: List[str] = [prefix] if prefix else []
        spoke_at = utc_iso() if prefix else ""
        try:
            if await asyncio.to_thread(sounds_unfinished, text):
                # Probably a pause mid-sentence ("what are the popular ..."): give the caller a moment to finish. The
                # model is already working meanwhile; if they go on, finish_utterance cancels this and answers it all.
                waited = max(0.0, unfinished_wait_seconds() - (perf_counter() - prepared["started"]))
                if waited:
                    await asyncio.sleep(waited)
            filler_spoken = bool(prefix)
            bridge_pending = False
            bridge_timing: Dict[str, Any] = dict(prepared.get("bridge_diagnostics") or {})
            bridge_timing.update({key: value for key, value in (turn_timing or {}).items() if key.startswith("bridge_")})

            async def first_sentence():
                try:
                    yield await first
                except StopAsyncIteration:
                    return

            async def make_bridge():
                return await prepared["bridge_ready"]

            def record_bridge(line):
                nonlocal bridge_pending
                bridge_pending = True
                bridge_timing["bridge_ms"] = round((perf_counter() - heard_at) * 1000)

            deadline = (_filler_after_seconds() or max(bridge_delay_seconds(), status_after_seconds())) if bridges_enabled() else 0
            stream = with_wait_bridge(first_sentence(), make_bridge, "", 0 if prefix else deadline, heard_at, record_bridge)
            try:
                sentence = ""
                async with contextlib.aclosing(stream):
                    async for chunk in stream:
                        if bridge_pending:
                            bridge_pending = False
                            filler_spoken = True
                            line = chunk.strip()
                            recent_bridges.append(line)
                            spoken.append(line)
                            spoke_at = utc_iso()
                            audio_delay = await speak(line, cache_key=_audio_cache_key(line, engine, preferred_language))
                            bridge_timing["bridge_first_audio_ms"] = bridge_timing["bridge_ms"] + round(audio_delay * 1000)
                        else:
                            sentence = chunk
                if not sentence:
                    sentence = "Sorry, I could not answer that just now."
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
            timing = {"pipeline": "custom_twilio", "tts_engine": engine,
                                       **(turn_timing or {}),
                                       **bridge_timing,
                                       **prepared.get("reply_timings", {}),
                                       "first_sentence_ms": round(brain_seconds * 1000),
                                       "tts_first_audio_ms": round(first_audio * 1000),
                                       "first_audio_ms": round((brain_seconds + first_audio) * 1000),
                                       "first_audible_ms": bridge_timing.get("bridge_first_audio_ms", round((brain_seconds + first_audio) * 1000))}
            if "turn_detection_ms" in timing:
                timing["speech_end_to_first_audible_ms"] = timing["turn_detection_ms"] + timing["first_audible_ms"]
            voice_turn_timings.append(timing)
            logger.info("custom voice turn call=%s first_sentence=%.2fs first_audio=%.2fs total=%.2fs", call_sid, brain_seconds, first_audio, brain_seconds + first_audio)
            async for sentence in sentences:
                spoken.append(sentence)
                await speak(sentence)
            if active and spoken:
                schedule_silence_reminder()
        finally:
            prepared["bridge_ready"].cancel()
            if not first.done():
                first.cancel()  # interrupted before the first sentence: stop the model request too
            await asyncio.gather(first, prepared["bridge_ready"], return_exceptions=True)
            await sentences.aclose()
            # Record what the caller actually heard, also when they interrupted part-way through.
            if spoken:
                reply = " ".join(spoken)
                memory.add(text, reply)
                responses.append({"content": reply, "timestamp": spoke_at or utc_iso(), "language": preferred_language})
                save_progress()
                asyncio.get_running_loop().run_in_executor(None, _log_voice_activity, text, reply, caller, perf_counter() - heard_at)

    def log_reply_failure(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and task.exception():
            logger.error("custom voice reply failed: %s", task.exception())

    def heard_changed(text: str) -> None:
        """The caller's words so far changed: an early reply for different words is no longer any use."""
        nonlocal heard_text, heard_changed_at, early_reply, nova_speech_final_text
        cancel_silence_reminder()
        if text != heard_text:
            if nova_speech_final_text and not _same_words(text, nova_speech_final_text):
                nova_speech_final_text = ""
            heard_text, heard_changed_at = text, perf_counter()
            if not transfer_pending:
                semantic_history = memory.history()
                semantic_prefetch.schedule(text, last_agent_text(semantic_history),
                    " ".join(" ".join(earlier_caller_texts(semantic_history, text)[-8:]).split()[-25:]))
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
        nonlocal forced_turn, forced_at, forced_fallback_text, early_handoff_checked, early_handoff_task
        nonlocal completion_task, completion_text
        wait = _speculate_after_seconds()
        stuck_after = _turn_timeout_seconds()
        incomplete_after = _env_number("CUSTOM_NOVA_INCOMPLETE_WAIT_MS", 1800, 1000, 5000) / 1000
        while active:
            await asyncio.sleep(0.05)
            quiet_for = perf_counter() - heard_changed_at
            if not flux and heard_text and not bot_busy():
                try:
                    complete_after = max(0.3, float(os.getenv("CUSTOM_COMPLETE_TURN_MS", "450")) / 1000)
                except ValueError:
                    complete_after = 0.45
                speech_final_pending = bool(nova_speech_final_text and _same_words(nova_speech_final_text, heard_text))
                if completion_task is not None and completion_task.done():
                    try:
                        complete = completion_task.result()
                    except Exception:
                        complete = None
                    completion_task = None
                    if (completion_text == heard_text and complete is True
                            and (speech_final_pending or quiet_for >= complete_after)):
                        await finish_utterance("nova_semantic_complete")
                        continue
                    if (completion_text == heard_text and complete is None
                            and (speech_final_pending or quiet_for >= complete_after)
                            and nova_speech_final_text and _same_words(nova_speech_final_text, heard_text)):
                        # Without a confident meaning result, retain Deepgram's final signal as the fallback.
                        await finish_utterance("nova_speech_final")
                        continue
                if completion_task is None and quiet_for >= complete_after and completion_text != heard_text:
                    completion_text = heard_text
                    completion_task = asyncio.create_task(asyncio.to_thread(complete_nova_turn, heard_text))
            if early_handoff_task is not None and early_handoff_task.done():
                try:
                    handoff = early_handoff_task.result()
                except Exception as error:
                    logger.debug("early handoff check failed: %s", type(error).__name__)
                    handoff = False
                early_handoff_task = None
                if handoff and heard_text == early_handoff_checked and not bot_busy():
                    # A clear live request should not wait for Flux's often-missing EndOfTurn.
                    forced_fallback_text = heard_text
                    logger.info("custom voice: ending live handoff turn early: %s", heard_text[:120])
                    await finish_utterance("flux_live_handoff")
                    continue
            if (flux and early_handoff_task is None and call_forwarding.forwarding_on() and heard_text
                    and quiet_for >= 0.6 and not bot_busy() and early_handoff_checked != heard_text):
                early_handoff_checked = heard_text
                # A network-backed classifier must not pause speculative preparation or the turn watchdog.
                early_handoff_task = asyncio.create_task(asyncio.to_thread(
                    call_forwarding.wants_person, heard_text, earlier_handoff_text))
            if flux and heard_text and stuck_after and quiet_for >= stuck_after and not bot_busy() and forced_turn == heard_text and forced_at and perf_counter() - forced_at >= 0.75:
                # Some Flux sessions acknowledge neither the request nor the turn. Do not leave the caller waiting.
                forced_fallback_text = heard_text
                logger.warning("custom voice: Flux did not end the requested turn; answering partial text: %s", heard_text[:120])
                await finish_utterance("flux_force_timeout")
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
                    await finish_utterance("flux_force_error")
                continue
            # With Nova-3 the same safeguard ends the turn here directly. The ¼-second early start applies to both (on a
            # real call Flux's EagerEndOfTurn mostly came together with EndOfTurn, giving no head start on its own).
            speech_final_pending = bool(nova_speech_final_text and _same_words(nova_speech_final_text, heard_text))
            timeout_after = max(stuck_after, incomplete_after) if speech_final_pending else stuck_after
            if not flux and heard_text and timeout_after and quiet_for >= timeout_after and not bot_busy():
                # The caller's words have not changed for a while and Deepgram never said the turn ended: end it
                # ourselves rather than leave the caller waiting in silence. After a final signal, incomplete
                # wording gets a short grace period so a pause between clauses is not routed as a separate request.
                trigger = "nova_speech_final" if speech_final_pending else "nova_timeout"
                logger.info("custom voice: closing Nova turn after %.1fs (%s): %s", quiet_for, trigger, heard_text[:120])
                await finish_utterance(trigger)
                continue
            if wait and quiet_for >= wait:
                start_early()

    async def on_flux_turn(payload: Dict[str, Any]) -> None:
        """Deepgram Flux turn events. The transcript is the whole turn so far."""
        nonlocal early_reply
        nonlocal flux_turn_started, flux_turn_logged, forced_fallback_text, heard_changed_at
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
            heard_changed_at = perf_counter()
            cancel_silence_reminder()
        elif text and flux_turn_started and not flux_turn_logged and perf_counter() - flux_turn_started > 4:
            # So the log shows what Flux had heard if a turn ever stays open too long.
            flux_turn_logged = True
            logger.info("custom voice: Flux turn open for %.1fs so far: %s", perf_counter() - flux_turn_started, text[:160])
        if event == "EndOfTurn":
            flux_turn_started = 0.0
        if event == "EagerEndOfTurn":
            start_early()  # probably finished: prepare the reply now
        elif event == "TurnResumed":
            if early_reply:
                drop_reply(early_reply)  # they went on after all
                early_reply = None
            heard_changed_at = perf_counter()
        elif event == "EndOfTurn":
            if payload.get("trigger") == "timeout":
                logger.info("custom voice: Flux ended the turn after silence: %s", text[:120])
            utterance[:] = [text] if text else []
            await finish_utterance(f"flux_{payload.get('trigger') or 'model'}")

    async def finish_utterance(trigger: str = "local") -> None:
        async with finish_lock:
            await finish_utterance_locked(trigger)

    async def finish_utterance_locked(trigger: str = "local") -> None:
        nonlocal reply_task, unanswered_text, early_reply, heard_text
        nonlocal turn_word_end, completion_text, completion_task, nova_speech_final_text
        completion_text = ""
        if completion_task is not None:
            completion_task.cancel()
            completion_task = None
        semantic_prefetch.discard_pending()
        bind_call(call_sid)
        turn_ended_at = perf_counter()
        turn_timing: Dict[str, Any] = {
            "stt_engine": "flux" if flux else "nova",
            "turn_end_trigger": trigger,
            "text_stable_ms": round(max(0.0, turn_ended_at - heard_changed_at) * 1000),
        }
        if turn_word_end and audio_received_seconds >= turn_word_end:
            turn_timing["turn_detection_ms"] = round((audio_received_seconds - turn_word_end) * 1000)
        turn_word_end = 0.0
        # The latest interim may extend a finalized prefix. A fallback/UtteranceEnd must not discard that continuation.
        text = (heard_text or " ".join(utterance)).strip()
        if not any(character.isalnum() for character in text):
            text = ""  # "." or "?" alone: no words were said
        if not any(character.isalnum() for character in heard_text):
            heard_text = ""
        if not text and heard_text:
            # No final words, but the partial transcript showed what the caller said: use that.
            logger.info("custom voice: Deepgram gave no final words; using what it heard: %s", heard_text[:120])
            text = heard_text
        nova_speech_final_text = ""
        utterance.clear()
        prepared, early_reply, heard_text = early_reply, None, ""
        if not text:
            drop_reply(prepared)
            return
        if transfer_pending:
            drop_reply(prepared)
            transcript.append({"content": text, "timestamp": utc_iso(), "language": preferred_language})
            logger.info("custom voice heard call=%s during handoff: %s", call_sid, text[:120])
            save_progress()
            return
        if await asyncio.to_thread(pause_request, text):
            drop_reply(prepared)
            if reply_task and not reply_task.done():
                reply_task.cancel()
            unanswered_text = ""
            transcript.append({"content": text, "timestamp": utc_iso(), "language": preferred_language})
            reply_task = asyncio.create_task(pause_for_caller(text))
            reply_task.add_done_callback(log_reply_failure)
            return
        if not unanswered_text and not (reply_task and not reply_task.done()) and await asyncio.to_thread(call_is_over, text, last_agent_text(memory.history())):
            # We said goodbye and the caller only said bye back: hang up instead of another goodbye per fragment.
            drop_reply(prepared)
            transcript.append({"content": text, "timestamp": utc_iso(), "language": preferred_language})
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
        transcript.append({"content": text, "timestamp": utc_iso(), "language": preferred_language})
        logger.info("custom voice heard call=%s: %s", call_sid, text[:120])
        save_progress()
        # All calls use the same semantic routing, even with live forwarding off.
        # Classification stays in the reply task, never in the STT event loop.
        if call_sid:
            reply_task = asyncio.create_task(answer_or_forward(text, turn_ended_at, prepared, turn_timing))
            reply_task.add_done_callback(log_reply_failure)
            return
        reply_task = asyncio.create_task(answer(text, turn_ended_at, prepared, turn_timing))
        reply_task.add_done_callback(log_reply_failure)

    deepgram: Any = None
    audio_backlog: List[bytes] = []   # caller audio that arrived before Deepgram finished connecting
    transcript_task: Optional[asyncio.Task[Any]] = None
    embedding_task: Optional[asyncio.Task[Any]] = None

    async def receive_transcripts(connection: Any) -> None:
        nonlocal preferred_language
        nonlocal embedding_task, latest_word_end, turn_word_end, heard_changed_at, nova_speech_final_text
        nonlocal completion_task, completion_text
        nonlocal stt_result_events, stt_final_events, stt_speech_final_events, stt_flux_end_of_turn_events
        def record_words(words):
            nonlocal latest_word_end, turn_word_end, heard_changed_at
            end = max((float(word.get("end") or 0) for word in words or []), default=0.0)
            if end > latest_word_end:
                latest_word_end = turn_word_end = end
                heard_changed_at = perf_counter()

        async for raw in connection:
            try:
                payload = json.loads(raw)
                kind = payload.get("type")
                if kind == "SpeechStarted":
                    cancel_silence_reminder()
                    heard_changed_at = perf_counter()
                    continue
                if kind == "TurnInfo":  # Deepgram Flux
                    if payload.get("event") == "EndOfTurn":
                        stt_flux_end_of_turn_events += 1
                    record_words(payload.get("words"))
                    words = str(payload.get("transcript") or "").split()
                    # Real words (not line noise) while the agent is busy means the caller is barging in.
                    if bot_busy() and payload.get("event") in {"Update", "StartOfTurn"}:
                        pause = await asyncio.to_thread(pause_request, " ".join(words))
                        if len(words) >= 2 or pause:
                            await interrupt(force=pause)
                    await on_flux_turn(payload)
                    continue
                if kind in {"Error", "Warning"}:
                    logger.warning("custom voice: Deepgram %s %s: %s", kind.lower(), payload.get("code"), payload.get("description"))
                    continue
                if kind == "UtteranceEnd":
                    if float(payload.get("last_word_end") or 0) + 0.001 < latest_word_end:
                        continue  # A delayed gap signal from an older finalized segment; the caller has continued.
                    if not flux and heard_text:
                        # Treat the fixed-gap signal as a candidate end. Semantic completion or the
                        # incomplete-turn grace period decides whether the caller is actually done.
                        nova_speech_final_text = heard_text
                    continue
                if kind != "Results":
                    continue
                stt_result_events += 1
                stt_final_events += int(bool(payload.get("is_final")))
                stt_speech_final_events += int(bool(payload.get("speech_final")))
                alternative = ((payload.get("channel") or {}).get("alternatives") or [{}])[0]
                record_words(alternative.get("words"))
                detected_languages = alternative.get("languages") or []
                if auto_language and detected_languages:
                    detected = normalize_language(detected_languages[0])
                    if detected in {"en", "ar", "ur"} and language_enabled(detected):
                        preferred_language = detected
                        languages_used.add(detected)
                text = str(alternative.get("transcript") or "").strip()
                if not text:
                    # Deepgram can end a turn with an empty final transcript for a short word it showed in the partial
                    # one ("kitchen"): keep the final signal so semantic completion or the watchdog can use it.
                    if payload.get("is_final") and payload.get("speech_final") and heard_text and not bot_busy():
                        nova_speech_final_text = heard_text
                    continue
                # Real words (not line noise) while the agent is busy means the caller is barging in.
                if bot_busy():
                    pause = await asyncio.to_thread(pause_request, text)
                    if len(text.split()) >= 2 or pause:
                        await interrupt(force=pause)
                # Start the knowledge search on what the caller has said so far (see brain_service.query_embedding).
                nonlocal prefetched_at
                if payload.get("is_final") or perf_counter() - prefetched_at > 0.4:
                    prefetched_at = perf_counter()
                    if embedding_task is None or embedding_task.done():
                        embedding_task = asyncio.create_task(asyncio.to_thread(
                            brain_service.prefetch_query_embedding, " ".join([*utterance, text])))
                if payload.get("is_final"):
                    utterance.append(text)
                    heard_changed(" ".join(utterance))
                    if payload.get("speech_final"):
                        if not flux:
                            nova_speech_final_text = heard_text
                            # The fixed-gap signal is a candidate end. Start meaning-based
                            # validation immediately so complete answers stay fast.
                            if completion_text != heard_text:
                                if completion_task is not None and not completion_task.done():
                                    completion_task.cancel()
                                completion_text = heard_text
                                completion_task = asyncio.create_task(asyncio.to_thread(complete_nova_turn, heard_text))
                            elif completion_task is None:
                                completion_task = asyncio.create_task(asyncio.to_thread(complete_nova_turn, heard_text))
                else:
                    heard_changed(" ".join([*utterance, text]))
            except Exception as error:
                logger.error("custom voice transcript error: %s", error)

    async def open_deepgram() -> None:
        """Connect to Deepgram while the greeting plays, instead of making the caller wait for it first."""
        nonlocal deepgram, transcript_task, flux, speculation_task
        headers = {"Authorization": f"Token {deepgram_key}"}
        connection = None
        if flux:
            try:
                connection = await websockets.connect(_flux_url(preferred_language), additional_headers=headers, open_timeout=12, ping_interval=20)
                logger.info("custom voice: listening with Deepgram Flux (turn detection by the model)")
            except Exception as error:
                # e.g. Flux not enabled on the Deepgram account: the call still works with Nova-3.
                logger.warning("custom voice: Deepgram Flux unavailable (%s); using Nova-3", error)
                flux = False
        if connection is None:
            connection = await websockets.connect(_deepgram_url(preferred_language), additional_headers=headers, open_timeout=12, ping_interval=20)
        while audio_backlog:
            await connection.send(audio_backlog.pop(0))
        deepgram = connection
        transcript_task = asyncio.create_task(receive_transcripts(connection))
        speculation_task = asyncio.create_task(speculate())

    try:
        deepgram_ready = None
        try:
                while True:
                    message = json.loads(await websocket.receive_text())
                    event = message.get("event")
                    if deepgram_ready is not None and deepgram_ready.done() and deepgram_ready.exception():
                        raise deepgram_ready.exception()
                    if event == "start":
                        start = message.get("start") or {}
                        stream_sid = str(start.get("streamSid") or message.get("streamSid") or "")
                        call_sid = str(start.get("callSid") or "")
                        params = start.get("customParameters") or {}
                        caller = str(params.get("caller") or caller)
                        preferred_language = effective_language(params.get("preferred_language"))
                        languages_used.add(preferred_language)
                        call_sid = str(params.get("call_sid") or call_sid)
                        bind_call(call_sid)
                        if not valid_stream_token(call_sid, str(params.get("stream_token") or "")):
                            logger.warning("closing media stream without a valid stream token (call %s)", call_sid)
                            await websocket.close(code=1008)
                            return
                        # Use the authenticated Start parameters before opening STT.
                        # Flux's English model cannot transcribe Arabic or Urdu.
                        flux = _use_flux() and not auto_language and preferred_language == "en"
                        deepgram_ready = asyncio.create_task(open_deepgram())
                        greeting = await asyncio.to_thread(_greeting_text, preferred_language)
                        logger.info("custom voice: greeting starts %.2fs after the call audio connected", perf_counter() - call_started)
                        responses.append({"content": greeting, "timestamp": utc_iso(), "language": preferred_language})
                        memory.add_assistant(greeting)
                        save_progress()
                        reply_task = asyncio.create_task(speak(greeting, cache_key=_audio_cache_key(greeting, engine, preferred_language)))
                        asyncio.create_task(warm_call())
                        reply_task.add_done_callback(log_reply_failure)
                        # Record the bridge phrases and fillers in this call's voice while the greeting plays, so
                        # they can be said instantly.
                        phrases = tuple(dict.fromkeys((*STATUS_PHRASES, *LOOKUP_BRIDGES, *ALL_BRIDGES))) if bridges_enabled() else ()
                        if phrases:
                            asyncio.create_task(_warm_audio_cache(phrases, engine, preferred_language))
                    elif event == "media":
                        encoded = ((message.get("media") or {}).get("payload") or "")
                        if encoded:
                            audio = base64.b64decode(encoded)
                            audio_received_seconds += len(audio) / 8000  # mono 8 kHz mu-law samples
                            if deepgram is None:
                                audio_backlog.append(audio)
                            else:
                                await deepgram.send(audio)
                    elif event == "mark":
                        if (message.get("mark") or {}).get("name") == pending_mark:
                            pending_mark = ""
                    elif event == "stop":
                        break
        finally:
                active = False
                cancel_silence_reminder()
                drop_reply(early_reply)  # a reply started early for a turn that never ended
                if reply_task and not reply_task.done():
                    reply_task.cancel()
                if deepgram_ready is not None:
                    deepgram_ready.cancel()
                if transcript_task:
                    transcript_task.cancel()
                    await asyncio.gather(transcript_task, return_exceptions=True)
                closing_tasks = [task for task in (reply_task, speculation_task, early_handoff_task, completion_task, deepgram_ready)
                                 if task is not None]
                for task in closing_tasks:
                    task.cancel()
                await asyncio.gather(*closing_tasks, return_exceptions=True)
                if deepgram is not None:
                    await deepgram.close()
    except WebSocketDisconnect:
        active = False
    except Exception as error:
        stream_failure = f"{type(error).__name__}: {error}"
        logger.error("custom Twilio voice websocket error: %s", error)
    finally:
        await semantic_prefetch.close()
        if contact_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*contact_tasks, return_exceptions=True), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("contact number capture did not finish before the call ended")
        await memory.close()
        if speaker is not None:
            await speaker.close()
        if transcript or responses:
            log_call(caller, None, {
                "direction": "inbound",
                "status": "transfer_requested" if callback_requested else "failed" if stream_failure else "completed",
                "duration": int(round(perf_counter() - call_started)),
                "provider": "custom_twilio",
                "provider_call_id": call_sid or stream_sid,
                "channel_type": "phone_call",
                "provider_call_status": "ended",
                "provider_metadata": {"stt_engine": "flux" if flux else "nova", "tts_engine": engine,
                                      "stt_connected": deepgram is not None,
                                      "input_audio_seconds": round(audio_received_seconds, 1),
                                      "stt_result_events": stt_result_events,
                                      "stt_final_events": stt_final_events,
                                      "stt_speech_final_events": stt_speech_final_events,
                                      "stt_flux_end_of_turn_events": stt_flux_end_of_turn_events,
                                      **({"stream_error": stream_failure} if stream_failure else {})},
                "transcription": [{"type": "user", **item} for item in transcript],
                "ai_responses": [{"type": "bot", **item} for item in responses],
                "messages": live_transcript.ordered_from_lists(transcript, responses),
                "voice_turn_timings": voice_turn_timings[-200:],
                "contact_phone": contact_phone,
                "contact_email": contact_email_from_messages(memory.history()),
                "transfer_requested": callback_requested,
                "transfer_status": "requested" if callback_requested else None,
                "transfer_destination": "Callback queue" if callback_requested else None,
                "human_callback_requested": callback_requested,
                "summary": memory.summary or f"Custom Twilio voice call with {len(transcript)} caller turns and {len(responses)} AI responses.",
                "preferred_language": preferred_language,
                "languages_used": sorted(languages_used),
                "summary_language": "en",
                "sentiment": "neutral",
                "call_session_id": call_sid or stream_sid,
            })
