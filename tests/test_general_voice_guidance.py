import pytest

import store_intent
from routers import retell_api


@pytest.mark.parametrize("question", [
    "Woah. Very nice. And I was just I was just bought a new house, and I was thinking, like, to make it good. So what I can do in that?",
    "I've just bought my first home and would like some general guidance. Where should I begin?",
    "We are planning to improve our home but haven't decided which room to start with. Can you help?",
])
def test_general_home_planning_does_not_trigger_a_live_store_read(monkeypatch, question):
    assert store_intent.wanted(question) == []
    monkeypatch.setattr(retell_api.brain_service, "_shopify_context",
                        lambda *args: pytest.fail("General planning must not wait on a live catalogue request"))
    assert retell_api._store_facts(question, None, []) == ""


@pytest.mark.parametrize("question", [
    "I'm furnishing a new house and need a tub.",
    "I just bought a house. Can you show me your round basins and their prices?",
    "Which of your shower sets are in stock?",
    "How much is the Lydia wall mixer?",
])
def test_specific_products_and_prices_still_require_store_data(question):
    assert "product" in store_intent.wanted(question)
    assert retell_api._no_lookup_turn(question) is False


def test_planning_followup_keeps_live_data_when_products_were_already_discussed(monkeypatch):
    earlier = "I need a new round basin and a shower set. Can you show me the range and prices?"
    question = "I want some guidance before deciding what to buy."
    looked_up = []
    monkeypatch.setattr(retell_api.brain_service, "_shopify_context",
                        lambda *args: looked_up.append(args) or ("Confirmed product facts", "products"))
    result = retell_api._store_facts(question, None, [{"role": "user", "content": earlier}])
    assert looked_up and "Confirmed product facts" in result


def test_general_guidance_retains_business_knowledge_in_the_model_prompt(monkeypatch):
    question = "I just moved in and want to make my place nicer. Where do I start?"
    monkeypatch.setattr(retell_api, "conversation_knowledge", lambda *args: [{"text": "Confirmed home renovation guides"}])
    monkeypatch.setattr(retell_api.brain_service, "_shopify_context",
                        lambda *args: pytest.fail("No live catalogue data was requested"))
    monkeypatch.setattr(retell_api.agent_config, "get_voice_prompt", lambda: "")
    monkeypatch.setattr(retell_api.agent_config, "get_system_prompt", lambda: "Business")
    monkeypatch.setattr(retell_api.brain_service, "channel_config", lambda *args: {})
    monkeypatch.setattr(retell_api, "_voice_prompt", lambda business, channel, caller, phone, facts, summary: facts[0]["text"])
    assert "Confirmed home renovation guides" in retell_api._system_prompt(question, "unknown", language="en")
