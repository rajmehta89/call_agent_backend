"""Pipecat speech workers behind the existing authenticated Twilio conversation controller.

The controller remains the single owner of semantics, callbacks, language changes, interruption decisions,
transcripts and playback marks. These adapters replace its STT/TTS network implementations with native Pipecat
services, retaining the same provider/model/voice settings. Import only when the native runtime is selected.
"""

from __future__ import annotations

import asyncio
import audioop
from collections import deque
import contextlib
import json
import logging
import os
import threading
from urllib.parse import parse_qs, urlsplit

import numpy as np
from pipecat.frames.frames import (Frame, InputAudioRawFrame, MetricsFrame, StartFrame,
                                  TTSAudioRawFrame, TTSSpeakFrame, TTSStoppedFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepgram.flux.tts import DeepgramFluxTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService
from pipecat.services.elevenlabs.tts_base import elevenlabs_language_code
from pipecat.workers.runner import WorkerRunner

from voice_audio import pcm_to_ulaw, ulaw_to_pcm
from voice_provider_settings import deepgram_voice
from voice_languages import normalize_language

_LOGGING_LOCK = threading.Lock()
_LOGGING_READY = False


def _configure_logging():
    """Route Loguru through the application's logger off the audio loop, without debug transcript spam."""
    global _LOGGING_READY
    with _LOGGING_LOCK:
        if _LOGGING_READY:
            return
        from loguru import logger

        def forward(message):
            record = message.record
            logging.getLogger(record["name"] or "pipecat").log(record["level"].no, record["message"])

        logger.remove()
        logger.add(forward, level=os.getenv("LOG_LEVEL", "INFO"), enqueue=True, format="{message}")
        _LOGGING_READY = True


class SpeechRuntimeError(RuntimeError):
    """Sanitized error: provider exceptions can contain signed URLs or credentials."""


class _Capture(FrameProcessor):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.channel.pending_audio = max(0, self.channel.pending_audio - len(frame.audio))
        if isinstance(frame, MetricsFrame):
            for item in frame.data:
                value = getattr(item, "value", None)
                if isinstance(value, (float, int)):
                    self.channel.metrics.append({"metric": type(item).__name__, "seconds": round(value, 4)})
        if self.channel.on_frame:
            await self.channel.on_frame(frame)
        await self.push_frame(frame, direction)


class _Channel:
    def __init__(self, service, *, on_frame=None, events=None, metrics=None):
        self.service = service
        self.on_frame = on_frame
        self.events = events
        self.metrics = metrics if metrics is not None else deque(maxlen=100)
        self.pending_audio = 0
        self.failure = None
        self.closing = False
        self.runner_task = None
        self.worker = None
        self.started = None
        self.open_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()

    def fail(self, message="Pipecat speech service failed"):
        if self.failure or self.closing:
            return
        self.failure = SpeechRuntimeError(message)
        if self.started is not None:
            self.started.set()
        if self.events is not None:
            # Wake a waiting iterator even when the bounded queue is full.
            if self.events.full():
                self.events.get_nowait()
            self.events.put_nowait(self.failure)

    async def open(self):
        async with self.open_lock:
            await self._open()

    async def _open(self):
        self.check()
        if self.runner_task:
            self.check()
            return
        self.started = asyncio.Event()
        self.worker = PipelineWorker(
            Pipeline([self.service, _Capture(self)]),
            params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000,
                                  enable_metrics=True, enable_usage_metrics=False),
            enable_rtvi=False, enable_turn_tracking=False, idle_timeout_secs=None,
            check_dangling_tasks=False, processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
            setup_timeout_secs=12, start_timeout_secs=12,
        )

        @self.worker.event_handler("on_pipeline_started")
        async def started(worker, frame):
            self.started.set()

        @self.worker.event_handler("on_pipeline_error")
        async def error(worker, frame):
            self.fail()

        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False, check_dangling_tasks=False)
        await runner.add_workers(self.worker)

        async def run():
            try:
                await runner.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.fail()
            finally:
                if not self.closing:
                    self.fail("Pipecat speech pipeline stopped")

        self.runner_task = asyncio.create_task(run(), name="pipecat-speech-worker")
        try:
            await asyncio.wait_for(self.started.wait(), 15)
            self.check()
        except BaseException:
            await self.close()
            raise

    def check(self):
        if self.failure:
            raise self.failure
        if self.closing:
            raise SpeechRuntimeError("Pipecat speech pipeline is closed")

    async def send(self, frame):
        self.check()
        if isinstance(frame, InputAudioRawFrame):
            self.pending_audio += len(frame.audio)
            if self.pending_audio > 80000:  # at most five seconds of 8 kHz PCM waiting to be processed
                self.fail("Pipecat recognition input backlog exceeded five seconds")
                self.check()
        await self.worker.queue_frame(frame)

    async def close(self):
        async with self.close_lock:
            await self._close()

    async def _close(self):
        if self.closing:
            return
        self.closing = True
        if self.started is not None:
            self.started.set()  # also release a concurrent warmup that is waiting for startup
        if self.worker and self.runner_task and not self.runner_task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.worker.cancel(), 4)
            try:
                await asyncio.wait_for(asyncio.shield(self.runner_task), 5)
            except (Exception, asyncio.CancelledError):
                self.runner_task.cancel()
                await asyncio.gather(self.runner_task, return_exceptions=True)


