"""Live call forwarding: settings checks, the Twilio instructions that ring the team, and what is recorded."""
from types import SimpleNamespace

import pytest

import call_forwarding


class FakeCollection:
    def __init__(self, docs=None):
        self.docs, self.updates = list(docs or []), []

    def find_one(self, query, *args, **kwargs):
        def matches(doc, q):
            if "$or" in q:
                return any(matches(doc, part) for part in q["$or"])
            return all(doc.get(k) == v for k, v in q.items())
        return next((doc for doc in self.docs if matches(doc, query)), None)

    def update_one(self, query, update, upsert=False):
        self.updates.append((query, update))
        doc = self.find_one(query)
        if doc is not None:
            doc.update(update.get("$set", {}))
        elif upsert:
            self.docs.append({**{k: v for k, v in query.items() if not k.startswith("$")}, **update.get("$set", {})})


@pytest.fixture
def db(monkeypatch):
    fake = SimpleNamespace(is_connected=lambda: True, platform_settings=FakeCollection(), calls=FakeCollection(),
                           inbox_threads=FakeCollection(), notifications=FakeCollection())
    monkeypatch.setattr(call_forwarding, "mongo_client", fake)
    monkeypatch.setenv("TWILIO_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+18178092336")
    monkeypatch.setenv("VOICE_DEFAULT_REGION", "AU")
    monkeypatch.setattr(call_forwarding, "_timeline", lambda *a, **k: None)
    call_forwarding._ON_CACHE["at"] = -1e9
    return fake


ADMIN = {"_id": "u1", "name": "Admin", "email": "admin@example.test"}


def test_numbers_are_saved_in_international_form(db):
    saved = call_forwarding.save_settings({"enabled": True, "agents": [{"name": "Sam", "phone": "0412 345 678"}], "ring_seconds": 20}, ADMIN)
    assert saved["agents"] == [{"name": "Sam", "phone": "+61412345678", "enabled": True}]
    assert saved["enabled"] is True


@pytest.mark.parametrize("payload, message", [
    ({"enabled": False, "agents": [{"name": "Sam", "phone": "12"}]}, "not a valid phone number"),
    ({"enabled": False, "agents": [{"name": "A", "phone": "0412345678"}, {"name": "B", "phone": "+61412345678"}]}, "listed twice"),
    ({"enabled": True, "agents": []}, "at least one active agent"),
    ({"enabled": True, "agents": [{"name": "Sam", "phone": "0412345678", "enabled": False}]}, "at least one active agent"),
    ({"enabled": False, "agents": [], "ring_seconds": 5}, "between"),
])
def test_bad_settings_are_refused_with_a_reason(db, payload, message):
    with pytest.raises(ValueError, match=message):
        call_forwarding.validate(payload)


def test_empty_form_rows_are_ignored(db):
    assert call_forwarding.validate({"enabled": False, "agents": [{"name": "", "phone": ""}]})["agents"] == []


def test_off_until_switched_on(db):
    assert call_forwarding.forwarding_on() is False
    call_forwarding.save_settings({"enabled": True, "agents": [{"name": "Sam", "phone": "0412345678"}]}, ADMIN)
    assert call_forwarding.forwarding_on() is True  # saving clears the cache: live calls see it on their next turn


def test_rings_every_active_agent_and_connects_on_answer(db):
    call_forwarding.save_settings({"enabled": True, "ring_seconds": 25, "agents": [
        {"name": "Sam", "phone": "0412345678"}, {"name": "Off", "phone": "0412000111", "enabled": False}, {"name": "Lee", "phone": "+61398765432"}]}, ADMIN)
    twiml = call_forwarding.transfer_twiml("+61400000000")
    assert twiml.count("<Number>") == 2 and "+61412345678" in twiml and "+61398765432" in twiml and "+61412000111" not in twiml
    assert "/screen" not in twiml and 'answerOnBridge="true"' in twiml
    assert 'record="record-from-answer-dual"' in twiml
    assert 'recordingStatusCallback="https://example.test/api/call-forwarding/recording-result"' in twiml
    assert 'timeout="25"' in twiml and 'action="https://example.test/api/call-forwarding/dial-result"' in twiml
    assert 'callerId="+18178092336"' in twiml
    assert 'callerId="+61400000000"' not in twiml


