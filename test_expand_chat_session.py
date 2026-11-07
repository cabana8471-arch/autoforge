"""
Expand Chat Session Tests
=========================

Verifies that features created through the feature MCP tools during an
expand-project chat are reported to the UI as ``features_created`` events,
and that the session grants and advertises the web tools (WebFetch/WebSearch,
issue #86) while keeping file mutation and Bash disabled.

Run with: python -m pytest test_expand_chat_session.py
"""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from server.services import expand_chat_session
from server.services.expand_chat_session import ExpandChatSession, parse_created_features

BULK = "mcp__features__feature_create_bulk"
SINGLE = "mcp__features__feature_create"

BULK_INPUT = {
    "features": [
        {"category": "style", "name": "Dark theme toggle", "description": "d", "steps": ["s"]},
        {"category": "functional", "name": "Theme persists", "description": "d", "steps": ["s"]},
    ]
}


def _text_content(payload: dict) -> list[dict]:
    return [{"type": "text", "text": json.dumps(payload)}]


def test_parse_bulk_with_ids():
    result = _text_content({"created": 2, "with_dependencies": 0, "feature_ids": [7, 8]})
    assert parse_created_features(BULK, BULK_INPUT, result) == [
        {"id": 7, "name": "Dark theme toggle", "category": "style"},
        {"id": 8, "name": "Theme persists", "category": "functional"},
    ]


def test_parse_bulk_without_ids_string_content():
    result = json.dumps({"created": 2, "with_dependencies": 0})
    features = parse_created_features(BULK, BULK_INPUT, result)
    assert [f["id"] for f in features] == [None, None]
    assert [f["name"] for f in features] == ["Dark theme toggle", "Theme persists"]


def test_parse_single():
    result = _text_content({
        "success": True,
        "feature": {"id": 3, "name": "Export CSV", "category": "data"},
    })
    assert parse_created_features(SINGLE, {}, result) == [
        {"id": 3, "name": "Export CSV", "category": "data"},
    ]


def test_parse_errors_and_garbage():
    assert parse_created_features(BULK, BULK_INPUT, _text_content({"error": "missing fields"})) == []
    assert parse_created_features(BULK, BULK_INPUT, "not json") == []
    assert parse_created_features(BULK, BULK_INPUT, None) == []
    assert parse_created_features(BULK, BULK_INPUT, _text_content({"created": 0})) == []


class _FakeClient:
    def __init__(self, messages):
        self._messages = messages

    async def query(self, _message):
        pass

    async def receive_response(self):
        for msg in self._messages:
            yield msg


def _run_query(messages) -> tuple[ExpandChatSession, list[dict]]:
    session = ExpandChatSession("demo", Path("."))
    session.client = _FakeClient(messages)  # type: ignore[assignment]

    async def collect():
        return [chunk async for chunk in session._query_claude("go")]

    return session, asyncio.run(collect())


def _result_message():
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s",
    )


def test_query_emits_features_created():
    session, chunks = _run_query([
        AssistantMessage(content=[
            TextBlock(text="Great! I'll create these 2 features now."),
            ToolUseBlock(id="tu1", name=BULK, input=BULK_INPUT),
        ], model="m"),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="tu1",
            content=_text_content({"created": 2, "with_dependencies": 0, "feature_ids": [7, 8]}),
        )]),
        _result_message(),
    ])

    created = [c for c in chunks if c["type"] == "features_created"]
    assert len(created) == 1
    assert created[0]["count"] == 2
    assert session.get_features_created() == 2
    assert session.created_feature_ids == [7, 8]


def test_query_ignores_failed_and_unrelated_tools():
    session, chunks = _run_query([
        AssistantMessage(content=[
            ToolUseBlock(id="tu1", name=BULK, input=BULK_INPUT),
            ToolUseBlock(id="tu2", name="Read", input={"file_path": "x"}),
        ], model="m"),
        UserMessage(content=[
            ToolResultBlock(tool_use_id="tu1", content="MCP server not connected", is_error=True),
            ToolResultBlock(tool_use_id="tu2", content=json.dumps({"created": 5})),
        ]),
        _result_message(),
    ])

    assert not [c for c in chunks if c["type"] == "features_created"]
    assert session.get_features_created() == 0


