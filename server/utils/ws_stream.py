"""
Forward a streamed Claude turn to a WebSocket without abandoning it.
"""

import asyncio
import logging
from typing import AsyncGenerator

from fastapi import WebSocket

logger = logging.getLogger(__name__)


async def forward_chunks(
    websocket: WebSocket,
    chunks: AsyncGenerator[dict, None],
    cancel: asyncio.Event | None = None,
) -> bool:
    """
    Forward streamed chunks to the WebSocket.

    If the client goes away mid-response (panel closed, tab hidden, network
    blip), keep consuming the stream so Claude's turn still completes: the
    answer is persisted, files and features it writes are not cut off half-way,
    and the session's SDK stream is not left with unread messages.

    When ``cancel`` is set (the client closed the chat on purpose), the turn is
    stopped at its next chunk instead; the caller then closes the session.

    Returns:
        True if the WebSocket is still usable, False if it disconnected.
    """
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
