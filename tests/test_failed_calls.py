"""Calls that fail before a voice agent answers (busy, no answer, ...) must still be recorded, or the inbox Failed folder stays empty."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.twilio_api as twilio_api
from twilio_security import verify_twilio_request


@pytest.fixture
def status(monkeypatch):
    recorded, updated = [], {"result": False}
    monkeypatch.setattr(twilio_api, "update_call_from_twilio", lambda sid, call_status=None: updated["result"])
    monkeypatch.setattr(twilio_api, "record_failed_call", lambda sid, caller, call_status, direction="inbound", error_code=None: recorded.append((sid, caller, call_status, direction)))
    app = FastAPI()
    app.include_router(twilio_api.router)
    app.dependency_overrides[verify_twilio_request] = lambda: None  # signatures are tested in test_security
    client = TestClient(app)
    post = lambda **form: client.post("/api/twilio/status", data={"CallSid": "CA9", "From": "+15550001111", "Direction": "inbound", **form})
    return post, recorded, updated


def test_busy_call_without_a_record_is_recorded(status):
    post, recorded, _ = status
    assert post(CallStatus="busy").status_code == 200
    assert recorded == [("CA9", "+15550001111", "busy", "inbound")]


def test_known_call_is_only_updated(status):
    post, recorded, updated = status
    updated["result"] = True
    post(CallStatus="busy")
    assert recorded == []


def test_failure_statuses_map_to_the_failed_folder():
    from routers.calls_api import FAILED_TWILIO_STATUSES
    from routers.inbox_api import FAILED_STATUSES

    assert set(FAILED_TWILIO_STATUSES.values()) <= FAILED_STATUSES