class PipecatRecognition:
    """Socket-shaped adapter; preserve raw provider clocks and events for the shared turn controller."""
    def __init__(self, url, api_key, metrics):
        self.events = asyncio.Queue(maxsize=256)
        self.is_flux = "/v2/listen" in urlsplit(url).path
        self.closed = False
        channel_owner = self

        class Flux(DeepgramFluxSTTService):
            async def _process_message(self, message):
                if isinstance(message, str):
                    channel_owner.receive(message)
                await super()._process_message(message)

        class Nova(DeepgramSTTService):
            async def _on_message(self, message):
                channel_owner.receive(json.dumps(message.model_dump(mode="json", by_alias=True, exclude_none=True)))
                await super()._on_message(message)

        query = parse_qs(urlsplit(url).query)
        value = lambda key, default=None: query.get(key, [default])[0]
        if self.is_flux:
            settings = Flux.Settings(
                model=value("model", "flux-general-en"), keyterm=query.get("keyterm", []),
                eot_threshold=float(value("eot_threshold", "0.7")),
                eager_eot_threshold=float(value("eager_eot_threshold", "0.4")),
                eot_timeout_ms=int(value("eot_timeout_ms", "1800")),
            )
            service = Flux(api_key=api_key, sample_rate=8000, settings=settings,
                           should_interrupt=False, enable_eager_end_of_turn=False)
        else:
            settings = Nova.Settings(model=value("model", "nova-3"), language=value("language", "en-US"),
                                     keyterm=query.get("keyterm", []), interim_results=True, smart_format=True,
                                     punctuate=True, endpointing=int(value("endpointing", "500")),
                                     utterance_end_ms=int(value("utterance_end_ms", "1000")))
            service = Nova(api_key=api_key, sample_rate=8000, encoding="linear16", settings=settings)
        self.channel = _Channel(service, events=self.events, metrics=metrics)

    def receive(self, message):
        try:
            self.events.put_nowait(message)
        except asyncio.QueueFull:
            self.channel.fail("Pipecat recognition event backlog exceeded its limit")

    async def open(self):
        await self.channel.open()
        return self

    async def send(self, message):
        self.channel.check()
        if isinstance(message, bytes):
            pcm = ulaw_to_pcm(message).astype("<i2").tobytes()
            await self.channel.send(InputAudioRawFrame(pcm, 8000, 1))
        else:
            control = json.loads(message)
            if control != {"type": "ForceEndTurn"} or not self.is_flux:
                raise SpeechRuntimeError("Unsupported recognition control")
            await self.channel.service._transport_send_json(control)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        item = await self.events.get()
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.channel.close()
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(None)


