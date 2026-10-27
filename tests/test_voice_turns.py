import voice_turns
from voice_turns import NumberSpeaker, caller_number_digits, choose_voice_model, needs_facts, say_digits, spoken_number


import pytest
from voice_turns import pause_request, search_text


def test_explicit_pause_is_not_a_new_product_request():
    for text in ["Sorry", "Sorry?", "Yeah, sorry, sorry.", "Wait, please.", "Hold on a second.", "Stop."]:
        assert pause_request(text)
    for text in ["Sorry, what sizes do you have?", "I want a stop valve.", "The delivery wait is too long."]:
        assert not pause_request(text)


def test_nova_turn_completion_only_fast_closes_confidently_complete_meaning(monkeypatch):
    from turn_meaning import CONTACT, TURN_COMPLETION

    monkeypatch.setattr(voice_turns, "pause_request", lambda _text: False)
    monkeypatch.setattr(CONTACT, "best", lambda text: "other")
    monkeypatch.setattr(TURN_COMPLETION, "rank", lambda text: [
        ("unfinished", 0.90), ("complete", 0.70)])
    assert voice_turns.complete_nova_turn("Yes. So what") is False

    monkeypatch.setattr(TURN_COMPLETION, "rank", lambda text: [
        ("unfinished", 0.90), ("complete", 0.88)])
    assert voice_turns.complete_nova_turn("Could you tell me") is None

    monkeypatch.setattr(TURN_COMPLETION, "rank", lambda text: [
        ("complete", 0.82), ("unfinished", 0.61)])
    assert voice_turns.complete_nova_turn("What do you sell?") is True

    monkeypatch.setattr(TURN_COMPLETION, "rank", lambda text: None)
    assert voice_turns.complete_nova_turn("A question") is None


def test_short_terminal_utterance_closes_when_completion_scores_are_ambiguous(monkeypatch):
    from turn_meaning import CONTACT, TURN_COMPLETION

    monkeypatch.setattr(voice_turns, "pause_request", lambda _text: False)
    monkeypatch.setattr(CONTACT, "best", lambda _text: "other")
    monkeypatch.setattr(TURN_COMPLETION, "rank", lambda _text: [
        ("unfinished", 0.714), ("complete", 0.688)])

    assert voice_turns.complete_nova_turn("Hello?") is True
    assert voice_turns.complete_nova_turn("Could you tell me") is None


def test_semantic_pause_closes_nova_turn_early(monkeypatch):
    monkeypatch.setattr(voice_turns, "pause_request", lambda _text: True)
    assert voice_turns.complete_nova_turn("a short interruption") is True
    assert voice_turns.pause_reply("Sorry?") == "Of course. Go ahead."


def test_would_love_to_preserves_the_product_context_for_an_accepted_offer(monkeypatch):
    monkeypatch.setattr(voice_turns, "detail_asked", lambda text: "")
    messages = [{"role": "user", "content": "A round matte black basin."},
                {"role": "assistant", "content": "Would you like advice on basin sizes or styles?"}]
    query = search_text("Yeah, I would love to.", messages)
    assert "matte black basin" in query and "sizes or styles" in query


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
    import store_intent
    def store_kinds(text, earlier=""):
        if ("basin" in text.lower() or "bathroom actually" in text.lower()
                or ("small" in text.lower() and "size is the room" in earlier.lower())):
            return ["product"]
        return []
    def store_rank(text, earlier=""):
        text = text.lower()
        if (any(term in text for term in ("basin", "bathroom actually"))
                or ("small" in text and "size is the room" in earlier.lower())):
            return [("product", 0.82), ("none", 0.62), ("order", 0.40), ("policy", 0.35)]
        if "can i do" in text or "drawing room" in text:
            return [("product", 0.666), ("none", 0.633), ("order", 0.611), ("policy", 0.35)]
        return [("none", 0.82), ("product", 0.55), ("order", 0.40), ("policy", 0.35)]
    monkeypatch.setattr(store_intent, "wanted", store_kinds)
    monkeypatch.setattr(store_intent, "rank", store_rank)
    monkeypatch.setenv("VOICE_FAST_MODEL", "")
    assert choose_voice_model("It's Raj.") is None
    fast = "meta-llama/llama-3.1-8b-instruct"
    monkeypatch.setenv("VOICE_FAST_MODEL", fast)
    assert choose_voice_model("It's Raj.", "May I have your name, please?") == fast        # the detail just asked for
    assert choose_voice_model("Okay. Cool. Bye.") == fast                                   # acknowledgement / goodbye
    assert choose_voice_model("No, that's all, thanks.") == fast
    assert choose_voice_model("What basins do you have?") is None
    assert choose_voice_model("What can I do?", earlier="I just bought a new house.") == fast
    assert choose_voice_model("Where should I start with the drawing room?") == fast
    # Product intent keeps the careful model; a business question can use the fast model with retrieved facts.
    assert choose_voice_model("Yeah, I'm doing up my bathroom actually.", "Are you after a particular type?") is None
    assert choose_voice_model("It's quite small.", "What size is the room?") is None
    from turn_meaning import CALLER_TURN
    monkeypatch.setattr(CALLER_TURN, "best", lambda text, earlier="": "question")
    assert choose_voice_model("What does Home DFO provide?") == fast


