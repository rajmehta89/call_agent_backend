"""Ordering, concurrency, rollback and event-loop regressions from the CRM review."""

import asyncio
import copy
from datetime import datetime
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

import auth_service
import brain_service
import call_status
from knowledge_store import replace_source
from routers import calls_api, retell_api, shopify_api, inbox_api


class Transaction:
    def __init__(self, database): self.database = database
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def with_transaction(self, callback, **kwargs):
        previous = copy.deepcopy(self.database.docs), copy.deepcopy(self.database.sources)
        try: callback(self)
        except Exception:
            self.database.docs, self.database.sources = previous
            raise


class KnowledgeDatabase:
    def __init__(self, fail=False):
        self.docs = [{"source_id": "old-pdf", "text": "working answer"}]
        self.sources = [{"source_id": "old-pdf", "status": "ready"}]
        self.fail = fail
        self.brain_documents = NS(delete_many=self.delete, insert_many=self.insert)
        self.brain_sources = NS(update_one=self.update, delete_many=self.delete_sources)
        self.client = NS(start_session=lambda: Transaction(self))
    def delete(self, query, session):
        if "$in" in query.get("source_id", {}):
            self.docs = [d for d in self.docs if d["source_id"] not in query["source_id"]["$in"]]
    def insert(self, documents, session):
        if self.fail: raise RuntimeError("simulated failed insert")
        self.docs.extend(copy.deepcopy(documents))
    def update(self, query, update, session, **kwargs):
        self.sources = [s for s in self.sources if s["source_id"] != query["source_id"]] + [copy.deepcopy(update["$set"])]
    def delete_sources(self, query, session):
        self.sources = [s for s in self.sources if s["source_id"] not in query["source_id"]["$in"]]


def test_failed_knowledge_replacement_keeps_the_working_pdf_and_metadata():
    database = KnowledgeDatabase(fail=True)
    with pytest.raises(RuntimeError):
        replace_source(database, "new-pdf", [{"source_id": "new-pdf", "text": "replacement"}], {"source_id": "new-pdf"}, ["old-pdf"])
    assert database.docs == [{"source_id": "old-pdf", "text": "working answer"}]
    assert database.sources == [{"source_id": "old-pdf", "status": "ready"}]


def test_successful_knowledge_replacement_commits_documents_and_metadata_together():
    database = KnowledgeDatabase()
    replace_source(database, "new-pdf", [{"source_id": "new-pdf", "text": "replacement"}], {"source_id": "new-pdf"}, ["old-pdf"])
    assert database.docs == [{"source_id": "new-pdf", "text": "replacement"}] and database.sources == [{"source_id": "new-pdf"}]


def test_embedding_failure_does_not_delete_existing_knowledge(monkeypatch):
    database = MagicMock()
    database.is_connected.return_value = True
    monkeypatch.setattr(brain_service, "mongo_client", database)
    monkeypatch.setattr(brain_service.brain_service, "_embeddings", MagicMock(side_effect=RuntimeError("model unavailable")))
    with pytest.raises(RuntimeError):
        brain_service.brain_service.ingest_text("text", "source", "replacement", source_id="working", chunks=["replacement"])
    database.brain_documents.delete_many.assert_not_called()
    database.client.start_session.assert_not_called()


class Calls:
    def __init__(self, race=False):
        self.doc = {"_id": ObjectId(), "call_session_id": "call-1", "call_date": datetime(2026, 10, 1)}
        self.race, self.writes = race, []
    def find_one(self, *args, **kwargs): return copy.deepcopy(self.doc)
    def update_one(self, query, update):
        if self.race:
            self.doc.update(analysis_status="complete", provider_event_rank=3, call_summary="Real provider summary")
            self.race = False
        if any(self.doc.get(key) != value for key, value in query.items()): return NS(matched_count=0)
        self.writes.append(copy.deepcopy(update["$set"]))
        self.doc.update(update["$set"])
        return NS(matched_count=1)


