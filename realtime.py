"""Live updates for the dashboard over Server-Sent Events.

Anything that changes what the inbox shows calls publish(); every open dashboard receives a small event
such as {"kind": "messages"} and reloads just that view through the normal API. Events never carry
message text or call content, so the API's per-user visibility rules still decide what each person sees.

publish() is safe to call from any thread (voice providers save transcripts from worker threads).
The browser connects to GET /api/realtime/events?token=<sign-in token>; EventSource cannot send headers.
"""

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional, Set

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from auth_service import has_permission, user_from_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/realtime", tags=["realtime"])

HEARTBEAT_SECONDS = 20  # keeps proxies and phone hotspots from closing an idle stream
_loop: Optional[asyncio.AbstractEventLoop] = None
_subscribers: Set[asyncio.Queue] = set()


def attach_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at startup with the server's event loop."""
    global _loop
    _loop = loop


# Bumped on every change; caches of inbox data (inbox_api._threads) are valid only for the version they were built at.
version = 0


def publish(kind: str, **data: Any) -> None:
    """Tell every open dashboard that something of this kind changed. Never raises."""
    global version
    version += 1
    if _loop is None or _loop.is_closed():
        return
    event = {"kind": kind, **{key: value for key, value in data.items() if value is not None}}
    try:
        _loop.call_soon_threadsafe(_fan_out, event)
    except RuntimeError:  # loop shutting down
        pass


def _fan_out(event: Dict[str, Any]) -> None:
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # a stalled browser; it refreshes everything when it reconnects
            pass
    logger.debug("realtime: %s -> %d dashboards", event, len(_subscribers))


async def _stream(user: Dict[str, Any]) -> AsyncIterator[str]:
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.add(queue)
    workspace = user.get("workspace_id", "default")
    logger.info("realtime: %s connected (%d open)", user.get("email"), len(_subscribers))
    try:
        yield "retry: 3000\n\n"  # browser reconnect delay
        yield f"data: {json.dumps({'kind': 'hello'})}\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event.get("workspace") not in (None, workspace):
                continue
            yield f"data: {json.dumps(event)}\n\n"
    finally:
        _subscribers.discard(queue)
        logger.info("realtime: %s disconnected (%d open)", user.get("email"), len(_subscribers))


@router.get("/events")
async def events(token: str = Query(..., min_length=10)):
    user = user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Your session has expired")
    if not has_permission(user, "handle_conversations"):
        raise HTTPException(status_code=403, detail="Permission required: handle_conversations")
    return StreamingResponse(_stream(user), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
