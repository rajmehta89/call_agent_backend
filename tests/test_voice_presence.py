"""Semantic connection checks and starvation-free turn repair through the real voice handler."""

import asyncio
import time

import pytest

import voice_presence
from routers import custom_twilio_voice_api as custom
from test_custom_voice_early_start import run_call


@pytest.fixture(scope="module", autouse=True)
def warm_semantic_decisions():
    import turn_meaning
    from meaning import Classifier
    for classifier in vars(turn_meaning).values():
        if isinstance(classifier, Classifier):
            classifier.rank("hello")
    voice_presence.CONNECTION_CHECK.rank("hello")


@pytest.mark.parametrize("text", [
    "Hello? Hello? Are you with me?", "Can you still hear what I am saying?",
    "Is anybody listening?", "Hello, are you still on the phone?", "Have you gone away?",
    "Can you hear me? Hello? Hello?",
])
def test_connection_checks_are_recognized_by_meaning(text):
    assert voice_presence.is_connection_check(text)


@pytest.mark.parametrize("text", [
    "Hello, I want to return my order.", "Are you there? Actually, stop, I need a refund.",
    "Hello, could you transfer me to your sales team?", "Can someone call me tomorrow?",
    "Please say that price again.", "Wait, I need to ask something else.",
    "No, I meant kitchen taps.", "Hello, hello. What are your hours tomorrow?",
    "Are you there? Cancel my callback and help me with delivery instead.", "Okay, thank you.",
])
def test_real_requests_and_explicit_interruptions_are_not_suppressed(text):
    assert not voice_presence.is_connection_check(text)


def test_unavailable_or_ambiguous_semantics_do_not_guess_presence(monkeypatch):
    monkeypatch.setattr(voice_presence.CONNECTION_CHECK, "close", lambda text: None)
    assert not voice_presence.is_connection_check("Hello?")
    monkeypatch.setattr(voice_presence.CONNECTION_CHECK, "close", lambda text: ["presence", "request"])
    assert not voice_presence.is_connection_check("Hello, help with my order?")


def test_connection_check_uses_shared_translation_and_localized_reply(monkeypatch):
    import meaning
    original = meaning.english_text
    monkeypatch.setattr(meaning, "english_text", lambda text: "Hello, can you hear me?"
                        if text == "ہیلو، کیا آپ میری آواز سن رہے ہیں؟" else original(text))
    assert voice_presence.is_connection_check("ہیلو، کیا آپ میری آواز سن رہے ہیں؟")
    assert "موجود" in voice_presence.connection_reply("ur", True)
    assert "أسمعك" in voice_presence.connection_reply("ar", False)


@pytest.mark.parametrize("stt", ["nova", "flux"])
def test_repeated_connection_checks_preserve_one_pending_answer(monkeypatch, stt):
    spoken, logged = [], []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")

    async def tts(text, engine, language="en"):
        spoken.append(text)
        yield b"\xff" * 160

    turns = [(0.2, "What are your opening hours?"), (0.65, "Hello, can you hear me?"),
             (0.9, "Hello? Hello? Are you there?")]
    if stt == "flux":
        script = [(at, {"event": "EndOfTurn", "transcript": text}) for at, text in turns]
    else:
        script = [(at, text, True, True) for at, text in turns]
    starts, finished = run_call(monkeypatch, script, stt=stt, tts=tts, logged=logged,
                                model_seconds=1.1, call_seconds=2.2, answer_text="We open at nine.")
    assert [text for _, text in starts] == [turns[0][1]]
    assert finished == [turns[0][1]]
    assert spoken.count(voice_presence.connection_reply("en", True)) == 1
    assert spoken.count("We open at nine.") == 1
    assert logged[-1]["provider_metadata"]["connection_checks"] == {"checks": 2, "reassurances": 1, "preserved_answers": 2}
    assert len([turn for turn in logged[-1]["transcription"] if turn.get("turn_kind") == "connection_check"]) == 2


def test_presence_then_real_correction_keeps_the_question_and_checks_in_history(monkeypatch):
    logged = []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")
    script = [(0.2, "What do you sell?", True, True), (0.6, "Hello, are you there?", True, True),
              (0.9, "I meant the kitchen products.", True, True)]
    starts, finished = run_call(monkeypatch, script, model_seconds=1.1, call_seconds=2.7, logged=logged)
    assert [text for _, text in starts] == ["What do you sell?", "What do you sell? I meant the kitchen products."]
    assert finished == ["What do you sell? I meant the kitchen products."]
    callers = [turn["content"] for turn in logged[-1]["transcription"]]
    assert callers == ["Hello, are you there?", "What do you sell? I meant the kitchen products."]


