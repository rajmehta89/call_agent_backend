"""Opening audio must survive failed native speech without a fictional transcript."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from routers import custom_twilio_voice_api as custom
from test_custom_voice_early_start import FakeDeepgram, run_call


GREETING = "Hello, thanks for calling our practice. How can I help you today?"


def test_startup_warms_the_configured_opening_for_each_enabled_language(monkeypatch):
    warmed = AsyncMock()
    monkeypatch.setattr(custom, "custom_stack_enabled", lambda: True)
    monkeypatch.setattr(custom, "custom_voice_engine", lambda: "deepgram")
    monkeypatch.setattr(custom, "enabled_language_codes", lambda: ["en", "ur"])
    monkeypatch.setattr(custom, "_greeting_text", lambda language: "Configured greeting " + language)
    monkeypatch.setattr(custom, "_warm_audio_cache", warmed)

    asyncio.run(custom.warm_configured_greetings())

    assert [call.args for call in warmed.await_args_list] == [
        (("Configured greeting en",), "deepgram", "en"),
        (("Configured greeting ur",), "deepgram", "ur"),
    ]


def test_empty_warmup_audio_is_not_cached_as_a_valid_opening(monkeypatch):
    monkeypatch.setattr(custom, "_cached_audio", {})
    monkeypatch.setattr(custom, "_speech", AsyncMock(return_value=b""))

    asyncio.run(custom._warm_audio_cache((GREETING,), "deepgram"))

    assert not custom._cached_audio


def failed_native_runtime(monkeypatch, failure):
    class Runtime:
        async def connect_stt(self, url, key):
            return FakeDeepgram([])

        async def tts_stream(self, text, engine, language):
            if failure == "error":
                await asyncio.sleep(0.03)
                raise RuntimeError("Synthetic first-audio timeout")
            return
            yield b""

        async def warm(self, *args):
            pass

        async def close(self):
            pass

        def diagnostics(self):
            return {"voice_runtime": "pipecat", "pipecat_version": "1.15.0"}

    monkeypatch.setattr(custom, "runtime_status", lambda: {"selected": "pipecat", "fallback_reason": ""})
    monkeypatch.setattr(custom, "create_runtime", Runtime)
    monkeypatch.setattr(custom, "_greeting_text", lambda language: GREETING)
    monkeypatch.setattr(custom, "_cached_audio", {})


@pytest.mark.parametrize("engine", ["deepgram", "elevenlabs"])
@pytest.mark.parametrize("failure", ["empty", "error"])
def test_greeting_recovers_through_the_same_provider_before_saving_it(monkeypatch, engine, failure):
    failed_native_runtime(monkeypatch, failure)
    spoken, logged = [], []

    async def direct_tts(text, selected_engine, language="en"):
        spoken.append((text, selected_engine))
        yield b"\xff" * 160

    run_call(monkeypatch, [], tts=direct_tts, engine=engine, stt="flux",
             call_seconds=0.1, logged=logged)

    assert spoken == [(GREETING, engine)]
    assert [line["content"] for line in logged[-1]["ai_responses"]] == [GREETING]
    metadata = logged[-1]["provider_metadata"]
    assert metadata["greeting"]["status"] == "audio_sent"
    assert metadata["greeting"]["stream_to_first_audio_ms"] >= 0
    if failure == "error":
        assert metadata["greeting"]["tts_first_audio_ms"] >= 25
    assert metadata["speech_recoveries"] == [{
        "engine": engine, "from": "pipecat", "to": "direct", "error_type": "RuntimeError",
    }]


def test_no_audio_from_either_path_is_a_failed_greeting_not_a_spoken_transcript(monkeypatch):
    failed_native_runtime(monkeypatch, "empty")
    logged = []

    async def empty_tts(*args, **kwargs):
        return
        yield b""

    run_call(monkeypatch, [], tts=empty_tts, engine="deepgram", stt="flux",
             call_seconds=0.2, logged=logged, wait_for_greeting=False)

    call = logged[-1]
    assert call["ai_responses"] == []
    assert call["status"] == "failed"
    assert call["provider_metadata"]["greeting"]["status"] == "failed"
    assert call["provider_metadata"]["greeting"]["error_type"] == "RuntimeError"
