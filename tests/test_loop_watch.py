"""The loop watch names the code that holds up the event loop, so a slow call shows its culprit (2026-10-29)."""

import asyncio
import time

import loop_watch


def blocking_work():
    time.sleep(0.4)  # stands in for a database read or model call run directly on the loop


def test_a_stall_is_recorded_with_the_code_that_caused_it(monkeypatch):
    monkeypatch.setattr(loop_watch, "_state", {"beat": 0.0, "thread": None, "loop_thread": None})
    loop_watch._STALLS.clear()
    started = time.time()

    async def call():
        loop_watch.start()
        await asyncio.sleep(0.15)
        blocking_work()
        await asyncio.sleep(0.2)

    asyncio.run(call())
    stalls = loop_watch.stalls_since(started)
    assert stalls and stalls[0]["ms"] >= 250
    assert "test_loop_watch.py" in stalls[0]["where"] and "blocking_work" in stalls[0]["where"]
