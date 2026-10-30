import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import retell_api as retell


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("RETELL_CUSTOM_LLM_SECRET", "test-retell-websocket")
    monkeypatch.setattr(retell, "_enabled", lambda: True)
    monkeypatch.setattr(retell, "_trace", lambda *args, **kwargs: None)
    services = SimpleNamespace(is_llm_configured=lambda: True)
    monkeypatch.setattr(retell, "AIServices", lambda: services)
    monkeypatch.setattr(retell, "configure_voice_services", lambda client, model: client)
    monkeypatch.setattr(retell, "prepare_turn_meaning", lambda *args: None)
    monkeypatch.setattr(retell, "call_is_over", lambda *args: False)
    monkeypatch.setattr(retell, "bridges_enabled", lambda: False)
    monkeypatch.setattr(retell, "warm_model_connection", lambda *args: None)
    monkeypatch.setattr(retell, "_system_prompt", lambda *args: "")
    monkeypatch.setattr(retell.call_forwarding, "handoff_intent", lambda *args: "")
    monkeypatch.setattr(retell.call_forwarding, "forwarding_on", lambda: False)
    monkeypatch.setattr(retell.live_transcript, "save_live", lambda *args, **kwargs: None)
    app = FastAPI()
    app.include_router(retell.router)
    return app


def test_sorry_is_acknowledged_without_restarting_product_reasoning(app, monkeypatch):
    def unexpected(*args):
        raise AssertionError("A pause should not trigger semantic, store or model work")

    monkeypatch.setattr(retell, "prepare_turn_meaning", unexpected)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-pause?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "Yeah, sorry, sorry."}]})
        result = socket.receive_json()
        assert result["content"] == "Of course. Go ahead."
        assert result["content_complete"] is True


@pytest.mark.parametrize("turntaking", [None, "user_turn"])
def test_only_an_explicit_user_turn_cancels_an_old_response(app, monkeypatch, turntaking):
    started, cancelled, release = Event(), Event(), Event()

    async def answer(*args, **kwargs):
        response_id = args[4]
        if response_id == 1:
            started.set()
            try:
                while not release.is_set():
                    await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield "First answer."
        else:
            yield "New answer."

    monkeypatch.setattr(retell, "_stream_reply", answer)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-turn?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "What do you sell?"}]})
        assert started.wait(3)
        update = {"interaction_type": "update_only", "transcript": [{"role": "user", "content": "One more question."}]}
        if turntaking:
            update["turntaking"] = turntaking
        socket.send_json(update)
        socket.send_json({"interaction_type": "ping_pong", "timestamp": 123})
        assert socket.receive_json()["response_type"] == "ping_pong"
        if turntaking:
            assert cancelled.wait(3)
            socket.send_json({"interaction_type": "response_required", "response_id": 2,
                              "transcript": [{"role": "user", "content": "What are your hours?"}]})
            assert socket.receive_json()["content"] == "New answer."
        else:
            assert not cancelled.is_set()
            release.set()
            assert socket.receive_json()["content"] == "First answer."


def test_call_uses_responsive_turn_detection_and_one_silence_reminder(app, monkeypatch):
    monkeypatch.delenv("RETELL_INTERRUPTION_SENSITIVITY", raising=False)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-settings?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "call_details", "call": {}})
        assert socket.receive_json() == {"response_type": "update_agent", "agent_config": {
            "interruption_sensitivity": 0.85,
            "responsiveness": 1.0,
            "reminder_trigger_ms": 7000,
            "reminder_max_count": 1,
        }}


def test_explicit_later_callback_is_queued_without_more_contact_questions(app, monkeypatch):
    queued = {}
    monkeypatch.setattr(retell.call_forwarding, "handoff_intent", lambda *args: "callback")
    monkeypatch.setattr(retell.call_forwarding, "requested_agent", lambda *args: None)
    monkeypatch.setattr(retell.call_forwarding, "create_callback_outreach",
                        lambda *args, **kwargs: queued.update(args=args, kwargs=kwargs) or "Human team")
    monkeypatch.setattr(retell.transfer_service, "request_transfer", lambda *args, **kwargs: queued.update(transfer=True))
    monkeypatch.setattr(retell.transfer_service, "update_transfer", lambda *args, **kwargs: None)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-callback?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "Someone call me later."}]})
        response = socket.receive_json()
        assert response["content_complete"] is True
        assert "marked your request" in response["content"]
        assert queued.get("transfer") is True


def test_slow_retell_answer_gets_one_wait_bridge_then_the_answer(app, monkeypatch):
    monkeypatch.setattr(retell, "bridges_enabled", lambda: True)
    monkeypatch.setattr(retell, "status_after_seconds", lambda: 0.01)
    monkeypatch.setattr(retell, "wait_bridge_phrase", lambda *args: "Just putting that together for you.")

    async def answer(*args, **kwargs):
        await asyncio.sleep(0.08)
        yield "Here are the round basins."

    monkeypatch.setattr(retell, "_stream_reply", answer)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-wait-bridge?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "I'm looking for a round basin."}]})
        bridge = socket.receive_json()
        reply = socket.receive_json()
        complete = socket.receive_json()
        assert bridge["content"].strip() == "Just putting that together for you."
        assert reply["content"] == "Here are the round basins."
        assert complete["content_complete"] is True and complete["content"] == ""


