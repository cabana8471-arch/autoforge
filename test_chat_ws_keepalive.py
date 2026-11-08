"""
Chat WebSockets stay open during long responses
===============================================

The spec, expand and assistant chat handlers stream a Claude turn from inside
their receive loop. The UI sends {"type": "ping"} every 30 s. Uvicorn stops
reading the socket while a received message waits unconsumed, so during a long
turn its own keepalive pongs went unread and it closed the connection
("keepalive ping timeout") ~20 s later: the onboard chat hung on
"Claude is thinking" after ~120 s in the browser.

These tests run the real routers under a real uvicorn server with short
keepalive timings and a fake session whose turn outlasts them.
"""

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from websockets.sync.client import connect

from server.routers import assistant_chat, expand_project, spec_creation

KEEPALIVE = 0.2  # uvicorn ws_ping_interval and ws_ping_timeout, seconds
TURN_SECONDS = 2.0  # far longer than interval + timeout


class FakeSession:
    """Session double: a quick greeting, then one slow turn."""

    mode = "expand"
    is_ready = False

    def __init__(self):
        self.turn_finished = threading.Event()
        self.interrupted = threading.Event()
        self._interrupt: asyncio.Event | None = None

    async def start(self):
        yield {"type": "text", "content": "hello"}
        yield {"type": "response_done"}

    async def send_message(self, content, attachments=None):
        self._interrupt = asyncio.Event()
        yield {"type": "text", "content": "working"}
        try:
            # Like the CLI: an interrupt ends the turn early, without the rest of the answer.
            await asyncio.wait_for(self._interrupt.wait(), TURN_SECONDS)
            yield {"type": "response_done"}
            return
        except asyncio.TimeoutError:
            pass
        yield {"type": "text", "content": "done"}
        yield {"type": "response_done"}
        self.turn_finished.set()

    async def interrupt(self):
        self.interrupted.set()
        if self._interrupt is not None:
            self._interrupt.set()

    def get_features_created(self):
        return 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def chat_server(tmp_path: Path, monkeypatch):
    project_dir = tmp_path / "proj"
    (project_dir / ".autoforge" / "prompts").mkdir(parents=True)
    (project_dir / ".autoforge" / "prompts" / "app_spec.txt").write_text("<spec/>", encoding="utf-8")

    session = FakeSession()
    removed: list[str] = []

    async def create(*args, **kwargs):
        return session

    async def remove(name):
        removed.append(name)

    for module in (expand_project, spec_creation, assistant_chat):
        monkeypatch.setattr(module, "_get_project_path", lambda name: project_dir)
    monkeypatch.setattr(expand_project, "create_expand_session", create)
    monkeypatch.setattr(expand_project, "get_expand_session", lambda name: None)
    monkeypatch.setattr(spec_creation, "create_session", create)
    monkeypatch.setattr(spec_creation, "get_session", lambda name: None)
    monkeypatch.setattr(expand_project, "remove_expand_session", remove)
    monkeypatch.setattr(spec_creation, "remove_session", remove)
    monkeypatch.setattr(assistant_chat, "create_session", create)
    monkeypatch.setattr(assistant_chat, "get_session", lambda name: None)

    app = FastAPI()
    for module in (expand_project, spec_creation, assistant_chat):
        app.include_router(module.router)

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        ws="auto", ws_ping_interval=KEEPALIVE, ws_ping_timeout=KEEPALIVE,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    yield f"ws://127.0.0.1:{port}", session, removed
    server.should_exit = True
    thread.join(timeout=10)


def _read_until_done(ws) -> list[dict]:
    messages = []
    while True:
        msg = json.loads(ws.recv(timeout=TURN_SECONDS * 5))
        messages.append(msg)
        if msg["type"] == "response_done":
            return messages


@pytest.mark.parametrize("path", [
    "/api/expand/ws/proj?mode=expand",
    "/api/spec/ws/proj",
    "/api/assistant/ws/proj",
])
def test_long_turn_survives_client_ping(chat_server, path):
    url, _, _ = chat_server
    with connect(url + path) as ws:
        ws.send(json.dumps({"type": "start"}))
        _read_until_done(ws)

        ws.send(json.dumps({"type": "message", "content": "analyze"}))
        ws.send(json.dumps({"type": "ping"}))  # the UI's 30 s keep-alive, mid-turn

        turn = _read_until_done(ws)
        assert [m["content"] for m in turn if m["type"] == "text"] == ["working", "done"]

        # The ping queued during the turn is still answered afterwards.
        assert json.loads(ws.recv(timeout=5)) == {"type": "pong"}


