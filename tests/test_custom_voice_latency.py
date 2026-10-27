import asyncio
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from routers import custom_twilio_voice_api as custom


def test_custom_pipeline_checks_and_retrieval_overlap_off_the_audio_loop(monkeypatch):
    loop_thread = get_ident()
    place_started, prompt_started = Event(), Event()
    seen = {}

    def prepare(*args):
        assert get_ident() != loop_thread
        # Semantic vectors are shared by all turn checks. Their warmup must
        # overlap prompt retrieval and the independent safety checks.
        assert place_started.wait(3)
        assert prompt_started.wait(3)

    def choose(*args):
        assert get_ident() != loop_thread
        assert place_started.wait(3)
        assert prompt_started.wait(3)
        return "fast-model"

    def place(*args):
        place_started.set()
        return "Coverage for Darwin has not been confirmed."

    def prompt(*args):
        seen["language"] = args[-1]
        prompt_started.set()
        return "Business facts"

    def complete(messages, *args, **kwargs):
        seen.update(messages=messages, model=kwargs.get("model"))
        yield "The team can confirm coverage for your area."

    monkeypatch.setattr(custom, "prepare_turn_meaning", prepare)
    monkeypatch.setattr(custom, "choose_voice_model", choose)
    monkeypatch.setattr(custom, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(custom, "place_note", place)
    monkeypatch.setattr(custom, "_system_prompt", prompt)
    monkeypatch.setattr(custom.brain_service, "knowledge_text", lambda: "")
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)
    monkeypatch.setenv("VOICE_CAREFUL_MODEL", "careful-model")
    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"),
                               stream_chat_completion=complete)

    timings = {}

    async def run():
        return "".join([sentence async for sentence in custom._voice_sentences(
            services, "Do you deliver to Darwin?", [], "unknown", "", language="ar", timings=timings)])

    assert "confirm coverage" in asyncio.run(run())
    assert seen["model"] == "careful-model"
    assert seen["language"] == "ar"
    assert any("not been confirmed" in message["content"] for message in seen["messages"])
    assert timings["semantic_checks_ms"] >= 0


@pytest.mark.parametrize("problem, expected_model, with_facts", [(False, "fast", False), (True, None, True)])
def test_custom_contact_validation_keeps_model_and_knowledge_selection(monkeypatch, problem, expected_model, with_facts):
    seen = {}
    monkeypatch.setattr(custom, "prepare_turn_meaning", lambda *args: None)
    monkeypatch.setattr(custom, "choose_voice_model", lambda *args: "fast")
    monkeypatch.setattr(custom, "contact_check", lambda *args: ("Check this number again.", problem))
    monkeypatch.setattr(custom, "place_note", lambda *args: "")
    monkeypatch.setattr(custom, "needs_facts", lambda text: False)
    # The prompt with facts is always started early; what matters is which prompt the model is given.
    monkeypatch.setattr(custom, "_system_prompt", lambda *args: "Business facts" if args[4] else "No facts")
    monkeypatch.setattr(custom.brain_service, "knowledge_text", lambda: "")
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)

    def complete(messages, *args, **kwargs):
        seen.update(model=kwargs.get("model"), messages=messages, with_facts=messages[0]["content"] == "Business facts")
        yield "Please confirm your number."

    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"),
                               stream_chat_completion=complete)

    async def run():
        return "".join([sentence async for sentence in custom._voice_sentences(
            services, "It's 0412 345 678.", [], "unknown", "")])

    asyncio.run(run())
    assert seen["model"] == expected_model
    assert seen["with_facts"] == with_facts
    assert any("Check this number" in message["content"] for message in seen["messages"])


def test_main_model_answer_turn_builds_facts_alongside_the_checks(monkeypatch):
    """A "Yeah." to our question stays on the main model with business facts. Those facts used to be searched only
    after the turn checks finished (0.6-1.6 s on real calls); now both run at once."""
    import time
    seen = {}

    def slow_check(*args):
        time.sleep(0.3)

    def slow_prompt(*args):
        time.sleep(0.3)
        return "Business facts" if args[4] else "No facts"

    monkeypatch.setattr(custom, "prepare_turn_meaning", slow_check)
    monkeypatch.setattr(custom, "choose_voice_model", lambda *args: slow_check() or None)
    monkeypatch.setattr(custom, "needs_facts", lambda text: False)
    monkeypatch.setattr(custom, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(custom, "place_note", lambda *args: "")
    monkeypatch.setattr(custom, "_system_prompt", slow_prompt)
    monkeypatch.setattr(custom, "search_text", lambda text, messages: text)
    monkeypatch.setattr(custom.brain_service, "knowledge_text", lambda: "")
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)

    def complete(messages, *args, **kwargs):
        seen.update(system=messages[0]["content"], model=kwargs.get("model"))
        yield "Could I have your full name?"

    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"),
                               stream_chat_completion=complete, provider="openrouter", voice_model_source="RETELL_OPENROUTER_MODEL")
    timings = {}

    async def run():
        return "".join([sentence async for sentence in custom._voice_sentences(
            services, "Yeah. Yeah.", [{"role": "assistant", "content": "Shall I pass your details to the team?"}],
            "unknown", "", timings=timings)])

    asyncio.run(run())
    assert seen == {"system": "Business facts", "model": None}
    assert timings["preparation_ms"] < 550, timings  # 0.3 s + 0.3 s one after the other would be >= 600 ms
    assert timings["model_route"] == "main" and timings["model_source"] == "RETELL_OPENROUTER_MODEL"
    assert timings["with_facts"] is True


def test_voice_model_source_is_recorded(monkeypatch):
    monkeypatch.delenv("CUSTOM_VOICE_MODEL", raising=False)
    monkeypatch.setenv("RETELL_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-oss-20b")
    services = custom._voice_services()
    assert services.config.OPENROUTER_MODEL == "openai/gpt-4.1-mini"
    assert services.voice_model_source == "RETELL_OPENROUTER_MODEL"


def test_custom_fast_business_question_keeps_retrieved_facts_and_records_model(monkeypatch):
    seen = {}
    fast = "meta-llama/llama-3.1-8b-instruct"
    monkeypatch.setattr(custom, "prepare_turn_meaning", lambda *args: None)
    monkeypatch.setattr(custom, "choose_voice_model", lambda *args: fast)
    monkeypatch.setattr(custom, "needs_facts", lambda text: True)
    monkeypatch.setattr(custom, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(custom, "place_note", lambda *args: "")
    monkeypatch.setattr(custom, "_system_prompt", lambda *args: seen.update(with_facts=args[4]) or "Relevant business facts")
    monkeypatch.setattr(custom.brain_service, "knowledge_text", lambda: "")
    monkeypatch.setattr(custom.call_forwarding, "forwarding_on", lambda: False)

    def complete(messages, *args, **kwargs):
        seen.update(messages=messages, model=kwargs.get("model"))
        yield "Home DFO supplies bathroom and kitchen products."

    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"),
                               stream_chat_completion=complete)
    timings = {}

    async def run():
        return "".join([sentence async for sentence in custom._voice_sentences(
            services, "What does Home DFO provide?", [], "unknown", "", timings=timings)])

    assert "bathroom and kitchen" in asyncio.run(run())
    assert seen["model"] == fast
    assert seen["with_facts"] is True
    assert any("Relevant business facts" in message["content"] for message in seen["messages"])
    assert timings["requested_model"] == fast
    assert timings["preparation_ms"] >= 0 and timings["model_wait_ms"] >= 0