def test_language_match_rings_the_best_team_members(db):
    settings = call_forwarding.save_settings({"enabled": True, "agents": [
        {"name": "English desk", "phone": "0412345678", "preferred_languages": ["en"]},
        {"name": "Arabic desk", "phone": "+61398765432", "preferred_languages": ["ar"]},
        {"name": "No preference", "phone": "+61400001111"},
    ]}, ADMIN)
    twiml = call_forwarding.transfer_twiml("+61400000000", settings, preferred_language="ar")
    assert "+61398765432" in twiml and "+61412345678" not in twiml and "+61400001111" not in twiml


def test_named_agent_wins_over_language_match(db):
    settings = call_forwarding.save_settings({"enabled": True, "agents": [
        {"name": "English desk", "phone": "0412345678", "preferred_languages": ["en"]},
        {"name": "Arabic desk", "phone": "+61398765432", "preferred_languages": ["ar"]},
    ]}, ADMIN)
    twiml = call_forwarding.transfer_twiml("+61400000000", settings, "Please connect me to English desk", "ar")
    assert "+61412345678" in twiml and "+61398765432" not in twiml


def test_language_routing_falls_back_to_the_active_team(db):
    settings = call_forwarding.save_settings({"enabled": True, "agents": [
        {"name": "English desk", "phone": "0412345678", "preferred_languages": ["en"]},
        {"name": "Arabic desk", "phone": "+61398765432", "preferred_languages": ["ar"]},
    ]}, ADMIN)
    twiml = call_forwarding.transfer_twiml("+61400000000", settings, preferred_language="ur")
    assert "+61412345678" in twiml and "+61398765432" in twiml


def test_browser_test_call_shows_the_business_number(db, monkeypatch):
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+18178092336")
    call_forwarding.save_settings({"enabled": True, "agents": [{"name": "Sam", "phone": "0412345678"}]}, ADMIN)
    assert 'callerId="+18178092336"' in call_forwarding.transfer_twiml("client:browser_abc")


