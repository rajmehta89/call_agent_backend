"""Retell's call summary is stored even when its webhook never reaches us, and our placeholder never replaces it."""

import asyncio

import routers.calls_api as calls_api
import routers.retell_api as retell_api

ANALYZED = {"call_id": "call_1", "call_status": "ended", "call_analysis": {"call_summary": "Caller asked for the lowest-priced basin."}}


def stub(monkeypatch, record, stored=False):
    saved = []
    monkeypatch.setattr(retell_api, "_retell_analysis_stored", lambda call_id: stored)
    monkeypatch.setattr(retell_api, "_retell_call_record", lambda call_id: record)
    monkeypatch.setattr(retell_api, "_upsert_retell_call", lambda call, event: saved.append(event))
    return saved


def test_the_summary_is_fetched_from_retell(monkeypatch):
    saved = stub(monkeypatch, ANALYZED)
    assert retell_api._store_retell_outcome("call_1") is True and saved == ["call_analyzed"]


def test_nothing_is_fetched_when_the_webhook_already_stored_it(monkeypatch):
    saved = stub(monkeypatch, ANALYZED, stored=True)
    assert retell_api._store_retell_outcome("call_1") is True and saved == []


def test_it_waits_while_retell_is_still_writing_the_summary(monkeypatch):
    saved = stub(monkeypatch, {"call_id": "call_1", "call_status": "ended", "call_analysis": {}})
    assert retell_api._store_retell_outcome("call_1") is False and saved == []
    assert retell_api._store_retell_outcome("call_1", final=True) is False and saved == ["call_ended"]  # last try: keep what Retell has


def test_a_call_still_running_is_left_alone(monkeypatch):
    saved = stub(monkeypatch, {"call_id": "call_1", "call_status": "ongoing"})
    assert retell_api._store_retell_outcome("call_1", final=True) is False and saved == []


def test_the_fetch_stops_once_the_summary_is_stored(monkeypatch):
    attempts = []
    monkeypatch.setattr(retell_api, "RETELL_OUTCOME_WAITS", (0, 0, 0, 0))
    monkeypatch.setattr(retell_api, "_store_retell_outcome", lambda call_id, final=False: attempts.append(final) or len(attempts) == 2)
    asyncio.run(retell_api._fetch_retell_outcome("call_1"))
    assert attempts == [False, False]


class Calls:
    def __init__(self, existing):
        self.existing, self.updates = existing, []

    def find_one(self, query, projection=None):
        return dict(self.existing)

    def update_one(self, query, update):
        self.updates.append(update["$set"])


class Mongo:
    def __init__(self, calls):
        self.calls, self.customers = calls, type("C", (), {"update_one": lambda *a, **k: None})()

    def is_connected(self):
        return True


def test_our_placeholder_never_replaces_retells_summary(monkeypatch):
    calls = Calls({"_id": "x", "call_session_id": "call_1", "call_date": "then", "analysis_status": "complete",
                   "call_summary": "Caller asked for the lowest-priced basin.", "sentiment": "Neutral"})
    monkeypatch.setattr(calls_api, "mongo_client", Mongo(calls))
    monkeypatch.setattr(calls_api, "schedule_auto_status", lambda *a: None)
    monkeypatch.setattr(calls_api.realtime, "publish", lambda *a, **k: None)
    monkeypatch.setattr(calls_api, "invalidate_customer_context", lambda *a: None, raising=False)
    calls_api.log_call("+61400000000", call_data={"call_session_id": "call_1", "summary": "Retell inbound AI call completed.",
                                                  "sentiment": "neutral", "status": "completed", "provisional": True})
    assert calls.updates and "call_summary" not in calls.updates[0] and "sentiment" not in calls.updates[0]


def test_our_retell_stand_ins_get_the_ai_summary():
    from call_status import is_placeholder_summary
    assert is_placeholder_summary("Retell inbound AI call completed.") and is_placeholder_summary("Retell call completed.")
    assert is_placeholder_summary("Human callback requested from the Retell AI call.")
    assert is_placeholder_summary("Custom Twilio voice call with 3 caller turns and 4 AI responses.")
    assert not is_placeholder_summary("The caller asked for the lowest-priced basin.")
