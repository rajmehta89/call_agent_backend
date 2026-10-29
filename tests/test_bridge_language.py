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


def test_transfer_and_callback_lines_are_said_in_the_callers_language():
    import call_forwarding as cf
    assert cf.caller_line(cf.CONNECTING_LINE, "ur") == "آپ کو ابھی ٹیم سے ملا رہے ہیں۔"
    assert cf.caller_line(cf.CONNECTING_LINE, "ar") == "أحوّلك إلى الفريق الآن."
    assert cf.caller_line(cf.CONNECTING_LINE, "en") == cf.CONNECTING_LINE
    assert cf.caller_line(cf.NAMED_REQUEST_LINE, "ur", destination="Sara").startswith("Sara کے لیے")
    assert cf.caller_line(cf.NAMED_REQUEST_LINE, "en", destination="Sara").startswith("I have noted your request for Sara.")
    lines = (cf.CONNECTING_LINE, cf.TRANSFER_FAILED_LINE, cf.CALLBACK_NOTED_LINE, cf.CALLBACK_NUMBER_LINE, cf.NO_LIVE_TRANSFER_PREFIX,
             cf.NOW_OR_LATER_LINE, cf.NAMED_REQUEST_LINE, cf.ANYTHING_ELSE_LINE, cf.WELCOME_LINE, cf.OF_COURSE_LINE)
    assert all(line in cf.CALLER_LINES[code] for code in ("ur", "ar") for line in lines)


def test_urdu_and_arabic_replies_are_spoken_sentence_by_sentence():
    # Without ۔ ؟ ، an Urdu reply went to the voice only when the model had written all of it (first audio 5-6 s).
    import routers.custom_twilio_voice_api as custom
    import routers.retell_api as retell
    reply = "ہم باتھ روم اور کچن کے پروڈکٹس بیچتے ہیں۔ آپ کو کیا چاہیے؟ "
    end = custom._next_chunk(reply, first=False)
    assert end and reply[:end.end()] == "ہم باتھ روم اور کچن کے پروڈکٹس بیچتے ہیں۔"
    assert custom._next_chunk("مرحبا، لدينا حنفيات المطبخ بأسعار مختلفة ", first=False) is None  # no sentence end yet
    assert retell._SENTENCE_END.search("آپ کو کیا چاہیے؟") and retell._SENTENCE_END.search("نعم ۔ ")


def test_silence_reminders_are_said_in_the_callers_language():
    import call_forwarding as cf
    assert cf.caller_line(cf.READY_WHEN_YOU_ARE_LINE, "ar") == "أنا هنا متى كنت مستعدًا. كيف يمكنني مساعدتك؟"
    assert cf.caller_line(cf.ANYTHING_ELSE_LINE, "ur") == "کیا کسی اور چیز میں مدد چاہیے؟"