def test_render_transfer_callbacks_stay_on_production(db, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://production.onrender.com")
    call_forwarding.save_settings({"enabled": True, "agents": [{"name": "Sam", "phone": "0412345678"}]}, ADMIN)
    twiml = call_forwarding.transfer_twiml("+61400000000")
    assert 'action="https://production.onrender.com/api/call-forwarding/dial-result"' in twiml
    assert 'recordingStatusCallback="https://production.onrender.com/api/call-forwarding/recording-result"' in twiml


def test_agent_must_press_1():
    assert "<Gather" in call_forwarding.screen_twiml() and "Press 1" in call_forwarding.screen_twiml()
    assert 'timeout="15"' in call_forwarding.screen_twiml()
    assert call_forwarding.screen_twiml().count("Press 1") == 2
    assert call_forwarding.screen_result_twiml("1") == '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    assert "<Hangup/>" in call_forwarding.screen_result_twiml("") and "<Hangup/>" in call_forwarding.screen_result_twiml("2")


def test_nobody_accepted_tells_caller_and_requests_a_callback(db, monkeypatch):
    db.calls.docs.append({"call_session_id": "CA1", "phone_number": "+61400000000", "transfer_mode": "ring_all_answer", "transfer_status": "ringing"})
    notes = []
    import notification_service
    monkeypatch.setattr(notification_service, "notify_human_assignment", lambda **kw: notes.append(kw) or True)
    accepted = call_forwarding.dial_finished("CA1", "no-answer", "+61400000000")
    assert accepted is False
    call = db.calls.docs[0]
    assert call["human_callback_requested"] is True and call["status"] == "transfer_requested" and call["transfer_status"] == "no-answer"
    assert notes and "nobody was free" in notes[0]["message"]
    assert "call you back" in call_forwarding.dial_result_twiml(False) and "<Say" in call_forwarding.dial_result_twiml(False)


def test_each_team_phone_result_is_saved(db, monkeypatch):
    call_forwarding.save_settings({"enabled": True, "agents": [
        {"name": "Sam", "phone": "0412345678"}, {"name": "Lee", "phone": "+61398765432"}]}, ADMIN)
    db.calls.docs.append({"call_session_id": "CA1", "phone_number": "+61400000000", "transfer_mode": call_forwarding.MODE,
                          "transfer_status": "ringing", "transfer_targets": [
                              {"name": "Sam", "phone": "+61412345678", "status": "requested"},
                              {"name": "Lee", "phone": "+61398765432", "status": "requested"}]})
    from routers import twilio_api
    legs = [SimpleNamespace(to="+61412345678", status="no-answer", sid="CA_SAM"),
            SimpleNamespace(to="+61398765432", status="busy", sid="CA_LEE")]
    monkeypatch.setattr(twilio_api, "_get_twilio_client", lambda: SimpleNamespace(calls=SimpleNamespace(list=lambda **kw: legs)))
    assert call_forwarding.dial_finished("CA1", "no-answer", "+61400000000") is False
    targets = db.calls.docs[0]["transfer_targets"]
    assert [(target["name"], target["status"], target["call_sid"]) for target in targets] == [
        ("Sam", "no-answer", "CA_SAM"), ("Lee", "busy", "CA_LEE")]


def test_answered_call_is_recorded_as_transferred(db, monkeypatch):
    call_forwarding.save_settings({"enabled": True, "agents": [{"name": "Sam", "phone": "0412345678"}]}, ADMIN)
    db.calls.docs.append({"call_session_id": "CA3", "transfer_mode": call_forwarding.MODE, "transfer_status": "ringing"})
    from routers import twilio_api
    client = SimpleNamespace(calls=lambda sid: SimpleNamespace(fetch=lambda: SimpleNamespace(to="+61412345678")))
    monkeypatch.setattr(twilio_api, "_get_twilio_client", lambda: client)
    assert call_forwarding.dial_finished("CA3", "completed", "", "CA_CHILD", "40") is True
    call = db.calls.docs[0]
    assert call["transfer_status"] == "completed" and call["transfer_succeeded"] is True
    assert call["status"] == "transferred" and call["handled_by"] == "Sam"
    assert call["human_call_sid"] == "CA_CHILD" and call["human_call_duration"] == 40
    assert call["human_call_status"] == "completed"
    assert call["transfer_error"] is None and "human_callback_requested" not in call


def test_recording_completion_is_saved_on_transferred_call(db):
    db.calls.docs.append({"call_session_id": "CA4", "transfer_mode": call_forwarding.MODE})
    call_forwarding.recording_finished("CA4", "RE123", "https://api.twilio.test/recording", "completed")
    assert db.calls.docs[0]["recording_sid"] == "RE123"
    assert db.calls.docs[0]["recording_url"] == "https://api.twilio.test/recording"
    assert db.calls.docs[0]["human_recording_status"] == "completed"


def test_accepted_call_is_recorded_with_the_agent(db):
    call_forwarding.save_settings({"enabled": True, "agents": [{"name": "Sam", "phone": "0412345678"}]}, ADMIN)
    db.calls.docs.append({"call_session_id": "call_retell1", "twilio_call_sid": "CA2", "transfer_status": "ringing"})
    call_forwarding.agent_accepted("CA2", "+61412345678")  # Retell call found by its Twilio call id
    assert db.calls.docs[0]["transfer_status"] == "connected" and db.calls.docs[0]["accepted_by"] == "Sam"
    assert call_forwarding.dial_finished("CA2", "completed", "") is True
    assert call_forwarding.dial_result_twiml(True).endswith("<Hangup/></Response>")


def test_retell_calls_are_remembered_by_their_twilio_call():
    call_forwarding.remember_retell_call("call_x", "CA9", "+61400000000")
    assert call_forwarding.twilio_call_for_retell("call_x") == {"call_sid": "CA9", "caller": "+61400000000", "at": pytest.approx(call_forwarding._RETELL_CALLS["call_x"]["at"])}
    assert call_forwarding.twilio_call_for_retell("unknown") is None


def test_later_ai_save_keeps_the_forwarding_outcome(monkeypatch):
    """The AI's end-of-call save (custom stream closing, Retell's reports) must not reset a forwarded call."""
    from routers import calls_api
    assert "status" in calls_api.FORWARDING_FIELDS and calls_api.FORWARDING_MODE == call_forwarding.MODE
    assert "human_callback_requested" in calls_api.FORWARDING_FIELDS


def test_parent_status_keeps_unanswered_human_transfer_in_callback_queue(db, monkeypatch):
    from routers import calls_api
    call = {"_id": "call-1", "call_session_id": "CA1", "status": "transfer_requested",
            "transfer_mode": call_forwarding.MODE, "transfer_requested": True,
            "transfer_status": "no-answer", "human_callback_requested": True, "duration": 30}
    db.calls.docs.append(call)
    monkeypatch.setattr(calls_api, "mongo_client", db)
    monkeypatch.setattr(calls_api.realtime, "publish", lambda *args, **kwargs: None)
    assert calls_api.update_call_from_twilio("CA1", call_status="completed", call_duration="45")
    assert call["status"] == "transfer_requested"
    assert call["duration"] == 45


@pytest.mark.parametrize("text, kind", [
    ("Yeah, can I just talk to somebody real?", "person"),
    ("Could you connect me to one of your staff?", "person"),
    ("I don't want a bot, give me a person.", "person"),
    ("Can someone ring me back this afternoon?", "callback"),
    ("Are you an AI?", ""),
    ("Do you have someone who can come and measure?", ""),
    ("How much is the black one?", ""),
])
def test_asks_for_team_by_meaning(text, kind):
    from turn_meaning import HANDOFF_ACTION
    if HANDOFF_ACTION.rank("hello") is None:
        pytest.skip("local meaning model not available")
    assert call_forwarding.asks_for_team(text) == kind


@pytest.mark.parametrize("text, expected", [
    ("Could I be connected with whoever handles sales while we are talking?", "live"),
    ("Could someone from your office ring me when they are free?", "callback"),
    ("Can you put me in touch with someone?", "unclear"),
    ("Can I speak to someone about my missing order?", "unclear"),
    ("I paid for my order and it has not turned up", ""),
    ("What does your staff do?", ""),
])
def test_handoff_meaning_paraphrases_and_order_issue(text, expected):
    assert call_forwarding.handoff_intent(text) == expected


def test_semantic_classifier_uses_network_vectors_when_local_model_is_disabled(monkeypatch):
    import brain_service as brain_module
    import meaning

    monkeypatch.setattr(brain_module, "local_embeddings", lambda texts: [])
    vectors = {"connect me now": [3.0, 0.0], "call me later": [0.0, 4.0],
               "please connect me": [2.0, 0.0]}
    monkeypatch.setattr(brain_module.brain_service, "_embeddings",
                        lambda texts: ([vectors[text] for text in texts], "openrouter"))
    meaning._CACHE.clear()
    classifier = meaning.Classifier("handoff", {"live": ["connect me now"], "callback": ["call me later"]})
    assert classifier.best("please connect me") == "live"
    meaning._CACHE.clear()


def test_production_embedding_score_accepts_the_recorded_live_request(monkeypatch):
    from turn_meaning import HANDOFF_ACTION

    monkeypatch.setattr(HANDOFF_ACTION, "rank", lambda text, earlier="": [
        ("live", 0.572), ("unclear", 0.495), ("callback", 0.489), ("other", 0.469)])
    assert call_forwarding.handoff_intent("Is there anything possible, like, you can call now?") == "live"


def test_live_request_wins_a_close_callback_embedding_tie(monkeypatch):
    from turn_meaning import HANDOFF_ACTION

    monkeypatch.setattr(HANDOFF_ACTION, "rank", lambda text, earlier="": [
        ("callback", 0.597), ("live", 0.593), ("unclear", 0.554), ("other", 0.357)])
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda text, earlier="": "live")
    assert call_forwarding.handoff_intent("Can you arrange a call to the teammate now?") == "live"


