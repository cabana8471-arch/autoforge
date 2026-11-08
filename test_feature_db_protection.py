"""
The agent cannot write the feature database
===========================================

``features.db`` holds the feature states, the spend records the cost cap reads
(``agent_usage``) and the test evidence. The agent changes it only through the
feature MCP server, which runs outside the Bash sandbox (user decision
08.11.2026). So:

- the agent's settings make every features.db location (current, legacy
  ``.autocoder/`` and legacy root) and its SQLite side files read-only for
  sandboxed Bash (``sandbox.filesystem.denyWrite``) and deny the file tools on
  them (``Edit(//<abs path>)``);
- the spec chat session, which may write project files, denies them too.

Checked live with the CLI (08.11.2026): sqlite3/python writes, ``>>``, ``rm``
and ``mv`` are refused, missing side files cannot be created, nothing is left
behind on disk, and a writer outside the sandbox keeps working.

Run with: python -m pytest test_feature_db_protection.py
"""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from security import cli_absolute_rule, protected_config_paths, protected_database_paths


class _AbortingClient:
    """Stand-in for ClaudeSDKClient: records the options and aborts the session start."""

    captured: list = []

    def __init__(self, options: Any = None, **_kwargs: Any) -> None:
        _AbortingClient.captured.append(options)

    async def __aenter__(self) -> "_AbortingClient":
        raise RuntimeError("stop after capture")

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    (folder / ".autoforge").mkdir(parents=True)
    return folder


def test_database_paths_cover_every_layout_and_side_file(project: Path) -> None:
    root = project.resolve()
    expected = [
        str(root / base / f"features.db{suffix}")
        for base in (".autoforge", ".autocoder", "")
        for suffix in ("", "-wal", "-shm", "-journal")
    ]
    assert protected_database_paths(project) == expected


def test_agent_settings_make_the_database_read_only(project: Path) -> None:
    import client

    with (
        patch.object(client, "ClaudeSDKClient", _AbortingClient),
        patch("registry.get_effective_sdk_env", return_value={}),
        patch("registry.get_effort_setting", return_value=None),
    ):
        client.create_client(project, "claude-opus-4-6")

    settings = json.loads((project / ".autoforge" / ".claude_settings.json").read_text(encoding="utf-8"))
    protected = [*protected_config_paths(project), *protected_database_paths(project)]
    assert settings["sandbox"]["filesystem"]["denyWrite"] == protected
    assert settings["permissions"]["deny"] == [cli_absolute_rule("Edit", Path(path)) for path in protected]


def test_spec_chat_denies_the_file_tools_on_the_database(project: Path) -> None:
    from server.services import spec_chat_session

    _AbortingClient.captured = []
    with (
        patch.object(spec_chat_session, "ClaudeSDKClient", _AbortingClient),
        patch.object(spec_chat_session, "find_claude_cli", return_value="/usr/bin/claude"),
        patch("registry.get_effective_sdk_env", return_value={}),
        patch("registry.get_effort_setting", return_value=None),
    ):
        session = spec_chat_session.SpecChatSession("demo", project)

        async def drain() -> list:
            return [chunk async for chunk in session.start()]

        asyncio.run(drain())

    assert len(_AbortingClient.captured) == 1
    settings = json.loads(Path(_AbortingClient.captured[0].settings).read_text(encoding="utf-8"))
    for path in protected_database_paths(project):
        assert cli_absolute_rule("Edit", Path(path)) in settings["permissions"]["deny"]


def _bash(command: str, project: Path) -> dict:
    from security import validate_bash_tool_use

    result: dict = validate_bash_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": command}}, {"project_dir": str(project)}
    )
    return result


@pytest.mark.parametrize(
    "command",
    [
        "sqlite3 .autoforge/features.db 'select * from features'",
        "python3 -c \"import sqlite3; sqlite3.connect('.autoforge/features.db')\"",
        "cp .autoforge/features.db /tmp/copy.db",
        "echo x >> features.db",
    ],
)
def test_bash_on_the_database_gets_the_reason_up_front(project: Path, command: str) -> None:
    """Even a read fails in the sandbox (SQLite writes its -wal/-shm side files), so say why at once."""
    result = _bash(command, project)
    assert result.get("decision") == "block"
    assert "feature_" in result["reason"]


def test_listing_the_database_is_allowed(project: Path) -> None:
    assert _bash("ls -la .autoforge/features.db", project) == {}
