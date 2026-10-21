import voice_turns
from voice_turns import NumberSpeaker, caller_number_digits, choose_voice_model, needs_facts, say_digits, spoken_number


import pytest


@pytest.fixture(autouse=True)
def business_in_australia(monkeypatch):
    """These checks describe the test business (Home DFO, Australia); real runs read VOICE_DEFAULT_REGION from .env."""
    monkeypatch.setenv("VOICE_DEFAULT_REGION", "AU")


def test_questions_need_facts():
    for text in ["What basins do you have?", "And what about the sync", "How much is delivery.", "Okay. Do you sell vanities.",
                 "Can someone call me back about a vanity", "Sorry, can you say that again?", "Tell me about tapware."]:
        assert needs_facts(text), text
    for text in ["It's Raj.", "Zero four one two, three four five, six seven eight.", "Yes, that's right.", "Okay, thanks."]:
        assert not needs_facts(text), text


def test_choose_voice_model(monkeypatch):
    monkeypatch.setenv("VOICE_FAST_MODEL", "")
    assert choose_voice_model("It's Raj.") is None
    fast = "meta-llama/llama-3.1-8b-instruct"
    monkeypatch.setenv("VOICE_FAST_MODEL", fast)
    assert choose_voice_model("It's Raj.", "May I have your name, please?") == fast        # the detail just asked for
    assert choose_voice_model("Okay. Cool. Bye.") == fast                                   # acknowledgement / goodbye
    assert choose_voice_model("No, that's all, thanks.") == fast
    assert choose_voice_model("What basins do you have?") is None
    # From a real call: telling us about the project needs the knowledge, not the fast model.
    assert choose_voice_model("Yeah, I'm doing up my bathroom actually.", "Are you after a particular type?") is None
    assert choose_voice_model("It's quite small.", "What size is the room?") is None


def test_spoken_number():
    assert spoken_number("Zero four one two, three four five, six seven eight.") == "0412345678"
    assert spoken_number("it's 0412 345 678") == "0412345678"
    assert spoken_number("oh four double one two, three four five, six seven") == "0411234567"
    assert spoken_number("I have one question about two basins") == ""
    assert caller_number_digits([{"role": "user", "content": "0412 345 678"}, {"role": "assistant", "content": "Thanks"},
                                 {"role": "user", "content": "Yes"}]) == "0412345678"


def test_say_digits():
    assert say_digits("0412345678") == "zero four one two, three four five, six seven eight"


def _stream(speaker, chunks):
    return "".join(speaker.feed(chunk) for chunk in chunks) + speaker.flush()


def test_number_speaker_fixes_regrouped_and_dropped_digits():
    spoken = "zero four one two, three four five, six seven eight"
    assert _stream(NumberSpeaker("0412345678"), ["I've got your number as 041 ", "234 5678. Is that right?"]) == f"I've got your number as {spoken}. Is that right?"
    assert _stream(NumberSpeaker("0412345678"), ["Is it (0) (4) (1) (2), (3) (4) (5), (6) (7)", "?"]) == f"Is it {spoken}?"
    assert _stream(NumberSpeaker("0412345678"), ["That's zero four one two three four five six seven eight?"]) == f"That's {spoken}?"


def test_number_speaker_leaves_other_text_alone():
    text = "Delivery is $49.95 and our store line is 1300 555 999. Someone will call you."
    from voice_turns import known_numbers  # the business's own line is in the knowledge
    assert _stream(NumberSpeaker("0412345678", known_numbers("Store line: 1300 555 999")), [text[:20], text[20:41], text[41:]]) == text
    assert _stream(NumberSpeaker(""), ["Our number is 0412 345 678."]) == "Our number is 0412 345 678."


from voice_turns import contact_check, spoken_email, valid_phone


def test_phone_numbers_checked_for_the_callers_country():
    # Google's libphonenumber, against the caller's country (from caller ID), else the business's (VOICE_DEFAULT_REGION).
    for digits in ["0412345678", "0298765432", "61412345678", "918866631491"]:
        assert valid_phone(digits), digits
    for digits in ["88366631491", "041234567", "8866631491"]:
        assert not valid_phone(digits), digits
    assert valid_phone("8866631491", "IN")  # an Indian caller's local mobile number


def test_caller_country_from_caller_id():
    from voice_turns import caller_region, describe_caller_id
    assert caller_region("+918866631491") == "IN" and caller_region("+61412345678") == "AU"
    assert caller_region("client:browser_abc") == ""
    assert describe_caller_id("+61412345678") == "+61 412 345 678 (Australia)"


