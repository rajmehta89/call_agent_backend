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


def test_later_general_question_does_not_send_it_back_to_new_lead(monkeypatch):
    # A callback is still pending (Outreach); the next call only asked what the business sells.
    stage, updates = run(monkeypatch, {"stage": "outreach", "stage_by": "ai"}, {"stage": "new_lead", "reason": "asked what the business sells", "confidence": 0.9})
    assert stage is None and not updates


def test_comes_back_to_book_a_time_moves_on(monkeypatch):
    # Follow-up -> Scheduling is earlier in the list but a real step: the caller is ready to book.
    stage, updates = run(monkeypatch, {"stage": "follow_up", "stage_by": "ai"}, {"stage": "scheduling", "reason": "wants to book a measure", "confidence": 0.9})
    assert stage == "scheduling" and updates


def test_not_interested_still_moves_to_lost(monkeypatch):
    stage, updates = run(monkeypatch, {"stage": "outreach", "stage_by": "ai"}, {"stage": "lost", "reason": "chose another supplier", "confidence": 0.9})
    assert stage == "lost" and updates


def test_may_move_rules():
    assert call_status.may_move("new_lead", "outreach")
    assert not call_status.may_move("outreach", "new_lead")
    assert not call_status.may_move("won", "new_lead")
    assert call_status.may_move("follow_up", "scheduling")


def test_ai_summary_only_fills_a_placeholder_still_in_place(monkeypatch):
    # Retell's summary can be stored while the AI is still classifying: the AI's write must not replace it.
    call = {"_id": "6a0000000000000000000002", "status": "completed", "phone_number": "+61400000000", "provider": "retell",
            "call_summary": "Retell inbound AI call completed."}
    writes, published = [], []

    def update_one(query, update):
        writes.append(query)
        return SimpleNamespace(modified_count=0)  # the summary changed meanwhile: nothing matched

    fake = SimpleNamespace(is_connected=lambda: True, calls=SimpleNamespace(find_one=lambda q: call, update_one=update_one),
                           inbox_threads=FakeCollection({}), users=SimpleNamespace(find_one=lambda q: None))
    monkeypatch.setattr(call_status, "mongo_client", fake)
    monkeypatch.setattr(call_status, "classify", lambda c, current: {"stage": "new_lead", "reason": "x", "confidence": 0.1, "summary": "AI summary"})
    monkeypatch.setattr(call_status.realtime, "publish", lambda *a, **k: published.append(a))
    from routers import inbox_api
    monkeypatch.setattr(inbox_api, "_thread_key", lambda c: c["phone_number"])
    call_status.apply_auto_status(call["_id"])
    assert writes == [{"_id": call["_id"], "call_summary": "Retell inbound AI call completed."}] and not published
