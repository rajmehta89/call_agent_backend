"""Early start in the custom voice pipeline: the reply is prepared while the caller finishes their turn, used if the
turn ends with the same words, and dropped if the caller goes on. Runs the real call handler with a scripted Deepgram,
a fake model and a fake voice."""
import asyncio
import json
import time
from threading import Event

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import custom_twilio_voice_api as custom


@pytest.fixture(scope="module", autouse=True)
def warm_semantic_decisions():
    # Production warms these matrices before taking calls. Timing tests exercise
    # the same steady-state path, excluding one-time model/classifier loading.
    import turn_meaning
    from meaning import Classifier
    for classifier in vars(turn_meaning).values():
        if isinstance(classifier, Classifier):
            classifier.rank("hello")
    from voice_presence import CONNECTION_CHECK
    CONNECTION_CHECK.rank("hello")
    from voice_control import CALLER_PAUSE, CONTROL_SCOPE
    CALLER_PAUSE.rank("hello")
    CONTROL_SCOPE.rank("hello")


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
                if "type" in message[0]:
                    yield json.dumps(message[0])
                    continue
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


def run_call(monkeypatch, script, stt="nova", tts=None, answer_force=True, call_seconds=3.0,
             bridges=False, model_seconds=0.6, bridge_selector=None, logged=None,
             answer_text="Here is the answer.", caller="unknown"):
    starts, finished = [], []
    saved = Event()

    def save_call(*args, **kwargs):
        if logged is not None:
            logged.append(args[2])
        saved.set()

    async def fake_sentences(services, text, history, caller, summary, bridge="", verified_phone="", language="en", **kwargs):
        starts.append((round(time.perf_counter() - t0, 2), text))
        try:
            await asyncio.sleep(model_seconds)
            yield answer_text
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
    monkeypatch.setattr(custom, "log_call", save_call)
    monkeypatch.setattr(custom, "_log_voice_activity", lambda *a, **k: None)
    monkeypatch.setattr(custom.live_transcript, "save_live", lambda *a, **k: None)
    monkeypatch.setattr(custom, "bridges_enabled", lambda: bridges)
    if bridge_selector is not None:
        monkeypatch.setattr(custom, "wait_bridge_phrase", bridge_selector)
        monkeypatch.setattr(custom, "choose_voice_model", lambda *args: None)
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
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1", "customParameters": {"caller": caller}}}))
        while True:  # like Twilio: report the greeting's playback marker once it has played
            message = json.loads(ws.receive_text())
            if message.get("event") == "mark":
                ws.send_text(json.dumps({"event": "mark", "mark": message["mark"]}))
                break
        time.sleep(call_seconds)
        ws.send_text(json.dumps({"event": "stop"}))
        if logged is not None:
            # Final persistence now runs in a worker. Let the handler finish
            # saving before TestClient tears down its websocket task.
            assert saved.wait(3), "The call was not persisted after its stop event"
    return starts, finished


def test_reply_starts_before_the_turn_ends_and_is_used(monkeypatch):
    script = [(1.0, "what do you", False, False), (1.1, "what do you sell", False, False),
              (1.7, "What do you sell?", True, True)]  # Deepgram confirms the end ~0.6 s after the last word
    starts, finished = run_call(monkeypatch, script)
    assert len(starts) == 1, starts                    # one reply, started early and then used
    assert starts[0][1] == "what do you sell"
    assert starts[0][0] < 1.7                          # before the end of the turn was confirmed
    assert finished == ["what do you sell"]


