"""A single wait acknowledgement that never holds up a ready voice answer."""

import asyncio
from time import perf_counter


async def with_wait_bridge(source, make_bridge, fallback, after_seconds, started_at, on_bridge=None):
    """Race the first answer against the wait deadline and bridge generation."""
    iterator = source.__aiter__()
    first = asyncio.create_task(anext(iterator))
    choice = None
    try:
        if after_seconds > 0:
            remaining = max(0.0, after_seconds - (perf_counter() - started_at))
            done, _ = await asyncio.wait({first}, timeout=remaining)
            if not done:
                choice = asyncio.create_task(make_bridge())
                done, _ = await asyncio.wait({first, choice}, timeout=0.2, return_when=asyncio.FIRST_COMPLETED)
                if first not in done and not first.done():
                    line = fallback
                    if choice in done:
                        try:
                            # An empty result is deliberate: this turn does not
                            # qualify for a bridge (for example, a short reply).
                            line = choice.result()
                        except Exception:
                            line = fallback
                    if line:
                        if on_bridge is not None:
                            on_bridge(line)
                        yield line + " "
        try:
            yield await first
        except StopAsyncIteration:
            return
        async for chunk in iterator:
            yield chunk
    finally:
        tasks = [task for task in (first, choice) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if close:
            await close()