def test_feature_create_bulk_returns_feature_ids(tmp_path, monkeypatch):
    import mcp_server.feature_mcp as feature_mcp
    from api.database import create_database

    engine, session_maker = create_database(tmp_path)
    monkeypatch.setattr(feature_mcp, "_session_maker", session_maker)
    try:
        result = json.loads(feature_mcp.feature_create_bulk(features=BULK_INPUT["features"]))
        session = session_maker()
        try:
            from api.database import Feature
            names = {f.id: f.name for f in session.query(Feature).all()}
        finally:
            session.close()
    finally:
        engine.dispose()

    assert result["created"] == 2
    assert [names[i] for i in result["feature_ids"]] == ["Dark theme toggle", "Theme persists"]


def test_parse_bulk_wrapped_in_fastmcp_result():
    # FastMCP wraps a tool's string return value as {"result": "<json>"}
    # (seen in a real expand session: server log line for feature_create_bulk)
    inner = json.dumps({"created": 2, "with_dependencies": 0, "feature_ids": [2, 3]})
    result = _text_content({"result": inner})
    assert parse_created_features(BULK, BULK_INPUT, result) == [
        {"id": 2, "name": "Dark theme toggle", "category": "style"},
        {"id": 3, "name": "Theme persists", "category": "functional"},
    ]


def test_parse_single_wrapped_in_fastmcp_result():
    inner = json.dumps({"success": True, "feature": {"id": 4, "name": "Export CSV", "category": "data"}})
    assert parse_created_features(SINGLE, {}, _text_content({"result": inner})) == [
        {"id": 4, "name": "Export CSV", "category": "data"},
    ]


# ---------------------------------------------------------------------------
# Tool permissions (#86)
# ---------------------------------------------------------------------------


class _CapturingClient:
    """Stand-in for ClaudeSDKClient that records options and refuses to connect."""

    captured: dict[str, Any] = {}

    def __init__(self, options: Any) -> None:
        _CapturingClient.captured["options"] = options

    async def __aenter__(self) -> "_CapturingClient":
        options = _CapturingClient.captured["options"]
        _CapturingClient.captured["settings"] = json.loads(Path(options.settings).read_text(encoding="utf-8"))
        # The prompt goes to the CLI via --system-prompt-file; read it before close() deletes it
        _CapturingClient.captured["prompt"] = Path(options.system_prompt["path"]).read_text(encoding="utf-8")
        raise RuntimeError("stop after capture")

    async def __aexit__(self, *args: Any) -> None:
        return None


def _start_session(tmp_path: Path) -> dict[str, Any]:
    prompts_dir = tmp_path / ".autoforge" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "app_spec.txt").write_text("<project_specification/>", encoding="utf-8")

    session = ExpandChatSession("demo", tmp_path)
    _CapturingClient.captured = {}

    async def run() -> list[dict]:
        chunks = [chunk async for chunk in session.start()]
        await session.close()
        return chunks

    # Stub the registry lookups so the test never creates ~/.autoforge/registry.db
    with patch.object(expand_chat_session, "ClaudeSDKClient", _CapturingClient), \
            patch.object(expand_chat_session, "find_claude_cli", return_value="/usr/bin/claude"), \
            patch("registry.get_effective_sdk_env", return_value={}), \
            patch("registry.get_effort_setting", return_value=None):
        chunks = asyncio.run(run())

    # The fake client aborts init, so the session must report an error
    assert any(c.get("type") == "error" for c in chunks)
    return _CapturingClient.captured


def test_web_tools_are_allowed(tmp_path: Path) -> None:
    options = _start_session(tmp_path)["options"]
    assert "WebFetch" in options.allowed_tools
    assert "WebSearch" in options.allowed_tools
    assert "WebFetch" not in options.disallowed_tools
    assert "WebSearch" not in options.disallowed_tools


def test_settings_file_allows_web_tools(tmp_path: Path) -> None:
    permissions = _start_session(tmp_path)["settings"]["permissions"]
    assert "WebFetch" in permissions["allow"]
    assert "WebSearch" in permissions["allow"]
    assert "WebFetch" not in permissions["deny"]
    assert "WebSearch" not in permissions["deny"]


def test_system_prompt_advertises_web_tools(tmp_path: Path) -> None:
    prompt = _start_session(tmp_path)["prompt"]
    assert "You ARE allowed to use WebFetch and WebSearch" in prompt
    assert str(tmp_path.resolve()) in prompt  # $ARGUMENTS still substituted


def test_mutation_tools_remain_disabled(tmp_path: Path) -> None:
    captured = _start_session(tmp_path)
    options = captured["options"]
    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
        assert tool not in options.allowed_tools
        assert tool in options.disallowed_tools
        assert tool in captured["settings"]["permissions"]["deny"]
