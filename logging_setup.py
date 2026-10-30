"""One logging setup for the whole backend.

Every line looks like:
    2026-10-17 21:08:19 INFO  [CA1f2e3d] routers.custom_twilio_voice_api: custom voice turn ...
The bracketed id is the call (or request) the line belongs to, so one call can be followed with
    tail_logs.ps1 -Filter CA1f2e3d
Logs go to the console and to logs/app.log (UTF-8, rotated at 10 MB, 5 files kept; LOG_DIR overrides the folder).
"""

from __future__ import annotations

import contextvars
import atexit
import logging
import os
import queue
import sys
import time
from contextlib import contextmanager
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Iterator, Optional

_call_id: contextvars.ContextVar[str] = contextvars.ContextVar("call_id", default="-")
FORMAT = "%(asctime)s %(levelname)-5s [%(call_id)s] %(name)s: %(message)s"
NOISY_LIBRARIES = ("pymongo", "httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "asyncio", "websockets",
                   "multipart", "watchfiles", "hpack", "openai", "twilio.http_client", "python_multipart")


class _CallIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = _call_id.get()
        return True


def bind_call(call_id: Optional[str]) -> None:
    """Tag every following log line in this request/websocket with the call id (last 8 characters)."""
    _call_id.set(str(call_id)[-8:] if call_id else "-")


@contextmanager
def timed(logger: logging.Logger, what: str, warn_after_ms: float = 3000) -> Iterator[None]:
    """Log how long an external call took; warn when it is slow, log the error when it fails."""
    started = time.perf_counter()
    try:
        yield
    except Exception as error:
        logger.warning("%s failed after %.0fms: %s", what, (time.perf_counter() - started) * 1000, error)
        raise
    elapsed = (time.perf_counter() - started) * 1000
    logger.log(logging.WARNING if elapsed > warn_after_ms else logging.INFO, "%s took %.0fms", what, elapsed)


def _start_sink_worker(handlers):
    """File rotation and stdout flushes must never hold the live audio loop."""
    queued = QueueHandler(queue.SimpleQueue())
    queued.addFilter(_CallIdFilter())  # capture request/call context before crossing threads
    listener = QueueListener(queued.queue, *handlers, respect_handler_level=True)
    listener.start()
    return queued, listener


def setup_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_relaydesk_configured", False):
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    formatter = logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    log_dir = Path(os.getenv("LOG_DIR") or Path(__file__).resolve().parent / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    handlers = [console, file_handler]
    for handler in handlers:
        handler.setFormatter(formatter)

    queued, listener = _start_sink_worker(handlers)
    root.handlers = [queued]
    root._relaydesk_listener = listener  # type: ignore[attr-defined]
    atexit.register(listener.stop)  # drain accepted records before the process exits
    root.setLevel(level)
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Uvicorn's own loggers use our format and file; per-request lines come from our middleware instead.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers, logger.propagate = [], True
    logging.getLogger("uvicorn.access").disabled = True
    root._relaydesk_configured = True  # type: ignore[attr-defined]
