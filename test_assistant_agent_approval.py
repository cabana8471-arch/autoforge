"""
The assistant's agent_start / agent_stop need a click in the UI (user decision 2A).

The assistant reads project files the coding agent writes, so text planted there
must not be able to start or stop the agent. The tools wait for an approval that
only the browser can send (an ``agent_action_decision`` WebSocket message); a
text answer from the model or the user's chat message is not an approval.
"""

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import uvicorn
from fastapi import FastAPI
from websockets.sync.client import connect

from server.services.agent_action_approvals import AgentActionApprovals
from server.services.assistant_agent_tools import build_agent_control_tools
from test_assistant_agent_tools import FakeManager, _payload


def run(coro):
    return asyncio.run(coro)


# --- AgentActionApprovals ---------------------------------------------------


def test_request_notifies_and_waits_for_the_decision():
    async def scenario():
        approvals = AgentActionApprovals()
        sent: list[dict] = []

        async def notifier(msg):
            sent.append(msg)

        await approvals.attach(notifier)
        task = asyncio.create_task(approvals.request("start", {"max_concurrency": 2}))
        await asyncio.sleep(0)
        assert sent[0]["type"] == "agent_action_approval"
        assert sent[0]["action"] == "start"
        assert sent[0]["details"] == {"max_concurrency": 2}
        assert approvals.resolve(sent[0]["id"], True) is True
        assert await task is True
        assert sent[-1] == {"type": "agent_action_approval_done", "id": sent[0]["id"], "approved": True}

    run(scenario())


def test_declined_request_returns_false():
    async def scenario():
        approvals = AgentActionApprovals()
        sent: list[dict] = []

        async def notifier(msg):
            sent.append(msg)

        await approvals.attach(notifier)
        task = asyncio.create_task(approvals.request("stop", {}))
        await asyncio.sleep(0)
        approvals.resolve(sent[0]["id"], False)
        assert await task is False

    run(scenario())


def test_unanswered_request_times_out_as_declined():
    async def scenario():
        approvals = AgentActionApprovals(timeout=0.05)
        assert await approvals.request("start", {}) is False
        assert approvals.pending() == []

    run(scenario())


def test_unknown_id_is_not_an_approval():
    async def scenario():
        approvals = AgentActionApprovals(timeout=0.2)
        task = asyncio.create_task(approvals.request("start", {}))
        await asyncio.sleep(0)
        assert approvals.resolve("not-the-id", True) is False
        assert await task is False

    run(scenario())


def test_reattached_client_gets_the_pending_request_again():
    """Closing and reopening the panel must not lose the Approve/Decline prompt."""
    async def scenario():
        approvals = AgentActionApprovals()
        first: list[dict] = []
        second: list[dict] = []

        async def notify_first(msg):
            first.append(msg)

        async def notify_second(msg):
            second.append(msg)

        await approvals.attach(notify_first)
        task = asyncio.create_task(approvals.request("start", {}))
        await asyncio.sleep(0)
        approvals.detach(notify_first)
        await approvals.attach(notify_second)
        assert second[0]["id"] == first[0]["id"]
        approvals.resolve(second[0]["id"], True)
        assert await task is True

    run(scenario())


def test_cancel_all_declines_pending_requests():
    async def scenario():
        approvals = AgentActionApprovals()
        task = asyncio.create_task(approvals.request("start", {}))
        await asyncio.sleep(0)
        approvals.cancel_all()
        assert await task is False

    run(scenario())


def test_failing_notifier_does_not_approve():
    async def scenario():
        approvals = AgentActionApprovals(timeout=0.05)

        async def broken(msg):
            raise RuntimeError("socket closed")

        await approvals.attach(broken)
        assert await approvals.request("start", {}) is False

    run(scenario())


# --- Tools ------------------------------------------------------------------


class _ToolHarness:
    def __init__(self, approve):
        self.manager = FakeManager()
        self.patches = [
            patch("server.routers.agent.get_project_manager", return_value=self.manager),
            patch("server.routers.agent._get_settings_defaults", return_value=(False, "claude-opus-4-6", 1, 3, 3)),
            patch("server.routers.agent._get_project_path", return_value=None),
        ]
        for p in self.patches:
            p.start()
        self.tools = {t.name: t for t in build_agent_control_tools("demo", approve=approve)}

    def call(self, name, args=None):
        return run(self.tools[name].handler(args or {}))

    def close(self):
        for p in self.patches:
            p.stop()


@pytest.fixture
def asked():
    return []


def _approver(answer, asked):
    async def approve(action, details):
        asked.append((action, details))
        return answer
    return approve


