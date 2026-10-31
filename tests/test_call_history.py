"""Returning caller context, evidence-based stages and useful handoffs, with no live providers."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from bson import ObjectId

import brain_service as brain
import call_forwarding
import call_history
import call_status
import notification_service
from routers import retell_api
from timeutil import utc_now


class Cursor(list):
    def sort(self, key, order):
        return Cursor(sorted(self, key=lambda row: row.get(key, utc_now()), reverse=order < 0))

    def limit(self, count):
        return Cursor(self[:count])


def conversation(index, words, **extra):
    return {"call_date": utc_now() - timedelta(minutes=index), "status": "completed",
            "messages": [{"role": "customer", "text": text} for text in words], **extra}


def test_only_customer_speech_counts_as_a_conversation():
    assert call_history.caller_turns({"messages": [{"role": "agent", "text": "How can I help?"}]}) == []
    assert call_history.caller_turns({"messages": [{"role": "customer", "text": "  "}]}) == []
    assert call_history.caller_turns({"transcription": [{"type": "user", "content": "I need a basin."}]}) == ["I need a basin."]
    assert call_history.caller_turns({"human_transcript": [{"role": "customer", "text": "Can we arrange delivery?"}]}) == ["Can we arrange delivery?"]


def test_history_is_scoped_recent_bounded_and_uses_recorded_transfer_results():
    reads = []
    rows = [conversation(1, []), conversation(2, ["I need a 1500 mm freestanding bath."], transfer_status="no-answer",
                          call_summary="Agent said they would connect me."),
            conversation(3, ["What about delivery?"]), conversation(4, ["Can you help with my bathroom?"]),
            conversation(5, ["An older question."])]

    def find(query, projection):
        reads.append((query, projection))
        return Cursor(rows)

    note = call_history.caller_history_context("+61400000000", NS(calls=NS(find=find)))
    data = json.loads(note.split("\n", 1)[1])
    assert len(data) == 3
    assert data[-1]["caller_said"] == ["I need a 1500 mm freestanding bath."]
    assert data[-1]["transfer_status"] == "no-answer" and data[-1]["transfer_connected"] is False
    assert "An older question." not in note
    query, projection = reads[0]
    assert query["phone_number"] == "+61400000000" and query["direction"] == {"$ne": "outbound"}
    assert query["call_date"]["$gte"] < utc_now() - timedelta(days=29)
    assert "in_progress" not in query["status"]["$in"]
    assert projection["messages"] == 1 and "contact_email" not in projection


@pytest.mark.parametrize("identity", ["unknown", "client:browser", "", "+bad", "+1", "other-caller"])
def test_anonymous_or_invalid_identity_cannot_retrieve_someone_elses_history(identity):
    def find(*args):
        raise AssertionError("No history lookup for this identity")
    assert call_history.caller_history_context(identity, NS(calls=NS(find=find))) == ""


def test_long_history_keeps_valid_json_and_a_fixed_prompt_budget():
    rows = [conversation(index, ["A product question with enough detail. " * 40] * 3,
                         call_summary="An older summary. " * 70) for index in range(6)]
    note = call_history.caller_history_context("+61400000000", NS(calls=NS(find=lambda *a: Cursor(rows))))
    encoded = note.split("\n", 1)[1]
    assert len(encoded) <= 3000 and 1 <= len(json.loads(encoded)) <= 3


def test_history_error_preserves_the_existing_crm_context(monkeypatch):
    def fail(*args):
        raise TimeoutError("history unavailable")
    database = NS(customers=NS(find_one=lambda q: None),
                  leads=NS(find_one=lambda q: {"name": "Example Customer", "requirement": "A freestanding bath"}),
                  calls=NS(find=fail))
    monkeypatch.setattr(brain, "mongo_client", database)
    context = brain.brain_service._customer_context_uncached("+61400000000")
    assert "Example Customer" in context and "A freestanding bath" in context


@pytest.mark.parametrize("status,provider", [("missed", "twilio"), ("failed", "retell"), ("completed", "custom_twilio")])
def test_a_nonconversational_call_cannot_disqualify_a_customer(monkeypatch, status, provider):
    writes = []
    call = {"_id": ObjectId(), "phone_number": "+61400000000", "status": status, "provider": provider,
            "call_summary": "The call ended without a request.", "summary_by": "provider",
            "messages": [{"role": "agent", "text": "How can I help?"}]}
    database = NS(is_connected=lambda: True, calls=NS(find_one=lambda q: call),
                  inbox_threads=NS(find_one=lambda q: {"stage": "outreach"}, update_one=lambda *a, **k: writes.append(a)))
    monkeypatch.setattr(call_status, "mongo_client", database)
    monkeypatch.setattr(call_status, "classify", lambda *a: {"stage": "unqualified", "confidence": 1, "reason": "No answer"})
    assert call_status.apply_auto_status(str(call["_id"])) is None and writes == []


def test_a_real_enquiry_can_reopen_an_unqualified_thread():
    assert call_status.may_move("unqualified", "new_lead")
    assert not call_status.may_move("outreach", "new_lead")
    assert not call_status.may_move("won", "new_lead")


def test_handoff_carries_product_requirements_without_inventing_a_summary(monkeypatch):
    notifications = []
    monkeypatch.setattr(call_forwarding, "get_settings", lambda: {})
    monkeypatch.setattr(call_forwarding, "requested_agent", lambda *args: None)
    monkeypatch.setattr(notification_service, "notify_human_assignment", lambda **kwargs: notifications.append(kwargs))
    history = [{"role": "user", "content": "I need a 1500 mm freestanding bath."},
               {"role": "assistant", "content": "An unverified price or promise."},
               {"role": "user", "content": "And a matching shower in black."}]
    call_forwarding.create_callback_outreach("+61400000000", "Can someone call me tomorrow?", "CA_TEST",
                                             conversation_history=history)
    message = notifications[0]["message"]
    assert "1500 mm freestanding bath" in message and "matching shower in black" in message
    assert "Can someone call me tomorrow?" in message and "unverified price" not in message


def test_failed_handoff_retains_the_saved_brief_for_the_callback_team(monkeypatch):
    notifications = []
    monkeypatch.setattr(call_forwarding, "_find_call", lambda sid: {"transfer_status": "ringing",
                         "handoff_context": "Caller needs a 1500 mm bath.", "phone_number": "+61400000000"})
    monkeypatch.setattr(call_forwarding, "_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(call_forwarding, "_timeline", lambda *args: None)
    monkeypatch.setattr(notification_service, "notify_human_assignment", lambda **kwargs: notifications.append(kwargs))
    assert call_forwarding.dial_finished("CA_TEST", "no-answer", "+61400000000") is False
    assert "1500 mm bath" in notifications[0]["message"]


def test_retell_passes_the_current_conversation_to_the_handoff(monkeypatch):
    passed = []
    monkeypatch.setattr(retell_api, "FORWARD_AFTER_SECONDS", 0)
    monkeypatch.setattr(call_forwarding, "transfer", lambda *a, **k: passed.append(k) or True)
    history = [{"role": "customer", "text": "I need a basin."}]
    assert asyncio.run(retell_api._hand_to_team("call_TEST", "CA_TEST", "+61400000000", "Connect me now", "en", history))
    assert passed[0]["conversation_history"] == history


@pytest.mark.parametrize("problem", [TimeoutError, ConnectionError])
def test_caller_history_failure_keeps_the_answerable_business_facts(monkeypatch, problem):
    class Result:
        def __init__(self, function):
            self.function = function

        def result(self, timeout):
            try:
                return self.function()
            except problem:
                assert timeout <= 0.5
                raise

        def cancel(self):
            return True

    def unavailable(*args):
        raise problem("simulated caller history failure")

    monkeypatch.setattr(retell_api, "_PROMPT_POOL", NS(submit=lambda function, *a: Result(function)))
    monkeypatch.setattr(retell_api.brain_service, "customer_context", unavailable)
    monkeypatch.setattr(retell_api, "_store_facts", lambda *args: "")
    monkeypatch.setattr(retell_api, "conversation_knowledge", lambda *a: [{"text": "Pickup is available in Campbellfield."}])
    monkeypatch.setattr(retell_api.agent_config, "get_system_prompt", lambda: "You answer for the store.")
    monkeypatch.setattr(retell_api.agent_config, "get_voice_prompt", lambda: "Use supplied knowledge: {{business_knowledge}}")
    monkeypatch.setattr(retell_api.brain_service, "channel_config", lambda *a: {})
    timings = {}
    prompt = retell_api._system_prompt("Where do I pick up?", "+61400000000", language="en", timings=timings)
    assert "Pickup is available in Campbellfield." in prompt
    assert timings["prompt_caller_failed"] is True
    assert timings["prompt_caller_timed_out"] is (problem is TimeoutError)