def test_wrong_number_from_real_call_is_flagged():
    # An Australian caller (caller ID) giving 11 digits: not valid there.
    note, problem = contact_check("name is Raj Mehta. And my phone number is eight eight three six six six three one four nine one.", region="AU")
    assert problem and "11 digits (too many digits)" in note and "NOT in the usual format" in note


def test_short_number_is_flagged_when_agent_asked_for_it():
    note, problem = contact_check("zero four one two, three four five", "What's the best phone number to reach you?")
    assert problem and "7 digits (too few digits)" in note


def test_good_number_is_confirmed_with_exact_digits():
    note, problem = contact_check("It's 0412 345 678.", "What's your phone number?")
    assert not problem and "zero four one two, three four five, six seven eight" in note


def test_numbers_outside_phone_talk_are_ignored():
    assert contact_check("Order 123456789 arrived broken.", "How can I help?") == ("", False)
    assert contact_check("I'll be at home tomorrow.", "") == ("", False)


def test_spoken_email():
    assert spoken_email("my email is raj at gmail dot com") == "raj@gmail.com"
    assert spoken_email("it's r a j underscore m at the rate gmail dot com") == "raj_m@gmail.com"
    assert spoken_email("raj.mehta@outlook.com.au") == "raj.mehta@outlook.com.au"
    assert spoken_email("I'm at the shop") == ""


def test_email_checks():
    note, problem = contact_check("my email is raj at gmail dot com")
    assert not problem and "raj at gmail dot com" in note
    note, problem = contact_check("my email is raj at gmail")
    assert problem and "NOT a complete email" in note


from voice_turns import place_note, unknown_places

KNOWLEDGE = "Store address: 1537 Sydney Road, Campbellfield, Vic 3061. Delivery to most of Australia. Installers nationwide."


def test_unknown_places_found_in_area_questions():
    assert unknown_places("Do you install in Brisbane?", KNOWLEDGE) == ["Brisbane"]
    assert unknown_places("Can you deliver to Darwin by Friday?", KNOWLEDGE) == ["Darwin"]
    assert unknown_places("Do you have a showroom in Sydney?", KNOWLEDGE) == ["Sydney"]  # only "Sydney Road" is known
    assert unknown_places("Can I pick up from Campbellfield?", KNOWLEDGE) == []          # the knowledge names it


def test_no_place_note_without_an_area_question():
    assert unknown_places("I live in Brisbane and like your taps.", KNOWLEDGE) == []
    assert place_note("What finishes do the taps come in?", KNOWLEDGE) == ""
    note = place_note("Do you install in Gold Coast?", KNOWLEDGE)
    assert "Gold Coast" in note and "do not say no" in note


from voice_turns import earlier_caller_texts, said_number


def test_number_broken_up_by_a_word_is_joined():
    # From a real call: "Right?" in the middle used to cut the number to its last 8 digits.
    assert said_number("So my first visit is eight eight Right? Six six six three one four nine one.") == "8866631491"
    assert said_number("I have one question about two basins") == ""


def test_overseas_number_first_time_asks_why_then_accepts_when_repeated():
    first = "phone number is eight eight six six six three one four nine one."
    note, problem = contact_check(first, "Can you please confirm your phone number?", region="AU")
    assert problem and "NOT in the usual format" in note and "overseas" in note
    again = "Yeah. So it is eight eight six six six three one four nine one."
    note, problem = contact_check(again, "That number doesn't sound right. Could you say it again?", [first], region="AU")
    assert not problem and "same number twice" in note and "Do not reject it again" in note


def test_earlier_caller_texts_skips_the_current_turn():
    convo = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    assert earlier_caller_texts(convo, "c") == ["a"]       # Retell: the transcript already ends with this turn
    assert earlier_caller_texts(convo[:2], "c") == ["a"]   # custom pipeline: history before this turn


from voice_turns import REQUEST_BRIDGES, STATEMENT_BRIDGES, bridge_phrase



def test_guard_drops_the_models_own_acknowledgement_after_a_bridge():
    from routers.retell_api import SpokenReplyGuard
    guard = SpokenReplyGuard(60, strip_acknowledgement=True)
    assert (guard.feed("Sure, we sell basins in a few styles. ") + guard.flush()).strip() == "We sell basins in a few styles."
    guard = SpokenReplyGuard(60)
    assert (guard.feed("Sure, we sell basins in a few styles. ") + guard.flush()).strip() == "Sure, we sell basins in a few styles."


