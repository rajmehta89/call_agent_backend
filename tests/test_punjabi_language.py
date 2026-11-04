"""Punjabi (pa-IN, Gurmukhi): speech recognition, voice and system lines on the custom stack and ConversationRelay."""

import voice_languages
import voice_localize
from routers import custom_twilio_voice_api as custom
from routers import twilio_api


def _enable(monkeypatch, codes=("en", "ur", "pa")):
    monkeypatch.setattr(voice_languages, "enabled_language_codes", lambda: list(codes))


def test_punjabi_names_and_script(monkeypatch):
    _enable(monkeypatch)
    assert voice_languages.normalize_language("Punjabi") == "pa"
    assert voice_languages.normalize_language("pa-IN") == "pa"
    assert voice_languages.text_language("ਕੀ ਤੁਹਾਡੇ ਕੋਲ ਬਾਥਰੂਮ ਦਾ ਸਮਾਨ ਹੈ") == "pa"
    assert voice_languages.text_language("क्या आपके पास बाथरूम का सामान है") == "ur"


def test_punjabi_is_off_until_switched_on():
    assert "pa" not in voice_languages.DEFAULT_ENABLED_LANGUAGES
    assert "pa" in voice_languages.SUPPORTED_LANGUAGES


def test_custom_stack_hears_punjabi_with_nova3_pa(monkeypatch):
    monkeypatch.setenv("CUSTOM_AUTO_LANGUAGE", "true")
    monkeypatch.setenv("CUSTOM_DEEPGRAM_MODEL_MULTI", "nova-3")
    monkeypatch.setattr(custom, "current_keyterms", lambda: [])
    url = custom._deepgram_url("pa")
    assert "model=nova-3" in url and "language=pa&" in url and "multi" not in url
    assert custom._use_flux_for_language("pa") is False