def test_slow_short_retell_reply_does_not_receive_generic_filler(app, monkeypatch):
    monkeypatch.setattr(retell, "bridges_enabled", lambda: True)
    monkeypatch.setattr(retell, "status_after_seconds", lambda: 0.01)

    async def answer(*args, **kwargs):
        await asyncio.sleep(0.05)
        yield "Go ahead."

    monkeypatch.setattr(retell, "_stream_reply", answer)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-short-no-bridge?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "No. So so."}]})
        assert socket.receive_json()["content"] == "Go ahead."
        assert socket.receive_json()["content_complete"] is True


def test_callback_then_ok_does_not_reopen_handoff_or_invoke_model(app, monkeypatch):
    queued = []
    monkeypatch.setattr(retell.call_forwarding, "requested_agent", lambda *args: None)
    monkeypatch.setattr(retell.call_forwarding, "create_callback_outreach",
                        lambda *args, **kwargs: queued.append(args) or "Human team")
    monkeypatch.setattr(retell.transfer_service, "request_transfer", lambda *args, **kwargs: None)
    monkeypatch.setattr(retell.transfer_service, "update_transfer", lambda *args, **kwargs: None)

    async def no_model(*args, **kwargs):
        raise AssertionError("The callback confirmation is handled directly")
        yield

    monkeypatch.setattr(retell.call_forwarding, "handoff_intent", lambda *args: "callback")
    monkeypatch.setattr(retell, "_stream_reply", no_model)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-callback-state?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "call_details", "call": {"from_number": "+61412345678"}})
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "Someone call me later."}]})
        queued_reply = socket.receive_json()["content"]
        assert "phone number" not in queued_reply and "using this number" in queued_reply
        socket.send_json({"interaction_type": "response_required", "response_id": 2,
                          "transcript": [{"role": "assistant", "content": queued_reply},
                                         {"role": "user", "content": "OK."}]})
        assert socket.receive_json()["content"] == "You're welcome."
        assert len(queued) == 1


def test_yes_to_live_offer_does_not_ask_now_or_later_again(app, monkeypatch):
    monkeypatch.setattr(retell.call_forwarding, "forwarding_on", lambda: True)
    monkeypatch.setattr(retell.call_forwarding, "twilio_call_for_retell",
                        lambda *args: {"call_sid": "CA_test", "caller": "+61412345678"})
    monkeypatch.setattr(retell.call_forwarding, "transfer", lambda *args, **kwargs: True)

    def no_classifier(*args):
        raise AssertionError("Acceptance of a live transfer offer is already explicit")

    monkeypatch.setattr(retell.call_forwarding, "handoff_intent", no_classifier)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-offer-accepted?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "assistant", "content": "Would you like me to connect you with the team for full details?"},
                                         {"role": "user", "content": "Yeah. Yeah. Please."}]})
        response = socket.receive_json()
        assert response["content"] == retell.call_forwarding.CONNECTING_LINE
        assert response["content_complete"] is True


def test_slow_retell_routing_gets_only_one_bridge_for_the_whole_turn(app, monkeypatch):
    import time
    monkeypatch.setattr(retell, "bridges_enabled", lambda: True)
    monkeypatch.setattr(retell, "status_after_seconds", lambda: 0.01)
    monkeypatch.setattr(retell, "wait_bridge_phrase", lambda *args: "Just putting that together for you.")
    def slow_routing(*args):
        time.sleep(0.08)
        return ""
    monkeypatch.setattr(retell.call_forwarding, "contextual_handoff_intent", slow_routing)
    async def answer(*args, **kwargs):
        await asyncio.sleep(0.08)
        yield "Here is the answer."
    monkeypatch.setattr(retell, "_stream_reply", answer)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/routing-wait?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "What services do you provide?"}]})
        assert socket.receive_json()["content"].strip() == "Just putting that together for you."
        assert socket.receive_json()["content"] == "Here is the answer."
        assert socket.receive_json()["content_complete"] is True


def test_answer_starts_alongside_the_handoff_check(app, monkeypatch):
    # Production Retell turns waited 0.5-1 s for the handoff check before the answer even started.
    import time
    order = []

    def slow_handoff(*args):
        time.sleep(0.4)
        order.append("handoff done")
        return ""

    async def answer(*args, **kwargs):
        order.append("answer started")
        yield "We sell bathroom products."

    monkeypatch.setattr(retell.call_forwarding, "handoff_intent", slow_handoff)
    monkeypatch.setattr(retell, "_stream_reply", answer)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-early?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "What do you sell?"}]})
        assert socket.receive_json()["content"] == "We sell bathroom products."
    assert order == ["answer started", "handoff done"]


def test_a_callback_turn_never_sends_the_early_answer(app, monkeypatch):
    monkeypatch.setattr(retell.call_forwarding, "handoff_intent", lambda *args: "callback")
    monkeypatch.setattr(retell.call_forwarding, "requested_agent", lambda *args: None)
    monkeypatch.setattr(retell.call_forwarding, "create_callback_outreach", lambda *args, **kwargs: "Human team")
    monkeypatch.setattr(retell.transfer_service, "request_transfer", lambda *args, **kwargs: None)
    monkeypatch.setattr(retell.transfer_service, "update_transfer", lambda *args, **kwargs: None)

    async def answer(*args, **kwargs):
        yield "EARLY ANSWER"

    monkeypatch.setattr(retell, "_stream_reply", answer)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-early-callback?token=test-retell-websocket") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "Someone call me later."}]})
        response = socket.receive_json()
        assert "marked your request" in response["content"] and "EARLY" not in response["content"]
        assert response["content_complete"] is True
