"""Callers who speak another language: the reply follows them (Retell) or the call switches to it (custom stack)."""

import routers.retell_api as retell_api
import turn_meaning
import voice_languages


def enabled(monkeypatch, codes):
    monkeypatch.setattr(voice_languages, "enabled_language_codes", lambda: list(codes))


def test_retell_rule_follows_the_callers_language(monkeypatch):
    enabled(monkeypatch, ["en", "ar", "ur"])
    rule = voice_languages.language_rule()
    assert "English, Arabic or Urdu" in rule and "caller's latest turn" in rule
    assert "own script" in rule and "ask which language they prefer" in rule


def test_custom_rule_keeps_the_calls_language_and_offers_the_others(monkeypatch):
    enabled(monkeypatch, ["en", "ar", "ur"])
    rule = voice_languages.language_rule("ur", follow_caller=False)
    assert "this call is in Urdu" in rule and "Reply only in Urdu" in rule
    assert "Urdu, English or Arabic" in rule and "call system switches" in rule


def test_no_rule_with_one_language(monkeypatch):
    enabled(monkeypatch, ["en"])
    assert voice_languages.language_rule() == ""


def test_rule_comes_after_a_written_prompt_that_says_english(monkeypatch):
    # The business prompt said "Use natural Australian English": the language rule must be the last word.
    enabled(monkeypatch, ["en", "ur"])
    monkeypatch.setattr(retell_api.agent_config, "get_voice_prompt", lambda: "Use natural Australian English.\n## Final Rules\n- Be brief.")
    assert retell_api.voice_turn_reminder().endswith(voice_languages.language_rule())
    monkeypatch.setattr(retell_api.agent_config, "get_voice_prompt", lambda: "")
    reminder = retell_api.voice_turn_reminder("ur", follow_caller=False)
    assert reminder.startswith(retell_api.VOICE_TURN_CHECKLIST) and reminder.endswith(voice_languages.language_rule("ur", False))


def ranked(monkeypatch, scores):
    monkeypatch.setattr(turn_meaning.LANGUAGE_REQUEST, "rank", lambda text, earlier="": sorted(scores.items(), key=lambda item: -item[1]))


def test_a_language_request_switches_to_that_language(monkeypatch):
    enabled(monkeypatch, ["en", "ar", "ur"])
    ranked(monkeypatch, {"ur": 0.95, "ar": 0.8, "en": 0.7, "none": 0.6})
    assert voice_languages.requested_language("Can you speak Urdu?", "en") == "ur"
    assert voice_languages.requested_language("Can you speak Urdu?", "ur") == ""  # already in Urdu


def test_no_switch_when_the_meaning_is_unclear_or_the_language_is_off(monkeypatch):
    enabled(monkeypatch, ["en", "ar", "ur"])
    ranked(monkeypatch, {"ar": 0.76, "none": 0.74, "en": 0.6, "ur": 0.6})  # "Is the manual in Arabic?"-like: too close to none
    assert voice_languages.requested_language("Is the manual in Arabic?", "en") == ""
    ranked(monkeypatch, {"none": 0.9, "ur": 0.5, "ar": 0.5, "en": 0.5})
    assert voice_languages.requested_language("What's the price?", "en") == ""
    enabled(monkeypatch, ["en", "ar"])
    ranked(monkeypatch, {"ur": 0.95, "none": 0.6, "ar": 0.7, "en": 0.7})
    assert voice_languages.requested_language("Can you speak Urdu?", "en") == ""
    monkeypatch.setattr(turn_meaning.LANGUAGE_REQUEST, "rank", lambda text, earlier="": None)  # embeddings unavailable
    assert voice_languages.requested_language("Can you speak Urdu?", "en") == ""


def test_the_language_switches_are_read_once_and_cached(monkeypatch):
    reads = []
    monkeypatch.setattr(voice_languages, "_read_enabled_language_codes", lambda: reads.append(1) or ["en", "ur"])
    monkeypatch.setitem(voice_languages._enabled_cache, "codes", None)
    assert voice_languages.enabled_language_codes() == ["en", "ur"]
    assert voice_languages.enabled_language_codes() == ["en", "ur"]
    assert len(reads) == 1


def test_examples_cover_every_supported_language():
    assert set(turn_meaning.LANGUAGE_REQUEST.examples) == {*voice_languages.SUPPORTED_LANGUAGES, "none"}
    assert any("Hindi" in example for example in turn_meaning.LANGUAGE_REQUEST.examples["ur"])


def test_hindi_is_answered_in_urdu():
    # Spoken Hindi and Urdu are the same for callers; speech recognition reports Hindi as "hi".
    for value in ("hi", "hi-IN", "Hindi", "हिन्दी"):
        assert voice_languages.normalize_language(value) == "ur"
    assert voice_languages.normalize_language("hello") == "en"


def test_recognised_languages_map_to_the_ones_we_serve():
    assert [voice_languages.spoken_language(code) for code in ("en", "ar", "hi", "ur")] == ["en", "ar", "ur", "ur"]
    assert voice_languages.spoken_language("es") == ""  # Hinglish misread as Spanish: not a reason to switch


def test_auto_language_keeps_flux_for_english_and_urdu_and_nova_for_arabic(monkeypatch):
    # Flux Multilingual hears English and Hindi/Urdu (and decides turn ends from speech); it has no Arabic, so
    # Arabic calls, and the language watch beside Flux, use Nova-3 multi.
    import routers.custom_twilio_voice_api as voice
    monkeypatch.setenv("CUSTOM_AUTO_LANGUAGE", "true")
    monkeypatch.delenv("CUSTOM_FLUX_MODEL", raising=False)
    monkeypatch.setenv("CUSTOM_STT", "flux")
    monkeypatch.setattr(voice, "current_keyterms", lambda: [])
    assert [voice._use_flux_for_language(code) for code in ("en", "ur", "ar")] == [True, True, False]
    assert "model=flux-general-multi" in voice._flux_url("en")
    assert "language=multi" in voice._deepgram_url("ar")
    monkeypatch.setenv("CUSTOM_AUTO_LANGUAGE", "false")
    assert "model=flux-general-en" in voice._flux_url("en") and not voice._use_flux_for_language("ur")
