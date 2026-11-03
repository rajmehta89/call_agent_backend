"""A single wait acknowledgement that never holds up a ready voice answer."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os
import re
from difflib import SequenceMatcher
from time import perf_counter


_BRIDGE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="voice-bridge")


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


_BRIDGE_ANSWER_CUES = re.compile(
    r"\d|[$€£]|\b(?:yes|no|yeah|nope|costs?|priced|in stock|available)\b", re.I
)
_BRIDGE_STOCK_OPENER = re.compile(r"^\s*(?:mm+|um+|one moment|just a moment|okay[, ]+just a moment)\b", re.I)
_BRIDGE_LOOKUP_VERB = re.compile(r"\b(?:check\w*|look\w*|find\w*|see|pull\w*)\b", re.I)
_BRIDGE_PROCESS_NARRATION = re.compile(
    r"\b(?:prepar\w*|generat\w*|process\w*|working through|putting (?:that|this|it) together|"
    r"clarifying (?:the|your)|confirming (?:the|your)|(?:product )?details will be confirmed|"
    r"(?:the|your) answer|backend|our system)\b",
    re.I,
)


def bridge_is_duplicate(line, recent, threshold=0.82):
    """Match a progress line against every prior sentence said during this call."""
    normalized = " ".join(re.findall(r"[^\W_]+", str(line or "").casefold()))
    if not normalized:
        return False
    recent = [recent] if isinstance(recent, str) else recent
    for previous in recent or ():
        for sentence in re.split(r"(?<=[.!?؟۔])\s+", str(previous or "")):
            candidate = " ".join(re.findall(r"[^\W_]+", sentence.casefold()))
            if not candidate:
                continue
            if normalized == candidate or SequenceMatcher(None, normalized, candidate).ratio() >= threshold:
                return True
            words, other_words = set(normalized.split()), set(candidate.split())
            if len(words | other_words) >= 4 and len(words & other_words) / len(words | other_words) >= 0.78:
                return True
    return False


def _clean_dynamic_bridge(text, recent, semantic_candidate, lookup, language):
    """Accept one brief progress line, never an answer, repeated line, or generic waiting phrase."""
    recent = [recent] if isinstance(recent, str) else list(recent or ())
    line = " ".join(str(text or "").replace('"', "").replace("`", "").split()).strip(" '`")
    line = re.split(r"(?<=[.!?؟۔])\s+", line, maxsplit=1)[0].strip()
    words = line.split()
    if not 4 <= len(words) <= 6 or "?" in line or "؟" in line or _BRIDGE_ANSWER_CUES.search(line):
        return ""
    if _BRIDGE_STOCK_OPENER.search(line):
        return ""
    if _BRIDGE_PROCESS_NARRATION.search(line):
        return ""  # a caller should hear a human acknowledgement, never a description of internal work
    if language == "en" and bool(_BRIDGE_LOOKUP_VERB.search(line)) != bool(lookup):
        return ""  # only claim to check something when the live lookup actually runs
    if bridge_is_duplicate(line, (*recent, semantic_candidate)):
        return ""
    if line[-1:] not in ".!؟۔":
        line += "."
    return line


async def request_bridge_line(services, messages, cleaner, budget, max_tokens=16, temperature=0.7):
    """Try the fast bridge model, then its configured backup inside one shared deadline."""
    from voice_turns import bridge_fallback_model, bridge_line_model

    client = getattr(services, "openai_client", None)
    if client is None:
        return "", "", "", "unavailable", 0

    configured = [
        ("primary", bridge_line_model()),
        ("fallback", bridge_fallback_model(getattr(services, "fallback_model", ""))),
    ]
    models = []
    seen = set()
    for role, model in configured:
        if model and model not in seen:
            models.append((role, model))
            seen.add(model)
    if not models:
        return "", "", "", "unavailable", 0

    started = perf_counter()
    attempt_state = {"count": 0}

    def choose():
        status = "error"
        attempts = 0
        # Split the bridge budget between configured models. Without this, the
        # primary can consume the full deadline and leave no time for fallback.
        per_model_budget = budget / len(models)
        for role, model in models:
            remaining = budget - (perf_counter() - started)
            if remaining <= 0.025:
                break
            attempts += 1
            attempt_state["count"] = attempts
            attempt_timeout = min(remaining, per_model_budget)
            try:
                response = client.with_options(timeout=attempt_timeout, max_retries=0).chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=getattr(services, "_openrouter_extra_body", lambda _model: None)(model),
                )
                line = cleaner(response.choices[0].message.content or "")
                if line:
                    return line, model, role, "generated", attempts
                status = "invalid_response"
            except Exception as error:
                status = type(error).__name__.lower()
        return "", "", "", status, attempts

    try:
        loop = asyncio.get_running_loop()
        context = copy_context()
        return await asyncio.wait_for(loop.run_in_executor(_BRIDGE_EXECUTOR, context.run, choose), timeout=budget)
    except asyncio.TimeoutError:
        return "", "", "", "timeout", attempt_state["count"]
    except Exception as error:
        return "", "", "", type(error).__name__.lower(), 1


async def wait_message(services, candidate, caller_text, agent_text="", recent=(), diagnostics=None, language="en"):
    """Generate a fresh progress line only when a slow turn needs one; never speak fixed fallback wording."""
    if not candidate:
        return ""
    started = perf_counter()
    from voice_turns import LOOKUP_BRIDGES, bridge_said, dynamic_bridge_seconds

    # VOICE_BRIDGE_MODEL: phrase the already-approved semantic bridge in fresh words for this caller.
    if os.getenv("VOICE_BRIDGE_MODEL", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        if diagnostics is not None:
            diagnostics.update(bridge_selector="none", bridge_model_attempted=False,
                               bridge_model_status="off", bridge_model_ms=0)
        return ""

    client = getattr(services, "openai_client", None)
    from voice_turns import bridge_fallback_model, bridge_line_model
    if not (bridge_line_model() or bridge_fallback_model(getattr(services, "fallback_model", ""))) or client is None:
        if diagnostics is not None:
            diagnostics.update(bridge_selector="none", bridge_model_attempted=False,
                               bridge_model_status="unavailable", bridge_model_ms=0)
        return ""

    recent = [recent] if isinstance(recent, str) else list(recent or ())
    from voice_languages import normalize_language
    language = normalize_language(language)
    budget = dynamic_bridge_seconds()
    lookup_requested = bridge_said(candidate) in LOOKUP_BRIDGES
    language_name = {"en": "English", "ur": "Urdu", "ar": "Arabic"}.get(str(language or "en").lower(), "the caller's language")
    recent_text = "\n".join(f"- {str(line)[-180:]}" for line in recent[-8:] if line)
    system_prompt = (
        "Write one fresh, natural spoken acknowledgement for a live customer-service call. "
        "Use 4 to 6 words, one sentence, and the caller's language (" + language_name + "). "
        "Tie it to the topic the caller just raised and sound like a person responding to them. "
        "Do not narrate preparation, model or backend activity. Never say you are preparing an answer, "
        "working through details, putting something together, or confirming details. Do not answer, guess, "
        "give facts, ask a question, greet, or use a generic wait phrase such as 'Mm', 'one moment', or 'just a moment'. "
        "Avoid the wording of the previous agent update. Return only the sentence. "
    )
    if lookup_requested:
        system_prompt += (
            "A live store lookup is the next step for this topic. You may say you will check the specific item, order, "
            "or policy, but do not imply you found or confirmed anything. Use a fresh, topic-specific construction, "
            "not a stock template."
        )
    else:
        system_prompt += (
            "No live store lookup is used for this turn. Acknowledge the topic or say what you can explain, "
            "without claiming to check or verify it. Use fresh wording, not a fixed template."
        )

    selected, model_used, model_role, model_status, model_attempts = await request_bridge_line(
        services,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"Previous agent response: {agent_text[-300:]}\n"
                f"Previous progress updates to avoid:\n{recent_text or '- none'}\n"
                f"Caller: {caller_text[-500:]}"
            )},
        ],
        lambda text: _clean_dynamic_bridge(text, recent, candidate, lookup_requested, language),
        budget,
        max_tokens=16,
        temperature=0.7,
    )
    if diagnostics is not None:
        diagnostics.update(bridge_selector="dynamic_model" if selected else "none",
                           bridge_model_attempted=True, bridge_model_status=model_status,
                           bridge_model_ms=round((perf_counter() - started) * 1000),
                           bridge_dynamic=bool(selected), bridge_model_used=model_used,
                           bridge_model_role=model_role, bridge_model_attempts=model_attempts,
                           bridge_model_fallback_used=model_role == "fallback")
    return selected


def bridge_latest_seconds() -> float:
    """VOICE_BRIDGE_LATEST_MS: stop looking for a fresh line after this much time, while the answer keeps racing.
    The 2026-11-03 policy call needed about 1.5 s for semantic eligibility, then a short model request; the prior
    1.2 s cutoff discarded that useful line before it could be generated."""
    try:
        return max(0.0, float(os.getenv("VOICE_BRIDGE_LATEST_MS", "3000"))) / 1000
    except ValueError:
        return 3.0


async def with_wait_bridge(source, make_bridge, fallback, after_seconds, started_at, on_bridge=None,
                           late_line="", late_after=0.0, latest=None, silence_line="", silence_after=0.0,
                           claim_bridge=None, cancel_bridge=None):
    """Race the first answer against the wait deadline and bridge generation.
    A semantic rejection remains a rejection after the late deadline. Elapsed
    time alone never gives a turn permission to play a waiting phrase.
    latest: seconds after started_at past which no chosen bridge starts (default bridge_latest_seconds()); a decision
    still pending then is dropped, so the answer is not queued behind a phrase begun at the last moment. The late line
    (a long silence) is not limited by it.
    silence_line: said when nothing at all has been said silence_after seconds after started_at, whatever the bridge
    decision: that decision is whether a lookup phrase suits the turn, not whether silence is fine. Turns judged not
    to need one were silent for 3.5-3.8 s on 2026-10-31 calls."""
    latest = bridge_latest_seconds() if latest is None else latest

    def too_late() -> bool:
        return latest > 0 and perf_counter() - started_at > latest

    def claim(line: str) -> bool:
        if not line or claim_bridge is None:
            return bool(line)
        try:
            return bool(claim_bridge(line))
        except Exception:
            return False

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
                    if line and not claim(line):
                        line = ""
                        rejected = True
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
                if claim(late_line):
                    said = True
                    if on_bridge is not None:
                        on_bridge(late_line)
                    yield late_line + " "
                else:
                    rejected = True
        if not said and not rejected and silence_line and silence_after > 0 and not first.done():
            remaining = silence_after - (perf_counter() - started_at)
            if remaining > 0:
                await asyncio.wait({first}, timeout=remaining)
            if not first.done() and claim(silence_line):
                said = True
                if on_bridge is not None:
                    on_bridge(silence_line)
                yield silence_line + " "
        try:
            yield await first
        except StopAsyncIteration:
            return
        async for chunk in iterator:
            yield chunk
    finally:
        if cancel_bridge is not None:
            try:
                cancel_bridge()
            except Exception:
                pass
        tasks = [task for task in (first, choice) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if close:
            await close()
