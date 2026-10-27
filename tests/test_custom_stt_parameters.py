from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from routers import custom_twilio_voice_api as custom
from twilio_security import verify_twilio_request


def test_nova_auto_detection_does_not_send_an_unsupported_language_hint(monkeypatch):
    monkeypatch.setenv("CUSTOM_AUTO_LANGUAGE", "true")
    monkeypatch.setattr(custom, "current_keyterms", lambda: [])
    english = parse_qs(urlsplit(custom._deepgram_url("en")).query)
    urdu = parse_qs(urlsplit(custom._deepgram_url("ur")).query)
    assert english["language"] == ["multi"]
    assert "language_hint" not in english
    assert urdu["language"] == ["ur"]


def test_flux_selects_language_by_model_and_has_no_language_parameter(monkeypatch):
    monkeypatch.setattr(custom, "current_keyterms", lambda: [])
    params = parse_qs(urlsplit(custom._flux_url()).query)
    assert params["model"] == ["flux-general-en"]
    assert "language" not in params
    assert params["eot_timeout_ms"] == ["1800"]


def test_english_calls_use_flux_by_default_and_preserve_multilingual_nova(monkeypatch):
    monkeypatch.delenv("CUSTOM_AUTO_LANGUAGE", raising=False)
    monkeypatch.delenv("CUSTOM_STT", raising=False)
    assert custom._use_flux_for_language("en")
    assert not custom._use_flux_for_language("ar")
    assert not custom._use_flux_for_language("ur")

    monkeypatch.setenv("CUSTOM_AUTO_LANGUAGE", "true")
    assert not custom._use_flux_for_language("en")
    monkeypatch.setenv("CUSTOM_STT", "nova")
    monkeypatch.setenv("CUSTOM_AUTO_LANGUAGE", "false")
    assert not custom._use_flux_for_language("en")


def test_twilio_stream_uses_custom_parameters_instead_of_a_query_string(monkeypatch):
    monkeypatch.setattr(custom, "provider_snapshot", lambda: {
        "retell": {"enabled": False, "ready": True},
        "custom": {"enabled": True, "ready": True},
    })
    monkeypatch.setattr(custom, "language_for_call", lambda form: "ur")

    async def no_warm(*args):
        pass

    monkeypatch.setattr(custom, "_warm_audio_cache", no_warm)
    app = FastAPI()
    app.include_router(custom.router)
    app.dependency_overrides[verify_twilio_request] = lambda: None
    with TestClient(app, base_url="https://voice.example") as client:
        response = client.post("/api/custom-voice/inbound", data={"CallSid": "CA_test", "From": "+61400000000"})
    stream = ElementTree.fromstring(response.text).find("./Connect/Stream")
    assert urlsplit(stream.attrib["url"]).query == ""
    params = {child.attrib["name"]: child.attrib["value"] for child in stream.findall("Parameter")}
    assert params["preferred_language"] == "ur"
    assert params["stream_token"]


def test_stt_does_not_open_before_the_start_token_is_validated(monkeypatch):
    connections = []

    async def connect(*args, **kwargs):
        connections.append(args)
        raise AssertionError("Unauthenticated media must not open a provider connection")

    monkeypatch.setattr(custom, "custom_stack_enabled", lambda: True)
    monkeypatch.setattr(custom, "provider_snapshot", lambda: {})
    monkeypatch.setattr(custom, "_active_custom", lambda providers: {"ready": True})
    monkeypatch.setattr(custom, "custom_voice_engine", lambda: "deepgram")
    monkeypatch.setattr(custom, "valid_stream_token", lambda *args: False)
    monkeypatch.setattr(custom.websockets, "connect", connect)
    app = FastAPI()
    app.include_router(custom.router)
    with TestClient(app).websocket_connect("/api/custom-voice/media") as socket:
        socket.send_json({"event": "start", "start": {"callSid": "CA_invalid", "customParameters": {}}})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert not connections