def test_idle_connection_check_gets_an_answer_without_business_model_work(monkeypatch):
    spoken = []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)

    async def tts(text, engine, language="en"):
        spoken.append(text)
        yield b"\xff" * 160

    starts, finished = run_call(monkeypatch, [(0.2, "Hello, can you hear me?", True, True)],
                                tts=tts, model_seconds=0.1, call_seconds=1.0)
    assert not starts and not finished
    assert voice_presence.connection_reply("en", False) in spoken


def test_answer_waits_for_a_real_continuation_instead_of_talking_over_it(monkeypatch):
    spoken, logged = [], []
    began = time.perf_counter()
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: text != "I actually meant")
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")

    async def tts(text, engine, language="en"):
        spoken.append((time.perf_counter() - began, text))
        yield b"\xff" * 160

    script = [(0.2, "What do you sell?", True, True),
              (0.45, "I actually meant", False, False),
              (1.1, "I actually meant the kitchen products.", True, True)]
    starts, finished = run_call(monkeypatch, script, model_seconds=0.6, call_seconds=2.4, tts=tts, logged=logged)
    assert [text for _, text in starts] == ["What do you sell?", "What do you sell? I actually meant the kitchen products."]
    assert finished == ["What do you sell? I actually meant the kitchen products."]
    answers = [(at, text) for at, text in spoken if text == "Here is the answer."]
    assert len(answers) == 1 and answers[0][0] >= 1.1


def test_reassurance_and_ready_answer_never_stream_audio_together(monkeypatch):
    streaming = 0
    spoken = []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")

    async def tts(text, engine, language="en"):
        nonlocal streaming
        streaming += 1
        try:
            assert streaming == 1, "reassurance overlapped the substantive answer"
            spoken.append(text)
            if text == voice_presence.connection_reply("en", True):
                yield b"\xff" * 160
                await asyncio.sleep(0.5)
            yield b"\xff" * 160
        finally:
            streaming -= 1

    starts, finished = run_call(monkeypatch, [(0.2, "What do you sell?", True, True),
                                             (0.4, "Can you hear me?", True, True)],
                                model_seconds=0.4, call_seconds=1.7, tts=tts)
    assert finished == ["What do you sell?"]
    assert spoken.index(voice_presence.connection_reply("en", True)) < spoken.index("Here is the answer.")


def test_unclear_partial_does_not_destroy_an_answer_before_final_presence_intent(monkeypatch):
    check = custom.is_connection_check
    monkeypatch.setattr(custom, "is_connection_check", lambda text: False if text == "Is somebody" else check(text))
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: text != "Is somebody")
    monkeypatch.setattr(custom.call_forwarding, "contextual_handoff_intent", lambda *args: None)
    monkeypatch.setattr(custom.call_forwarding, "handoff_intent", lambda *args: "")
    logged = []
    script = [(0.2, "What do you sell?", True, True), (0.4, "Is somebody", False, False),
              (0.9, "Is anybody there?", True, True)]
    starts, finished = run_call(monkeypatch, script, model_seconds=0.5, call_seconds=1.6, logged=logged)
    assert [text for _, text in starts] == ["What do you sell?"]
    assert finished == ["What do you sell?"]
    assert logged[-1]["provider_metadata"]["connection_checks"]["preserved_answers"] == 1


def test_stop_still_interrupts_a_connection_reassurance(monkeypatch):
    cancelled = []
    spoken = []
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)

    async def tts(text, engine, language="en"):
        spoken.append(text)
        try:
            yield b"\xff" * 160
            if text == voice_presence.connection_reply("en", False):
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            cancelled.append(text)
            raise

    starts, finished = run_call(monkeypatch, [(0.2, "Hello, can you hear me?", True, True),
                                             (0.5, "Stop.", True, True)],
                                tts=tts, call_seconds=1.2)
    assert not starts and not finished
    assert cancelled == [voice_presence.connection_reply("en", False)]
    assert custom.pause_reply("Stop.") in spoken
