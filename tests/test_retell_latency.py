import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from routers import retell_api as retell


def test_independent_checks_overlap_and_unknown_place_still_uses_careful_model(monkeypatch):
    place_started = Event()
    seen = {}

    def choose(*args):
        assert place_started.wait(3), "Place check was held until after model selection"
        return "fast-model"

    def place(*args):
        place_started.set()
        return "The business has not confirmed delivery to Darwin."

    def complete(messages, *args, **kwargs):
        seen.update(messages=messages, model=kwargs.get("model"))
        yield "The team can confirm delivery for your area."

    monkeypatch.setattr(retell, "choose_voice_model", choose)
    monkeypatch.setattr(retell, "place_note", place)
    monkeypatch.setattr(retell, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(retell, "sounds_unfinished", lambda text: False)
    monkeypatch.setattr(retell, "bridges_enabled", lambda: False)
    monkeypatch.setattr(retell, "_system_prompt", lambda *args: "Business facts")
    monkeypatch.setattr(retell, "_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(retell.brain_service, "knowledge_text", lambda: "")
    monkeypatch.setenv("VOICE_CAREFUL_MODEL", "careful-model")
    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"),
                               stream_chat_completion=complete)

    async def run():
        return "".join([chunk async for chunk in retell._stream_reply(
            services, [{"role": "user", "content": "Do you deliver to Darwin?"}], "unknown", "test-call", 1)])

    assert "confirm" in asyncio.run(run())
    assert seen["model"] == "careful-model"
    assert any("not confirmed delivery" in message["content"] for message in seen["messages"])


@pytest.mark.parametrize("elapsed, expected_pause", [(0.1, 0.6), (1.4, None)])
def test_unfinished_pause_counts_preprocessing_time(monkeypatch, elapsed, expected_pause):
    pauses, prompts = [], []
    monkeypatch.setattr(retell, "perf_counter", lambda: 10 + elapsed)
    monkeypatch.setattr(retell, "unfinished_wait_seconds", lambda: 0.7)
    monkeypatch.setattr(retell, "choose_voice_model", lambda *args: None)
    monkeypatch.setattr(retell, "place_note", lambda *args: "")
    monkeypatch.setattr(retell, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(retell, "bridges_enabled", lambda: False)
    monkeypatch.setattr(retell, "_system_prompt", lambda *args: prompts.append(args) or "Business facts")
    monkeypatch.setattr(retell, "_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(retell.brain_service, "knowledge_text", lambda: "")

    async def pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(retell.asyncio, "sleep", pause)
    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"),
                               stream_chat_completion=lambda *args, **kwargs: iter(["We sell bathroom products."]))

    async def run():
        return "".join([chunk async for chunk in retell._stream_reply(
            services, [{"role": "user", "content": "What is Home DFO for—"}], "unknown", "test-call", 1, started_at=10)])

    assert "bathroom products" in asyncio.run(run())
    assert len(prompts) == 1  # reuse the prompt built during the checks
    if expected_pause is None:
        assert not pauses
    else:
        assert pauses == pytest.approx([expected_pause])


def test_question_retrieval_overlaps_model_selection(monkeypatch):
    prompt_started = Event()
    prompts = []

    def choose(*args):
        assert prompt_started.wait(3), "Knowledge retrieval waited for model selection"
        return None

    def prompt(*args):
        prompts.append(args)
        prompt_started.set()
        return "Business facts"

    monkeypatch.setattr(retell, "choose_voice_model", choose)
    monkeypatch.setattr(retell, "place_note", lambda *args: "")
    monkeypatch.setattr(retell, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(retell, "bridges_enabled", lambda: False)
    monkeypatch.setattr(retell, "_system_prompt", prompt)
    monkeypatch.setattr(retell, "_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(retell.brain_service, "knowledge_text", lambda: "")
    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"),
                               stream_chat_completion=lambda *args, **kwargs: iter(["We sell bathroom products."]))

    async def run():
        return "".join([chunk async for chunk in retell._stream_reply(
            services, [{"role": "user", "content": "What do you sell?"}], "unknown", "test-call", 1)])

    assert "bathroom products" in asyncio.run(run())
    assert len(prompts) == 1


def test_semantic_prefetch_coalesces_partial_words_and_reuses_unchanged_text(monkeypatch):
    calls = []
    monkeypatch.setattr(retell, "prepare_turn_meaning", lambda *args: calls.append(args))
    monkeypatch.setattr(retell._SemanticPrefetch, "STABLE_SECONDS", 0.001)

    async def run():
        warm = retell._SemanticPrefetch()
        warm.schedule("What", "How can I help?")
        warm.schedule("What is Home", "How can I help?")
        warm.schedule("What is Home DFO for?", "How can I help?")
        await asyncio.wait_for(warm.task, 2)
        warm.schedule("What is Home DFO for?", "How can I help?")
        await warm.close()

    asyncio.run(run())
    assert calls == [("What is Home DFO for?", "How can I help?")]


def test_semantic_prefetch_never_queues_obsolete_partial_transcripts(monkeypatch):
    entered, release = Event(), Event()
    calls = []

    def prepare(*args):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)

    monkeypatch.setattr(retell, "prepare_turn_meaning", prepare)
    monkeypatch.setattr(retell._SemanticPrefetch, "STABLE_SECONDS", 0)

    async def run():
        warm = retell._SemanticPrefetch()
        warm.schedule("First partial", "Hello")
        assert await asyncio.to_thread(entered.wait, 3)
        try:
            warm.schedule("Obsolete partial", "Hello")
            warm.schedule("Latest words", "Hello")
        finally:
            release.set()
        await asyncio.wait_for(warm.task, 3)
        await warm.close()

    asyncio.run(run())
    assert calls == [("First partial", "Hello"), ("Latest words", "Hello")]


def test_disconnecting_or_starting_a_reply_discards_queued_prefetch(monkeypatch):
    calls = []
    monkeypatch.setattr(retell, "prepare_turn_meaning", lambda *args: calls.append(args))

    async def run():
        warm = retell._SemanticPrefetch()
        warm.schedule("Old fragment", "Hello")
        warm.discard_pending()
        await asyncio.wait_for(warm.task, 2)
        warm.schedule("Another fragment", "Hello")
        await warm.close()
        assert warm.task.done()

    asyncio.run(run())
    assert not calls
