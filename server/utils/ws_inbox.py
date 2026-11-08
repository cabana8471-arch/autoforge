"""
Keep reading a WebSocket while its handler is busy.

The chat handlers stream a whole Claude turn from inside their receive loop.
Uvicorn stops reading the socket while a received message waits unconsumed
(``transport.pause_reading()``), so a client keep-alive ping sent mid-turn
blocked the reading of uvicorn's own keepalive pongs, and uvicorn closed the
connection with "keepalive ping timeout" about 20 s later.

``WebSocketInbox`` drains the socket in a background task into a bounded
in-process queue; the handler reads from the inbox instead of the socket.
"""

import asyncio
import contextlib

from fastapi import WebSocket, WebSocketDisconnect

# A well-behaved client pings every 30 s; this many unread messages means flooding.
MAX_PENDING_MESSAGES = 256
POLICY_VIOLATION = 1008
# Close codes of a client that left on purpose: the UI's ws.close(1000) and a
# closed or reloaded tab (1001). Not 1005: uvicorn reports a lost connection
# (network drop, keepalive timeout) as 1005, same as a close frame without status.
CLIENT_CLOSE_CODES = frozenset({1000, 1001})


class WebSocketInbox:
    """Receives text messages in the background: ``start()``, then ``aclose()`` in a finally."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._queue: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        # Set when the client closed the connection on purpose (not a network drop).
        self.client_left = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._pump())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _pump(self) -> None:
        try:
            while True:
                text = await self._websocket.receive_text()
                if self._queue.qsize() >= MAX_PENDING_MESSAGES:
                    await self._websocket.close(code=POLICY_VIOLATION, reason="Too many pending messages")
                    raise WebSocketDisconnect(code=POLICY_VIOLATION, reason="Too many pending messages")
                self._queue.put_nowait(text)
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect as exc:
            if exc.code in CLIENT_CLOSE_CODES:
                self.client_left.set()
            self._queue.put_nowait(exc)
        except Exception as exc:  # e.g. RuntimeError once closed
            self._queue.put_nowait(exc)

    async def receive_text(self) -> str:
        """Next text message; re-raises the error that ended the connection."""
        item = await self._queue.get()
        if isinstance(item, BaseException):
            self._queue.put_nowait(item)  # every later call raises it too
            raise item
        return item
