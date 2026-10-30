"""A single wait acknowledgement that never holds up a ready voice answer."""

import asyncio
import json
import os
from time import perf_counter


async def stream_in_background(iterator):
    """The pieces of a model's streamed reply as they arrive, read by one worker thread for the whole reply.

    Fetching each piece with its own asyncio.to_thread waited for a free worker every time: an Urdu reply comes in
    2-3x as many pieces as the same reply in English, and on a busy server its first sentence reached the voice ~4 s
    after the model's first words (2026-10-29). Closing this generator stops the worker and closes the stream."""
    import threading
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    done = object()

    def pump() -> None:
        try:
            for piece in iterator:
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(queue.put_nowait, piece)
        except BaseException as error:  # handed to the reader, which raises it
            loop.call_soon_threadsafe(queue.put_nowait, error)
        finally:
            if stop.is_set():
                close = getattr(iterator, "close", None)
                if close:
                    try:
                        close()  # drops the model request; safe here, in the thread that ran the stream
                    except Exception:
                        pass
            try:
                loop.call_soon_threadsafe(queue.put_nowait, done)
            except RuntimeError:
                pass  # the loop has closed (the call ended)

    worker = loop.run_in_executor(None, pump)
    try:
        while True:
            piece = await queue.get()
            if piece is done:
                return
            if isinstance(piece, BaseException):
                raise piece
            yield piece
    finally:
        stop.set()
        worker.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)


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


def bridge_latest_seconds() -> float:
    """VOICE_BRIDGE_LATEST_MS: no waiting phrase starts later than this after the caller finished (0 = no limit).
    From the 2026-10-30 calls: bridges decided at 1.5-2.3 s started just as the answer was ready (median 2.1 s) and
    held it back by the phrase's length, 1-2 s; the answer itself was then less than a second away."""
    try:
        return max(0.0, float(os.getenv("VOICE_BRIDGE_LATEST_MS", "1500"))) / 1000
    except ValueError:
        return 1.5


async def with_wait_bridge(source, make_bridge, fallback, after_seconds, started_at, on_bridge=None,
                           late_line="", late_after=0.0, latest=None):
    """Race the first answer against the wait deadline and bridge generation.
    A semantic rejection remains a rejection after the late deadline. Elapsed
    time alone never gives a turn permission to play a waiting phrase.
    latest: seconds after started_at past which no chosen bridge starts (default bridge_latest_seconds()); a decision
    still pending then is dropped, so the answer is not queued behind a phrase begun at the last moment. The late line
    (a long silence) is not limited by it."""
    latest = bridge_latest_seconds() if latest is None else latest

    def too_late() -> bool:
        return latest > 0 and perf_counter() - started_at > latest

    iterator = source.__aiter__()
    first = asyncio.create_task(anext(iterator))
    choice = None
    said = False
    rejected = False
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
                    limit = max(0.0, latest - (perf_counter() - started_at)) if latest > 0 else None
                    done, _ = await asyncio.wait({first, choice}, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
                if first not in done and not first.done() and not too_late():
                    line = fallback
                    if choice in done:
                        try:
                            # An empty result is deliberate: this turn does not
                            # qualify for a bridge (for example, a short reply).
                            line = choice.result()
                            rejected = not line
                        except Exception:
                            line = fallback
                    if line:
                        said = True
                        if on_bridge is not None:
                            on_bridge(line)
                        yield line + " "
        if not said and not rejected and late_line and late_after > 0 and fallback and not first.done():
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
