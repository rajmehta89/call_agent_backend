"""Early start in the custom voice pipeline: the reply is prepared while the caller finishes their turn, used if the
turn ends with the same words, and dropped if the caller goes on. Runs the real call handler with a scripted Deepgram,
a fake model and a fake voice."""
import asyncio
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import custom_twilio_voice_api as custom


class FakeDeepgram:
    """Plays a script of (seconds after connecting, transcript, is_final, speech_final) like Deepgram's listen socket.
    On a ForceEndTurn request it answers like Flux: an EndOfTurn with the turn's words so far."""

    sent_controls: list = []

    def __init__(self, script, answer_force=True):
        self.script = script
        self.answer_force = answer_force
        self.last_text = ""
        self.forced = asyncio.Event()

    async def send(self, data):
        if isinstance(data, str) and "ForceEndTurn" in data:
            FakeDeepgram.sent_controls.append("ForceEndTurn")
            self.forced.set()

    async def close(self):
        pass

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        started = time.perf_counter()
        for at, *message in self.script:
            await asyncio.sleep(max(0.0, at - (time.perf_counter() - started)))
            if isinstance(message[0], dict):  # a Flux TurnInfo message
                self.last_text = message[0].get("transcript") or self.last_text
                yield json.dumps({"type": "TurnInfo", **message[0]})
                continue
            text, is_final, speech_final = message
            yield json.dumps({"type": "Results", "is_final": is_final, "speech_final": speech_final,
                              "channel": {"alternatives": [{"transcript": text}]}})
        try:
            await asyncio.wait_for(self.forced.wait(), 30)
            if self.answer_force:
                yield json.dumps({"type": "TurnInfo", "event": "EndOfTurn", "transcript": self.last_text, "trigger": "manual"})
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(30)


