"""Automatic conversation status: rules around the AI's decision (the model call itself is faked)."""
from types import SimpleNamespace

import call_status


class FakeCollection:
    def __init__(self, docs=None):
        self.docs, self.updates = docs or {}, []

    def find_one(self, query, *args, **kwargs):
        return self.docs.get(query.get("_id"))

    def update_one(self, query, update, upsert=False):
        self.updates.append(update)


def run(monkeypatch, thread_state, verdict):
    call = {"_id": "6a0000000000000000000001", "status": "completed", "phone_number": "+61400000000"}
    threads = FakeCollection({"+61400000000": thread_state} if thread_state is not None else {})
    fake = SimpleNamespace(is_connected=lambda: True, calls=SimpleNamespace(find_one=lambda q: call), inbox_threads=threads,
                           users=SimpleNamespace(find_one=lambda q: None))
    monkeypatch.setattr(call_status, "mongo_client", fake)
    monkeypatch.setattr(call_status, "classify", lambda c, current: verdict)
    monkeypatch.setattr(call_status.realtime, "publish", lambda *a, **k: None)
    from routers import inbox_api
    monkeypatch.setattr(inbox_api, "_thread_key", lambda c: c["phone_number"])
    return call_status.apply_auto_status(call["_id"]), threads.updates


def test_moves_conversation_with_reason(monkeypatch):
    stage, updates = run(monkeypatch, {}, {"stage": "scheduling", "reason": "wants a site visit", "confidence": 0.9})
    assert stage == "scheduling"
    assert updates[0]["$set"]["stage_by"] == "ai" and updates[0]["$push"]["stage_history"]["reason"] == "wants a site visit"


def test_never_overrides_a_person(monkeypatch):
    stage, updates = run(monkeypatch, {"stage": "won", "stage_by": "user123", "stage_by_name": "Raj"}, {"stage": "lost", "reason": "x", "confidence": 1})
    assert stage is None and not updates


def test_low_confidence_leaves_it(monkeypatch):
    stage, updates = run(monkeypatch, {}, {"stage": "won", "reason": "maybe", "confidence": 0.3})
    assert stage is None and not updates