def test_slow_product_answer_gets_one_bridge_even_when_audio_was_not_warmed(monkeypatch):
    spoken, logged = [], []
    monkeypatch.setenv("VOICE_STATUS_AFTER_MS", "60")
    monkeypatch.setenv("VOICE_BRIDGE_DELAY_MS", "0")
    monkeypatch.setattr(custom, "_cached_audio", {})

    async def tts(text, engine, language="en"):
        spoken.append(text)
        yield b"\xff" * 160

    run_call(monkeypatch, [(0.3, "I'm looking for a round basin.", True, True)],
             tts=tts, bridges=True, model_seconds=0.3, call_seconds=1.2,
             bridge_selector=lambda *args: "Let me check that for you.", logged=logged)
    assert spoken.count("Let me check that for you.") == 1
    assert spoken.index("Let me check that for you.") < spoken.index("Here is the answer.")
    timing = logged[-1]["voice_turn_timings"][0]
    assert timing["bridge_first_audio_ms"] < timing["first_audio_ms"]
    assert timing["stt_engine"] == "nova"
    assert timing["turn_end_trigger"] == "nova_semantic_complete"
    telemetry = logged[-1]["provider_metadata"]
    assert telemetry["stt_connected"] is True
    assert telemetry["stt_result_events"] == 1
    assert telemetry["stt_final_events"] == 1
    assert telemetry["stt_speech_final_events"] == 1


def test_slow_bridge_classifier_does_not_delay_a_ready_answer(monkeypatch):
    spoken = []
    monkeypatch.setenv("VOICE_STATUS_AFTER_MS", "20")
    monkeypatch.setenv("VOICE_BRIDGE_DELAY_MS", "0")

    def slow_bridge(*args):
        time.sleep(0.7)
        return "Let me check that for you."

    async def tts(text, engine, language="en"):
        spoken.append(text)
        yield b"\xff" * 160

    starts, finished = run_call(monkeypatch, [(0.3, "I'm looking for a round basin.", True, True)],
                               tts=tts, bridges=True, model_seconds=0.04, call_seconds=1.25,
                               bridge_selector=slow_bridge)
    assert finished == ["I'm looking for a round basin."]
    assert "Here is the answer." in spoken
    assert "Let me check that for you." not in spoken
    assert starts[0][0] < 0.6


def test_callback_request_is_queued_once_without_a_spoken_answer_or_more_questions(monkeypatch):
    queued, spoken, logged = [], [], []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)

    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "callback")
    monkeypatch.setattr(custom.call_forwarding, "create_callback_outreach",
                        lambda *args, **kwargs: queued.append(args) or "Human team")

    async def tts(text, engine, language="en"):
        spoken.append(text)
        yield b"\xff" * 160

    script = [(0.3, "Yeah. Someone call me later.", True, True)]
    starts, _ = run_call(monkeypatch, script, tts=tts, logged=logged, call_seconds=1.2, caller="+61412345678")
    # The answer starts alongside the person/callback check (it would otherwise wait for it) and is dropped unheard.
    assert "Here is the answer." not in spoken
    assert len(queued) == 1
    assert any("I've noted your callback request." in line for line in spoken)
    assert not any("?" in line for line in spoken[1:])
    assert [turn["action"] for turn in logged[-1]["voice_turn_timings"]] == ["callback_requested"]
    assert all(turn["handoff_source"].startswith("semantic") for turn in logged[-1]["voice_turn_timings"])


def test_nova_speech_final_does_not_route_an_incomplete_prefix(monkeypatch):
    queued, logged = [], []
    monkeypatch.setenv("CUSTOM_COMPLETE_TURN_MS", "100")
    monkeypatch.setattr(custom, "complete_nova_turn",
                        lambda text: "Yes. So what is Home DFO serving" in text)
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent",
                        lambda text, *args: "callback" if text.strip() == "Yes. So what" else "")
    monkeypatch.setattr(custom.call_forwarding, "create_callback_outreach",
                        lambda *args, **kwargs: queued.append(args) or "Human team")

    script = [(0.2, "Yes. So what", True, True),
              (0.65, "is Home DFO serving, actually?", True, True)]
    _, finished = run_call(monkeypatch, script, logged=logged, call_seconds=1.7, model_seconds=0.05,
                           answer_text="We supply bathroom and kitchen products.")

    assert finished == ["Yes. So what is Home DFO serving, actually?"]
    assert not queued
    assert logged[-1]["transfer_requested"] is False


