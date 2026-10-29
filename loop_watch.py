"""Find what holds up the server's event loop during calls.

Every call's audio, bridges and replies run on one asyncio event loop; anything slow run directly on it (a database
read, a model call, heavy work) stops all of them. A Hindi call on 2026-10-29 had the loop held up 16 times, once for
2.3 s, which a replay here never showed. A heartbeat task notes the time every 50 ms and a small thread checks it: when
the loop is held up longer than LOOP_WATCH_STALL_MS (250 ms), the thread records the code the loop is running at that
moment (sys._current_frames), logs it, and keeps the recent stalls for the calls to save with their records.
"""

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROJECT = os.path.dirname(os.path.abspath(__file__))
_STALLS: "deque[Dict[str, Any]]" = deque(maxlen=200)
_state: Dict[str, Any] = {"beat": 0.0, "thread": None, "loop_thread": None}


def _stall_ms() -> float:
    try:
        return max(50.0, float(os.getenv("LOOP_WATCH_STALL_MS", "250")))
    except ValueError:
        return 250.0


def _where(frame) -> str:
    """The code holding the loop: our innermost frames, plus the library call they are waiting in."""
    stack = traceback.extract_stack(frame)
    ours = [item for item in stack if item.filename.startswith(_PROJECT) and os.sep + ".venv" + os.sep not in item.filename]
    parts = [f"{os.path.relpath(item.filename, _PROJECT)}:{item.lineno} {item.name}" for item in ours[-3:]]
    if stack and (not ours or stack[-1] is not ours[-1]):
        innermost = stack[-1]
        parts.append(f"{os.path.basename(innermost.filename)}:{innermost.lineno} {innermost.name}")
    return " > ".join(parts) or "unknown"


def _watch() -> None:
    limit = _stall_ms() / 1000
    stalled_since = 0.0
    while True:
        time.sleep(0.05)
        beat, loop_thread = _state["beat"], _state["loop_thread"]
        if not beat or loop_thread is None:
            continue
        behind = time.monotonic() - beat
        if behind > limit and not stalled_since:
            stalled_since = beat
            frame = sys._current_frames().get(loop_thread)
            where = _where(frame) if frame is not None else "unknown"
            _STALLS.append({"at": time.time() - behind, "where": where, "ms": round(behind * 1000)})
        elif behind > limit and _STALLS:
            _STALLS[-1]["ms"] = round(behind * 1000)  # still held up: the same stall grows
        elif behind <= limit and stalled_since:
            stall = _STALLS[-1] if _STALLS else None
            if stall:
                logger.warning("event loop held up %d ms by %s", stall["ms"], stall["where"])
            stalled_since = 0.0


async def _heartbeat() -> None:
    while True:
        _state["beat"] = time.monotonic()
        await asyncio.sleep(0.05)


def start() -> None:
    """Start watching the running event loop (once per process). LOOP_WATCH=false turns it off."""
    if _state["thread"] is not None or os.getenv("LOOP_WATCH", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    _state["loop_thread"] = threading.get_ident()
    _state["beat"] = time.monotonic()
    asyncio.get_running_loop().create_task(_heartbeat())
    _state["thread"] = threading.Thread(target=_watch, name="loop-watch", daemon=True)
    _state["thread"].start()


def stalls_since(started_at: float, limit: int = 8) -> List[Dict[str, Any]]:
    """The longest loop stalls since a wall-clock time (a call's start), longest first, for its record."""
    found = [dict(stall) for stall in list(_STALLS) if stall["at"] >= started_at]
    return sorted(found, key=lambda stall: -stall["ms"])[:limit]
