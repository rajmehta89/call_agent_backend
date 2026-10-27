"""Third voice option: the custom pipeline speaking with Deepgram Aura-2 instead of ElevenLabs."""
import voice_provider_settings as settings
from routers import custom_twilio_voice_api as custom


def test_deepgram_variant_needs_no_elevenlabs(monkeypatch):
    monkeypatch.setattr(settings, "_saved_controls", lambda: {"custom_deepgram": True})
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TWILIO_PUBLIC_BASE_URL", "DEEPGRAM_API_KEY"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    snapshot = settings.provider_snapshot()
    assert snapshot["custom_deepgram"]["ready"] and snapshot["custom_deepgram"]["enabled"]
    assert not snapshot["custom"]["ready"]  # the ElevenLabs variant still needs its keys
    assert settings.custom_stack_enabled() and settings.custom_voice_engine() == "deepgram"
    assert custom._active_custom(snapshot)["id"] == "custom_deepgram"


def test_elevenlabs_is_the_default_voice(monkeypatch):
    monkeypatch.setattr(settings, "_saved_controls", lambda: {"custom": True})
    assert settings.custom_voice_engine() == "elevenlabs"


def test_cached_audio_is_kept_apart_per_voice():
    assert custom._audio_cache_key("Hello", "deepgram") != custom._audio_cache_key("Hello", "elevenlabs")


def test_elevenlabs_retries_on_a_fresh_connection_when_no_audio_comes(monkeypatch):
    # From a real call: a stale connection gave no audio for ~10 s. Now: retry after the first-audio timeout.
    import asyncio
    import contextlib
    from routers import custom_twilio_voice_api as custom

    attempts = []

    class Response:
        def __init__(self, hang):
            self.hang = hang
        def raise_for_status(self):
            pass
        async def aiter_bytes(self, size):
            if self.hang:
                await asyncio.sleep(10)
            yield b"\xff" * 160

    class Client:
        def stream(self, *args, **kwargs):
            attempts.append(1)
            @contextlib.asynccontextmanager
            async def opened():
                yield Response(hang=len(attempts) == 1)
            return opened()
        async def aclose(self):
            pass

    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    monkeypatch.setenv("CUSTOM_TTS_FIRST_AUDIO_TIMEOUT_MS", "500")
    monkeypatch.setattr(custom, "_elevenlabs_client", lambda: Client())

    async def run():
        started = asyncio.get_running_loop().time()
        audio = [chunk async for chunk in custom._elevenlabs_stream("Hello")]
        return audio, asyncio.get_running_loop().time() - started

    audio, took = asyncio.run(run())
    assert audio and len(attempts) == 2 and took < 2.0


def test_deepgram_tts_retries_over_rest_when_websocket_first_audio_stalls(monkeypatch):
    import asyncio

    monkeypatch.setenv("CUSTOM_DEEPGRAM_TTS_FIRST_AUDIO_TIMEOUT_MS", "500")
    websocket_cancelled = asyncio.Event()
    rest_calls = []

    class SlowSpeaker:
        async def stream(self, text):
            try:
                await asyncio.sleep(10)
                yield b"websocket-audio"
            finally:
                websocket_cancelled.set()

    async def rest_stream(text, language="en"):
        rest_calls.append((text, language))
        yield b"rest-audio"

    monkeypatch.setattr(custom, "_deepgram_tts_stream", rest_stream)

    async def run():
        started = asyncio.get_running_loop().time()
        chunks = [chunk async for chunk in custom._deepgram_speaker_stream(SlowSpeaker(), "Hello", "en")]
        return chunks, asyncio.get_running_loop().time() - started

    chunks, elapsed = asyncio.run(run())
    assert chunks == [b"rest-audio"]
    assert rest_calls == [("Hello", "en")]
    assert websocket_cancelled.is_set()
    assert elapsed < 1.5
