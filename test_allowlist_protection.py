"""
The project allowlist is edited only by the user
=================================================

``.autoforge/allowed_commands.yaml`` widens what the agent may run, so the agent
must not edit it (user decision 08.11.2026). Three layers keep it out:

- the agent's settings deny the file tools on it (``Edit(//<abs path>)``; Edit
  rules cover Write too) and make it read-only for sandboxed Bash
  (``sandbox.filesystem.denyWrite``);
- the spec chat session (which may write project files) denies it too;
- the Bash hook refuses commands that would change it, with a clear reason.

Run with: python -m pytest test_allowlist_protection.py
"""

import asyncio
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from unittest.mock import patch

import pytest

from security import cli_absolute_rule, load_project_commands, protected_config_paths, validate_bash_tool_use


class _AbortingClient:
    """Stand-in for ClaudeSDKClient: aborts the session start."""

    def __init__(self, options: Any = None, **_kwargs: Any) -> None:
        self.options = options

    async def __aenter__(self) -> "_AbortingClient":
        raise RuntimeError("stop after capture")

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    (folder / ".autoforge").mkdir(parents=True)
    (folder / ".autoforge" / "allowed_commands.yaml").write_text("version: 1\ncommands: []\n")
    return folder


def _bash(command: str, project: Path) -> dict:
    result: dict = validate_bash_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": command}}, {"project_dir": str(project)}
    )
    return result


def test_protected_paths_are_absolute_and_cover_the_legacy_location(project: Path) -> None:
    root = project.resolve()
    assert protected_config_paths(project) == [
        str(root / ".autoforge" / "allowed_commands.yaml"),
        str(root / ".autocoder" / "allowed_commands.yaml"),
    ]


@pytest.mark.parametrize(
    "command",
    [
        "echo '  - name: python3' >> .autoforge/allowed_commands.yaml",
        "sed -i 's/commands: \\[\\]/commands: [x]/' .autoforge/allowed_commands.yaml",
        "cp /tmp/x.yaml .autoforge/allowed_commands.yaml",
        "mv new.yaml .autoforge/allowed_commands.yaml",
        "echo x | tee -a .autoforge/allowed_commands.yaml",
        "printf x > ./.autocoder/allowed_commands.yaml",
        "bash -c 'echo x >> .autoforge/allowed_commands.yaml'",
        "rm .autoforge/allowed_commands.yaml",
        "cat extra.yaml > .autoforge/allowed_commands.yaml",
    ],
)
def test_bash_cannot_change_the_allowlist(project: Path, command: str) -> None:
    result = _bash(command, project)
    assert result.get("decision") == "block"
    assert "only the user" in result["reason"].lower()


@pytest.mark.parametrize(
    "command",
    ["cat .autoforge/allowed_commands.yaml", "grep name .autoforge/allowed_commands.yaml"],
)
def test_bash_may_read_the_allowlist(project: Path, command: str) -> None:
    assert _bash(command, project) == {}


def test_not_allowed_message_tells_the_agent_to_ask_the_user(project: Path) -> None:
    result = _bash("definitely-not-a-command --x", project)
    assert result["decision"] == "block"
    assert "Ask the user" in result["reason"]
    assert "mid-session approval" not in result["reason"]


def test_agent_settings_deny_the_allowlist(project: Path) -> None:
    import client

    with (
        patch.object(client, "ClaudeSDKClient", _AbortingClient),
        patch("registry.get_effective_sdk_env", return_value={}),
        patch("registry.get_effort_setting", return_value=None),
    ):
        client.create_client(project, "claude-opus-4-6")

    settings = json.loads((project / ".autoforge" / ".claude_settings.json").read_text(encoding="utf-8"))
    # (the feature database is protected next to it: test_feature_db_protection.py)
    for path in protected_config_paths(project):
        assert path in settings["sandbox"]["filesystem"]["denyWrite"]
        assert cli_absolute_rule("Edit", Path(path)) in settings["permissions"]["deny"]
    # The CLI ignores Write/Glob/Grep(...) rules (only Edit and Read rules match file tools) and warns
    assert not any(rule.startswith(("Write(", "Glob(", "Grep(")) for rule in settings["permissions"]["allow"])


def test_extra_read_paths_are_absolute_read_rules(project: Path, tmp_path: Path, monkeypatch) -> None:
    """``Read(/x)`` is project-relative for the CLI; an absolute path needs ``Read(//x)`` (checked live)."""
    import client

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setenv(client.EXTRA_READ_PATHS_VAR, str(docs))
    with (
        patch.object(client, "ClaudeSDKClient", _AbortingClient),
        patch("registry.get_effective_sdk_env", return_value={}),
        patch("registry.get_effort_setting", return_value=None),
    ):
        client.create_client(project, "claude-opus-4-6")

    settings = json.loads((project / ".autoforge" / ".claude_settings.json").read_text(encoding="utf-8"))
    assert f"Read(/{docs.resolve().as_posix()}/**)" in settings["permissions"]["allow"]


def test_absolute_rule_on_posix() -> None:
    assert cli_absolute_rule("Edit", PurePosixPath("/home/a/p/x.yaml")) == "Edit(//home/a/p/x.yaml)"


def test_absolute_rule_on_windows_uses_the_cli_drive_form() -> None:
    # The CLI matches Windows paths in POSIX form: C:\Users\a -> /c/Users/a
    assert cli_absolute_rule("Edit", PureWindowsPath(r"C:\Users\a\p\x.yaml")) == "Edit(//c/Users/a/p/x.yaml)"
    assert cli_absolute_rule("Read", PureWindowsPath(r"D:\docs"), "/**") == "Read(//d/docs/**)"


def test_symlinked_allowlist_is_not_loaded(project: Path, tmp_path: Path) -> None:
    """A link planted in an earlier run must not point the allowlist at an agent-writable file."""
    planted = tmp_path / "agent-file.yaml"
    planted.write_text("version: 1\ncommands:\n  - name: python3\n")
    allowlist = project / ".autoforge" / "allowed_commands.yaml"
    allowlist.unlink()
    allowlist.symlink_to(planted)
    assert load_project_commands(project) is None


def test_regular_allowlist_still_loads(project: Path) -> None:
    (project / ".autoforge" / "allowed_commands.yaml").write_text("version: 1\ncommands:\n  - name: swift\n")
    config = load_project_commands(project)
    assert config is not None and config["commands"] == [{"name": "swift"}]


def test_spec_chat_settings_deny_the_allowlist(project: Path) -> None:
    from server.services import spec_chat_session

    async def drain() -> None:
        async for _ in spec_chat_session.SpecChatSession("demo", project).start():
            pass

    with (
        patch.object(spec_chat_session, "ClaudeSDKClient", _AbortingClient),
        patch.object(spec_chat_session, "find_claude_cli", return_value="/usr/bin/claude"),
        patch("registry.get_effective_sdk_env", return_value={}),
        patch("registry.get_effort_setting", return_value=None),
    ):
        asyncio.run(drain())

    settings = json.loads((project / ".autoforge" / ".claude_settings.json").read_text(encoding="utf-8"))
    for path in protected_config_paths(project):
        assert cli_absolute_rule("Edit", Path(path)) in settings["permissions"]["deny"]
    assert not any(rule.startswith(("Write(", "Glob(")) for rule in settings["permissions"]["allow"])
