"""A single wait acknowledgement that never holds up a ready voice answer."""

import asyncio
import json
from time import perf_counter


async def wait_message(services, fallback, caller_text, agent_text="", recent=()):
    """Let the fast model select a fact-free wait line; never wait indefinitely for it."""
    if not fallback:
        return ""
    from voice_turns import fast_voice_model, bridge_model_seconds, STATUS_PHRASES
    model = fast_voice_model()
    client = getattr(services, "openai_client", None)
    if not model or client is None:
        return fallback
    choices = list(dict.fromkeys(line for line in (fallback, *STATUS_PHRASES) if line not in recent))
    if not choices:
        return ""

    def choose():
        response = client.with_options(timeout=bridge_model_seconds(), max_retries=0).chat.completions.create(
            model=model,
            messages=[{"role": "system", "content":
                       "Select the most natural holding message for this ongoing phone conversation. "
                       "The actual answer is still being prepared. Do not answer or add any words. "
                       "Return only JSON with an integer index into this approved list: " + json.dumps(choices)},
                      {"role": "user", "content": f"Agent: {agent_text}\nCaller: {caller_text}"}],
            max_tokens=12, temperature=0,
        )
        value = json.loads(response.choices[0].message.content or "{} ").get("index")
        return choices[value] if type(value) is int and 0 <= value < len(choices) else fallback

    try:
        return await asyncio.wait_for(asyncio.to_thread(choose), bridge_model_seconds())
    except Exception:
        return fallback


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
                if first not in done and choice not in done and not fallback:
                    # Eligibility may itself need a semantic check. Without an approved fallback, keep racing
                    # the answer against that decision instead of inventing filler or dropping a late decision.
                    done, _ = await asyncio.wait({first, choice}, return_when=asyncio.FIRST_COMPLETED)
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
