"""A single wait acknowledgement that never holds up a ready voice answer."""

import asyncio
import json
from time import perf_counter


async def wait_message(services, fallback, caller_text, agent_text="", recent=(), diagnostics=None):
    """Ask the small model to choose a safe bridge while the real answer runs; always keep the approved fallback."""
    if not fallback:
        return ""
    started = perf_counter()
    from voice_turns import STATUS_PHRASES, bridge_model_seconds, fast_voice_model

    model = fast_voice_model()
    client = getattr(services, "openai_client", None)
    if not model or client is None:
        if diagnostics is not None:
            diagnostics.update(bridge_selector="semantic_fallback", bridge_model_attempted=False,
                               bridge_model_status="unavailable", bridge_model_ms=0)
        return fallback

    recent = {recent} if isinstance(recent, str) else set(recent or ())
    choices = list(dict.fromkeys(line for line in (fallback, *STATUS_PHRASES) if line and line not in recent))
    if not choices:
        if diagnostics is not None:
            diagnostics.update(bridge_selector="semantic_fallback", bridge_model_attempted=False,
                               bridge_model_status="no_options", bridge_model_ms=0)
        return fallback
    budget = bridge_model_seconds()

    def choose():
        response = client.with_options(timeout=budget, max_retries=0).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Choose the single approved short waiting line that best fits this phone conversation. "
                    "The actual answer is being prepared separately. Do not answer, add wording, or reason aloud. "
                    "Return only JSON: {\"index\": integer}. Approved lines: " + json.dumps(choices)
                )},
                {"role": "user", "content": f"Previous agent line: {agent_text}\nCaller: {caller_text}"},
            ],
            max_tokens=12,
            temperature=0,
            extra_body=getattr(services, "_openrouter_extra_body", lambda _model: None)(model),
        )
        body = response.choices[0].message.content or "{}"
        value = json.loads(body).get("index")
        if type(value) is int and 0 <= value < len(choices):
            return choices[value], True, "selected"
        return fallback, False, "invalid_response"

    selected = fallback
    model_selected = False
    model_status = "error"
    try:
        selected, model_selected, model_status = await asyncio.wait_for(asyncio.to_thread(choose), timeout=budget)
    except asyncio.TimeoutError:
        model_status = "timeout"
    except Exception as error:
        model_status = type(error).__name__.lower()
    if diagnostics is not None:
        diagnostics.update(bridge_selector="fast_model" if model_selected else "semantic_fallback",
                           bridge_model_attempted=True, bridge_model_status=model_status,
                           bridge_model_ms=round((perf_counter() - started) * 1000))
    return selected


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
