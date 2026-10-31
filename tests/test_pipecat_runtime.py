"""Native Pipecat workers with fake services: no speech-provider calls or CRM writes."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

pytest.importorskip("pipecat", reason="Optional requirements-pipecat.txt is not installed")

import pipecat_voice as voice
from voice_audio import pcm_to_ulaw, ulaw_to_pcm


class FakeSpeech(voice.FrameProcessor):
    def __init__(self, *, hold=False, fail=False):
        super().__init__()
        self.hold, self.fail = hold, fail
        self.requests = []
        self.blocked = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, voice.TTSSpeakFrame):
            self.requests.append(frame.text)
            if self.fail:
                await self.push_error(error_msg="synthetic failure", force_treat_as_permanent=True)
                return
            pcm = np.array([0, 1000, -1000, 3000] * 40, dtype="<i2").tobytes()
            await self.push_frame(voice.TTSAudioRawFrame(pcm, 8000, 1))
            if self.hold:
                await self.blocked.wait()
            await self.push_frame(voice.TTSStoppedFrame())
        else:
            await self.push_frame(frame, direction)


def test_real_worker_reuses_speech_service_and_converts_phone_audio():
    async def run():
        service = FakeSpeech()
        speech = voice.PipecatSpeech(service, [])
        try:
            first = b"".join([chunk async for chunk in speech.stream("First sentence.")])
            task = speech.channel.runner_task
            second = b"".join([chunk async for chunk in speech.stream("Second sentence.")])
            expected = pcm_to_ulaw(np.array([0, 1000, -1000, 3000] * 40))
            assert first == second == expected
            assert service.requests == ["First sentence.", "Second sentence."]
            assert speech.channel.runner_task is task
        finally:
            await speech.close()
        assert task.done()

    asyncio.run(run())


def test_concurrent_warm_and_speech_use_one_worker():
    async def run():
        speech = voice.PipecatSpeech(FakeSpeech(), [])
        try:
            async def speak():
                return b"".join([chunk async for chunk in speech.stream("A ready answer.")])
            _, audio = await asyncio.gather(speech.channel.open(), speak())
            assert audio and speech.channel.worker is not None
            assert len(speech.channel.service.requests) == 1
        finally:
            await speech.close()

    asyncio.run(run())


def test_interruption_closes_worker_instead_of_replaying_queued_audio():
    async def run():
        speech = voice.PipecatSpeech(FakeSpeech(hold=True), [])
        async with contextlib.aclosing(speech.stream("An interrupted answer.")) as stream:
            assert await anext(stream)
        assert speech.channel.closing
        assert speech.channel.runner_task.done()

    asyncio.run(run())


def test_service_error_wakes_waiter_and_returns_a_sanitized_failure():
    async def run():
        speech = voice.PipecatSpeech(FakeSpeech(fail=True), [])
        with pytest.raises(voice.SpeechRuntimeError, match="Pipecat speech service failed"):
            async for chunk in speech.stream("Test."):
                pass
        assert speech.channel.runner_task.done()

    asyncio.run(run())


def test_no_audio_respects_the_existing_first_audio_deadline(monkeypatch):
    class Silent(FakeSpeech):
        async def process_frame(self, frame, direction):
            if not isinstance(frame, voice.TTSSpeakFrame):
                await super().process_frame(frame, direction)

    monkeypatch.setenv("CUSTOM_TTS_FIRST_AUDIO_TIMEOUT_MS", "500")

    async def run():
        speech = voice.PipecatSpeech(Silent(), [])
        with pytest.raises(voice.SpeechRuntimeError, match="timed out"):
            async for chunk in speech.stream("Test."):
                pass
        assert speech.channel.runner_task.done()

    asyncio.run(run())


def test_flux_events_keep_original_provider_clocks_and_do_not_decide_intent(monkeypatch):
    original = AsyncMock()
    monkeypatch.setattr(voice.DeepgramFluxSTTService, "_process_message", original)

    async def run():
        connection = voice.PipecatRecognition(
            "wss://api.deepgram.com/v2/listen?model=flux-general-multi&eot_threshold=0.7&eager_eot_threshold=0.4", "test", [])
        event = json.dumps({"type": "TurnInfo", "event": "EndOfTurn", "transcript": "A product question.",
                            "audio_window_end": 2.3, "words": [{"word": "question", "end": 2.0}]})
        await connection.channel.service._process_message(event)
        assert await anext(connection) == event
        original.assert_awaited_once_with(event)
        assert connection.channel.service._should_interrupt is False
        assert connection.channel.service._settings.model == "flux-general-multi"

    asyncio.run(run())


def test_nova_preserves_final_and_speech_final_fields(monkeypatch):
    monkeypatch.setattr(voice.DeepgramSTTService, "_on_message", AsyncMock())

    async def run():
        connection = voice.PipecatRecognition("wss://api.deepgram.com/v1/listen?model=nova-3&language=multi", "test", [])
        payload = {"type": "Results", "is_final": True, "speech_final": True,
                   "channel": {"alternatives": [{"transcript": "Question", "languages": ["ar"]}]}}
        await connection.channel.service._on_message(SimpleNamespace(model_dump=lambda **kwargs: payload))
        assert json.loads(await anext(connection)) == payload

    asyncio.run(run())


def test_recognition_audio_conversion_and_backpressure(monkeypatch):
    async def run():
        connection = voice.PipecatRecognition("wss://api.deepgram.com/v2/listen", "test", [])
        queued = []
        connection.channel.worker = SimpleNamespace(queue_frame=AsyncMock(side_effect=lambda frame: queued.append(frame)))
        audio = bytes([0, 127, 255, 128] * 40)
        await connection.send(audio)
        assert queued[0].audio == ulaw_to_pcm(audio).astype("<i2").tobytes()
        connection.channel.pending_audio = 80000
        with pytest.raises(voice.SpeechRuntimeError, match="backlog"):
            await connection.send(audio)

    asyncio.run(run())


def test_flux_tts_waits_for_speech_metadata_after_early_flushed(monkeypatch):
    """Use Pipecat's separate v2 Flux service: v1's Flushed boundary would truncate Urdu speech."""
    monkeypatch.setattr(voice, "deepgram_voice", lambda language: "flux-ambreen-ur")
    runtime = voice.PipecatVoiceRuntime()
    service = runtime._speech("deepgram", "ur").channel.service
    assert isinstance(service, voice.DeepgramFluxTTSService)

    async def run():
        appended = []
        monkeypatch.setattr(service, "get_active_audio_context_id", lambda: "test")
        monkeypatch.setattr(service, "append_to_audio_context", AsyncMock(side_effect=lambda *args: appended.append(args)))
        monkeypatch.setattr(service, "remove_audio_context", AsyncMock())
        await service._handle_message({"type": "Flushed"})
        assert not appended
        await service._handle_message({"type": "SpeechMetadata"})
        assert isinstance(appended[-1][1], voice.TTSStoppedFrame)

    asyncio.run(run())