def run_call(monkeypatch, script, stt="nova", tts=None, answer_force=True, call_seconds=3.0):
    starts, finished = [], []

    async def fake_sentences(services, text, history, caller, summary, bridge="", verified_phone="", language="en"):
        starts.append((round(time.perf_counter() - t0, 2), text))
        try:
            await asyncio.sleep(0.6)  # the model thinking
            yield "Here is the answer."
            finished.append(text)
        except asyncio.CancelledError:
            raise

    async def fake_tts(text, engine, language="en"):
        yield b"\xff" * 160

    async def fake_connect(*args, **kwargs):
        return FakeDeepgram(script, answer_force=answer_force)

    async def no_op(*args, **kwargs):
        return None

    class Services:
        fallback_model = ""

    monkeypatch.setattr(custom, "custom_stack_enabled", lambda: True)
    monkeypatch.setattr(custom, "_active_custom", lambda snapshot: {"ready": True})
    monkeypatch.setattr(custom, "provider_snapshot", lambda: {})
    monkeypatch.setattr(custom, "custom_voice_engine", lambda: "elevenlabs")
    monkeypatch.setattr(custom, "valid_stream_token", lambda *a: True)
    monkeypatch.setattr(custom.websockets, "connect", fake_connect)
    monkeypatch.setattr(custom, "_voice_sentences", fake_sentences)
    monkeypatch.setattr(custom, "prepare_turn_meaning", lambda *args: None)
    monkeypatch.setattr(custom, "_tts_stream", tts or fake_tts)
    monkeypatch.setattr(custom, "_voice_services", lambda: Services())
    monkeypatch.setattr(custom, "_system_prompt", lambda *a, **k: "")
    monkeypatch.setattr(custom, "warm_model_connection", lambda *a, **k: None)
    monkeypatch.setattr(custom, "_warm_tts_connection", no_op)
    monkeypatch.setattr(custom, "_warm_audio_cache", no_op)
    monkeypatch.setattr(custom, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(custom, "_log_voice_activity", lambda *a, **k: None)
    monkeypatch.setattr(custom.live_transcript, "save_live", lambda *a, **k: None)
    monkeypatch.setattr(custom, "bridges_enabled", lambda: False)
    # The live server loads the local search model at startup; here its first load would block the test's event loop.
    monkeypatch.setattr(custom.brain_service, "prefetch_query_embedding", lambda *a, **k: None)
    monkeypatch.setenv("CUSTOM_FILLER_AFTER_SECONDS", "0")
    monkeypatch.setenv("CUSTOM_SPECULATE_AFTER_MS", "250")
    monkeypatch.setenv("CUSTOM_STT", stt)
    # Flux is only selected when automatic language detection is off; mirror
    # the production switch explicitly so these tests exercise the Flux path.
    monkeypatch.setenv("CUSTOM_AUTO_LANGUAGE", "false" if stt == "flux" else "true")

    app = FastAPI()
    app.include_router(custom.router)
    t0 = time.perf_counter()
    with TestClient(app) as client, client.websocket_connect("/api/custom-voice/media") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1", "customParameters": {}}}))
        while True:  # like Twilio: report the greeting's playback marker once it has played
            message = json.loads(ws.receive_text())
            if message.get("event") == "mark":
                ws.send_text(json.dumps({"event": "mark", "mark": message["mark"]}))
                break
        time.sleep(call_seconds)
        ws.send_text(json.dumps({"event": "stop"}))
    return starts, finished


def test_reply_starts_before_the_turn_ends_and_is_used(monkeypatch):
    script = [(1.0, "what do you", False, False), (1.1, "what do you sell", False, False),
              (1.7, "What do you sell?", True, True)]  # Deepgram confirms the end ~0.6 s after the last word
    starts, finished = run_call(monkeypatch, script)
    assert len(starts) == 1, starts                    # one reply, started early and then used
    assert starts[0][1] == "what do you sell"
    assert starts[0][0] < 1.7                          # before the end of the turn was confirmed
    assert finished == ["what do you sell"]


def test_one_word_sorry_cancels_the_pending_answer_without_a_new_product_lookup(monkeypatch):
    outgoing = []
    original = custom.WebSocket.send_text

    async def record(socket, text):
        outgoing.append(json.loads(text))
        await original(socket, text)

    monkeypatch.setattr(custom.WebSocket, "send_text", record)
    script = [(1.0, "What do you sell?", True, True),
              (1.2, "Sorry", False, False), (1.3, "Sorry.", True, True)]
    starts, finished = run_call(monkeypatch, script)
    assert starts and all("sorry" not in text.lower() for _, text in starts)
    assert not finished
    assert any(event.get("event") == "clear" for event in outgoing)


def test_early_reply_is_dropped_when_the_caller_goes_on(monkeypatch):
    script = [(0.5, "do you sell", False, False),     # pause: an early reply starts
              (1.0, "do you sell basins", False, False),  # caller goes on: dropped
              (1.6, "Do you sell basins?", True, True)]
    starts, finished = run_call(monkeypatch, script)
    texts = [text for _, text in starts]
    assert texts[0] == "do you sell" and texts[-1] == "do you sell basins", starts
    assert finished == ["do you sell basins"]          # only the full sentence was answered


def test_short_word_with_an_empty_final_transcript_is_still_answered(monkeypatch):
    # From a real call: "kitchen" showed in Deepgram's partial transcript, the final one came back empty, and the
    # caller waited 23 seconds. The turn must end and answer what was heard.
    script = [(1.0, "kitchen", False, False), (1.6, "", True, True)]
    starts, finished = run_call(monkeypatch, script)
    assert finished and finished[-1] == "kitchen", (starts, finished)


def test_turn_is_ended_when_deepgram_never_ends_it(monkeypatch):
    monkeypatch.setenv("CUSTOM_TURN_TIMEOUT_MS", "800")
    script = [(1.0, "kitchen", False, False)]  # no final transcript, no end of turn at all
    starts, finished = run_call(monkeypatch, script)
    assert finished and finished[-1] == "kitchen", (starts, finished)


def test_flux_turns_start_early_and_end_on_the_models_signal(monkeypatch):
    # The event sequence Flux sent for real speech ("Chicken" corrected to "Kitchen." while speaking).
    turn = lambda event, text: {"event": event, "transcript": text, "turn_index": 0}
    script = [(1.0, turn("StartOfTurn", "Do you")), (1.2, turn("Update", "Do you sell kitchen sinks?")),
              (1.3, turn("EagerEndOfTurn", "Do you sell kitchen sinks?")), (1.6, turn("EndOfTurn", "Do you sell kitchen sinks?"))]
    starts, finished = run_call(monkeypatch, script, stt="flux")
    assert len(starts) == 1 and starts[0][0] < 1.6, starts   # started on EagerEndOfTurn, used at EndOfTurn
    assert finished == ["Do you sell kitchen sinks?"]


def test_flux_turn_resumed_drops_the_early_reply(monkeypatch):
    turn = lambda event, text: {"event": event, "transcript": text, "turn_index": 0}
    script = [(1.0, turn("StartOfTurn", "Do you sell")), (1.1, turn("EagerEndOfTurn", "Do you sell")),
              (1.4, turn("TurnResumed", "Do you sell")), (1.6, turn("Update", "Do you sell kitchen sinks?")),
              (1.9, turn("EndOfTurn", "Do you sell kitchen sinks?"))]
    starts, finished = run_call(monkeypatch, script, stt="flux")
    assert finished == ["Do you sell kitchen sinks?"], (starts, finished)


def test_flux_single_word_turn_is_answered(monkeypatch):
    turn = lambda event, text: {"event": event, "transcript": text, "turn_index": 0}
    script = [(1.0, turn("Update", "Chicken")), (1.2, turn("StartOfTurn", "Kitchen.")),
              (1.5, turn("EagerEndOfTurn", "Kitchen.")), (2.1, turn("EndOfTurn", "Kitchen."))]
    starts, finished = run_call(monkeypatch, script, stt="flux")
    assert finished == ["Kitchen."], (starts, finished)


def test_a_turn_with_no_words_is_not_answered(monkeypatch):
    # From a real call: Flux ended a silent turn with the transcript "." and the agent answered it.
    turn = lambda event, text: {"event": event, "transcript": text, "turn_index": 0}
    script = [(1.0, turn("EndOfTurn", "."))]
    starts, finished = run_call(monkeypatch, script, stt="flux")
    assert finished == [] and starts == [], (starts, finished)



def test_flux_turn_that_never_ends_is_ended_with_force_end_turn(monkeypatch):
    # From a real call: the caller read out their number, Flux never ended the turn, and the caller hung up unanswered.
    monkeypatch.setenv("CUSTOM_TURN_TIMEOUT_MS", "800")
    FakeDeepgram.sent_controls.clear()
    turn = lambda event, text: {"event": event, "transcript": text, "turn_index": 0}
    script = [(1.0, turn("StartOfTurn", "eight eight six six")), (1.3, turn("Update", "eight eight six six six three one four nine one"))]
    starts, finished = run_call(monkeypatch, script, stt="flux")
    assert FakeDeepgram.sent_controls == ["ForceEndTurn"]
    assert finished == ["eight eight six six six three one four nine one"], (starts, finished)


def test_flux_force_end_without_reply_answers_partial_transcript(monkeypatch):
    # Deepgram kept the turn open even after ForceEndTurn in the latest real call.
    monkeypatch.setenv("CUSTOM_TURN_TIMEOUT_MS", "800")
    FakeDeepgram.sent_controls.clear()
    turn = lambda event, text: {"event": event, "transcript": text, "turn_index": 0}
    script = [(0.5, turn("StartOfTurn", "I am talking about")),
              (0.8, turn("Update", "I am talking about what are your services like"))]
    starts, finished = run_call(monkeypatch, script, stt="flux", answer_force=False, call_seconds=3.6)
    assert FakeDeepgram.sent_controls == ["ForceEndTurn"]
    assert finished == ["I am talking about what are your services like"], (starts, finished)


def test_forwarding_off_does_not_force_end_an_unfinished_turn(monkeypatch):
    monkeypatch.setenv("CUSTOM_TURN_TIMEOUT_MS", "10000")
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)
    turn = lambda event, text: {"event": event, "transcript": text, "turn_index": 0}
    script = [(0.8, turn("Update", "Could you arrange a call to your"))]
    starts, finished = run_call(monkeypatch, script, stt="flux")
    assert starts and starts[0][0] < 2.0, (starts, finished)
    assert finished == [], (starts, finished)


def test_when_elevenlabs_cuts_off_the_call_carries_on_with_the_deepgram_voice(monkeypatch):
    # From a real call: ElevenLabs ran out of credits mid-call; each reply stopped partway and the caller heard
    # only fragments. The cut sentence is said again with the Deepgram voice, and so is everything after it.
    import httpx
    spoken = []

    async def elevenlabs_out_of_credits(text, engine, language="en"):
        yield b"\xff" * 160  # part of the sentence played, then the stream was cut
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    class FakeDeepgramVoice:
        def __init__(self, language="en"):
            self.language = language

        async def set_language(self, language):
            self.language = language

        async def stream(self, text):
            spoken.append(text)
            yield b"\xff" * 160

        async def warm(self):
            pass

        async def close(self):
            pass

    monkeypatch.setenv("DEEPGRAM_API_KEY", "test")
    monkeypatch.setattr(custom, "_DeepgramSpeaker", FakeDeepgramVoice)
    script = [(1.0, "What do you sell?", True, True)]
    starts, finished = run_call(monkeypatch, script, tts=elevenlabs_out_of_credits)
    assert finished == ["What do you sell?"]
    assert spoken[-1] == "Here is the answer."  # the reply was spoken in full with the other voice
