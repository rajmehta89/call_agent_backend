import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import retell_api as retell


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("RETELL_CUSTOM_LLM_SECRET", "")
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
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-pause") as socket:
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
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-turn") as socket:
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
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/test-settings") as socket:
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"interaction_type": "call_details", "call": {}})
        assert socket.receive_json() == {"response_type": "update_agent", "agent_config": {
            "interruption_sensitivity": 0.85,
            "responsiveness": 1.0,
            "reminder_trigger_ms": 7000,
            "reminder_max_count": 1,
        }}
