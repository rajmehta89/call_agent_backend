import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.custom_twilio_voice_api as custom
from twilio_security import verify_twilio_request


def snapshot(retell=False, custom_on=False, ready=True):
    return {"retell": {"enabled": retell, "ready": ready, "missing_configuration": []},
            "custom": {"enabled": custom_on, "ready": ready, "missing_configuration": []}}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(custom.router)
    app.dependency_overrides[verify_twilio_request] = lambda: None  # signatures are tested in test_security
    return TestClient(app, base_url="https://voice.example")


def route(client, monkeypatch, **state):
    monkeypatch.setattr(custom, "provider_snapshot", lambda: snapshot(**state))
    return client.post("/api/custom-voice/inbound", data={"CallSid": "CA1", "From": "+15550001111", "To": "+18178092336"}).text


def test_custom_stack_streams_audio_with_a_stream_token(client, monkeypatch):
    twiml = route(client, monkeypatch, custom_on=True)
    assert "wss://voice.example/api/custom-voice/media" in twiml and 'name="stream_token"' in twiml


def test_browser_phone_is_forwarded_to_custom_voice_stream(client, monkeypatch):
    monkeypatch.setattr(custom, "provider_snapshot", lambda: snapshot(custom_on=True))
    twiml = client.post(
        "/api/custom-voice/inbound",
        data={"CallSid": "CA-browser", "From": "client:browser_test", "caller_phone": "+918866631491"},
    ).text
    assert 'name="caller" value="+918866631491"' in twiml


def test_browser_phone_is_forwarded_to_conversation_relay(client, monkeypatch):
    monkeypatch.setattr(custom, "provider_snapshot", lambda: snapshot())
    twiml = client.post(
        "/api/custom-voice/inbound",
        data={"CallSid": "CA-browser", "From": "client:browser_test", "caller_phone": "+918866631491"},
    ).text
    assert 'name="caller_phone" value="+918866631491"' in twiml


def test_nothing_selected_falls_back_to_the_default_agent(client, monkeypatch):
    assert "<ConversationRelay" in route(client, monkeypatch)


def test_selected_but_not_configured_falls_back(client, monkeypatch):
    assert "<ConversationRelay" in route(client, monkeypatch, custom_on=True, ready=False)


def test_retell_selected_dials_retell_over_sip(client, monkeypatch):
    async def fake_register(form):
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Dial><Sip>sip:call_x@sip.retellai.com</Sip></Dial></Response>'

    monkeypatch.setattr(custom, "_retell_sip_twiml", fake_register)
    assert "sip:call_x@sip.retellai.com" in route(client, monkeypatch, retell=True)


def test_retell_failure_is_answered_by_the_custom_stack(client, monkeypatch):
    # The default agent it went to before had no transfers and switched languages turn by turn (2026-10-29 08:54).
    async def failing(form):
        raise RuntimeError("Retell down")

    monkeypatch.setattr(custom, "_retell_sip_twiml", failing)
    twiml = route(client, monkeypatch, retell=True)
    assert "wss://voice.example/api/custom-voice/media" in twiml and 'name="fallback_engine" value="elevenlabs"' in twiml
    monkeypatch.setattr(custom, "provider_snapshot", lambda: {**snapshot(retell=True),
                                                             "custom_deepgram": {"enabled": False, "ready": True, "missing_configuration": []}})
    twiml = client.post("/api/custom-voice/inbound", data={"CallSid": "CA1", "From": "+15550001111"}).text
    assert 'name="fallback_engine" value="deepgram"' in twiml  # the Deepgram voice when it is set up


def test_retell_failure_without_a_ready_custom_stack_uses_the_default_agent(client, monkeypatch):
    async def failing(form):
        raise RuntimeError("Retell down")

    monkeypatch.setattr(custom, "_retell_sip_twiml", failing)
    monkeypatch.setattr(custom, "provider_snapshot", lambda: {
        "retell": {"enabled": True, "ready": True, "missing_configuration": []},
        "custom": {"enabled": False, "ready": False, "missing_configuration": ["DEEPGRAM_API_KEY"]}})
    twiml = client.post("/api/custom-voice/inbound", data={"CallSid": "CA1", "From": "+15550001111"}).text
    assert "<ConversationRelay" in twiml and "fallback_engine" not in twiml


@pytest.mark.parametrize("raw, expected", [
    ("Sure! Raj builds bots.", "Raj builds bots."),
    ("Great, I can help with that.", "I can help with that."),
    ("Surely this works.", "Surely this works."),
])
def test_stock_openers_are_removed(raw, expected):
    assert custom._spoken_reply(raw) == expected


def test_spoken_replies_stop_at_a_sentence_within_the_limit(monkeypatch):
    monkeypatch.setenv("CUSTOM_MAX_SPOKEN_WORDS", "12")
    reply = custom._spoken_reply("One two three four five six. Seven eight nine ten eleven twelve thirteen. Fourteen.")
    assert reply == "One two three four five six."


def test_a_single_long_sentence_is_kept_whole(monkeypatch):
    monkeypatch.setenv("CUSTOM_MAX_SPOKEN_WORDS", "10")
    sentence = "This single sentence is deliberately much longer than the configured spoken word limit."
    assert custom._spoken_reply(sentence) == sentence
