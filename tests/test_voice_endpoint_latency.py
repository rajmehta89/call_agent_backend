"""Keep provider endpoint timing separate from local processing on both voice paths."""

import asyncio
import json
import logging
from threading import Event, get_ident

import pytest

import call_forwarding
import logging_setup
import meaning
import voice_control
import voice_endpoint
import voice_turns
from routers import custom_twilio_voice_api as custom, retell_api as retell
import test_custom_voice_early_start as harness
from test_retell_turn_control import app as retell_app
from fastapi.testclient import TestClient


@pytest.fixture(scope="module", autouse=True)
def warm_semantics():
    import turn_meaning
    from voice_presence import CONNECTION_CHECK
    for classifier in [*vars(turn_meaning).values(), *vars(voice_control).values(), CONNECTION_CHECK]:
        if isinstance(classifier, meaning.Classifier):
            classifier.rank("hello")


def test_flux_separates_provider_detection_from_audio_delivery_backlog():
    data = voice_endpoint.flux_endpoint_timing({"audio_window_end": 1.3, "words": [{"end": 0.78}],
                "turn_index": 3, "end_of_turn_confidence": 0.86}, 2.0)
    assert data["stt_provider_detection_ms"] == 520
    assert data["stt_delivery_backlog_ms"] == 700
    assert data["stt_end_of_turn_confidence"] == 0.86


def test_flux_reconnect_uses_the_same_audio_clock_and_missing_words_are_unknown():
    data = voice_endpoint.flux_endpoint_timing({"audio_window_end": 1.3, "words": [{"end": 0.78}]}, 12.0, 10.0)
    assert data["stt_provider_detection_ms"] == 520 and data["stt_delivery_backlog_ms"] == 700
    assert data["stt_last_word_end_ms"] == 10780
    assert "stt_provider_detection_ms" not in voice_endpoint.flux_endpoint_timing({"audio_window_end": 1.3}, 2.0)
    assert "stt_provider_detection_ms" not in voice_endpoint.flux_endpoint_timing({"audio_window_end": float("nan")}, 2.0)


def test_retell_diagnostics_do_not_clip_the_observed_endpoint_lag_to_the_bridge_budget():
    transcript = [{"role": "user", "words": [{"end": 1.0}]}]
    data = voice_endpoint.retell_endpoint_timing(transcript, 100.0, 105.0)
    assert data["speech_end_to_event_received_ms"] == 4000
    assert data["turn_detection_source"] == "retell_word_clock_estimate"
    mismatch = voice_endpoint.retell_endpoint_timing(transcript, 100.0, 100.5)
    assert mismatch["stt_clock_mismatch"] is True and "speech_end_to_event_received_ms" not in mismatch


def test_slow_log_flush_does_not_hold_the_voice_loop_and_keeps_call_context():
    entered, release, finished = Event(), Event(), Event()
    records, sink_threads = [], []

    class Sink(logging.Handler):
        def emit(self, record):
            sink_threads.append(get_ident())
            entered.set()
            assert release.wait(2)
            records.append(record)
            finished.set()

    queued, listener = logging_setup._start_sink_worker([Sink()])
    logger = logging.Logger("voice-latency-test", level=logging.INFO)
    logger.addHandler(queued)

    async def run():
        loop_thread = get_ident()
        logging_setup.bind_call("CA123456789")
        logger.info("First sentence ready")
        assert await asyncio.to_thread(entered.wait, 2)
        # This heartbeat executes while the sink is still blocked.
        await asyncio.sleep(0)
        assert not finished.is_set()
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert sink_threads == [thread for thread in sink_threads if thread != loop_thread]

    try:
        asyncio.run(run())
    finally:
        release.set()
        listener.stop()
    assert len(records) == 1 and records[0].call_id == "23456789"


def test_generic_semantic_probes_reuse_the_full_foreign_turn_translation(monkeypatch):
    import voice_keyterms
    import shopify_service
    calls = []
    text = "मुझे Home DFO से क्या मदद मिल सकती है?"
    monkeypatch.setenv("MEANING_TRANSLATION", "true")
    monkeypatch.setattr(meaning, "_TRANSLATIONS", meaning.OrderedDict())
    monkeypatch.setattr(meaning, "_translate", lambda value: calls.append(value) or "What help can Home DFO give me?")
    monkeypatch.setattr(voice_keyterms, "business_name", lambda: "Home DFO")
    monkeypatch.setattr(shopify_service.shopify_service, "product_kinds", lambda: frozenset())
    probe = voice_turns.kind_of_turn_text(text)
    assert probe == "What help can your company give me?"
    assert meaning.english_text(text) == "What help can Home DFO give me?"
    assert meaning.english_text(probe) == probe and calls == [text]


def test_translation_prefetch_coalesces_words_without_the_cpu_round_debounce(monkeypatch):
    calls = []
    monkeypatch.setattr(retell._TranslationPrefetch, "STABLE_SECONDS", 0.001)
    monkeypatch.setattr(meaning, "english_text", lambda text: calls.append(text) or "A question")

    async def run():
        warm = retell._TranslationPrefetch()
        warm.round_seconds = 2.0
        assert warm.pause_seconds() == 0.001
        warm.schedule("मुझे")
        warm.schedule("मुझे मदद")
        warm.schedule("मुझे घर के लिए मदद चाहिए")
        await asyncio.wait_for(warm.task, 2)
        await warm.close()

    asyncio.run(run())
    assert calls == ["मुझे घर के लिए मदद चाहिए"]


