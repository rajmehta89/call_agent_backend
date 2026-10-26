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