def test_flooding_client_is_disconnected(chat_server):
    """Draining the socket must not buffer without bound while a turn runs."""
    from server.utils.ws_inbox import MAX_PENDING_MESSAGES

    url, _, _ = chat_server
    with connect(url + "/api/expand/ws/proj?mode=expand") as ws:
        ws.send(json.dumps({"type": "start"}))
        _read_until_done(ws)
        ws.send(json.dumps({"type": "message", "content": "analyze"}))
        closed = False
        try:
            for _ in range(MAX_PENDING_MESSAGES + 10):
                ws.send(json.dumps({"type": "ping"}))
            while True:
                ws.recv(timeout=TURN_SECONDS * 5)
        except Exception as exc:  # ConnectionClosed
            closed = True
            assert getattr(exc, "rcvd", None) is not None and exc.rcvd.code == 1008
        assert closed


def _start_turn(ws) -> None:
    ws.send(json.dumps({"type": "start"}))
    _read_until_done(ws)
    ws.send(json.dumps({"type": "message", "content": "analyze"}))
    assert json.loads(ws.recv(timeout=5))["content"] == "working"


@pytest.mark.parametrize("path", [
    "/api/expand/ws/proj?mode=expand",
    "/api/spec/ws/proj",
    "/api/assistant/ws/proj",
])
def test_turn_completes_when_connection_drops_mid_turn(chat_server, path):
    """A network drop (no close frame) must not abandon Claude's turn half-way."""
    url, session, removed = chat_server
    ws = connect(url + path)
    _start_turn(ws)
    ws.socket.shutdown(socket.SHUT_RDWR)  # connection lost, like a network drop
    ws.socket.close()
    assert session.turn_finished.wait(timeout=TURN_SECONDS * 5)
    assert removed == []
    assert not session.interrupted.is_set()


@pytest.mark.parametrize("path", [
    "/api/expand/ws/proj?mode=expand",
    "/api/spec/ws/proj",
])
def test_reload_or_closed_tab_mid_turn_lets_it_finish(chat_server, path):
    """A reload or a closed tab (1001, going away) is not a cancel: the turn finishes on the server."""
    url, session, removed = chat_server
    ws = connect(url + path)
    _start_turn(ws)
    ws.close(code=1001)
    assert session.turn_finished.wait(timeout=TURN_SECONDS * 5)
    assert removed == []
    assert not session.interrupted.is_set()


@pytest.mark.parametrize("path", [
    "/api/expand/ws/proj?mode=expand",
    "/api/spec/ws/proj",
])
def test_closing_the_chat_mid_turn_cancels_it(chat_server, path):
    """Closing the chat on purpose (close frame) stops the turn and closes the session."""
    url, session, removed = chat_server
    with connect(url + path) as ws:
        _start_turn(ws)
    # The turn is interrupted right away (not at Claude's next message) and the session is closed.
    deadline = time.monotonic() + TURN_SECONDS / 2
    while not removed and time.monotonic() < deadline:
        time.sleep(0.05)
    assert session.interrupted.is_set()
    assert removed == ["proj"]
    assert not session.turn_finished.is_set()


def test_closing_the_assistant_mid_turn_still_persists_the_answer(chat_server):
    """The assistant keeps finishing (and storing) its answer when the panel closes."""
    url, session, removed = chat_server
    with connect(url + "/api/assistant/ws/proj") as ws:
        _start_turn(ws)
    assert session.turn_finished.wait(timeout=TURN_SECONDS * 5)
    assert removed == []
    assert not session.interrupted.is_set()


class _RecordingClient:
    def __init__(self):
        self.calls: list[str] = []

    async def query(self, _message):
        self.calls.append("query")

    async def interrupt(self):
        self.calls.append("interrupt")

    async def receive_response(self):
        from claude_agent_sdk import ResultMessage

        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
        )


@pytest.mark.parametrize("session_cls", ["expand", "spec"])
def test_interrupt_requested_before_the_client_is_ready_still_stops_the_turn(tmp_path, session_cls):
    """Closing the chat while the session is still starting its CLI must interrupt the turn once it runs."""
    from server.services.expand_chat_session import ExpandChatSession
    from server.services.spec_chat_session import SpecChatSession

    session = ExpandChatSession("demo", tmp_path) if session_cls == "expand" else SpecChatSession("demo", tmp_path)

    async def run():
        await session.interrupt()  # no client yet: the request must be remembered
        client = _RecordingClient()
        session.client = client
        session._client_entered = True
        [chunk async for chunk in session._query_claude("go")]
        return client.calls

    assert asyncio.run(run()) == ["query", "interrupt"]