def test_product_follow_up_stays_on_main_model_using_prior_caller_context(monkeypatch):
    import store_intent
    seen = {}
    monkeypatch.setenv("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")
    def ranked(text, earlier=""):
        seen.update(text=text, earlier=earlier)
        return [("product", 0.82), ("none", 0.55)] if earlier else [("none", 0.8), ("product", 0.4)]
    monkeypatch.setattr(store_intent, "rank", ranked)
    assert choose_voice_model("And the price?", earlier="I'm looking for a matte black basin.") is None
    assert seen["earlier"] == "I'm looking for a matte black basin."


def test_semantic_prefetch_covers_the_contextual_store_probe(monkeypatch):
    import meaning
    seen = []
    monkeypatch.setattr(meaning, "vectors", lambda texts: seen.extend(texts))
    voice_turns.prepare_turn_meaning("And what is the price?", "Would you like dimensions?",
                                     "I'm interested in a round basin.")
    assert "I'm interested in a round basin. And what is the price?" in seen
    assert "I'm interested in a round basin. Would you like dimensions? And what is the price?" in seen


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


@pytest.mark.parametrize("said, email", [
    # From a real call: the start was spelled, then the whole address said; it was saved as rajrajm267747@gmail.com.
    ("Yeah. It's Raj Mehta. Yeah. So it is r a j, Rajm267747@Gmail.com.", "rajm267747@gmail.com"),
    ("j o h n, john at gmail dot com", "john@gmail.com"),
    ("r a j m two six seven seven four seven at gmail dot com", "rajm267747@gmail.com"),  # spelled only: kept
    ("it is j jones at outlook dot com", "jjones@outlook.com"),  # one letter may be a real initial
    ("s m smithers at gmail dot com", "smithers@gmail.com"),
    ("a b c xyz at gmail dot com", "abcxyz@gmail.com"),  # spelled letters that are not the next piece are kept
])
def test_spoken_email_spelled_then_said_whole(said, email):
    assert spoken_email(said) == email


def test_email_checks():
    note, problem = contact_check("my email is raj at gmail dot com")
    assert not problem and "r a j at gmail dot com" in note  # short name parts are spelled out so the caller can check them
    note, problem = contact_check("my email is raj at gmail")
    assert problem and "NOT a complete email" in note


def test_email_from_earlier_turn_is_retained_without_asking_again():
    from voice_turns import contact_email_followup_note, contact_email_from_messages
    messages = [
        {"role": "user", "content": "My email is raj at gmail dot com."},
        {"role": "assistant", "content": "Your email is raj at gmail dot com. Is that right?"},
        {"role": "user", "content": "Yes, that is right."},
    ]
    assert contact_email_from_messages(messages) == "raj@gmail.com"
    note = contact_email_followup_note(messages + [{"role": "user", "content": "Someone call me later."}], "Someone call me later.")
    assert "already provided" in note and "do not ask" in note


