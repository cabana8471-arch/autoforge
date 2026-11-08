"""
Agent Action Approvals
======================

The assistant's agent_start / agent_stop tools wait here for the user's click in
the assistant panel (user decision 2A). The assistant reads project files the
coding agent writes, so a decision has to come from the browser: only an
``agent_action_decision`` WebSocket message resolves a request, never text the
model or the chat produces. No answer within the timeout counts as declined.
"""

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_SECONDS = 300.0

Notifier = Callable[[dict[str, Any]], Awaitable[None]]


class AgentActionApprovals:
    """Pending approval requests of one assistant session."""

    def __init__(self, timeout: float = APPROVAL_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._notifier: Notifier | None = None
        self._pending: dict[str, tuple[dict[str, Any], asyncio.Future[bool]]] = {}

    def pending(self) -> list[dict[str, Any]]:
        """Approval request messages still waiting for a decision."""
        return [message for message, _ in self._pending.values()]

    async def attach(self, notifier: Notifier) -> None:
        """Send requests to this client from now on, starting with the pending ones."""
        self._notifier = notifier
        for message in self.pending():
            await self._notify(message)

    def detach(self, notifier: Notifier) -> None:
        if self._notifier == notifier:
            self._notifier = None

    async def request(self, action: str, details: dict[str, Any]) -> bool:
        """Ask the user to approve ``action``; True only after an Approve click."""
        request_id = uuid.uuid4().hex
        message = {"type": "agent_action_approval", "id": request_id, "action": action, "details": details}
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (message, future)
        approved = False
        try:
            await self._notify(message)
            approved = await asyncio.wait_for(asyncio.shield(future), self._timeout)
        except asyncio.TimeoutError:
            logger.info(f"Agent {action} not approved within {self._timeout:.0f} s")
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.set_result(False)
            await self._notify({"type": "agent_action_approval_done", "id": request_id, "approved": approved})
        return approved

    def resolve(self, request_id: str, approved: bool) -> bool:
        """Record the user's decision; False when no such request is waiting."""
        entry = self._pending.get(request_id)
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(approved)
        return True

    def cancel_all(self) -> None:
        """Decline every pending request (the session is closing)."""
        for _, future in self._pending.values():
            if not future.done():
                future.set_result(False)

    async def _notify(self, message: dict[str, Any]) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier(message)
        except Exception as e:  # a closed socket must not approve or crash the tool
            logger.info(f"Could not deliver agent approval message: {e}")
