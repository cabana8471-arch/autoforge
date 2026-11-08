"""
Forward a streamed Claude turn to a WebSocket without abandoning it.
"""

import asyncio
import contextlib
import logging
from typing import AsyncGenerator, Awaitable, Callable

from fastapi import WebSocket

logger = logging.getLogger(__name__)


async def forward_chunks(
    websocket: WebSocket,
    chunks: AsyncGenerator[dict, None],
    cancel: asyncio.Event | None = None,
    interrupt: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """
    Forward streamed chunks to the WebSocket.

    If the client goes away mid-response (panel closed, tab hidden, network
    blip), keep consuming the stream so Claude's turn still completes: the
    answer is persisted, files and features it writes are not cut off half-way,
    and the session's SDK stream is not left with unread messages.

    When ``cancel`` is set (the client closed the chat on purpose), ``interrupt``
    is called right away so Claude stops working, and the turn is stopped at its
    next chunk at the latest; the caller then closes the session.

    Returns:
        True if the WebSocket is still usable, False if it disconnected.
    """
    watcher: asyncio.Task[None] | None = None
    if cancel is not None and interrupt is not None:
        watcher = asyncio.create_task(_interrupt_when_set(cancel, interrupt))
    try:
        return await _forward(websocket, chunks, cancel)
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


async def _interrupt_when_set(cancel: asyncio.Event, interrupt: Callable[[], Awaitable[None]]) -> None:
    await cancel.wait()
    logger.info("Chat closed by the client on purpose; interrupting the turn")
    await interrupt()


async def _forward(websocket: WebSocket, chunks: AsyncGenerator[dict, None], cancel: asyncio.Event | None) -> bool:
    connected = True
    async for chunk in chunks:
        if connected:
            try:
                await websocket.send_json(chunk)
                continue
            except Exception:
                connected = False
                logger.info("Chat WebSocket closed mid-response")
        if cancel is not None and cancel.is_set():
            logger.info("Chat closed by the client on purpose; stopping the turn")
            await chunks.aclose()
            break
    return connected