def test_nova_incomplete_final_uses_short_grace_instead_of_full_watchdog(monkeypatch):
    logged = []
    monkeypatch.setenv("CUSTOM_TURN_TIMEOUT_MS", "2500")
    monkeypatch.setenv("CUSTOM_NOVA_INCOMPLETE_WAIT_MS", "800")
    monkeypatch.setattr(custom, "complete_nova_turn", lambda _text: False)

    starts, finished = run_call(monkeypatch, [(0.2, "Could you please tell me about the range", True, True)],
                                logged=logged, model_seconds=0.05, call_seconds=2.0)

    assert finished == ["Could you please tell me about the range"]
    assert starts
    assert logged[-1]["voice_turn_timings"][0]["turn_end_trigger"] == "nova_speech_final"


def test_yes_to_connect_offer_starts_live_transfer_without_collecting_contact(monkeypatch):
    spoken = []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: True)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")
    monkeypatch.setattr(custom.call_forwarding, "transfer", lambda *args: True)

    async def tts(text, engine, language="en"):
        spoken.append(text)
        yield b"\xff" * 160

    offer = "Would you like me to connect you with the team for full product details?"
    script = [(0.2, "Can you help with this product?", True, True),
              (1.5, "Yeah. Yeah. Please.", True, True)]
    starts, _ = run_call(monkeypatch, script, tts=tts, model_seconds=0.05,
                         answer_text=offer, call_seconds=2.4)
    # The "yes" turn's answer starts alongside the check and is dropped: the offer is said once, then the transfer.
    assert spoken.count(offer) == 1
    assert custom.call_forwarding.CONNECTING_LINE in spoken
    assert not any("back later" in line or "phone number" in line or "email" in line for line in spoken)


def test_nova_fallback_preserves_the_newer_interim_after_a_final_prefix(monkeypatch):
    monkeypatch.setenv("CUSTOM_TURN_TIMEOUT_MS", "350")
    script = [(0.3, "I'm looking for", True, False),
              (0.4, "a round basin", False, False)]
    starts, finished = run_call(monkeypatch, script, call_seconds=1.9)
    assert finished == ["I'm looking for a round basin"], (starts, finished)


def test_non_latin_caller_turn_is_not_discarded_as_silence(monkeypatch):
    text = "\u0623\u0631\u064a\u062f \u0645\u0639\u0631\u0641\u0629 \u0627\u0644\u0633\u0639\u0631."
    _, finished = run_call(monkeypatch, [(0.3, text, True, True)], call_seconds=1.5)
    assert finished == [text]
    assert not custom._same_words(text, "\u0623\u0631\u064a\u062f \u062d\u0648\u0636\u0627")


def test_stale_nova_gap_does_not_end_a_newer_caller_continuation(monkeypatch):
    def result(text, end, final=False, speech_final=False):
        return {"type": "Results", "is_final": final, "speech_final": speech_final,
                "channel": {"alternatives": [{"transcript": text, "words": [{"end": end}]}]}}
    script = [(0.2, result("I'm looking for", 0.2, final=True)),
              (0.3, result("a round basin", 0.3)),
              (0.4, {"type": "UtteranceEnd", "last_word_end": 0.2}),
              (0.8, result("a round basin.", 0.7, final=True, speech_final=True))]
    starts, finished = run_call(monkeypatch, script, call_seconds=1.8)
    assert len(finished) == 1 and custom._same_words(finished[0], "I'm looking for a round basin."), (starts, finished)


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


