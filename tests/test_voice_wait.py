import asyncio
from time import perf_counter

from voice_wait import with_wait_bridge


def test_ready_answer_has_no_bridge_or_extra_model_request():
    async def source():
        yield "The answer."

    async def bridge():
        raise AssertionError("A ready answer needs no bridge request")

    async def run():
        return [part async for part in with_wait_bridge(source(), bridge, "One moment.", 0.1, perf_counter())]

    assert asyncio.run(run()) == ["The answer."]


def test_slow_answer_gets_one_bridge_then_keeps_streaming():
    async def run():
        release = asyncio.Event()
        recorded = []

        async def source():
            await release.wait()
            yield "The first sentence."
            yield "The second sentence."

        async def bridge():
            return "Let me check that."

        stream = with_wait_bridge(source(), bridge, "One moment.", 0.001, perf_counter(), recorded.append)
        assert await anext(stream) == "Let me check that. "
        release.set()
        rest = [part async for part in stream]
        assert rest == ["The first sentence.", "The second sentence."]
        assert recorded == ["Let me check that."]

    asyncio.run(run())


def test_ineligible_turn_does_not_get_a_generic_bridge():
    async def run():
        release = asyncio.Event()

        async def source():
            await release.wait()
            yield "Direct answer."

        async def no_bridge():
            return ""

        stream = with_wait_bridge(source(), no_bridge, "One moment.", 0.001, perf_counter())
        async def release_answer():
            await asyncio.sleep(0.03)
            release.set()
        asyncio.create_task(release_answer())
        assert [part async for part in stream] == ["Direct answer."]

    asyncio.run(run())


def test_slow_eligibility_decision_is_preserved_without_inventing_a_fallback():
    async def run():
        async def source():
            await asyncio.sleep(0.28)
            yield "The answer."

        async def choose():
            await asyncio.sleep(0.22)
            return "Let me check that."

        result = [part async for part in with_wait_bridge(source(), choose, "", 0.001, perf_counter())]
        assert result == ["Let me check that. ", "The answer."]

    asyncio.run(run())


def test_answer_wins_while_bridge_model_is_still_working():
    async def run():
        bridge_cancelled = asyncio.Event()

        async def source():
            await asyncio.sleep(0.02)
            yield "Ready answer."

        async def bridge():
            try:
                await asyncio.Event().wait()
            finally:
                bridge_cancelled.set()

        result = [part async for part in with_wait_bridge(source(), bridge, "One moment.", 0.001, perf_counter())]
        assert result == ["Ready answer."]
        assert bridge_cancelled.is_set()

    asyncio.run(run())


def test_slow_bridge_model_falls_back_and_caller_interruption_cancels_both():
    async def run():
        source_cancelled, bridge_cancelled = asyncio.Event(), asyncio.Event()

        async def source():
            try:
                await asyncio.Event().wait()
                yield "stale"
            finally:
                source_cancelled.set()

        async def bridge():
            try:
                await asyncio.Event().wait()
            finally:
                bridge_cancelled.set()

        stream = with_wait_bridge(source(), bridge, "One moment.", 0.001, perf_counter())
        assert await asyncio.wait_for(anext(stream), 1) == "One moment. "
        await stream.aclose()
        assert source_cancelled.is_set() and bridge_cancelled.is_set()

    asyncio.run(run())


def test_fast_model_selects_only_an_approved_wait_line(monkeypatch):
    from types import SimpleNamespace
    import voice_turns
    from voice_wait import wait_message

    seen = {}
    class Client:
        def with_options(self, **options):
            seen["options"] = options
            return self

        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    seen["request"] = kwargs
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='```json\n{"index": "1"}\n```'))])

    services = SimpleNamespace(openai_client=Client(), _openrouter_extra_body=lambda model: {"model": model})
    fallback = "Let's see what would work best for you."
    diagnostics = {}
    monkeypatch.setenv("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")
    monkeypatch.setattr(voice_turns, "bridge_model_seconds", lambda: 0.4)
    line = asyncio.run(wait_message(services, fallback, "What should I do with the drawing room?", diagnostics=diagnostics))

    assert line in voice_turns.STATUS_PHRASES
    assert seen["request"]["model"] == "meta-llama/llama-3.1-8b-instruct"
    assert seen["request"]["max_tokens"] == 12
    assert seen["options"] == {"timeout": 0.4, "max_retries": 0}
    assert diagnostics["bridge_selector"] == "fast_model"
    assert diagnostics["bridge_model_status"] == "selected"


def test_wait_line_falls_back_when_fast_model_is_unavailable(monkeypatch):
    from types import SimpleNamespace
    from voice_wait import wait_message

    monkeypatch.setenv("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")
    services = SimpleNamespace(openai_client=None)
    diagnostics = {}
    fallback = "Just putting that together for you."
    assert asyncio.run(wait_message(services, fallback, "What options should I consider?", diagnostics=diagnostics)) == fallback
    assert diagnostics == {"bridge_selector": "semantic_fallback", "bridge_model_attempted": False,
                           "bridge_model_status": "unavailable", "bridge_model_ms": 0}


def test_slow_bridge_selection_uses_the_semantic_line_within_its_budget(monkeypatch):
    import time
    from types import SimpleNamespace
    import voice_turns
    from voice_wait import wait_message

    class Client:
        def with_options(self, **options):
            return self

        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    time.sleep(0.25)  # Simulate a provider ignoring its own request timeout.
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"index": 1}'))])

    monkeypatch.setenv("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")
    monkeypatch.setattr(voice_turns, "bridge_model_seconds", lambda: 0.1)
    services = SimpleNamespace(openai_client=Client(), _openrouter_extra_body=lambda model: None)
    diagnostics = {}
    fallback = "Let's see what would work best for you."
    async def run():
        started = time.perf_counter()
        line = await wait_message(services, fallback, "What should I do?", diagnostics=diagnostics)
        return line, time.perf_counter() - started

    line, elapsed = asyncio.run(run())

    assert line == fallback
    assert elapsed < 0.18
    assert diagnostics["bridge_selector"] == "semantic_fallback"
    assert diagnostics["bridge_model_status"] == "timeout"
