"""Keep the server's memory flat over many calls: a Render instance has 512 MB, and running out of it kills the
process mid-call (Retell: error_llm_websocket_lost_connection, 2026-10-28).

- limit_malloc_arenas(): glibc gives each busy thread its own malloc arena; with the call threads and worker pools
  those arenas hold freed memory that the process never gives back. Two arenas are plenty here.
- release_after_call(): after a call ends, collect its short-lived cyclic garbage and hand freed heap pages back
  to the operating system (malloc_trim), so the next call starts from the same footprint.
- freeze_startup_objects(): after the warm-up, keep the long-lived startup objects out of garbage collection, so
  Python's own full collections no longer pause a live call.
- resident_mb(): the process's memory right now, for /ready.

Garbage collection is never run on a timer or per turn: once per finished call, young generations only.

Linux only where it says so; elsewhere (Windows development) these do nothing and cost nothing.
"""

import ctypes
import ctypes.util
import gc
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)
_M_ARENA_MAX = -8  # glibc mallopt() parameter
_libc: Any = None


def _glibc() -> Any:
    global _libc
    if _libc is None:
        _libc = False
        if sys.platform.startswith("linux"):
            try:
                _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            except OSError:
                _libc = False
    return _libc or None


def limit_malloc_arenas() -> None:
    """Call once at startup, before the worker threads start (MALLOC_ARENA_MAX overrides the default of 2)."""
    libc = _glibc()
    if libc is None or not hasattr(libc, "mallopt"):
        return
    try:
        arenas = max(1, int(os.getenv("MALLOC_ARENA_MAX", "2")))
    except ValueError:
        arenas = 2
    if libc.mallopt(_M_ARENA_MAX, arenas):
        logger.info("memory: malloc arenas limited to %d", arenas)


def freeze_startup_objects() -> None:
    """Call once when the startup warm-up has finished. The ~320,000 objects made at startup (modules, the model,
    the catalogue, classifiers) live as long as the server, yet Python's automatic full collection walked them all:
    ~220 ms in which a live call could not speak. Frozen, they are left out of every collection (~0.02 ms)."""
    gc.collect()
    gc.freeze()
    logger.info("memory: %d startup objects excluded from garbage collection", gc.get_freeze_count())


def release_after_call() -> None:
    """Run in a worker thread when a call ends. Collects only the young generations: they hold a finished call's
    objects, and a full collection could pause audio on another live call. malloc_trim runs outside the GIL."""
    before = resident_mb()
    gc.collect(1)
    libc = _glibc()
    if libc is not None and hasattr(libc, "malloc_trim"):
        libc.malloc_trim(0)
    after = resident_mb()
    if before is not None and after is not None:
        logger.info("memory after call: %d MB (released %d MB)", after, max(0, before - after))


def resident_mb() -> Optional[int]:
    """Resident memory of this process in MB (Linux), None where it cannot be read cheaply."""
    try:
        with open("/proc/self/statm") as statm:
            pages = int(statm.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 2**20)
    except (OSError, ValueError, IndexError, AttributeError):
        return None