def test_recognized_barge_in_stops_reply_and_keeps_the_unfinished_caller_turn(monkeypatch):
    outgoing = []
    original = custom.WebSocket.send_text

    async def record(socket, text):
        outgoing.append(json.loads(text))
        await original(socket, text)

    monkeypatch.setattr(custom.WebSocket, "send_text", record)
    monkeypatch.setattr(custom, "complete_nova_turn", lambda _text: True)
    script = [
        (0.2, "What do you sell?", True, True),
        (1.0, {"type": "SpeechStarted"}),
        (1.2, "I meant the bathroom products.", True, True),
    ]
    starts, finished = run_call(monkeypatch, script, model_seconds=1.0, call_seconds=4.0)

    assert len(starts) == 2, starts
    assert starts[1][1] == "What do you sell? I meant the bathroom products."
    assert finished == ["What do you sell? I meant the bathroom products."]


def test_recognized_speech_clears_audio_already_playing(monkeypatch):
    outgoing = []
    original = custom.WebSocket.send_text

    async def record(socket, text):
        outgoing.append(json.loads(text))
        await original(socket, text)

    monkeypatch.setattr(custom.WebSocket, "send_text", record)
    monkeypatch.setattr(custom, "complete_nova_turn", lambda _text: True)
    script = [
        (0.2, "What do you sell?", True, True),
        (1.1, {"type": "SpeechStarted"}),
        (1.3, "Actually, I meant the bathroom products.", True, True),
    ]
    starts, finished = run_call(monkeypatch, script, model_seconds=0.05, call_seconds=3.5)

    media_indices = [index for index, event in enumerate(outgoing) if event.get("event") == "media"]
    clear_indices = [index for index, event in enumerate(outgoing) if event.get("event") == "clear"]
    assert len(media_indices) >= 2, outgoing  # greeting plus the answer that was interrupted
    assert clear_indices and clear_indices[0] > media_indices[1], outgoing
    assert len(starts) == 2, starts
    assert starts[1][1] == "Actually, I meant the bathroom products."
    assert finished == ["What do you sell?", "Actually, I meant the bathroom products."]


def test_vad_noise_during_greeting_does_not_interrupt_or_claim_caller_was_unclear(monkeypatch):
    outgoing, logged = [], []
    original = custom.WebSocket.send_text

    async def record(socket, text):
        outgoing.append(json.loads(text))
        await original(socket, text)

    monkeypatch.setattr(custom.WebSocket, "send_text", record)
    monkeypatch.setenv("VOICE_REMINDER_TRIGGER_MS", "1000")
    monkeypatch.setattr(custom, "complete_nova_turn", lambda _text: True)
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")
    script = [
        (0.1, {"type": "SpeechStarted"}),
        (0.3, {"type": "Results", "is_final": True, "speech_final": True,
               "channel": {"alternatives": [{"transcript": ""}]}}),
    ]
    run_call(monkeypatch, script, model_seconds=0.05, call_seconds=2.5, logged=logged)

    spoken = [item["content"] for item in logged[-1]["ai_responses"]]
    assert "I couldn't hear you clearly. Please say that again." not in spoken
    assert "I'm here whenever you're ready. What can I help you with?" in spoken
    assert not any(event.get("event") == "clear" for event in outgoing)
    metadata = logged[-1]["provider_metadata"]
    assert metadata["stt_speech_started_events"] == 1
    assert metadata["stt_empty_speech_final_events"] == 1
    assert metadata["stt_nonempty_transcript_events"] == 0


def test_vad_without_transcript_does_not_cancel_or_repeat_the_pending_answer(monkeypatch):
    monkeypatch.setattr(custom, "complete_nova_turn", lambda _text: True)
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")
    question = "What do you sell?"
    script = [(0.2, question, True, True), (1.0, {"type": "SpeechStarted"})]

    starts, finished = run_call(monkeypatch, script, model_seconds=1.2, call_seconds=3.5)

    assert [text for _, text in starts] == [question]
    assert finished == [question]


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