def test_failed_email_retry_is_not_repeated_on_later_callback_turn():
    from voice_turns import contact_email_followup_note
    messages = [
        {"role": "assistant", "content": "Could you please give me your email address?"},
        {"role": "user", "content": "My email is राज at gmail dot com."},
        {"role": "assistant", "content": "Please say your email address again slowly."},
        {"role": "user", "content": "Someone call me later."},
    ]
    note = contact_email_followup_note(messages, "Someone call me later.")
    assert "Do not ask for the email again" in note


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



def test_acknowledgements_fit_the_situation_or_stay_silent(monkeypatch):
    import voice_turns
    from voice_turns import CLARIFY_BRIDGES, DETAIL_BRIDGES, PROBLEM_BRIDGES, SEARCH_BRIDGES
    monkeypatch.setattr(voice_turns, "store_lookup_runs", lambda text, earlier="": False)
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


def test_a_question_the_live_store_answers_gets_a_short_let_me_check(monkeypatch):
    import voice_turns
    from voice_turns import LOOKUP_BRIDGES
    seen = []
    monkeypatch.setattr(voice_turns, "store_lookup_runs", lambda text, earlier="": seen.append(earlier) or True)
    # From a real call: ~2-3.6 s of silence before each of these answers.
    assert bridge_phrase("Yeah. So how much is the Lydia wall mixer with diverter?") in LOOKUP_BRIDGES
    assert bridge_phrase("And what is the warranty on it?", earlier="How much is the Lydia wall mixer?") in LOOKUP_BRIDGES
    assert seen[-1] == "How much is the Lydia wall mixer?"  # follow-ups are judged with what was said before
    monkeypatch.setattr(voice_turns, "store_lookup_runs", lambda text, earlier="": False)
    assert bridge_phrase("What finishes do your taps come in?") == ""  # answered from the knowledge: no lookup to cover


def test_acknowledgements_never_repeat_the_last_few_and_only_lookups_say_check():
    from voice_turns import ALL_BRIDGES, LOOKUP_BRIDGES
    recent = []
    for _ in range(4):
        phrase = bridge_phrase("I just bought a new home and I'm doing it up.", recent[-3:])
        assert phrase not in recent[-3:]
        if phrase:
            recent.append(phrase)
    # "Let me check" is only true when a store lookup runs, so only those phrases may say it.
    others = [phrase for phrase in ALL_BRIDGES if phrase not in LOOKUP_BRIDGES]
    assert not any("check" in phrase.lower() or "look" in phrase.lower() or "question" in phrase.lower() for phrase in others)


def test_product_shopping_statement_gets_lookup_bridge_even_when_classified_as_detail(monkeypatch):
    import voice_turns
    from turn_meaning import CALLER_TURN
    monkeypatch.setattr(CALLER_TURN, "best", lambda text: "detail")
    monkeypatch.setattr(voice_turns, "store_lookup_runs", lambda *args: True)
    assert voice_turns.bridge_phrase("I'm looking for a round basin.") in voice_turns.LOOKUP_BRIDGES


def test_delayed_question_bridge_still_excludes_short_replies_and_contact_details(monkeypatch):
    import voice_turns
    monkeypatch.setattr(voice_turns, "bridge_kind", lambda text, *args: "status" if text == "__status__" else "")
    assert voice_turns.wait_bridge_phrase("What is your Home DFO business for?") in voice_turns.STATUS_PHRASES
    for text in ("No. So so.", "Sorry. Sorry.", "Yes, please.", "my email is john at gmail dot com",
                 "Could you please tell me about"):
        assert voice_turns.wait_bridge_phrase(text) == ""


def test_one_word_what_does_not_trigger_a_wait_bridge(monkeypatch):
    import voice_turns
    monkeypatch.setattr(voice_turns, "store_lookup_runs", lambda *args: False)
    monkeypatch.setattr(voice_turns, "pause_request", lambda *args: False)
    monkeypatch.setattr(voice_turns, "is_acknowledgement", lambda *args: False)
    assert voice_turns.wait_bridge_phrase("What") == ""


