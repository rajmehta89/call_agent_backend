from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auth_service import hash_password
from routers import data_reset_api


class Collection:
    def __init__(self, count=2):
        self.count = count
        self.deleted_query = None
        self.inserted = []

    def find(self, query, projection):
        return [{"_id": "call1", "phone_number": "+15550001111", "call_session_id": "session1"}]

    def count_documents(self, query):
        return self.count

    def delete_many(self, query):
        self.deleted_query = query
        return SimpleNamespace(deleted_count=self.count)

    def insert_one(self, value):
        self.inserted.append(value)


@pytest.fixture
def reset_db(monkeypatch):
    names = (*data_reset_api.LABELS, "users", "audit_logs", "customers", "email_outbox", "whatsapp_messages", "campaign_prospects")
    collections = {name: Collection() for name in names}
    collections["users"].distinct = lambda field: ["rj"]
    fake = SimpleNamespace(is_connected=lambda: True, users=collections["users"], calls=collections["calls"], audit_logs=collections["audit_logs"], db=collections)
    monkeypatch.setattr(data_reset_api, "mongo_client", fake)
    return collections


USER = {"_id": "admin1", "role": "admin", "workspace_id": "rj", "password_hash": hash_password("testing-password")}


def test_preview_only_counts_call_related_records(reset_db):
    data = data_reset_api.preview_data_reset(USER)["data"]
    assert set(data["counts"]) == set(data_reset_api.LABELS)
    assert data["total"] == 12
    assert all(collection.deleted_query is None for collection in reset_db.values())


def test_wrong_confirmation_or_password_cannot_delete(reset_db):
    for phrase, password in [("wrong", "testing-password"), ("CLEAR CALL DATA", "wrong")]:
        with pytest.raises(HTTPException):
            data_reset_api.reset_data(data_reset_api.ResetRequest(confirmation=phrase, password=password), USER)
    assert all(collection.deleted_query is None for collection in reset_db.values())


def test_reset_filters_call_records_and_preserves_other_data(reset_db):
    result = data_reset_api.reset_data(data_reset_api.ResetRequest(confirmation="CLEAR CALL DATA", password="testing-password"), USER)
    assert result["success"] is True
    assert reset_db["calls"].deleted_query == {}
    assert reset_db["ai_activity"].deleted_query == {"channel": "voice"}
    assert reset_db["leads"].deleted_query == data_reset_api.AUTO_VOICE_LEADS
    assert reset_db["inbox_threads"].deleted_query == {"_id": {"$in": ["+15550001111"]}}
    assert reset_db["team_messages"].deleted_query == {"conversation_id": {"$in": ["+15550001111"]}}
    assert all(reset_db[name].deleted_query is None for name in ("users", "customers", "email_outbox", "whatsapp_messages", "campaign_prospects"))
    assert reset_db["audit_logs"].inserted


def test_multiple_workspaces_block_reset(reset_db):
    reset_db["users"].distinct = lambda field: ["rj", "another"]
    with pytest.raises(HTTPException) as error:
        data_reset_api.reset_data(data_reset_api.ResetRequest(confirmation="CLEAR CALL DATA", password="testing-password"), USER)
    assert error.value.status_code == 409
    assert all(collection.deleted_query is None for collection in reset_db.values())
