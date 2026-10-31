"""Voice calls get live Shopify data: the lookup runs with the knowledge search and its answer is in the prompt."""

import time

from routers import retell_api
from voice_turns import choose_voice_model


def test_store_answer_is_in_the_voice_prompt(monkeypatch):
    seen = {}

    def lookup(text, phone, conversation):
        seen.update(text=text, phone=phone, conversation=conversation)
        return "Order DFO001114: paid, fulfilled.", "get_order"

    monkeypatch.setattr(retell_api.brain_service, "_shopify_context", lookup)
    monkeypatch.setattr(retell_api, "_no_lookup_turn", lambda *args: False)
    history = [{"role": "user", "content": "my order is DFO001114"}, {"role": "assistant", "content": "What's the email on it?"}]
    prompt = retell_api._system_prompt("it's priya@example.com", "+61412345678", "", history)
    assert "Live store data from Shopify" in prompt and "Order DFO001114: paid, fulfilled." in prompt
    assert "DFO001114" in seen["conversation"] and "priya@example.com" in seen["conversation"]  # earlier turns go along


def test_no_store_question_adds_nothing(monkeypatch):
    monkeypatch.setattr(retell_api, "_no_lookup_turn", lambda *args: False)
    monkeypatch.setattr(retell_api.brain_service, "_shopify_context", lambda *args: ("", None))
    assert "Live store data" not in retell_api._system_prompt("what are your opening hours?", "unknown", "")


def test_a_slow_store_never_holds_the_reply(monkeypatch):
    monkeypatch.setattr(retell_api, "_no_lookup_turn", lambda *args: False)
    monkeypatch.setattr(retell_api, "STORE_LOOKUP_SECONDS", 0.2)
    monkeypatch.setattr(retell_api.brain_service, "_shopify_context", lambda *args: (time.sleep(1), ("STORE_LATE_MARKER", "get_order"))[1])
    started = time.perf_counter()
    prompt = retell_api._system_prompt("where is my order 1114?", "unknown", "")
    assert "STORE_LATE_MARKER" not in prompt and time.perf_counter() - started < 0.9


def test_prompt_timings_separate_work_queue_and_cumulative_checkpoints(monkeypatch):
    from types import SimpleNamespace

    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(retell_api, "perf_counter", lambda: clock.now)

    class DeferredPool:
        def submit(self, function):
            return SimpleNamespace(result=lambda timeout: function())

    def work(seconds, value):
        clock.now += seconds
        return value

    monkeypatch.setattr(retell_api, "_PROMPT_POOL", DeferredPool())
    monkeypatch.setattr(retell_api, "conversation_knowledge", lambda *args: work(0.4, []))
    monkeypatch.setattr(retell_api, "_store_facts", lambda *args: work(0.2, "Store facts"))
    monkeypatch.setattr(retell_api.brain_service, "customer_context", lambda *args: work(0.1, "Caller context"))
    monkeypatch.setattr(retell_api.agent_config, "get_voice_prompt", lambda: "")
    monkeypatch.setattr(retell_api.agent_config, "get_system_prompt", lambda: "Business")
    monkeypatch.setattr(retell_api.brain_service, "channel_config", lambda *args: {})
    monkeypatch.setattr(retell_api, "_voice_prompt", lambda *args: "Prompt")
    timings = {}
    assert "Prompt" in retell_api._system_prompt("Product question", "+61412345678", language="en", timings=timings)
    assert timings == {
        "prompt_timing_schema": 2,
        "prompt_knowledge_work_ms": 400, "prompt_knowledge_ms": 400,
        "prompt_store_work_ms": 200, "prompt_store_queue_ms": 400, "prompt_store_ms": 600,
        "prompt_caller_work_ms": 100, "prompt_caller_queue_ms": 600, "prompt_caller_ms": 700,
    }


def test_order_details_go_to_the_main_model(monkeypatch):
    monkeypatch.setenv("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")
    assert choose_voice_model("DFO001114", "What's your order number?") is None
    assert choose_voice_model("it's priya at example dot com", "What email is on the order?") is None


def test_a_tracking_number_from_the_store_is_read_as_written():
    from voice_turns import NumberSpeaker, reply_known_numbers
    prompt = "Live store data from Shopify: Order DFO001112: tracking: Australia Post 33GJU350793501361810103\nCaller ID: +61412345678"
    caller = "61412345678"
    speaker = NumberSpeaker(caller, reply_known_numbers("", prompt, caller))
    spoken = speaker.feed("Your tracking number is 33GJU350793501361810103.") + speaker.flush()
    assert "350793501361810103" in spoken
    misread = NumberSpeaker(caller, reply_known_numbers("", prompt, caller))
    said = misread.feed("I have your number as 0412 345 679.") + misread.flush()
    assert "679" not in said  # the caller's own number is still corrected


def test_a_garbled_business_number_is_said_exactly_as_written():
    from voice_turns import NumberSpeaker, reply_known_numbers
    knowledge = "Phone: (04) 2221 3831"
    for caller in ("61412345678", ""):  # a phone call, and a web call with no caller number
        speaker = NumberSpeaker(caller, reply_known_numbers(knowledge, "", caller))
        said = speaker.feed("Our phone number is 0422 221 383. Anything else?") + speaker.flush()
        assert "zero four two two, two one three, eight three one" in said, caller  # 0422213831, the real number
        right = NumberSpeaker(caller, reply_known_numbers(knowledge, "", caller))
        assert "2221 3831" in right.feed("Call us on (04) 2221 3831 any time.") + right.flush()  # the right number is left alone