def test_guard_drops_a_first_sentence_that_only_repeats_the_bridge():
    from routers.retell_api import SpokenReplyGuard
    def run(bridge, text):
        guard = SpokenReplyGuard(60, strip_acknowledgement=True, echo_of=bridge)
        out = "".join(guard.feed(text[i:i + 5]) for i in range(0, len(text), 5))
        return (out + guard.flush()).strip()
    # From a replayed call: the model echoed the bridge.
    assert run("Right, okay.", "Okay. We have various basins in a few styles. ") == "We have various basins in a few styles."
    assert run("Yeah, I can sort that out.", "I can sort that out. Can I get your name? ") == "Can I get your name?"
    # A real first sentence is kept.
    assert run("Sure, let me check.", "We do have matte black basins. ") == "We do have matte black basins."


def test_connected_check_trusts_a_recent_ping(monkeypatch):
    import time
    from mongo_client import mongo_client
    if mongo_client.client is None:
        return
    pings = []
    monkeypatch.setattr(mongo_client.client.admin, "command", lambda *a, **k: pings.append(1))
    mongo_client._last_ok = 0.0
    assert mongo_client.is_connected() and mongo_client.is_connected() and mongo_client.is_connected()
    assert len(pings) == 1
    mongo_client._last_ok = time.monotonic() - 60
    mongo_client.is_connected()
    assert len(pings) == 2



def test_email_from_a_real_call_with_number_words_and_at_the_rate():
    # Flux heard "at the rate" as "at direct"; the digits came as words.
    heard = "Yeah. So my email is rad m two six seven seven four seven at direct Gmail dot com."
    assert spoken_email(heard) == "radm267747@gmail.com"
    assert spoken_email("my email is raj m 267747 at the rate gmail dot com") == "rajm267747@gmail.com"


def test_no_place_check_while_the_caller_gives_their_number():
    # From a real call: "it's for India ... nine one" was about the phone number, not a delivery area.
    knowledge = "Delivery to most of Australia. Installers nationwide."
    assert place_note("No. So it is for India. So that's why it covers with nine plus nine one.", knowledge,
                      "Could you please say it again or confirm if it's an overseas number?") == ""
    assert place_note("Do you deliver to Darwin?", knowledge, "How can I help?") != ""




def test_provider_choice_is_kept_in_memory(monkeypatch):
    import voice_provider_settings as settings
    loads = []
    monkeypatch.setattr(settings, "_load_controls", lambda: loads.append(1) or {"custom": True})
    settings._CACHE.update(value=None, at=0.0, refreshing=False)
    assert settings.provider_enabled("custom") and settings.custom_stack_enabled() and settings.custom_voice_engine() == "elevenlabs"
    assert len(loads) == 1  # one database read for all of these, not one each
    settings._CACHE.update(value=None, at=0.0, refreshing=False)



def test_acknowledgements_fit_the_situation_or_stay_silent():
    from voice_turns import CLARIFY_BRIDGES, DETAIL_BRIDGES, PROBLEM_BRIDGES, SEARCH_BRIDGES
    # Simple questions, answers to the agent and short replies: no filler (a random one sounded out of place).
    assert bridge_phrase("Yeah. So what is Home DFO is for?") == ""
    assert bridge_phrase("What finishes do your taps come in?") == ""
    assert bridge_phrase("Yes. Yes, please. Go ahead.") == ""
    assert bridge_phrase("Yeah. So what?") == ""
    # Upset or urgent: no casual filler either.
    assert bridge_phrase("My basin arrived broken and I want a refund.") == ""
    # Fitting acknowledgements otherwise.
    assert bridge_phrase("I'm looking for a basin that would suit a small bathroom.") in SEARCH_BRIDGES
    assert bridge_phrase("I have a problem, the tap I bought is not working properly.") in PROBLEM_BRIDGES
    assert bridge_phrase("I need a vanity and a basin and also some tiles for the floor and the walls.") in DETAIL_BRIDGES
    assert bridge_phrase("I just bought a new home and I'm doing it up.") in STATEMENT_BRIDGES
    assert bridge_phrase("Can someone call me back about a vanity?") in REQUEST_BRIDGES
    assert bridge_phrase("What do you mean by what room?") in CLARIFY_BRIDGES