def test_later_callback_keeps_its_intent_when_scores_are_distinct(monkeypatch):
    from turn_meaning import HANDOFF_ACTION

    monkeypatch.setattr(HANDOFF_ACTION, "rank", lambda text, earlier="": [
        ("callback", 0.743), ("live", 0.609), ("unclear", 0.527), ("other", 0.52)])
    assert call_forwarding.handoff_intent("Can someone call me back tomorrow?") == "callback"


@pytest.mark.parametrize("text", [
    "Someone call me later.",
    "Could you ring me tomorrow?",
    "Please have the team get back to us this afternoon.",
    "Can you call me back?",
])
def test_explicit_callback_request_is_detected_without_forwarding(text):
    assert call_forwarding.explicit_callback_request(text)


def test_close_tie_can_still_be_a_later_callback(monkeypatch):
    from turn_meaning import HANDOFF_ACTION

    monkeypatch.setattr(HANDOFF_ACTION, "rank", lambda text, earlier="": [
        ("live", 0.766), ("callback", 0.765), ("unclear", 0.74), ("other", 0.458)])
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda text, earlier="": "callback")
    assert call_forwarding.handoff_intent("Can you arrange a call tomorrow so I can talk with him?") == "callback"


def test_explicit_request_to_take_current_call_is_live_transfer():
    assert call_forwarding.wants_person("Okay. No. I want someone to pick my call now.")
    assert call_forwarding.wants_person("No. Not more. Could you arrange a call to your")
    assert call_forwarding.wants_person("Can we transfer our call?")
    assert call_forwarding.wants_person("Just transfer your call to your agent something so I can just talk with a human.")
    assert call_forwarding.wants_person("I need to speak with a human agent now.")
    assert call_forwarding.wants_person("Can you transfer me please?")
    assert call_forwarding.wants_person("Yeah. Yeah. Please. And if you could arrange a call, then it would be fine for me now.")
    assert call_forwarding.explicit_live_request("Could you please arrange a a call with your human so I can talk with him?")
    assert call_forwarding.explicit_live_request("Can you arrange a call so I can just talk with him?")
    assert call_forwarding.explicit_live_request("Could someone call me now?")
    assert not call_forwarding.explicit_live_request("Can you arrange a call tomorrow so I can talk with him?")
    assert not call_forwarding.explicit_live_request("Can someone call me back tomorrow?")
    assert not call_forwarding.explicit_live_request("Can you transfer my call tomorrow?")
    assert call_forwarding.wants_person("Yeah. Like... and I need to talk with your team if you can link them. It would be fine for me.")
    assert call_forwarding.followup_live_request("No. Can you just help me to call now?")
    assert call_forwarding.followup_live_request("Yeah. Like, just forward it.")
    assert call_forwarding.followup_live_request("Connect")
    assert not call_forwarding.followup_live_request("Can the team call me back tomorrow?")


def test_ambiguous_team_call_is_clarified_before_ringing():
    assert call_forwarding.ambiguous_team_call("Can you arrange a a call from your team?")
    assert not call_forwarding.ambiguous_team_call("Can you arrange a call from your team tomorrow?")
    assert not call_forwarding.ambiguous_team_call("Can you arrange a call from your team now?")
    assert call_forwarding.wants_person("Could you link me with your team?")
    assert not call_forwarding.team_handoff_request("Can your team call me back later?")