@pytest.fixture
def provider_storage(monkeypatch):
    calls = Calls()
    database = NS(is_connected=lambda: True, calls=calls, customers=MagicMock())
    monkeypatch.setattr(calls_api, "mongo_client", database)
    monkeypatch.setattr(calls_api, "_ensure_inbound_lead", lambda *a: (None, None))
    monkeypatch.setattr(calls_api, "_remember_caller_name", lambda *a: None)
    monkeypatch.setattr(calls_api, "schedule_auto_status", lambda *a: None)
    monkeypatch.setattr(brain_service, "invalidate_customer_context", lambda *a: None)
    monkeypatch.setattr(calls_api.realtime, "publish", lambda *a, **k: None)
    monkeypatch.setattr(retell_api, "log_call", calls_api.log_call)
    monkeypatch.setattr(retell_api.transfer_service, "get_call", lambda *a: None)
    return calls


def test_late_retell_end_and_transcript_events_preserve_complete_analysis(provider_storage):
    call = {"call_id": "call-1", "from_number": "+15550001111", "call_status": "ended", "call_analysis": {"call_summary": "Real provider summary"}}
    retell_api._upsert_retell_call(call, "call_analyzed")
    for event in ("call_ended", "transcript_updated", "call_ended"):
        retell_api._upsert_retell_call({**call, "call_analysis": {}}, event)
    assert provider_storage.doc["analysis_status"] == "complete"
    assert provider_storage.doc["call_summary"] == "Real provider summary"
    assert len(provider_storage.writes) == 1


def test_analysis_arriving_during_a_stale_write_is_preserved(provider_storage):
    provider_storage.race = True
    retell_api._upsert_retell_call({"call_id": "call-1", "from_number": "+15550001111", "call_status": "ended"}, "call_ended")
    assert provider_storage.doc["call_summary"] == "Real provider summary"
    assert provider_storage.doc["analysis_status"] == "complete" and not provider_storage.writes


def test_human_stage_change_during_classification_wins(monkeypatch):
    call = {"_id": ObjectId(), "phone_number": "+15550001111", "status": "completed", "call_summary": "Specific request",
            "messages": [{"role": "customer", "text": "I would like help with a bathroom renovation."}]}
    state = {"_id": call["phone_number"], "stage": "new_lead", "stage_revision": 1}
    published = []
    def classify(*args):
        state.update(stage="won", stage_revision=2, stage_by="human", stage_by_name="Human operator")
        return {"stage": "lost", "confidence": 1, "reason": "Stale AI decision"}
    def update(query, value, **kwargs):
        assert query["stage_revision"] == 1 and state["stage_revision"] == 2
        return NS(matched_count=0, modified_count=0, upserted_id=None)
    database = NS(is_connected=lambda: True, calls=NS(find_one=lambda *a: copy.deepcopy(call)),
                  inbox_threads=NS(find_one=lambda *a: copy.deepcopy(state), update_one=update))
    monkeypatch.setattr(call_status, "mongo_client", database)
    monkeypatch.setattr(call_status, "classify", classify)
    monkeypatch.setattr(call_status.realtime, "publish", lambda *a, **k: published.append(a))
    assert call_status.apply_auto_status(str(call["_id"])) is None
    assert state["stage"] == "won" and not published


@pytest.mark.parametrize("surface", ["shopify", "retell"])
def test_slow_integration_does_not_block_a_voice_heartbeat(monkeypatch, surface):
    started, heartbeat = Event(), Event()
    blocked = []
    def upstream(*args, **kwargs):
        started.set()
        blocked.append(not heartbeat.wait(1))
        return {} if surface == "shopify" else retell_api.Response(status_code=204)
    app = FastAPI()
    if surface == "shopify":
        app.include_router(shopify_api.router)
        monkeypatch.setattr(shopify_api, "_apply_saved_shopify_config", lambda: None)
        monkeypatch.setattr(shopify_api, "shopify_service", NS(configured=True, catalog=upstream))
        app.dependency_overrides[auth_service.require_user] = lambda: {"role": "admin"}
        monkeypatch.setattr(auth_service, "role_document", lambda name: {"permissions": ["*"]})
    else:
        app.include_router(retell_api.router)
        monkeypatch.setattr(retell_api, "_process_retell_webhook", upstream)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://audit") as client:
            request = asyncio.create_task(client.get("/api/platform/shopify/catalog") if surface == "shopify" else client.post("/api/retell/webhook", json={}))
            assert await asyncio.to_thread(started.wait, 2)
            heartbeat.set()
            assert (await request).status_code in {200, 204}
    asyncio.run(run())
    assert blocked == [False]
