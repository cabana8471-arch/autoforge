"""
Unit tests for onboarding an existing codebase (issue #134).

Covers:
- The PreToolUse hook that limits onboarding writes to app_spec.txt
- feature_create_bulk's optional "passes" flag, accepted only in the onboarding
  session (user decision 1A)
- The onboarding session's tools, MCP environment and spec-path checks
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import mcp_server.feature_mcp as feature_mcp
from api.database import Feature, create_database
from env_constants import ONBOARD_SESSION_ENV
from server.services import expand_chat_session
from server.services.expand_chat_session import ExpandChatSession, make_spec_only_write_hook


def _run_hook(hook, tool_name: str, tool_input: dict) -> dict:
    return asyncio.run(hook({"tool_name": tool_name, "tool_input": tool_input}))


class TestSpecOnlyWriteHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name).resolve()
        self.spec = self.project / ".autoforge" / "prompts" / "app_spec.txt"
        self.spec.parent.mkdir(parents=True)
        self.hook = make_spec_only_write_hook(self.spec)

    def tearDown(self):
        self.tmp.cleanup()

    def test_allows_writing_spec(self):
        assert _run_hook(self.hook, "Write", {"file_path": str(self.spec)}) == {}
        assert _run_hook(self.hook, "Edit", {"file_path": str(self.spec)}) == {}

    def test_blocks_source_files(self):
        result = _run_hook(self.hook, "Write", {"file_path": str(self.project / "src" / "app.ts")})
        assert result["decision"] == "block"
        result = _run_hook(self.hook, "Edit", {"file_path": str(self.project / "README.md")})
        assert result["decision"] == "block"

    def test_blocks_traversal_and_missing_path(self):
        sneaky = self.spec.parent / ".." / ".." / "app_spec.txt"
        assert _run_hook(self.hook, "Write", {"file_path": str(sneaky)})["decision"] == "block"
        assert _run_hook(self.hook, "Write", {})["decision"] == "block"
        assert _run_hook(self.hook, "NotebookEdit", {"notebook_path": "x.ipynb"})["decision"] == "block"

    def test_blocks_relative_paths(self):
        # The CLI resolves relative paths against its cwd (the project root), not the
        # spec folder, so "app_spec.txt" would land in the project root
        assert _run_hook(self.hook, "Write", {"file_path": "app_spec.txt"})["decision"] == "block"
        assert _run_hook(self.hook, "Write", {"file_path": ".autoforge/prompts/app_spec.txt"})["decision"] == "block"

    def test_ignores_other_tools(self):
        assert _run_hook(self.hook, "Read", {"file_path": str(self.project / "src" / "app.ts")}) == {}

    def test_blocks_spec_replaced_by_symlink(self):
        # A symlink planted at the spec path later must not let the write escape
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("keep")
        self.spec.symlink_to(outside)
        assert _run_hook(self.hook, "Write", {"file_path": str(self.spec)})["decision"] == "block"


class TestBulkCreatePasses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine, self.session_maker = create_database(Path(self.tmp.name))
        self._orig = feature_mcp._session_maker
        feature_mcp._session_maker = self.session_maker

    def tearDown(self):
        feature_mcp._session_maker = self._orig
        self.engine.dispose()
        self.tmp.cleanup()

    def _feature(self, name: str, **extra) -> dict:
        return {"category": "functional", "name": name, "description": "d", "steps": ["s"], **extra}

    def _names_to_passes(self) -> dict[str, bool]:
        session = self.session_maker()
        try:
            return {f.name: f.passes for f in session.query(Feature).all()}
        finally:
            session.close()

    def test_passes_flag_marks_existing_features_in_onboarding(self):
        with patch.dict(os.environ, {ONBOARD_SESSION_ENV: "1"}):
            result = json.loads(feature_mcp.feature_create_bulk([
                self._feature("existing", passes=True),
                self._feature("new"),
            ]))
        assert result["created"] == 2
        assert self._names_to_passes() == {"existing": True, "new": False}

    def test_passes_true_rejected_outside_onboarding(self):
        # Initializer, assistant and expand sessions share this tool
        with patch.dict(os.environ, {ONBOARD_SESSION_ENV: ""}):
            result = json.loads(feature_mcp.feature_create_bulk([
                self._feature("new"),
                self._feature("claims to work", passes=True),
            ]))
        assert "only allowed during project onboarding" in result["error"]
        assert self._names_to_passes() == {}  # nothing created from a rejected batch

    def test_passes_false_allowed_everywhere(self):
        result = json.loads(feature_mcp.feature_create_bulk([self._feature("new", passes=False)]))
        assert result["created"] == 1

    def test_rejects_non_boolean_passes(self):
        with patch.dict(os.environ, {ONBOARD_SESSION_ENV: "1"}):
            result = json.loads(feature_mcp.feature_create_bulk([self._feature("bad", passes="yes")]))
        assert "error" in result


class _CapturingClient:
    """Stand-in for ClaudeSDKClient that records options and refuses to connect."""

    captured: dict[str, Any] = {}

    def __init__(self, options: Any) -> None:
        _CapturingClient.captured["options"] = options

    async def __aenter__(self) -> "_CapturingClient":
        options = _CapturingClient.captured["options"]
        _CapturingClient.captured["prompt"] = Path(options.system_prompt["path"]).read_text(encoding="utf-8")
        raise RuntimeError("stop after capture")

    async def __aexit__(self, *args: Any) -> None:
        return None


def _start(project: Path, mode: str) -> tuple[list[dict], dict[str, Any]]:
    session = ExpandChatSession("demo", project, mode)  # type: ignore[arg-type]
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
    return chunks, _CapturingClient.captured


class TestOnboardSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_onboard_session_enables_passes_and_spec_writes_only(self):
        _, captured = _start(self.project, "onboard")
        options = captured["options"]
        assert options.mcp_servers["features"]["env"][ONBOARD_SESSION_ENV] == "1"
        assert "Write" in options.allowed_tools and "Edit" in options.allowed_tools
        assert "Bash" in options.disallowed_tools and "MultiEdit" in options.disallowed_tools
        assert "PreToolUse" in options.hooks
        assert "ONLY for `.autoforge/prompts/app_spec.txt`" in captured["prompt"]
        # The analyzed codebase is untrusted input: no web tools to send data out
        for tool in ("WebFetch", "WebSearch"):
            assert tool not in options.allowed_tools
            assert tool in options.disallowed_tools

    def test_expand_session_does_not_enable_passes(self):
        prompts = self.project / ".autoforge" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "app_spec.txt").write_text("<project_specification/>", encoding="utf-8")
        _, captured = _start(self.project, "expand")
        options = captured["options"]
        assert ONBOARD_SESSION_ENV not in options.mcp_servers["features"]["env"]
        assert "Write" not in options.allowed_tools
        assert not options.hooks

    def test_onboarding_refuses_symlinked_spec(self):
        prompts = self.project / ".autoforge" / "prompts"
        prompts.mkdir(parents=True)
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("keep")
        (prompts / "app_spec.txt").symlink_to(outside)

        chunks, captured = _start(self.project, "onboard")

        assert any("symlink" in c.get("content", "") for c in chunks if c.get("type") == "error")
        assert "options" not in captured  # never reached the client
        assert outside.read_text() == "keep"


if __name__ == "__main__":
    unittest.main()