@pytest.mark.parametrize("text", [
    "I just bought a home and need help getting started.",
    "I have just purchased a home, so I need a startup.",
    "Can you guide me on what to choose for my new house?",
])
def test_a_home_project_request_does_not_trigger_a_transfer_timing_choice(monkeypatch, text):
    from turn_meaning import HANDOFF_ACTION
    monkeypatch.setattr(HANDOFF_ACTION, "rank", lambda *args: [
        ("unclear", 0.80), ("other", 0.70), ("callback", 0.60), ("live", 0.59)])
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff_cached", lambda *a, **k:
                        pytest.fail("Do not make another network decision for clear business guidance"))
    assert not voice_control.clearly_requests_person(text)
    assert call_forwarding.handoff_intent(text) == ""


@pytest.mark.parametrize("text,earlier,agent", [
    ("Could I speak to someone?", "", ""),
    ("Either works.", "", "Would you like the team now, or should they call you back?"),
    ("Can we arrange that?", "I need to speak with your team.", ""),
])
def test_unclear_handoff_timing_preserves_a_person_request_or_existing_offer(monkeypatch, text, earlier, agent):
    from turn_meaning import HANDOFF_ACTION
    monkeypatch.setenv("HANDOFF_LLM_CHECK", "false")
    monkeypatch.setattr(HANDOFF_ACTION, "rank", lambda *args: [
        ("unclear", 0.80), ("other", 0.70), ("callback", 0.60), ("live", 0.59)])
    assert call_forwarding.handoff_intent(text, earlier, agent) == "unclear"


def test_eager_semantic_check_does_not_block_reading_the_final_turn(monkeypatch):
    final_received = Event()
    blocked, logged = [], []
    question = "Do you sell bathroom basins?"
    original = harness.FakeDeepgram

    class ObservedDeepgram(original):
        async def _messages(self):
            async for raw in super()._messages():
                data = json.loads(raw)
                if data.get("event") == "EndOfTurn":
                    final_received.set()
                yield raw

    def presence(text):
        if text == question:
            blocked.append(not final_received.wait(1))
        return False

    monkeypatch.setattr(harness, "FakeDeepgram", ObservedDeepgram)
    monkeypatch.setattr(custom, "is_connection_check", presence)
    monkeypatch.setattr(call_forwarding, "contextual_handoff_intent", lambda *args: "")
    script = [(0.2, {"event": "EagerEndOfTurn", "transcript": question}),
              (0.25, {"event": "EndOfTurn", "transcript": question, "audio_window_end": 1.3,
                      "words": [{"end": 0.78}], "end_of_turn_confidence": 0.86})]
    harness.run_call(monkeypatch, script, stt="flux", model_seconds=0.02, call_seconds=1.6, logged=logged)
    assert blocked and not any(blocked)
    assert logged[-1]["voice_turn_timings"][0]["stt_provider_detection_ms"] == 520


def test_retell_saves_full_endpoint_lag_and_persists_off_the_voice_loop(retell_app, monkeypatch):
    clock = [100.0]
    saved = Event()
    calls, voice_threads, save_threads = [], [], []

    async def answer(*args, **kwargs):
        voice_threads.append(get_ident())
        yield "We can help you choose bathroom products."

    def save(*args, **kwargs):
        save_threads.append(get_ident())
        calls.append(kwargs["call_data"])
        saved.set()

    monkeypatch.setattr(retell, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(retell, "_stream_reply", answer)
    monkeypatch.setattr(retell, "log_call", save)
    monkeypatch.setattr(retell, "fetch_retell_outcome_later", lambda *args: None)
    with TestClient(retell_app) as client:
        with client.websocket_connect("/api/retell/llm-websocket/test-endpoint?token=test-retell-websocket") as socket:
            socket.receive_json()
            socket.receive_json()
            socket.send_json({"interaction_type": "call_details", "call": {"from_number": "+61412345678"}})
            socket.receive_json()
            clock[0] = 106.0
            socket.send_json({"interaction_type": "response_required", "response_id": 1,
                              "transcript": [{"role": "user", "content": "What do you sell?",
                                              "words": [{"end": 1.0}]}]})
            assert socket.receive_json()["content"] == "We can help you choose bathroom products."
            assert socket.receive_json()["content_complete"] is True
            socket.send_json({"interaction_type": "ping_pong", "timestamp": 123})
            assert socket.receive_json()["response_type"] == "ping_pong"
            # Send the disconnect while the ASGI task is still alive. Exiting
            # the test websocket context can cancel it before async cleanup.
            socket.close()
            assert saved.wait(3)
    timing = calls[-1]["voice_turn_timings"][0]
    assert timing["speech_end_to_event_received_ms"] == 5000
    assert timing["speech_end_lag_ms"] == 1500  # bounded clock remains only for bridge scheduling
    assert timing["stt_event_handling_ms"] == 0 and timing["stt_event_received_at"]
    assert voice_threads and save_threads and set(voice_threads).isdisjoint(save_threads)