@pytest.mark.parametrize("name", ["agent_start", "agent_stop"])
def test_declined_action_does_not_touch_the_agent(name, asked):
    h = _ToolHarness(_approver(False, asked))
    try:
        h.manager.status = "running" if name == "agent_stop" else "stopped"
        result = h.call(name)
        assert result.get("is_error")
        assert "declined" in _payload(result)["error"]
        assert h.manager.calls == []
        assert asked == [(name.removeprefix("agent_"), {})]
    finally:
        h.close()


def test_start_asks_with_the_requested_concurrency(asked):
    h = _ToolHarness(_approver(True, asked))
    try:
        assert _payload(h.call("agent_start", {"max_concurrency": 2}))["success"]
        assert asked == [("start", {"max_concurrency": 2})]
    finally:
        h.close()


def test_invalid_start_arguments_do_not_ask(asked):
    h = _ToolHarness(_approver(True, asked))
    try:
        assert h.call("agent_start", {"max_concurrency": 9}).get("is_error")
        assert asked == []
    finally:
        h.close()


@pytest.mark.parametrize("name", ["agent_start", "agent_stop"])
def test_without_an_approver_start_and_stop_are_refused(name):
    h = _ToolHarness(None)
    try:
        h.manager.status = "running" if name == "agent_stop" else "stopped"
        assert h.call(name).get("is_error")
        assert h.manager.calls == []
    finally:
        h.close()


def test_pause_resume_and_status_do_not_ask(asked):
    h = _ToolHarness(_approver(False, asked))
    try:
        h.manager.status = "running"
        h.call("agent_get_status")
        h.call("agent_pause")
        h.call("agent_resume")
        assert asked == []
    finally:
        h.close()


# --- Session ----------------------------------------------------------------


def test_closing_the_session_declines_a_pending_approval(tmp_path):
    from server.services.assistant_chat_session import AssistantChatSession

    async def scenario():
        session = AssistantChatSession("demo", tmp_path)
        task = asyncio.create_task(session.approvals.request("start", {}))
        await asyncio.sleep(0)
        await session.close()
        assert await task is False

    run(scenario())


# --- WebSocket round trip ---------------------------------------------------


class _ApprovalSession:
    """Session double whose turn calls a gated tool and reports the decision."""

    is_ready = False

    def __init__(self):
        self.approvals = AgentActionApprovals(timeout=5)

    async def start(self):
        yield {"type": "response_done"}

    async def send_message(self, content):
        approved = await self.approvals.request("start", {})
        yield {"type": "text", "content": "approved" if approved else "declined"}
        yield {"type": "response_done"}


@pytest.fixture
def approval_server(tmp_path: Path, monkeypatch):
    from server.routers import assistant_chat
    from test_chat_ws_keepalive import _free_port

    project_dir = tmp_path / "proj"
    (project_dir / ".autoforge").mkdir(parents=True)
    session = _ApprovalSession()

    async def create(*args, **kwargs):
        return session

    monkeypatch.setattr(assistant_chat, "_get_project_path", lambda name: project_dir)
    monkeypatch.setattr(assistant_chat, "create_session", create)
    monkeypatch.setattr(assistant_chat, "get_session", lambda name: None)

    app = FastAPI()
    app.include_router(assistant_chat.router)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    yield f"ws://127.0.0.1:{port}/api/assistant/ws/proj"
    server.should_exit = True
    thread.join(timeout=10)


def _recv(ws, wanted):
    while True:
        msg = json.loads(ws.recv(timeout=10))
        if msg["type"] == wanted:
            return msg


@pytest.mark.parametrize("approved", [True, False])
def test_decision_sent_mid_turn_reaches_the_waiting_tool(approval_server, approved):
    with connect(approval_server) as ws:
        ws.send(json.dumps({"type": "start"}))
        _recv(ws, "response_done")
        ws.send(json.dumps({"type": "message", "content": "start the agent"}))
        request = _recv(ws, "agent_action_approval")
        ws.send(json.dumps({"type": "agent_action_decision", "id": request["id"], "approved": approved}))
        text = _recv(ws, "text")
        assert text["content"] == ("approved" if approved else "declined")
        _recv(ws, "response_done")


def test_chat_answer_is_not_an_approval(approval_server):
    """An 'answer' (what ask_user returns) or a chat message must not resolve the request."""
    with connect(approval_server) as ws:
        ws.send(json.dumps({"type": "start"}))
        _recv(ws, "response_done")
        ws.send(json.dumps({"type": "message", "content": "start the agent"}))
        request = _recv(ws, "agent_action_approval")
        ws.send(json.dumps({"type": "answer", "answers": {"0": "Approve"}}))
        ws.send(json.dumps({"type": "agent_action_decision", "id": request["id"], "approved": "yes"}))
        ws.send(json.dumps({"type": "agent_action_decision", "id": request["id"], "approved": False}))
        assert _recv(ws, "text")["content"] == "declined"
