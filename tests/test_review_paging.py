"""Complete history, stable continuation and persisted thread-state regressions."""

import copy
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth_service
import thread_summaries
from list_paging import record_after, record_cursor
from routers import inbox_api
from timeutil import utc_now


def test_live_summary_uses_recent_activity_and_latest_lead_even_for_long_calls():
    now = utc_now().replace(tzinfo=timezone.utc)
    newer = {"_id": ObjectId(), "phone_number": "+15550000001", "call_date": now - timedelta(minutes=10),
             "updated_at": now, "status": inbox_api.live_transcript.LIVE_STATUS, "lead_id": "latest-lead"}
    older = {**newer, "_id": ObjectId(), "call_date": now - timedelta(days=1), "status": "completed", "lead_id": "old-lead"}
    result = thread_summaries._summarize([newer, older])[newer["phone_number"]]
    assert result["live_until"] == now.replace(tzinfo=None) + inbox_api.LIVE_WINDOW
    assert result["lead_id"] == "latest-lead"


def test_backfill_keeps_an_old_open_thread_after_3000_newer_calls(monkeypatch):
    old = {"_id": ObjectId(), "phone_number": "+15550000001", "call_date": datetime(2020, 1, 1), "status": "completed"}
    recent = [{"_id": ObjectId(), "phone_number": "+15550000002", "call_date": datetime(2026, 10, 29), "status": "completed"} for _ in range(3000)]
    states = {old["phone_number"]: {"_id": old["phone_number"], "stage": "follow_up", "assigned_to": "human-owner", "events": [{"text": "Keep this note"}]}}
    database = MagicMock()
    database.calls.find.return_value = iter(recent + [old])
    def write(operations, **kwargs):
        for operation in operations:
            key = operation._filter["_id"]
            states.setdefault(key, {"_id": key}).update(copy.deepcopy(operation._doc["$set"]))
    database.inbox_threads.bulk_write.side_effect = write
    database.inbox_threads.find.side_effect = lambda *a, **k: list(states.values())
    monkeypatch.setattr(thread_summaries, "mongo_client", database)
    monkeypatch.setattr(thread_summaries, "_ready", False)
    monkeypatch.setattr(inbox_api, "mongo_client", database)
    monkeypatch.setattr(inbox_api, "_browser_owner_names", lambda: {})
    rows = inbox_api._build_threads()
    found = next(row for row in rows if row["key"] == old["phone_number"])
    assert found["stage"] == "follow_up" and found["assigned_to"] == "human-owner" and not found["closed"]
    assert next(row for row in rows if row["key"] == "+15550000002")["call_count"] == 3000
    assert states[old["phone_number"]]["events"] == [{"text": "Keep this note"}]
    # Later list reads use the durable rows; they do not rescan the entire call collection.
    inbox_api._build_threads()
    assert database.calls.find.call_count == 1


def client_for_threads(monkeypatch, threads):
    monkeypatch.setattr(inbox_api, "_require_db", lambda: None)
    monkeypatch.setattr(inbox_api, "_threads", lambda: copy.deepcopy(threads))
    monkeypatch.setattr(auth_service, "role_document", lambda name: {"permissions": ["handle_conversations"]})
    app = FastAPI()
    app.include_router(inbox_api.router)
    app.dependency_overrides[auth_service.require_user] = lambda: {"_id": "agent", "role": "agent"}
    return TestClient(app)


def rows(count=250):
    return [{"key": f"caller-{i:04d}", "name": f"Caller {i}", "last_at": datetime(2026, 10, 29, tzinfo=timezone.utc),
             "stage": "follow_up", "closed": False, "failed": False, "assigned_to": "agent" if i % 2 else None} for i in range(count)]


@pytest.mark.parametrize("aware", [True, False])
def test_all_250_threads_are_reachable_and_cursor_survives_a_new_call(monkeypatch, aware):
    threads = rows()
    if not aware:
        for row in threads:
            row["last_at"] = row["last_at"].replace(tzinfo=None)
    client = client_for_threads(monkeypatch, threads)
    first = client.get("/api/inbox/conversations").json()
    assert len(first["data"]) == 200 and first["total"] == 250 and first["next_cursor"]
    threads.append({**threads[0], "key": "new-caller", "last_at": datetime(2026, 10, 30, tzinfo=timezone.utc)})
    second = client.get("/api/inbox/conversations", params={"after": first["next_cursor"]}).json()
    keys = [row["key"] for row in first["data"] + second["data"]]
    assert len(keys) == len(set(keys)) == 250 and second["next_cursor"] is None


def test_date_filter_accepts_mongo_and_provider_times(monkeypatch):
    threads = rows(2)
    threads[0]["last_at"] = threads[0]["last_at"].replace(tzinfo=None)
    client = client_for_threads(monkeypatch, threads)
    result = client.get("/api/inbox/conversations", params={"since": "2026-10-29T00:00:00Z", "until": "2026-10-30T00:00:00Z"}).json()
    assert result["total"] == 2
    assert all(row["last_at"] == "2026-10-29T00:00:00Z" for row in result["data"])


def test_quick_filters_and_counts_cover_all_pages(monkeypatch):
    client = client_for_threads(monkeypatch, rows(500))
    result = client.get("/api/inbox/conversations", params={"list_filter": "unassigned", "limit": 50}).json()
    assert result["total"] == 250 and result["filter_counts"]["all"] == 500
    assert result["filter_counts"]["assigned"] == 250 and len(result["data"]) == 50
    assert all(not row["assigned_to"] for row in result["data"])
    mine = client.get("/api/inbox/conversations", params={"folder": "mine", "stage": "follow_up"}).json()
    assert mine["total"] == 250 and all(row["assigned_to"] == "agent" for row in mine["data"])


def test_oldest_first_and_invalid_cursors(monkeypatch):
    client = client_for_threads(monkeypatch, rows())
    first = client.get("/api/inbox/conversations", params={"order": "asc", "limit": 1}).json()
    assert first["data"][0]["key"] == "caller-0000"
    second = client.get("/api/inbox/conversations", params={"order": "asc", "after": first["next_cursor"], "limit": 1}).json()
    assert second["data"][0]["key"] == "caller-0001"
    assert client.get("/api/inbox/conversations", params={"after": "broken"}).status_code == 400


def test_record_cursors_preserve_ties_and_include_legacy_missing_dates():
    identifier = ObjectId()
    at = datetime(2026, 10, 29)
    query = record_after(record_cursor({"_id": identifier, "call_date": at}, "call_date"), "call_date")
    assert query["$or"][1]["_id"] == {"$lt": identifier} and query["$or"][-1] == {"call_date": None}
    assert record_after(record_cursor({"_id": identifier}, "created_at"), "created_at") == {"created_at": None, "_id": {"$lt": identifier}}