def test_bridge_model_timeout_can_be_lowered_but_not_raised(monkeypatch):
    import voice_turns
    monkeypatch.setenv("VOICE_BRIDGE_MODEL_MS", "180")
    assert voice_turns.bridge_model_seconds() == 0.18
    monkeypatch.setenv("VOICE_BRIDGE_MODEL_MS", "1500")
    assert voice_turns.bridge_model_seconds() == 0.45


def test_completed_open_request_gets_a_bridge_when_answer_is_still_running(monkeypatch):
    import voice_turns
    from turn_meaning import CALLER_TURN, TURN_COMPLETION, WAIT_QUESTION
    monkeypatch.setattr(voice_turns, "bridge_kind", lambda text, *args: "status" if text == "__status__" else "")
    monkeypatch.setattr(voice_turns, "pause_request", lambda *args: False)
    monkeypatch.setattr(voice_turns, "is_acknowledgement", lambda *args: False)
    monkeypatch.setattr(CALLER_TURN, "best", lambda *args, **kwargs: "question")
    monkeypatch.setattr(TURN_COMPLETION, "best", lambda *args: "unfinished")
    probes = []
    def complete_from_context(text):
        probes.append(text)
        return "complete" if "new house" in text.lower() else "other"
    monkeypatch.setattr(WAIT_QUESTION, "best", complete_from_context)
    line = voice_turns.wait_bridge_phrase("What can I do?", earlier="I just bought a new house.")
    assert line in voice_turns.STATUS_PHRASES, (line, probes)
    assert "new house" in probes[0].lower() and "what can i do" in probes[0].lower()


def test_complete_semantic_question_can_bridge_without_a_template_match(monkeypatch):
    import voice_turns
    from turn_meaning import CALLER_TURN, TURN_COMPLETION, WAIT_QUESTION
    monkeypatch.setattr(voice_turns, "bridge_kind", lambda *args: "")
    monkeypatch.setattr(voice_turns, "pause_request", lambda *args: False)
    monkeypatch.setattr(voice_turns, "is_acknowledgement", lambda *args: False)
    monkeypatch.setattr(CALLER_TURN, "best", lambda *args, **kwargs: "question")
    monkeypatch.setattr(TURN_COMPLETION, "best", lambda *args: "complete")
    monkeypatch.setattr(WAIT_QUESTION, "best", lambda *args: "other")
    assert voice_turns.wait_bridge_phrase("Could you explain what I should do next?") in voice_turns.STATUS_PHRASES


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


def test_spoken_indian_number_is_saved_as_a_valid_contact():
    from voice_turns import provided_contact_phone

    text = "Yeah. So it is Indian number. So eight eight six six six three one four nine one."
    asked = "Could you please say your phone number slowly so I can get that right?"
    assert provided_contact_phone(text, asked) == "+918866631491"
    assert provided_contact_phone("Order 1234567890 arrived broken.", "How can I help?") == ""


def test_yes_or_no_to_the_agents_question_goes_to_the_main_model(monkeypatch):
    monkeypatch.setenv("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")
    assert choose_voice_model("Yeah.", "Hello, thanks for calling. How can I help you today?") is None
    assert choose_voice_model("No.", "Could you clarify what you mean?") is None
    assert choose_voice_model("Okay, thanks. Bye.", "Anything else I can help with?") is not None
    assert choose_voice_model("My name is Raj.", "May I have your name, please?") is not None


def test_close_semantic_scores_allow_fast_route_for_mixed_acknowledgement_and_question(monkeypatch):
    import store_intent
    from turn_meaning import AGENT_ASKED, CALLER_TURN

    fast = "meta-llama/llama-3.1-8b-instruct"
    monkeypatch.setenv("VOICE_FAST_MODEL", fast)
    monkeypatch.setattr(CALLER_TURN, "best", lambda *args: "acknowledge")
    monkeypatch.setattr(CALLER_TURN, "rank", lambda *args: [
        ("acknowledge", 0.659), ("clarify", 0.651), ("question", 0.641), ("search", 0.640),
    ])
    monkeypatch.setattr(AGENT_ASKED, "best", lambda *args: "other")
    monkeypatch.setattr(store_intent, "wanted", lambda text: [])
    monkeypatch.setattr(store_intent, "rank", lambda *args: [("none", 0.82), ("product", 0.45), ("order", 0.35)])

    greeting = "Hello, thanks for calling Home DFO. How can I help you today?"
    assert choose_voice_model("Yes sir so what is home dear for his", greeting) == fast


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