def test_acknowledgements_never_repeat_the_last_few_and_never_claim_to_check():
    from voice_turns import ALL_BRIDGES
    recent = []
    for _ in range(4):
        phrase = bridge_phrase("I just bought a new home and I'm doing it up.", recent[-3:])
        assert phrase not in recent[-3:]
        if phrase:
            recent.append(phrase)
    assert not any("check" in phrase.lower() or "question" in phrase.lower() for phrase in ALL_BRIDGES)


def test_a_scrambled_readback_of_the_callers_number_is_replaced_by_their_exact_digits():
    # From a real call: the caller said 8866631491 and the model read back "+91 8866663149".
    spoken = say_digits("8866631491")
    out = _stream(NumberSpeaker("8866631491"), ["Just to confirm, you'd like us to call you on +91 8866663149", ", correct?"])
    assert out == f"Just to confirm, you'd like us to call you on {spoken}, correct?"
    # The business's own number (in the knowledge) is read out as written.
    from voice_turns import known_numbers
    known = known_numbers("Phone: (04) 2221 3831")
    assert _stream(NumberSpeaker("8866631491", known), ["Call us on (04) 2221 3831 any time."]) == "Call us on (04) 2221 3831 any time."


def test_an_international_number_with_country_code_is_accepted():
    note, problem = contact_check("It's an Indian number, nine one eight eight six six six three one four nine one.",
                                  "Could you confirm if it's an overseas number?")
    assert not problem and "valid India number" in note and "+91 88666 31491" in note  # they said it's Indian
    # An Indian caller (caller ID +91...) saying their local number: valid, no country code needed.
    note, problem = contact_check("It's eight eight six six six three one four nine one.", "What's your phone number?", region="IN")
    assert not problem and "valid India number" in note


def test_yes_or_no_to_the_agents_question_goes_to_the_main_model(monkeypatch):
    monkeypatch.setenv("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")
    assert choose_voice_model("Yeah.", "Hello, thanks for calling. How can I help you today?") is None
    assert choose_voice_model("No.", "Could you clarify what you mean?") is None
    assert choose_voice_model("Okay, thanks. Bye.", "Anything else I can help with?") is not None
    assert choose_voice_model("My name is Raj.", "May I have your name, please?") is not None


def test_business_name_mishearings_get_a_note():
    from voice_turns import name_note
    assert "Home DFO" in name_note("So what is home data price for?", "Home DFO")
    assert name_note("What is Home DFO for?", "Home DFO") == ""
    assert name_note("Do you do home delivery?", "Home DFO") == ""


def test_the_agent_knows_the_callers_country_and_never_needs_a_country_code(monkeypatch):
    from voice_turns import caller_country_line
    line = caller_country_line("+918866631491")
    assert "India" in line and "never ask for their phone number or a country code" in line
    monkeypatch.setenv("VOICE_DEFAULT_REGION", "AU")
    assert "Australia" in caller_country_line("client:browser_abc")  # browser test call: no caller ID
    # An Indian caller saying their local number: the team gets it with the country code, nobody asks for it.
    note, problem = contact_check("It's eight eight six six six three one four nine one.", "What's your number?", region="IN")
    assert not problem and "+91 88666 31491" in note and "never ask" in note


def test_calls_from_any_country_without_caller_id(monkeypatch):
    from voice_turns import caller_country_line, mentioned_region
    monkeypatch.setenv("VOICE_DEFAULT_REGION", "AU")
    # No caller ID and no country named: a number valid elsewhere is not called wrong; the agent asks which country.
    note, problem = contact_check("It is eight eight six six six three one four nine one.", "What is your phone number?")
    assert "India" in note and "valid phone number in" in note and "country code" in note
    # The caller named the country earlier in the call: no question needed.
    note, problem = contact_check("It is eight eight six six six three one four nine one.", "What is your phone number?",
                                  ["It's an overseas number, an Indian number."])
    assert not problem and "valid India number" in note
    assert mentioned_region(["please call us back"]) == ""  # "us" is not the USA
    assert "do not assume" in caller_country_line("client:browser_abc")



def test_with_caller_id_the_agent_never_asks_for_the_phone_number():
    from voice_turns import caller_country_line, FAST_TURN_NOTE
    line = caller_country_line("+61412345678")
    assert "never ask for their phone number" in line and "ask only for their name" in line
    assert "ONLY if there is no caller ID" in FAST_TURN_NOTE
