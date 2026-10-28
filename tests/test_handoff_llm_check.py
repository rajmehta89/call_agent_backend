"""A possible transfer that is not clearly one is read by a language model before the team is rung."""

import call_forwarding
from turn_meaning import HANDOFF_ACTION


def scores(monkeypatch, ranked):
    monkeypatch.setenv("HANDOFF_LLM_CHECK", "true")
    monkeypatch.setattr(call_forwarding, "_HANDOFF_VERDICTS", call_forwarding.OrderedDict())
    monkeypatch.setattr(HANDOFF_ACTION, "rank", lambda text, earlier="": ranked)


def test_talking_to_the_assistant_never_rings_the_team(monkeypatch):
    # Production call 2026-10-28 09:24: "...I just talk with you." scored live 0.78, other 0.68.
    scores(monkeypatch, [("live", 0.78), ("unclear", 0.76), ("callback", 0.70), ("other", 0.68)])
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda text, earlier="", **kw: "other")
    assert call_forwarding.handoff_intent("So as I talk, like, I need two things, I just talk with you.") == ""


def test_an_indirect_request_is_still_understood(monkeypatch):
    scores(monkeypatch, [("live", 0.80), ("unclear", 0.78), ("callback", 0.70), ("other", 0.71)])
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda text, earlier="", **kw: "live")
    assert call_forwarding.handoff_intent("Put me through to sales please.") == "live"


def test_a_clear_request_goes_at_once_without_the_model(monkeypatch):
    scores(monkeypatch, [("live", 0.95), ("unclear", 0.86), ("callback", 0.80), ("other", 0.76)])
    def unexpected(*args):
        raise AssertionError("a clear request must not wait for the model")
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", unexpected)
    assert call_forwarding.handoff_intent("I want to speak to a person.") == "live"


def test_one_model_call_per_turn_and_failures_keep_the_scores(monkeypatch):
    scores(monkeypatch, [("live", 0.80), ("unclear", 0.78), ("callback", 0.70), ("other", 0.71)])
    calls = []
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda text, earlier="", **kw: calls.append(text) or "callback")
    for _ in range(3):  # wants a person? a callback? unclear? - the same turn
        assert call_forwarding.handoff_intent("Can someone ring me?") == "callback"
    assert len(calls) == 1
    scores(monkeypatch, [("live", 0.80), ("unclear", 0.78), ("callback", 0.70), ("other", 0.71)])
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda text, earlier="", **kw: "")  # model unavailable
    assert call_forwarding.handoff_intent("Can I talk to someone?") == "unclear"  # ask, never ring on a borderline score


def test_switching_the_check_off_uses_the_scores_alone(monkeypatch):
    scores(monkeypatch, [("live", 0.78), ("unclear", 0.76), ("callback", 0.70), ("other", 0.68)])
    monkeypatch.setenv("HANDOFF_LLM_CHECK", "false")
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda *a: (_ for _ in ()).throw(AssertionError("off")))
    assert call_forwarding.handoff_intent("I just talk with you.") == "live"


def test_the_check_reads_what_we_just_said(monkeypatch):
    scores(monkeypatch, [("live", 0.78), ("unclear", 0.76), ("callback", 0.70), ("other", 0.68)])
    seen = {}
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda text, earlier="", agent_text="": seen.update(agent=agent_text) or "other")
    assert call_forwarding.handoff_intent("I just talk with you.", "", "Would you like details on those handles?") == ""
    assert seen["agent"] == "Would you like details on those handles?"


def test_any_answer_to_our_team_offer_is_understood(monkeypatch):
    # "Later is fine." after our now-or-later offer matched no example and booked nothing.
    scores(monkeypatch, [("other", 0.80), ("callback", 0.62), ("live", 0.60), ("unclear", 0.58)])
    monkeypatch.setattr(call_forwarding, "_offered_team", lambda agent_text: True)
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda text, earlier="", agent_text="": "callback")
    assert call_forwarding.handoff_intent("Later is fine.", "", "Now, or should the team call you back?") == "callback"


def test_ordinary_turns_after_a_product_reply_never_wait_for_the_model(monkeypatch):
    scores(monkeypatch, [("other", 0.80), ("callback", 0.62), ("live", 0.60), ("unclear", 0.58)])
    monkeypatch.setattr(call_forwarding, "_offered_team", lambda agent_text: False)
    monkeypatch.setattr(call_forwarding, "_adjudicate_handoff", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call")))
    assert call_forwarding.handoff_intent("Yes, the black ones.", "", "Would you like the black or the chrome?") == ""
