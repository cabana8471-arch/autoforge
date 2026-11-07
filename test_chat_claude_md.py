"""
Chat Sessions Must Not Touch the Project's CLAUDE.md
====================================================

Older versions wrote the assistant and spec chat system prompts into the
project's root CLAUDE.md, which clobbered the user's file and leaked the chat
instructions into the coding agents (``setting_sources=["project"]``).

These tests start real chat sessions against a fake Claude SDK client and check
that a user-authored CLAUDE.md survives unchanged, while a CLAUDE.md that an
older AutoForge version generated is removed.

Run with: python -m pytest test_chat_claude_md.py -v
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

import registry
from server.services import assistant_chat_session, spec_chat_session
from server.services.chat_constants import remove_legacy_claude_md

USER_CLAUDE_MD = "# My project\n\nUse tabs, not spaces.\n"


class FakeClaudeSDKClient:
    """Stands in for ClaudeSDKClient: records options, never spawns the CLI."""

    instances: list["FakeClaudeSDKClient"] = []

    def __init__(self, options: Any) -> None:
        self.options = options
        FakeClaudeSDKClient.instances.append(self)

    async def __aenter__(self) -> "FakeClaudeSDKClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def query(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def receive_response(self):
        return
        yield  # pragma: no cover - makes this an async generator


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    FakeClaudeSDKClient.instances = []
    # A registered project always has .autoforge/ (scaffolded at creation)
    (tmp_path / ".autoforge").mkdir()
    monkeypatch.setattr(assistant_chat_session, "ClaudeSDKClient", FakeClaudeSDKClient)
    monkeypatch.setattr(spec_chat_session, "ClaudeSDKClient", FakeClaudeSDKClient)
    # Keep the test independent of the user's global settings
    monkeypatch.setattr(registry, "get_effective_sdk_env", lambda: {})
    monkeypatch.setattr(registry, "get_effort_setting", lambda: None)


async def _drain(session: Any) -> list[dict]:
    chunks = [chunk async for chunk in session.start()]
    await session.close()
    return chunks


def _start_assistant(project_dir: Path) -> list[dict]:
    session = assistant_chat_session.AssistantChatSession("demo", project_dir)
    return asyncio.run(_drain(session))


def _start_spec(project_dir: Path) -> list[dict]:
    session = spec_chat_session.SpecChatSession("demo", project_dir)
    return asyncio.run(_drain(session))


@pytest.mark.parametrize("start", [_start_assistant, _start_spec], ids=["assistant", "spec"])
def test_session_leaves_user_claude_md_unchanged(tmp_path: Path, start: Any) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(USER_CLAUDE_MD, encoding="utf-8")

    chunks = start(tmp_path)

    assert not [c for c in chunks if c.get("type") == "error"], chunks
    assert claude_md.read_text(encoding="utf-8") == USER_CLAUDE_MD
    # The prompt went to the CLI through a file, not through CLAUDE.md
    system_prompt = FakeClaudeSDKClient.instances[-1].options.system_prompt
    assert system_prompt["type"] == "file"
    assert Path(system_prompt["path"]).name.startswith(".system_prompt.")


@pytest.mark.parametrize("start", [_start_assistant, _start_spec], ids=["assistant", "spec"])
def test_session_does_not_create_claude_md(tmp_path: Path, start: Any) -> None:
    start(tmp_path)

    assert not (tmp_path / "CLAUDE.md").exists()


@pytest.mark.parametrize(
    "legacy_content",
    [
        'You are a helpful project assistant and backlog manager for the "demo" project.\n...',
        "---\ndescription: Create an app spec for autonomous coding (project)\n---\n...",
        "---\r\ndescription: Create an app spec for autonomous coding (project)\r\n---\r\n...",
    ],
    ids=["assistant-prompt", "spec-prompt", "spec-prompt-crlf"],
)
def test_session_removes_claude_md_generated_by_old_versions(tmp_path: Path, legacy_content: str) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_bytes(legacy_content.encode("utf-8"))

    _start_assistant(tmp_path)

    assert not claude_md.exists()


def test_remove_legacy_claude_md_keeps_user_file(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(USER_CLAUDE_MD, encoding="utf-8")

    remove_legacy_claude_md(tmp_path)

    assert claude_md.read_text(encoding="utf-8") == USER_CLAUDE_MD


def test_remove_legacy_claude_md_without_file(tmp_path: Path) -> None:
    remove_legacy_claude_md(tmp_path)  # must not raise

    assert not (tmp_path / "CLAUDE.md").exists()
