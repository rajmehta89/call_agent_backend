import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import retell_api as retell


def test_pending_handoff_does_not_start_another_reply_or_transfer(monkeypatch):
    monkeypatch.setenv("RETELL_CUSTOM_LLM_SECRET", "test-retell-websocket")
    monkeypatch.setattr(retell, "_enabled", lambda: True)
    monkeypatch.setattr(retell, "_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(retell, "prepare_turn_meaning", lambda *args: None)
    services = SimpleNamespace(is_llm_configured=lambda: True)
    monkeypatch.setattr(retell, "AIServices", lambda: services)
    monkeypatch.setattr(retell, "configure_voice_services", lambda client, model: client)
    monkeypatch.setattr(retell.call_forwarding, "handoff_intent", lambda text, *rest: "live")
    monkeypatch.setattr(retell.call_forwarding, "forwarding_on", lambda: True)
    monkeypatch.setattr(retell.call_forwarding, "twilio_call_for_retell", lambda call_id: {"call_sid": "CA_TEST", "caller": "+61400000000"})
    monkeypatch.setattr(retell, "call_is_over", lambda *args: False)
    monkeypatch.setattr(retell.live_transcript, "save_live", lambda *args, **kwargs: None)
    transfers = []

    async def transfer(*args):
        transfers.append(args)
        await asyncio.sleep(60)
        return True

    async def unexpected_reply(*args, **kwargs):
        raise AssertionError("AI reply started after handoff")
        yield ""

    monkeypatch.setattr(retell, "_hand_to_team", transfer)
    monkeypatch.setattr(retell, "_stream_reply", unexpected_reply)
    app = FastAPI()
    app.include_router(retell.router)
    with TestClient(app).websocket_connect("/api/retell/llm-websocket/call_handoff_test?token=test-retell-websocket") as socket:
        socket.receive_json()  # config
        socket.receive_json()  # greeting
        socket.send_json({"interaction_type": "response_required", "response_id": 1,
                          "transcript": [{"role": "user", "content": "Connect me to your team now."}]})
        assert socket.receive_json()["content"] == retell.call_forwarding.CONNECTING_LINE
        socket.send_json({"interaction_type": "response_required", "response_id": 2,
                          "transcript": [{"role": "user", "content": "Your teammate?"}]})
        assert socket.receive_json() == {"response_type": "response", "response_id": 2,
                                         "content": "", "content_complete": True}
        assert len(transfers) == 1
