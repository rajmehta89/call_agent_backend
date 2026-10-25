from routers import retell_api
from routers import custom_twilio_voice_api as custom


def test_sounds_unfinished():
    assert retell_api.sounds_unfinished("What are the popular")
    assert retell_api.sounds_unfinished("Can you tell me about the.")
    assert not retell_api.sounds_unfinished("What are the popular basins?")
    assert not retell_api.sounds_unfinished("Okay.")
    assert not retell_api.sounds_unfinished("")


def test_conversation_knowledge_uses_recent_turns(monkeypatch):
    queries = []

    def fake_retrieve(text, limit=4, embed_budget_ms=None):
        queries.append(text)
        if "basins" in text:
            return [{"text": "Basins: vessel and wall-hung."}, {"text": "Delivery across Australia."}]
        return [{"text": "Delivery across Australia."}]

    monkeypatch.setattr(retell_api.brain_service, "retrieve_knowledge", fake_retrieve)
    history = [{"role": "assistant", "content": "Hello"}, {"role": "user", "content": "What basins do you have?"}]
    facts = retell_api.conversation_knowledge("What about white ones?", history)
    assert sorted(queries) == ["What about white ones?", "What basins do you have? What about white ones?"]  # run side by side
    assert [item["text"] for item in facts] == ["Delivery across Australia.", "Basins: vessel and wall-hung."]


def test_conversation_knowledge_first_turn_searches_once(monkeypatch):
    queries = []
    monkeypatch.setattr(retell_api.brain_service, "retrieve_knowledge", lambda text, limit=4, embed_budget_ms=None: queries.append(text) or [])
    # Retell's transcript already contains the current utterance: it must not be counted as an earlier turn.
    retell_api.conversation_knowledge("Hi there.", [{"role": "user", "content": "Hi there."}])
    assert queries == ["Hi there."]


def test_deepgram_url_carries_keyterms(monkeypatch):
    monkeypatch.setattr(custom, "current_keyterms", lambda: ["Acme Co", "sink"])
    url = custom._deepgram_url()
    assert "&keyterm=Acme%20Co" in url and "&keyterm=sink" in url and url.count("keyterm=") == 2
    monkeypatch.setattr(custom, "current_keyterms", lambda: [])
    assert "keyterm" not in custom._deepgram_url()


def test_keyterms_cleaned_and_merged_with_extras(monkeypatch):
    import voice_keyterms
    monkeypatch.setenv("VOICE_KEYTERMS_EXTRA", "Acme Co")
    monkeypatch.delenv("CUSTOM_DEEPGRAM_KEYTERMS", raising=False)
    merged = voice_keyterms._merged(["acme co", "Flora", "the", "  Vivid series ", "https://x.com/a", "1300", "a very long phrase of words"])
    assert merged == ["Acme Co", "Flora", "Vivid series"]


def test_plain_fallback_finds_repeated_names():
    import voice_keyterms
    text = "Shop the Flora basin. The Flora range suits Regio vanities. Regio tapware is popular. Delivery is free."
    terms = voice_keyterms._plain_terms(text, 10)
    assert "Flora" in terms and "Regio" in terms and "Delivery" not in terms


def test_reminder_has_conversation_rules(monkeypatch):
    monkeypatch.setattr(retell_api.agent_config, "get_voice_prompt", lambda: "Prompt\n## 11. Final Response Rules\n- Be brief.")
    monkeypatch.setenv("VOICE_TURN_SAFETY", "false")
    monkeypatch.setenv("VOICE_TURN_CONVERSATION", "true")
    reminder = retell_api.voice_turn_reminder()
    assert reminder.startswith("## 11. Final Response Rules") and retell_api.VOICE_CONVERSATION_RULES in reminder
    monkeypatch.setenv("VOICE_TURN_CONVERSATION", "false")
    assert retell_api.VOICE_CONVERSATION_RULES not in retell_api.voice_turn_reminder()


def test_guard_strips_question_label_but_not_real_words():
    guard = retell_api.SpokenReplyGuard(60)
    assert (guard.feed("Question! We don't sell hot tubs. ") + guard.flush()).strip() == "We don't sell hot tubs."
    guard = retell_api.SpokenReplyGuard(60)
    assert (guard.feed("Question about installs is a good one. ") + guard.flush()).strip() == "Question about installs is a good one."


def test_thinking_switched_off_for_listed_models(monkeypatch):
    from ai_services import AIServices
    services = AIServices()
    monkeypatch.setattr(services, "provider", "openrouter")
    monkeypatch.setenv("VOICE_NO_THINKING_MODELS", "inception/mercury-3")
    assert services._openrouter_extra_body("inception/mercury-3")["reasoning"] == {"enabled": False}
    assert "reasoning" not in services._openrouter_extra_body("openai/gpt-4.1-mini")


def test_first_chunk_starts_at_a_comma_after_enough_words(monkeypatch):
    monkeypatch.setenv("CUSTOM_FIRST_CHUNK_WORDS", "7")
    text = "We sell bathroom and kitchen products online, with doorstep delivery across most of Australia. More"
    first = custom._next_chunk(text, first=True)
    assert text[:first.end()] == "We sell bathroom and kitchen products online,"
    later = custom._next_chunk(text, first=False)
    assert text[:later.end()].endswith("Australia.")
    short = "Yes, we do. We also stock vanities and more"
    assert short[:custom._next_chunk(short, first=True).end()] == "Yes, we do."   # too few words before the comma
    monkeypatch.setenv("CUSTOM_FIRST_CHUNK_WORDS", "0")
    assert text[:custom._next_chunk(text, first=True).end()].endswith("Australia.")


def test_a_question_mark_means_the_question_is_finished():
    # Ending on "for" / "by" would otherwise wait 0.7 s for more words that never come.
    assert not retell_api.sounds_unfinished("Yeah. Uh, so what is Home DFO is for?")
    assert not retell_api.sounds_unfinished("Who is it made by?")
    assert retell_api.sounds_unfinished("I was looking for")  # no punctuation: still waits


def test_no_store_lookup_for_clarify_yes_or_bye(monkeypatch):
    from turn_meaning import CALLER_TURN
    if CALLER_TURN.rank("hello") is None:
        import pytest
        pytest.skip("local meaning model not available")
    import voice_turns
    monkeypatch.setattr(voice_turns, "kind_of_turn_text", lambda t: t)
    assert retell_api._no_lookup_turn("Sorry?")
    assert retell_api._no_lookup_turn("Yes, please.")
    assert not retell_api._no_lookup_turn("How much is the black basin?")
