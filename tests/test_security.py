import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import twilio_security
from secret_store import decrypt_secret, encrypt_secret
from twilio_security import stream_token, valid_stream_token, verify_twilio_request

# Routes that must stay reachable without a sign-in or a Twilio signature.
PUBLIC_ROUTES = {
    "/", "/health",
    "/api/auth/login", "/api/auth/signup", "/api/auth/config", "/api/auth/invite/{token}", "/api/auth/accept-invite",
    # These verify the caller themselves: stream token, Retell secret, or Retell signature.
    "/api/custom-voice/media", "/api/twilio/ws", "/api/retell/llm-websocket/{call_id}", "/api/retell/webhook",
    # Checks the sign-in token itself (passed in the URL: browsers' EventSource cannot send headers).
    "/api/realtime/events",
}
GUARDS = {"require_user", "dependency", "verify_twilio_request"}


def _dependency_names(dependant):
    names = []
    for sub in dependant.dependencies:
        names.append(getattr(sub.call, "__name__", ""))
        names.extend(_dependency_names(sub))
    return names


def _routes(routes):
    for route in routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            yield route
        elif hasattr(route, "routes"):
            yield from _routes(route.routes)
        elif hasattr(route, "original_router"):
            yield from _routes(route.original_router.routes)


def test_every_route_is_protected_unless_explicitly_public():
    import main

    unprotected = sorted({route.path for route in _routes(main.app.router.routes)
                          if route.path not in PUBLIC_ROUTES and not GUARDS & set(_dependency_names(route.dependant))})
    assert unprotected == [], f"new routes need a sign-in or Twilio signature: {unprotected}"


@pytest.fixture
def webhook_client():
    app = FastAPI()

    @app.post("/hook", dependencies=[Depends(verify_twilio_request)])
    async def hook():
        return {"ok": True}

    return TestClient(app, base_url="https://example.ngrok.app")


def test_twilio_webhook_accepts_a_valid_signature(webhook_client):
    params = {"CallSid": "CA123", "From": "+15550001111"}
    signature = RequestValidator("test-twilio-token").compute_signature("https://example.ngrok.app/hook", params)
    assert webhook_client.post("/hook", data=params, headers={"X-Twilio-Signature": signature}).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"X-Twilio-Signature": "forged"}])
def test_twilio_webhook_rejects_missing_or_forged_signatures(webhook_client, headers):
    assert webhook_client.post("/hook", data={"CallSid": "CA123"}, headers=headers).status_code == 403


def test_signature_uses_the_public_url_behind_a_proxy(webhook_client):
    params = {"CallSid": "CA123"}
    signature = RequestValidator("test-twilio-token").compute_signature("https://public.example/hook", params)
    headers = {"X-Twilio-Signature": signature, "X-Forwarded-Proto": "https", "X-Forwarded-Host": "public.example"}
    assert webhook_client.post("/hook", data=params, headers=headers).status_code == 200


def test_stream_token_is_bound_to_the_call_and_expires(monkeypatch):
    token = stream_token("CA-one")
    assert valid_stream_token("CA-one", token)
    assert not valid_stream_token("CA-two", token)
    assert not valid_stream_token("CA-one", "garbage")
    later = time.time() + twilio_security.STREAM_TOKEN_TTL_SECONDS + 5
    monkeypatch.setattr(twilio_security.time, "time", lambda: later)
    assert not valid_stream_token("CA-one", token)


def test_secrets_are_encrypted_and_legacy_plain_text_still_reads():
    stored = encrypt_secret("shpss_secret_value")
    assert stored.startswith("enc:v1:") and "shpss" not in stored
    assert decrypt_secret(stored) == "shpss_secret_value"
    assert decrypt_secret("shpat_legacy") == "shpat_legacy"
    assert encrypt_secret(stored) == stored  # never double-encrypts


def test_realtime_stream_requires_a_valid_sign_in():
    from fastapi.testclient import TestClient

    import main

    client = TestClient(main.app)
    assert client.get("/api/realtime/events").status_code == 422  # no token at all
    assert client.get("/api/realtime/events", params={"token": "not-a-real-token"}).status_code == 401
