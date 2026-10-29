"""Urdu, Hindi and Arabic callers are understood: their words are read in English, one translation per new turn."""

import meaning


def stand_in(monkeypatch, translations):
    asked = []
    monkeypatch.setenv("MEANING_TRANSLATION", "true")
    monkeypatch.setattr(meaning, "_TRANSLATIONS", meaning.OrderedDict())
    monkeypatch.setattr(meaning, "_translate", lambda text: asked.append(text) or translations.get(text, ""))
    return asked


URDU = "مجھے کسی انسان سے بات کرنی ہے۔"
ARABIC = "ليس الآن، اتصلوا بي لاحقًا."


def test_english_is_read_as_said_without_a_translation(monkeypatch):
    asked = stand_in(monkeypatch, {})
    assert meaning.english_text("I want  to talk to a person.") == "I want to talk to a person."
    assert asked == []


def test_a_turn_is_translated_once_and_reused(monkeypatch):
    asked = stand_in(monkeypatch, {URDU: "I need to speak with a person."})
    assert meaning.english_text(URDU) == "I need to speak with a person."
    assert meaning.english_text(URDU) == "I need to speak with a person."
    assert asked == [URDU]


def test_a_context_of_earlier_turns_reuses_their_translations(monkeypatch):
    asked = stand_in(monkeypatch, {URDU: "I need to speak with a person.", ARABIC: "Not now, call me later."})
    meaning.english_text(URDU)
    meaning.english_text(ARABIC)
    assert meaning.english_text(f"{URDU} {ARABIC}") == "I need to speak with a person. Not now, call me later."
    # Cut to its last words, a context can start mid-way through an earlier turn: that piece is left out.
    assert meaning.english_text(f"{URDU.split()[-2]} {URDU.split()[-1]} {ARABIC}") == "Not now, call me later."
    assert asked == [URDU, ARABIC]


def test_without_a_translation_the_words_are_used_as_said(monkeypatch):
    stand_in(monkeypatch, {})
    assert meaning.english_text(URDU) == URDU
    assert meaning.is_foreign(URDU) and meaning.is_foreign("नमस्ते") and not meaning.is_foreign("Hello")


def test_switched_off_for_tests_and_when_asked(monkeypatch):
    asked = stand_in(monkeypatch, {URDU: "x"})
    monkeypatch.setenv("MEANING_TRANSLATION", "false")
    assert meaning.english_text(URDU) == URDU and asked == []
