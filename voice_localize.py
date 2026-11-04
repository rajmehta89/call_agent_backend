"""The call system's own short lines (greeting, "connecting you", "go ahead", callback lines) in a call language that
has no hand-written copy, such as Punjabi: written by the voice language model from the English line, never typed in.

The lines are said from the event loop, so a model call is never made there: `localized` returns the cached line, or
the English one while its translation is made in the background. `warm` translates a call's lines as soon as the call
is in that language, so they are ready before they are needed; `localized(..., wait=)` may wait, off the event loop
only (the greeting is written that way before the call is answered).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Dict, Iterable, Tuple

logger = logging.getLogger(__name__)

_CACHE: Dict[Tuple[str, str], str] = {}
_PENDING: Dict[Tuple[str, str], Future] = {}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-localize")
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
_translator = None


def _script_note(code: str) -> str:
    return {"pa": " Write it in Gurmukhi script.", "ur": " Write it in Urdu script.", "ar": " Write it in Arabic script."}.get(code, "")


def _translate(line: str, code: str) -> str:
    global _translator
    from ai_services import AIServices
    from voice_languages import language_label
    if _translator is None:
        _translator = AIServices()
    model = (os.getenv("TRANSLATION_MODEL") or os.getenv("RETELL_OPENROUTER_MODEL")
             or ("openai/gpt-4.1-mini" if _translator.provider == "openrouter" else "gpt-4.1-mini"))
    timeout = float(os.getenv("TRANSLATION_TIMEOUT_SECONDS", "2.5") or 2.5)
    response = _translator.openai_client.with_options(timeout=timeout * 2, max_retries=1).chat.completions.create(
        model=model, max_tokens=200, temperature=0, extra_body=_translator._openrouter_extra_body(model),
        messages=[{"role": "system", "content": (
            f"Translate what a phone assistant says to a caller into natural spoken {language_label(code)}, as a "
            f"native speaker would say it on the phone.{_script_note(code)} Keep words callers say in English (shop "
            "and product words) as the English word written in that script, and keep anything in curly braces, such "
            "as {destination}, exactly as it is. Say \"we\" rather than \"I\" where the language would otherwise show the "
            "speaker's gender. Reply with the translation only.")},
            {"role": "user", "content": line}])
    text = " ".join((response.choices[0].message.content or "").split()).strip().strip('"')
    # A line with {destination} must keep it, or the name could not be filled in.
    if not text or set(_PLACEHOLDER.findall(line)) != set(_PLACEHOLDER.findall(text)):
        raise ValueError("translation lost the line's placeholders")
    return text


def _run(key: Tuple[str, str]) -> str:
    try:
        # A prefix line ("A live transfer isn't available right now. ") keeps its trailing space.
        text = _translate(key[0], key[1]) + (" " if key[0].endswith(" ") else "")
        with _LOCK:
            _CACHE[key] = text
        return text
    except Exception as error:
        logger.warning("voice line could not be written in %s (%s); English is said", key[1], type(error).__name__)
        return ""
    finally:
        with _LOCK:
            _PENDING.pop(key, None)


def _needs_model(code: str) -> bool:
    return code != "en"


def _start(key: Tuple[str, str]) -> Future:
    with _LOCK:
        future = _PENDING.get(key)
        if future is None:
            future = _PENDING[key] = _POOL.submit(_run, key)
        return future


def localized(line: str, language: str = "en", wait: float = 0.0) -> str:
    """`line` in the call's language: the cached translation, else the English line (translation started for next
    time). `wait` > 0 waits up to that many seconds for it; use it only off the event loop."""
    from voice_languages import normalize_language
    code = normalize_language(language)
    if not line or not _needs_model(code):
        return line
    key = (line, code)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    future = _start(key)
    if wait > 0:
        try:
            return future.result(timeout=wait) or line
        except Exception:
            return line
    return line


def warm(lines: Iterable[str], language: str = "en") -> None:
    """Start translating these lines for `language` in the background (already cached ones are skipped)."""
    from voice_languages import normalize_language
    code = normalize_language(language)
    if not _needs_model(code):
        return
    for line in lines:
        if line:
            key = (line, code)
            with _LOCK:
                cached = key in _CACHE
            if not cached:
                _start(key)