def test_slow_handoff_check_does_not_stall_the_flux_turn_watchdog(monkeypatch):
    monkeypatch.setenv("CUSTOM_TURN_TIMEOUT_MS", "800")
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: True)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")

    def slow_handoff(*args):
        time.sleep(1.5)
        return False

    monkeypatch.setattr(custom.call_forwarding, "wants_person", slow_handoff)
    FakeDeepgram.sent_controls.clear()
    script = [(0.2, {"event": "Update", "transcript": "I'm looking for a round basin."})]
    starts, finished = run_call(monkeypatch, script, stt="flux", model_seconds=0.1, call_seconds=1.9)
    assert FakeDeepgram.sent_controls == ["ForceEndTurn"]
    assert finished == ["I'm looking for a round basin."], (starts, finished)


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


def test_slow_semantic_routing_gets_one_bridge_before_the_answer(monkeypatch):
    spoken, logged = [], []
    monkeypatch.setenv("VOICE_STATUS_AFTER_MS", "40")
    monkeypatch.setenv("VOICE_BRIDGE_DELAY_MS", "40")
    def slow_routing(*args):
        time.sleep(0.15)
        return ""
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", slow_routing)
    async def tts(text, engine, language="en"):
        spoken.append(text)
        yield b"\xff" * 160
    run_call(monkeypatch, [(0.2, "What services do you provide?", True, True)],
             tts=tts, logged=logged, bridges=True, model_seconds=0.1, call_seconds=1.3,
             bridge_selector=lambda *args: "Just putting that together for you.")
    assert spoken.count("Just putting that together for you.") == 1
    assert spoken.index("Just putting that together for you.") < spoken.index("Here is the answer.")
    assert logged[-1]["voice_turn_timings"][0]["bridge_first_audio_ms"] < logged[-1]["voice_turn_timings"][0]["first_audio_ms"]


def test_control_reply_keeps_the_earlier_routing_bridge_audio_clock(monkeypatch):
    logged = []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    monkeypatch.setenv("VOICE_STATUS_AFTER_MS", "40")
    monkeypatch.setenv("VOICE_BRIDGE_DELAY_MS", "40")

    def slow_routing(*args):
        time.sleep(0.2)
        return "live"

    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", slow_routing)
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: True)
    monkeypatch.setattr(custom.call_forwarding, "transfer", lambda *args, **kwargs: True)
    run_call(monkeypatch, [(0.2, "Connect me to the team now.", True, True)],
             logged=logged, bridges=True, model_seconds=0.5, call_seconds=1.3,
             bridge_selector=lambda *args: "Just putting that together for you.")
    timing = logged[-1]["voice_turn_timings"][0]
    assert timing["action"] == "live_transfer"
    assert timing["first_audible_ms"] == timing["bridge_first_audio_ms"] < timing["first_audio_ms"]


def test_complete_nova_question_ends_before_the_long_watchdog(monkeypatch):
    logged = []
    monkeypatch.setenv("CUSTOM_COMPLETE_TURN_MS", "350")
    monkeypatch.setenv("CUSTOM_TURN_TIMEOUT_MS", "2500")
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    starts, finished = run_call(monkeypatch, [(0.2, "What services do you provide?", False, False)],
                                model_seconds=0.05, logged=logged, call_seconds=1.2)
    assert finished == ["What services do you provide?"]
    assert logged[-1]["voice_turn_timings"][0]["turn_end_trigger"] == "nova_semantic_complete"


def test_the_answer_is_written_while_the_person_check_runs(monkeypatch):
    # 2026-10-31: answers ready at 1.9 s were heard at 4.0-4.3 s because they only started after this check.
    heard = []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)

    def slow_check(*args):
        time.sleep(0.6)
        return ""
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", slow_check)
    started = time.perf_counter()

    async def tts(text, engine, language="en"):
        heard.append((time.perf_counter() - started, text))
        yield b"\xff" * 160

    run_call(monkeypatch, [(0.3, "Do you deliver to Perth?", True, True)], tts=tts, model_seconds=0.5, call_seconds=2.4)
    answer_at = next(at for at, text in heard if text == "Here is the answer.")
    # Turn ends at 0.3 s; one after the other (0.6 s check + 0.5 s answer) it would be heard after 1.4 s.
    assert answer_at < 1.3, heard
