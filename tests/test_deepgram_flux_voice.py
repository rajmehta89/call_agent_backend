"""Deepgram Flux voices (Urdu) send "Flushed" before the audio: the reply must still be spoken (2026-10-29)."""

import asyncio
import json
from unittest.mock import patch

import routers.custom_twilio_voice_api as voice

FLUX_ORDER = [json.dumps({"type": "Connected"}), json.dumps({"type": "SpeechStarted"}), json.dumps({"type": "Flushed"}),
              b"\xff" * 160, b"\xff" * 160, json.dumps({"type": "SpeechMetadata"})]


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)

    async def send(self, _):
        return None

    async def recv(self):
        return self.messages.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def collect(stream):
    async def run():
        return b"".join([chunk async for chunk in stream])
    return asyncio.run(run())


def test_live_line_speaks_the_audio_after_flushed(monkeypatch):
    monkeypatch.setattr(voice, "deepgram_voice", lambda language: "flux-ambreen-ur")

    async def connect(*args, **kwargs):
        return FakeSocket(FLUX_ORDER)
    with patch.object(voice.websockets, "connect", connect):
        assert len(collect(voice._DeepgramSpeaker("ur").stream("جی ہاں"))) == 320


def test_single_request_speaks_the_audio_after_flushed(monkeypatch):
    monkeypatch.setattr(voice, "deepgram_voice", lambda language: "flux-ambreen-ur")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test")
    with patch.object(voice.websockets, "connect", lambda *args, **kwargs: FakeSocket(FLUX_ORDER)):
        assert len(collect(voice._deepgram_tts_stream("جی ہاں", "ur"))) == 320
