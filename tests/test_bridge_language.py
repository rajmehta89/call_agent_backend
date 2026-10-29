"""Bridges ("one moment", "let me check that") are said in the caller's language, on both pipelines (2026-10-29)."""

import voice_languages
import voice_turns


def test_neutral_bridges_are_translated_and_others_stay_english_only():
    assert voice_turns.spoken_bridge("Mm, one moment.", "ur") == "جی، ایک لمحہ۔"
    assert voice_turns.spoken_bridge("Let me check that for you.", "ar") == "دعني أتحقق من ذلك لك."
    assert voice_turns.spoken_bridge("Mm, one moment.", "en") == "Mm, one moment."
    # Which acknowledgement fits is decided by meaning in English: none is said in another language.
    assert voice_turns.spoken_bridge("I see what you mean. Let's work through that.", "ur") == ""
    assert all(voice_turns.spoken_bridge(line, code) for code in ("ur", "ar") for line in voice_turns.NEUTRAL_BRIDGES)


def test_a_said_bridge_is_recognised_in_any_language_so_it_is_not_repeated():
    assert voice_turns.bridge_said("جی، ایک لمحہ۔ ہیلو") == "Mm, one moment."
    assert voice_turns.bridge_said("Okay, just a moment. Sure.") == "Okay, just a moment."
    assert voice_turns.bridge_said("ہیلو، آپ کو کس بارے میں مدد چاہیے؟") == ""
    assert voice_turns.late_filler([voice_turns.bridge_said("جی، ایک لمحہ۔")]) == "Okay, just a moment."


def test_bridges_recorded_ahead_are_the_ones_the_call_can_say():
    assert set(voice_turns.bridge_lines("ar")) == set(voice_turns.BRIDGE_TRANSLATIONS["ar"].values())
    assert set(voice_turns.bridge_lines("en")) == set(voice_turns.ALL_BRIDGES)


def test_the_callers_language_is_read_from_their_alphabet(monkeypatch):
    monkeypatch.setattr(voice_languages, "enabled_language_codes", lambda: ["en", "ar", "ur"])
    assert voice_languages.text_language("پہلی بات میں کہہ دوں، ہیلو") == "ur"            # Retell's Urdu
    assert voice_languages.text_language("नमस्ते, मुझे नल की कीमत जाननी है") == "ur"        # Hindi script
    assert voice_languages.text_language("مرحبا، أريد أن أسأل عن الأسعار") == "ar"
    assert voice_languages.text_language("Hi, how much are your taps?") == "en"
    assert voice_languages.text_language("...") == ""
    monkeypatch.setattr(voice_languages, "enabled_language_codes", lambda: ["en"])
    assert voice_languages.text_language("مرحبا، أريد أن أسأل") == "en"  # Arabic switched off