def test_custom_stack_speaks_punjabi_with_elevenlabs(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_MODEL_ID_PA", raising=False)
    monkeypatch.delenv("DEEPGRAM_TTS_VOICE_PA", raising=False)
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    assert custom.elevenlabs_model("pa") == "eleven_v4_turbo"
    assert custom.elevenlabs_model("en") == "eleven_flash_v2_5"
    assert custom._tts_engine_for_language("deepgram", "pa") == "elevenlabs"
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    assert custom.can_speak_language("pa") is False
    assert custom.can_speak_language("en") is True


def test_conversation_relay_uses_google_for_punjabi(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(twilio_api, "enabled_language_codes", lambda: ["en", "ur", "pa"])
    monkeypatch.setattr(twilio_api, "effective_language", lambda code: code)
    monkeypatch.setattr(twilio_api, "language_greeting", lambda code, english: english)
    english = twilio_api._build_conversation_twiml("https://example.test", "en")
    assert '<Language code="pa-IN" ttsProvider="Google"' in english and 'transcriptionLanguage="multi"' in english
    punjabi = twilio_api._build_conversation_twiml("https://example.test", "pa")
    assert 'language="pa-IN" ttsProvider="Google"' in punjabi
    assert twilio_api._relay_language_message("pa")["transcriptionLanguage"] == "pa-IN"
    assert twilio_api._relay_language_message("ur")["transcriptionLanguage"] == "multi"


def test_system_lines_are_written_by_the_model_not_hard_coded(monkeypatch):
    import call_forwarding
    calls = []

    def translate(line, code):
        calls.append((line, code))
        return f"[{code}] {line}"

    monkeypatch.setattr(voice_localize, "_translate", translate)
    voice_localize._CACHE.clear()
    line = call_forwarding.CONNECTING_LINE
    assert call_forwarding.caller_line(line, "pa") in {line, f"[pa] {line}"}  # never waits on the event loop
    assert voice_localize.localized(line, "pa", wait=2) == f"[pa] {line}"
    assert call_forwarding.caller_line(line, "pa") == f"[pa] {line}"
    # Urdu keeps its hand-written line; English is never translated.
    assert call_forwarding.caller_line(line, "ur") == call_forwarding.CALLER_LINES["ur"][line]
    assert voice_localize.localized(line, "en") == line
    assert all(code == "pa" for _, code in calls)


def test_translation_that_drops_a_placeholder_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(voice_localize, "_translate", voice_localize._translate)
    voice_localize._CACHE.clear()

    def bad(line, code):
        raise ValueError("translation lost the line's placeholders")

    monkeypatch.setattr(voice_localize, "_translate", bad)
    line = "I have noted your request for {destination}."
    assert voice_localize.localized(line, "pa", wait=2) == line


def test_punjabi_sentences_break_at_the_danda():
    from routers.retell_api import _SENTENCE_END
    reply = "ਹਾਂ, ਸਾਡੇ ਕੋਲ ਸਿੰਕ ਹਨ। ਤੁਸੀਂ ਕਿਹੜਾ ਚਾਹੁੰਦੇ ਹੋ?"
    assert custom._SENTENCE_BREAK.search(reply).group(0) == "।"
    assert _SENTENCE_END.search(reply).group(0) == "।"


def test_stray_arabic_letters_are_not_read_in_punjabi():
    from voice_spoken import for_voice
    assert for_voice("ਕਿਚਨ ਐਪਲਾਇੰਸز ਵੇਚਦੇ ਹਾਂ।", "pa") == "ਕਿਚਨ ਐਪਲਾਇੰਸ ਵੇਚਦੇ ਹਾਂ।"
    assert for_voice("Hello there.", "en") == "Hello there."


def test_saying_where_you_are_from_is_not_asking_for_punjabi(monkeypatch):
    _enable(monkeypatch, ("en", "ar", "ur", "pa"))
    for text in ("My name is Harpreet from Punjab.", "I'm Gurpreet, I come from Punjab originally.", "My parents are from Amritsar in Punjab."):
        assert voice_languages.requested_language(text, "en") == "", text
    for text in ("Can you speak Punjabi?", "Punjabi please.", "I want to talk in Punjabi."):
        assert voice_languages.requested_language(text, "en") == "pa", text


def test_latin_words_on_a_punjabi_call_stay_on_the_main_model(monkeypatch):
    # Real call: "Hi" and "Shawar" read as English by script went to the fast model, which wrote broken Punjabi or English.
    import asyncio
    from types import SimpleNamespace
    seen = {}

    def complete(messages, *args, **kwargs):
        seen["model"] = kwargs.get("model")
        yield "ਜੀ।"

    monkeypatch.setattr(custom, "choose_voice_model", lambda *args: "fast")
    monkeypatch.setattr(custom, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(custom, "place_note", lambda *args: "")
    monkeypatch.setattr(custom, "needs_facts", lambda text: False)
    monkeypatch.setattr(custom, "prepare_turn_meaning", lambda *args: None)
    monkeypatch.setattr(custom, "_system_prompt", lambda *args: "Business facts")
    monkeypatch.setattr(custom.brain_service, "knowledge_text", lambda: "")
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)
    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"), stream_chat_completion=complete)

    async def run(language):
        return [sentence async for sentence in custom._voice_sentences(services, "Hi", [], "unknown", "", language=language)]

    asyncio.run(run("pa"))
    assert seen["model"] != "fast"
    asyncio.run(run("en"))
    assert seen["model"] == "fast"


def _reply_on_punjabi_call(monkeypatch, written, translation="ਕੀ ਤੁਸੀਂ ਸ਼ਾਵਰ ਸਕਰੀਨ ਬਾਰੇ ਪੁੱਛ ਰਹੇ ਹੋ?"):
    import asyncio
    from types import SimpleNamespace
    import meaning
    seen = {}

    def complete(messages, *args, **kwargs):
        seen["messages"] = messages
        yield written

    monkeypatch.setattr(custom, "choose_voice_model", lambda *args: None)
    monkeypatch.setattr(custom, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(custom, "place_note", lambda *args: "")
    monkeypatch.setattr(custom, "needs_facts", lambda text: False)
    monkeypatch.setattr(custom, "prepare_turn_meaning", lambda *args: None)
    monkeypatch.setattr(custom, "_system_prompt", lambda *args: "Business facts")
    monkeypatch.setattr(custom.brain_service, "knowledge_text", lambda: "")
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)
    monkeypatch.setattr(meaning, "english_text", lambda text: "What is the price of a shower screen?")
    monkeypatch.setattr(voice_localize, "localized", lambda line, language, wait=0: translation)
    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"), stream_chat_completion=complete)

    async def run():
        return [s async for s in custom._voice_sentences(services, "ਸ਼ਰਬਤ ਸਕਰੀਨ ਦੀ ਕੀਮਤ ਕੀ ਹੈ", [], "unknown", "", language="pa")]

    return asyncio.run(run()), seen["messages"]


def test_the_reply_model_is_told_what_a_misheard_punjabi_turn_meant(monkeypatch):
    _, messages = _reply_on_punjabi_call(monkeypatch, "ਜੀ ਹਾਂ।")
    assert any("shower screen" in message["content"] and message["role"] == "system" for message in messages)


def test_an_english_reply_on_a_punjabi_call_is_put_into_punjabi(monkeypatch):
    said, _ = _reply_on_punjabi_call(monkeypatch, "Yes, we have bathroom vanities available.")
    assert said == ["ਕੀ ਤੁਸੀਂ ਸ਼ਾਵਰ ਸਕਰੀਨ ਬਾਰੇ ਪੁੱਛ ਰਹੇ ਹੋ?"]


def test_punjabi_replies_and_lone_addresses_are_left_alone(monkeypatch):
    import asyncio
    monkeypatch.setattr(voice_localize, "localized", lambda *a, **k: "translated")
    for sentence in ("ਸਾਡੇ ਕੋਲ vanity ਹੈ।", "sales@homedfo.com.au", "Home DFO."):
        assert asyncio.run(custom._in_call_language(sentence, "pa")) == sentence
    assert asyncio.run(custom._in_call_language("Yes we have sinks.", "en")) == "Yes we have sinks."


def test_a_failed_translation_is_not_retried_by_every_check(monkeypatch):
    import meaning
    monkeypatch.setenv("MEANING_TRANSLATION", "true")
    calls = []
    monkeypatch.setattr(meaning, "_translate", lambda text: calls.append(text) or "")
    text = "ਇਹ ਅਨੁਵਾਦ ਨਹੀਂ ਹੋਇਆ"
    meaning._UNTRANSLATED.pop(text, None)
    assert meaning.english_text(text) == text
    assert meaning.english_text(text) == text
    assert len(calls) == 1