def test_the_small_models_lookup_line_is_said_only_when_it_states_nothing():
    from voice_turns import clean_lookup_bridge
    assert clean_lookup_bridge('"Let me check the Lydia mixer for you."') == "Let me check the Lydia mixer for you."
    assert clean_lookup_bridge("One sec, I'll look up the warranty on that") == "One sec, I'll look up the warranty on that."
    # A guess said before the real answer, an answer, a question or no "checking" at all: the fixed phrase instead.
    assert clean_lookup_bridge("Let me check, it's 140 dollars.") == ""
    assert clean_lookup_bridge("Yes, it comes in black.") == ""
    assert clean_lookup_bridge("Shall I look that up for you?") == ""
    assert clean_lookup_bridge("Great choice, the Lydia is lovely.") == ""


def test_after_both_goodbyes_the_call_ends_instead_of_another_goodbye():
    from voice_turns import call_is_over
    # From a real call: "Okay. / Thank you. / Bye. / Okay." got four goodbyes in a row.
    goodbye = "You're welcome, Raj. Have a great day, and we'll be in touch soon. Goodbye."
    assert call_is_over("Okay. Thank you.", goodbye)
    assert call_is_over("Okay. Thank you. Bye. Okay.", goodbye)
    # Not before we said goodbye, and never when the caller still wants something.
    assert not call_is_over("Okay. Thank you.", "Would you like details on sizes?")
    assert not call_is_over("Oh wait, how much is delivery?", goodbye)
    assert not call_is_over("Okay. Thank you.", "Is there anything else I can help with, or shall I say goodbye?")


def test_a_yes_to_our_offer_asks_for_what_was_offered_not_a_repeat():
    from voice_turns import yes_to_offer_note
    last = ("We have the freestanding oval bath in 1500 mm and 1700 mm sizes. "
            "Would you like details on colours or prices for those?")
    note = yes_to_offer_note("Yeah.", last)
    assert "colours or prices" in note and "Do not repeat" in note
    assert yes_to_offer_note("No.", last) == ""                       # a no is not a yes to the offer
    assert yes_to_offer_note("Yeah.", "We have it in two sizes.") == ""  # we offered nothing
    assert yes_to_offer_note("How much is it?", last) == ""            # a new question, answered as one


def test_the_question_we_asked_is_judged_not_the_facts_before_it():
    from voice_turns import agent_question
    reply = "You can pick up your order from our Campbellfield store within 14 days. Would you like details on collection?"
    assert agent_question(reply) == "Would you like details on collection?"
    assert agent_question("We have it in two sizes.") == "We have it in two sizes."


def test_a_place_the_knowledge_does_not_name_is_never_confirmed(monkeypatch):
    import voice_turns
    knowledge = "We deliver to most of Australia. Pick up from our Campbellfield store."
    monkeypatch.setattr(voice_turns, "detail_asked", lambda agent_text: "order")  # what the meaning model said in the call
    # From a real call: after an answer about picking up an order, "do deliver out to Geelong" got "including Geelong".
    assert "Geelong" in voice_turns.place_note("No. Like, do deliver out to Geelong.", knowledge, "Would you like details on collection?")
    monkeypatch.setattr(voice_turns, "detail_asked", lambda agent_text: "address")
    assert "Geelong" in voice_turns.place_note("Geelong.", knowledge, "Are you looking for delivery to a specific area?")
    assert voice_turns.place_note("Campbellfield.", knowledge, "Are you looking for delivery to a specific area?") == ""
    monkeypatch.setattr(voice_turns, "detail_asked", lambda agent_text: "phone")
    assert voice_turns.place_note("It's an Indian number, double eight six six.", knowledge, "What's the best number?") == ""


