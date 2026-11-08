"""
Project delete sites never follow a symlinked directory the agent planted
=========================================================================

``Path.unlink`` and ``shutil.rmtree`` leave a symlink at the final component
alone, but follow a symlinked parent: with ``.autoforge`` -> elsewhere, deleting
``.autoforge/features.db`` deletes the file at the link's target. Each test plants
such a parent and checks that the files outside the project survive.

Run with: python -m pytest test_project_delete_sites.py
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from api import safe_project_io as sio

pytestmark = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    return folder


def _plant(project: Path, outside: Path, *names: str) -> None:
    """Make ``.autoforge`` a symlink to ``outside`` holding the given files."""
    for name in names:
        path = outside / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")
    (project / ".autoforge").symlink_to(outside, target_is_directory=True)


def _survived(outside: Path, *names: str) -> bool:
    return all((outside / name).read_text(encoding="utf-8") == "keep" for name in names)


def _reset(project: Path, monkeypatch: pytest.MonkeyPatch, full_reset: bool) -> Any:
    from server.routers import projects

    registry_functions = (None, None, lambda _name: project, None, None, None, None, None, None)
    monkeypatch.setattr(projects, "_get_registry_functions", lambda: registry_functions)
    return asyncio.run(projects.reset_project("demo", full_reset=full_reset))


def test_reset_does_not_delete_through_symlinked_autoforge(
    project: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(project, outside, "features.db", "assistant.db", ".claude_settings.json")

    with pytest.raises(HTTPException) as excinfo:
        _reset(project, monkeypatch, full_reset=False)

    assert excinfo.value.status_code == 500
    assert _survived(outside, "features.db", "assistant.db", ".claude_settings.json")


def test_full_reset_does_not_remove_prompts_through_symlinked_autoforge(
    project: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(project, outside, "prompts/app_spec.txt")

    with pytest.raises(HTTPException):
        _reset(project, monkeypatch, full_reset=True)

    assert _survived(outside, "prompts/app_spec.txt")


def test_full_reset_still_removes_real_prompts(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompts_dir = project / ".autoforge" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "app_spec.txt").write_text("spec")
    (project / ".autoforge" / "features.db").write_text("db")

    result = _reset(project, monkeypatch, full_reset=True)

    assert not prompts_dir.exists() and not (project / ".autoforge" / "features.db").exists()
    assert result["success"] is True


def _process_manager(project: Path) -> Any:
    from server.services.process_manager import AgentProcessManager

    return AgentProcessManager("demo", project, Path(__file__).parent)


def test_agent_lock_and_drain_removal_skip_symlinked_autoforge(project: Path, outside: Path) -> None:
    _plant(project, outside, ".agent.lock", ".pause_drain")
    manager = _process_manager(project)
    manager.process = SimpleNamespace(pid=4242)
    manager._status = "paused_graceful"

    manager._remove_lock()
    ok, _message = asyncio.run(manager.graceful_resume())

    assert ok is True  # the in-project drain file is absent: resuming goes on
    assert _survived(outside, ".agent.lock", ".pause_drain")


def test_orphaned_lock_cleanup_skips_symlinked_autoforge(
    project: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import registry
    from server.services import dev_server_manager, process_manager

    _plant(project, outside, ".agent.lock", ".devserver.lock", ".pause_drain")
    (outside / ".agent.lock").write_text("not-a-pid")  # invalid content -> removal path
    (outside / ".devserver.lock").write_text("not-a-pid")
    monkeypatch.setattr(registry, "list_registered_projects", lambda: {"demo": {"path": str(project)}})
    monkeypatch.setattr(dev_server_manager, "list_registered_projects", lambda: {"demo": {"path": str(project)}})

    process_manager.cleanup_orphaned_locks()
    dev_server_manager.cleanup_orphaned_devserver_locks()

    assert (outside / ".agent.lock").exists() and (outside / ".devserver.lock").exists()
    assert _survived(outside, ".pause_drain")


def test_dev_server_lock_removal_skips_symlinked_autoforge(project: Path, outside: Path) -> None:
    from server.services.dev_server_manager import DevServerProcessManager

    _plant(project, outside, ".devserver.lock")
    DevServerProcessManager("demo", project)._remove_lock()

    assert _survived(outside, ".devserver.lock")


def test_orchestrator_drain_clear_skips_symlinked_autoforge(project: Path, outside: Path) -> None:
    from parallel_orchestrator import ParallelOrchestrator

    _plant(project, outside, ".pause_drain")
    stub = SimpleNamespace(project_dir=project, _drain_requested=True)
    ParallelOrchestrator._clear_drain_signal(stub)  # type: ignore[arg-type]

    assert _survived(outside, ".pause_drain")
    assert stub._drain_requested is False


def test_clear_dev_command_skips_symlinked_autoforge(project: Path, outside: Path) -> None:
    from server.services import project_config

    _plant(project, outside)
    (outside / "config.json").write_text('{"dev_command": "npm run dev"}', encoding="utf-8")

    project_config.clear_dev_command(project)

    assert (outside / "config.json").exists()


def test_spec_chat_does_not_delete_spec_through_symlinked_autoforge(project: Path, outside: Path) -> None:
    from unittest.mock import patch

    from server.services import spec_chat_session

    _plant(project, outside, "prompts/app_spec.txt")
    session = spec_chat_session.SpecChatSession("demo", project)

    async def drain() -> list[dict]:
        return [chunk async for chunk in session.start()]

    with patch("registry.get_effective_sdk_env", return_value={}), \
            patch("registry.get_effort_setting", return_value=None):
        with pytest.raises(sio.UnsafeProjectPathError):
            asyncio.run(drain())

    assert _survived(outside, "prompts/app_spec.txt")


def test_browser_view_screenshot_cleanup_skips_symlinked_dir(project: Path, outside: Path) -> None:
    from server.services.browser_view_service import BrowserViewService

    (outside / "_view_s1.png").write_text("keep", encoding="utf-8")
    (project / ".playwright-cli").symlink_to(outside, target_is_directory=True)

    BrowserViewService("demo", project)._cleanup_screenshot_file("s1")

    assert _survived(outside, "_view_s1.png")
