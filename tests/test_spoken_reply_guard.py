"""The voice reply guard: no filler openers, stop at the first question, no rambling."""
from routers.retell_api import SpokenReplyGuard


def stream(text, size=4, max_words=45):
    guard = SpokenReplyGuard(max_words)
    out = ""
    for start in range(0, len(text), size):
        out += guard.feed(text[start:start + size])
        if guard.done:
            break
    return out + guard.flush()


def test_drops_hollow_opener_and_capitalises():
    assert stream("Absolutely! Raj builds voice AI agents.") == "Raj builds voice AI agents."
    assert stream("Great question, we stock basins.") == "We stock basins."


def test_keeps_natural_acknowledgements():
    assert stream("Sure, may I have your name, please?") == "Sure, may I have your name, please?"
    assert stream("Yeah, we do sell basins.") == "Yeah, we do sell basins."


def test_stops_at_first_question_so_agent_never_answers_for_caller():
    reply = "I'll pass that to the team. The email is j o h n at gmail dot com, correct?Yes.Great, the team will reach out."
    assert stream(reply) == "I'll pass that to the team. The email is j o h n at gmail dot com, correct?"


def test_cuts_long_reply_at_sentence_end():
    long = "Raj builds voice agents. " * 12
    out = stream(long, max_words=20)
    assert out.endswith(".") and 20 <= len(out.split()) < 26


def test_keeps_prices_and_decimals_intact():
    assert stream("Raj's rate is $20.00 per hour on Upwork.") == "Raj's rate is $20.00 per hour on Upwork."


def test_short_and_plain_replies_pass_through():
    assert stream("Hi, go ahead.") == "Hi, go ahead."
    assert stream("Okay") == "Okay"


def test_cuts_leaked_notes_glued_to_a_sentence():
    leaked = "Please confirm the full ten-digit phone number.User didn't give missing digit. Need to ask again."
    assert stream(leaked) == "Please confirm the full ten-digit phone number."


def test_keeps_tech_names_with_dots():
    assert stream("Raj builds sites with Next.js and Node.js backends.") == "Raj builds sites with Next.js and Node.js backends."


def test_reasoning_setting_only_for_thinking_models():
    from ai_services import is_reasoning_model
    from routers.retell_api import _max_response_tokens
    assert is_reasoning_model("openai/gpt-oss-120b") and not is_reasoning_model("openai/gpt-4.1-mini")
    assert _max_response_tokens("openai/gpt-oss-120b") > _max_response_tokens("openai/gpt-4.1-mini")


def test_business_voice_prompt_fills_knowledge_and_call_details():
    from routers.retell_api import _business_voice_prompt
    prompt = _business_voice_prompt("Role.\n\n## Knowledge\n{{business_knowledge}}\n\n## 11. Final Rules\nBe brief.", "", None, [{"text": "Fact one."}], "")
    assert "- Fact one." in prompt and "{{business_knowledge}}" not in prompt
    assert "Caller ID: not available" in prompt


def test_keeps_great_when_it_is_part_of_the_sentence():
    # From a real call: "Great to hear you're planning ..." came out as "To hear you're planning ...".
    assert stream("Great to hear you're planning something for your new home.") == "Great to hear you're planning something for your new home."
    assert stream("Great! We stock basins.") == "We stock basins."