@pytest.mark.parametrize("said, email", [
    # From a real call: rejected three times before (the provider said as "g mail", "at the rate" heard as "at direct",
    # and the name part said without "my email is").
    ("Yeah. It is Raj m. Two six seven seven four seven at g mail dot com.", "rajm267747@gmail.com"),
    ("Yeah. So it is raj m two six seven seven four seven at direct g mail dot com.", "rajm267747@gmail.com"),
    ("it is john dot smith at outlook dot com dot au", "john.smith@outlook.com.au"),
    ("r a j at the rate gmail dot com", "raj@gmail.com"),
    ("yeah it is priya dash sharma at hot mail dot com", "priya-sharma@hotmail.com"),
    ("my email is j o h n at g mail dot com", "john@gmail.com"),
    ("sure, info at home dfo dot com dot au thanks", "info@homedfo.com.au"),
])
def test_an_email_said_in_pieces_is_rebuilt(said, email):
    from voice_turns import spoken_email
    assert spoken_email(said) == email


def test_a_complete_spoken_email_is_read_back_not_asked_again():
    from voice_turns import contact_check
    note, problem = contact_check("Yeah. It is Raj m. Two six seven seven four seven at g mail dot com.", "Could you please provide your email address?")
    assert not problem and "rajm267747@gmail.com" in note.replace(" ", "") or "r a j m" in note


@pytest.mark.parametrize("said", ["Sorry?", "Sorry? Like...", "Excuse me?", "Sorry, can you say that again?"])
def test_sorry_after_the_agent_finished_asks_to_repeat_not_to_pause(said):
    # From a real call: "Sorry?" to a finished question got "Of course. Go ahead." twice.
    from voice_turns import asks_to_repeat
    assert asks_to_repeat(said)
    assert not asks_to_repeat(said, spoke_over_agent=True)  # said over the agent: the caller wants it to stop


@pytest.mark.parametrize("said", ["Wait a second.", "Hold on a moment.", "Stop.", "Sorry. Sorry.", "Let me finish."])
def test_real_pause_requests_still_pause(said):
    from voice_turns import asks_to_repeat
    assert pause_request(said) and not asks_to_repeat(said)


@pytest.mark.parametrize("said", ["I couldn't hear you.", "You broke up there.", "Huh?", "Sorry, I missed that.",
                                  "I lost you for a second.", "It was not clear, can you tell me again?"])
def test_not_hearing_the_agent_is_a_repeat_request_by_meaning(said):
    from voice_turns import asks_to_repeat
    assert asks_to_repeat(said)


@pytest.mark.parametrize("said", ["What is your home beer voice for?", "Sorry, do you have black basins?",
                                  "I'm sorry to hear that.", "Sorry, my name is Raj."])
def test_a_real_ask_or_remark_with_sorry_is_not_a_repeat_request(said):
    # "What is your home beer voice for?" is a misheard business question: a close call is answered, not repeated.
    from voice_turns import asks_to_repeat
    assert not asks_to_repeat(said)


def test_a_product_question_that_starts_with_okay_goes_to_the_careful_model(monkeypatch):
    # Production call: "Okay. ... the handle and the basin ... So is it possible?" was judged by its last sentence,
    # answered on the fast model and got no products. Margins below are the measured store-intent scores.
    import store_intent
    measured = {
        "Okay. So I just bought a new house, and I was thinking about the handle and the basin. So is it possible?":
            [("product", 0.745), ("none", 0.661), ("order", 0.601)],
        "Do you have matte black basins?": [("product", 0.710), ("none", 0.658), ("order", 0.559)],
        "What can I do?": [("product", 0.782), ("none", 0.742), ("order", 0.688)],
    }
    monkeypatch.setattr(store_intent, "rank", lambda text, earlier="": measured.get(text, [("none", 0.8), ("product", 0.5)]))
    monkeypatch.setenv("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")
    from voice_turns import store_question, STORE_ROUTE_LEAD
    assert store_question(list(measured)[0], lead=STORE_ROUTE_LEAD)
    assert store_question("Do you have matte black basins?", lead=STORE_ROUTE_LEAD)
    assert not store_question("What can I do?", lead=STORE_ROUTE_LEAD)  # a broad question stays a general one
    assert choose_voice_model(list(measured)[0], "We sell bathroom products.") is None
