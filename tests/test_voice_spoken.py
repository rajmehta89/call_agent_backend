"""Urdu replies are written for the Urdu voice: it misread digits and said bracketed English twice (2026-10-31)."""

import pytest

from voice_spoken import for_voice, urdu_number


@pytest.mark.parametrize("value, words", [
    (0, "صفر"), (49, "انچاس"), (100, "ایک سو"), (149, "ایک سو انچاس"), (1200, "ایک ہزار دو سو"),
    (1249, "ایک ہزار دو سو انچاس"), (25000, "پچیس ہزار"), (150000, "ایک لاکھ پچاس ہزار"), (2500000, "پچیس لاکھ"),
])
def test_urdu_number_words(value, words):
    assert urdu_number(value) == words


def test_a_price_is_said_as_number_words():
    assert for_voice("قیمت 1,249 ڈالر ہے۔", "ur") == "قیمت ایک ہزار دو سو انچاس ڈالر ہے۔"


def test_a_phone_number_is_said_digit_by_digit_in_its_groups():
    assert for_voice("نمبر (04) 2221 3831 ہے۔", "ur") == "نمبر صفر چار، دو دو دو ایک، تین آٹھ تین ایک ہے۔"


def test_english_repeated_in_brackets_is_said_once():
    assert for_voice("میٹ بلیک (matte black) فنش", "ur") == "میٹ بلیک فنش"


def test_sizes_and_emails_are_spelled_for_the_voice():
    assert for_voice("سائز 600x1200 ہے", "ur") == "سائز چھ سو ضرب ایک ہزار دو سو ہے"
    assert for_voice("sales@homedfo.com.au پر", "ur") == "sales ایٹ homedfo ڈاٹ com ڈاٹ اے یو پر"


def test_other_languages_are_left_as_written():
    assert for_voice("The bath is $1,249. Call (04) 2221 3831.", "en") == "The bath is $1,249. Call (04) 2221 3831."
    assert for_voice("السعر 1249 دولار", "ar") == "السعر 1249 دولار"


def test_each_language_can_have_its_own_main_model(monkeypatch):
    from ai_services import language_voice_model
    monkeypatch.setenv("VOICE_MAIN_MODEL_UR", "google/gemini-3.1-flash-lite")
    monkeypatch.delenv("VOICE_MAIN_MODEL_EN", raising=False)
    assert language_voice_model("ur") == "google/gemini-3.1-flash-lite"
    assert language_voice_model("hi") == "google/gemini-3.1-flash-lite"  # Hindi callers are on the Urdu line
    assert language_voice_model("en") == ""


def test_a_pinned_model_tries_its_fast_provider_first_and_does_not_think(monkeypatch):
    from ai_services import AIServices
    monkeypatch.delenv("VOICE_MODEL_PROVIDERS", raising=False)
    services = AIServices.__new__(AIServices)
    services.provider = "openrouter"
    body = services._openrouter_extra_body("deepseek/deepseek-v4.1-flash")
    assert body["provider"] == {"allow_fallbacks": True, "order": ["together"]}
    assert body["reasoning"] == {"enabled": False}


def test_waiting_lines_can_use_a_quicker_model_than_simple_answers(monkeypatch):
    from voice_turns import bridge_line_model, fast_voice_model
    monkeypatch.setenv("VOICE_FAST_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.delenv("VOICE_BRIDGE_LINE_MODEL", raising=False)
    assert bridge_line_model() == "deepseek/deepseek-v4.1-flash"  # unset: the fast model, as before
    monkeypatch.setenv("VOICE_BRIDGE_LINE_MODEL", "meta-llama/llama-3.1-8b-instruct")
    assert bridge_line_model() == "meta-llama/llama-3.1-8b-instruct"
    assert fast_voice_model() == "deepseek/deepseek-v4.1-flash"
