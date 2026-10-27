"""A single wait acknowledgement that never holds up a ready voice answer."""

import asyncio
import json
import os
from time import perf_counter


async def wait_message(services, fallback, caller_text, agent_text="", recent=(), diagnostics=None):
    """Ask the small model to choose a safe bridge while the real answer runs; always keep the approved fallback."""
    if not fallback:
        return ""
    started = perf_counter()
    from voice_turns import STATUS_PHRASES, bridge_model_seconds, fast_voice_model

    # VOICE_BRIDGE_MODEL (default off): the line passed in is already chosen by meaning for this turn. Asking the
    # small model to re-pick it timed out on 4 of 10 production Retell attempts (450 ms each) for no better line.
    if os.getenv("VOICE_BRIDGE_MODEL", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        if diagnostics is not None:
            diagnostics.update(bridge_selector="semantic", bridge_model_attempted=False,
                               bridge_model_status="off", bridge_model_ms=0)
        return fallback

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
        cleaned = body.strip()
        payload = None
        try:
            payload = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            # Some small-model gateways wrap JSON in a code fence or include a
            # short preamble. Decode the first JSON object without accepting
            # any free-form bridge text.
            start = cleaned.find("{")
            if start >= 0:
                try:
                    payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
                except json.JSONDecodeError:
                    payload = None
        value = payload.get("index") if isinstance(payload, dict) else payload
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if type(value) is int and 0 <= value < len(choices):
            return choices[value], True, "selected"
        # Accept the exact approved line when the model ignores the JSON shape.
        # Exact membership keeps the model from adding a second response or facts.
        approved = cleaned.strip("` \n\r\t\"'")
        if approved in choices:
            return approved, True, "selected"
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


async def with_wait_bridge(source, make_bridge, fallback, after_seconds, started_at, on_bridge=None,
                           late_line="", late_after=0.0):
    """Race the first answer against the wait deadline and bridge generation.
    late_line/late_after: when no bridge was said (a "Yeah." turn does not get one) and the answer has still not started
    late_after seconds after started_at, say late_line once, so the caller does not sit in a long silence."""
    iterator = source.__aiter__()
    first = asyncio.create_task(anext(iterator))
    choice = None
    said = False
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
                        said = True
                        if on_bridge is not None:
                            on_bridge(line)
                        yield line + " "
        if not said and late_line and late_after > 0 and not first.done():
            remaining = late_after - (perf_counter() - started_at)
            if remaining > 0:
                await asyncio.wait({first}, timeout=remaining)
            if not first.done():
                if on_bridge is not None:
                    on_bridge(late_line)
                yield late_line + " "
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