class PipecatSpeech:
    def __init__(self, service, metrics):
        self.events = asyncio.Queue(maxsize=256)
        self.lock = asyncio.Lock()
        self.rate_state = None
        self.channel = _Channel(service, events=self.events, on_frame=self.capture, metrics=metrics)

    async def capture(self, frame):
        if isinstance(frame, TTSAudioRawFrame):
            if frame.num_channels != 1:
                self.channel.fail("Pipecat returned non-mono speech audio")
                return
            pcm = frame.audio
            if frame.sample_rate != 8000:
                pcm, self.rate_state = audioop.ratecv(pcm, 2, 1, frame.sample_rate, 8000, self.rate_state)
            await self.events.put(pcm_to_ulaw(np.frombuffer(pcm, dtype="<i2")))
        elif isinstance(frame, TTSStoppedFrame):
            await self.events.put(None)

    async def stream(self, text):
        async with self.lock:
            complete = False
            try:
                try:
                    first_timeout = max(0.5, float(os.getenv("CUSTOM_TTS_FIRST_AUDIO_TIMEOUT_MS", "2500")) / 1000)
                except ValueError:
                    first_timeout = 2.5
                async with asyncio.timeout(first_timeout) as deadline:
                    await self.channel.open()
                    while not self.events.empty():
                        self.events.get_nowait()
                    self.rate_state = None
                    await self.channel.send(TTSSpeakFrame(text, append_to_context=False))
                    while True:
                        item = await asyncio.wait_for(self.events.get(), 20)
                        if isinstance(item, Exception):
                            raise item
                        if item is None:
                            complete = True
                            break
                        deadline.reschedule(None)
                        yield item
            except TimeoutError:
                raise SpeechRuntimeError("Pipecat speech audio timed out") from None
            finally:
                if not complete:
                    # Barge-in discards the worker/connection, so buffered audio cannot leak into another turn.
                    await self.channel.close()

    async def close(self):
        await self.channel.close()


class PipecatVoiceRuntime:
    def __init__(self):
        _configure_logging()
        self.metrics = deque(maxlen=100)
        self.recognizers = []
        self.speakers = {}
        self.closed = False

    async def connect_stt(self, url, api_key):
        connection = PipecatRecognition(url, api_key, self.metrics)
        self.recognizers.append(connection)
        return await connection.open()

    def _speech(self, engine, language):
        from routers.custom_twilio_voice_api import _elevenlabs_voice, _tts_engine_for_language
        # Apply the same language routing to warmup, direct synthesis and controller replies.
        # In particular, an Arabic call must not get the default English Deepgram voice.
        language = normalize_language(language)
        engine = _tts_engine_for_language(engine, language)
        key = (engine, language)
        existing = self.speakers.get(key)
        if existing is not None and not existing.channel.closing:
            return existing
        if engine == "deepgram":
            voice = deepgram_voice(language)
            cls = DeepgramFluxTTSService if voice.startswith("flux-") else DeepgramTTSService
            service = cls(api_key=os.getenv("DEEPGRAM_API_KEY", ""), sample_rate=8000,
                          settings=cls.Settings(voice=voice))
        else:
            model = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip() or "eleven_flash_v2_5"
            cls = ElevenLabsDialogueTTSService if model.startswith(("eleven_v3", "eleven_v4")) else ElevenLabsTTSService
            service = cls(
                api_key=os.getenv("ELEVENLABS_API_KEY", ""), sample_rate=8000,
                settings=cls.Settings(voice=_elevenlabs_voice(language),
                    model=model, language=elevenlabs_language_code(model, language)),
            )
        speech = PipecatSpeech(service, self.metrics)
        self.speakers[key] = speech
        return speech

    async def warm(self, engine, language):
        if self.closed:
            raise SpeechRuntimeError("Pipecat call runtime is closed")
        await self._speech(engine, language).channel.open()

    async def tts_stream(self, text, engine, language):
        if self.closed:
            raise SpeechRuntimeError("Pipecat call runtime is closed")
        async with contextlib.aclosing(self._speech(engine, language).stream(text)) as stream:
            async for chunk in stream:
                yield chunk

    async def close(self):
        self.closed = True
        resources = [*self.recognizers, *self.speakers.values()]
        await asyncio.gather(*(resource.close() for resource in resources), return_exceptions=True)

    def diagnostics(self):
        return {"voice_runtime": "pipecat", "pipecat_version": "1.15.0", "speech_metrics": list(self.metrics)}
