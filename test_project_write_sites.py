"""
Project write sites never follow links the agent planted
========================================================

The coding agent can create symlinks and hard links anywhere inside the project.
The server, scaffolding, migrations and the agent runner write project files from
outside the agent's sandbox; each test plants a link at (or above) one of those
files, pointing outside the project, and checks that the outside file or
directory is left untouched.

Run with: python -m pytest test_project_write_sites.py
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import autoforge_paths
import prompts
from api import migration
from api import safe_project_io as sio

pytestmark = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    (folder / ".autoforge").mkdir(parents=True)
    return folder


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    return folder


@pytest.fixture
def victim(outside: Path) -> Path:
    path = outside / "victim.txt"
    path.write_text("keep", encoding="utf-8")
    return path


def _link_autoforge_outside(project: Path, outside: Path) -> None:
    (project / ".autoforge").rmdir()
    (project / ".autoforge").symlink_to(outside, target_is_directory=True)


def _outside_names(outside: Path) -> set[str]:
    return {p.name for p in outside.iterdir()}


class _AbortingClient:
    """Stand-in for ClaudeSDKClient: records options, aborts the session start."""

    def __init__(self, options: Any = None, **_kwargs: Any) -> None:
        self.options = options

    async def __aenter__(self) -> "_AbortingClient":
        raise RuntimeError("stop after capture")

    async def __aexit__(self, *args: Any) -> None:
        return None


async def _drain(session: Any) -> list[dict]:
    try:
        return [chunk async for chunk in session.start()]
    finally:
        await session.close()


def _run_chat_session(module: Any, session_cls: Any, project: Path) -> list[dict]:
    with (
        patch.object(module, "ClaudeSDKClient", _AbortingClient),
        patch.object(module, "find_claude_cli", return_value="/usr/bin/claude"),
        patch("registry.get_effective_sdk_env", return_value={}),
        patch("registry.get_effort_setting", return_value=None),
    ):
        return asyncio.run(_drain(session_cls("demo", project)))


# ---------------------------------------------------------------------------
# Claude settings files (agent runner and chat sessions)
# ---------------------------------------------------------------------------


def test_agent_client_settings_not_written_through_symlink(project: Path, victim: Path) -> None:
    import client

    settings = project / ".autoforge" / ".claude_settings.json"
    settings.symlink_to(victim)
    with patch.object(client, "ClaudeSDKClient", _AbortingClient):
        client.create_client(project, "claude-opus-4-6")

    assert victim.read_text(encoding="utf-8") == "keep"
    assert not settings.is_symlink()
    assert json.loads(settings.read_text(encoding="utf-8"))["sandbox"]["enabled"] is True


def test_spec_chat_settings_not_written_through_symlink(project: Path, victim: Path) -> None:
    from server.services import spec_chat_session

    settings = project / ".autoforge" / ".claude_settings.json"
    settings.symlink_to(victim)
    _run_chat_session(spec_chat_session, spec_chat_session.SpecChatSession, project)

    assert victim.read_text(encoding="utf-8") == "keep"
    assert not settings.is_symlink()


def test_assistant_chat_settings_not_written_through_symlink(
    project: Path, victim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.services import assistant_chat_session

    settings = project / ".autoforge" / ".claude_assistant_settings.json"
    settings.symlink_to(victim)
    monkeypatch.setattr(assistant_chat_session, "ClaudeSDKClient", _AbortingClient)
    monkeypatch.setattr("registry.get_effective_sdk_env", lambda: {})
    monkeypatch.setattr("registry.get_effort_setting", lambda: None)
    asyncio.run(_drain(assistant_chat_session.AssistantChatSession("demo", project)))

    assert victim.read_text(encoding="utf-8") == "keep"
    assert not settings.is_symlink()


def test_expand_chat_refuses_symlinked_autoforge_dir(project: Path, outside: Path) -> None:
    from server.services import expand_chat_session

    (outside / "prompts").mkdir()
    (outside / "prompts" / "app_spec.txt").write_text("<project_specification/>", encoding="utf-8")
    _link_autoforge_outside(project, outside)

    with pytest.raises(sio.UnsafeProjectPathError):
        _run_chat_session(expand_chat_session, expand_chat_session.ExpandChatSession, project)

    assert _outside_names(outside) == {"prompts"}


def test_system_prompt_file_refuses_symlinked_autoforge_dir(project: Path, outside: Path) -> None:
    from server.services.chat_constants import write_system_prompt_file

    _link_autoforge_outside(project, outside)
    with pytest.raises(sio.UnsafeProjectPathError):
        write_system_prompt_file(project, "assistant", "prompt")
    assert _outside_names(outside) == set()


# ---------------------------------------------------------------------------
# Server services
# ---------------------------------------------------------------------------


def test_project_config_not_written_through_symlink(project: Path, victim: Path) -> None:
    from server.services import project_config

    config = project / ".autoforge" / "config.json"
    config.symlink_to(victim)
    project_config._save_config(project, {"dev_command": "npm run dev"})

    assert victim.read_text(encoding="utf-8") == "keep"
    assert json.loads(config.read_text(encoding="utf-8")) == {"dev_command": "npm run dev"}


def test_autocoder_config_not_written_through_symlink(project: Path, victim: Path) -> None:
    from server.services import autocoder_config

    config = project / ".autoforge" / "config.json"
    config.symlink_to(victim)
    autocoder_config.save_autocoder_config(project, {"version": 1})  # type: ignore[typeddict-item]

    assert victim.read_text(encoding="utf-8") == "keep"
    assert json.loads(config.read_text(encoding="utf-8")) == {"version": 1}


def _process_manager(project: Path) -> Any:
    from server.services.process_manager import AgentProcessManager

    return AgentProcessManager("demo", project, Path(__file__).parent)


def test_playwright_headless_update_not_read_or_written_through_symlink(project: Path, outside: Path) -> None:
    secret = outside / "settings.json"
    secret.write_text(json.dumps({"browser": {"launchOptions": {"headless": True}}, "token": "s3cr3t"}))
    original = secret.read_text()
    (project / ".playwright").mkdir()
    (project / ".playwright" / "cli.config.json").symlink_to(secret)

    _process_manager(project)._apply_playwright_headless(False)

    assert secret.read_text() == original
    # Refused (logged): the outside file's contents were not copied into the project
    assert (project / ".playwright" / "cli.config.json").is_symlink()


def test_agent_lock_not_created_in_symlinked_dir(project: Path, outside: Path) -> None:
    import os
    import subprocess

    _link_autoforge_outside(project, outside)
    manager = _process_manager(project)
    manager.lock_file = project / ".autoforge" / ".agent.lock"
    manager.process = subprocess.Popen(["sleep", "5"])
    try:
        with pytest.raises(sio.UnsafeProjectPathError):  # its own reason, not "already running"
            manager._create_lock()
    finally:
        manager.process.kill()
        manager.process.wait()
    assert _outside_names(outside) == set()
    assert os.path.lexists(manager.lock_file) is False


def test_agent_start_reports_refused_lock_instead_of_another_instance(
    project: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from server.services import process_manager

    class _FakeProcess:
        pid = os.getpid()  # psutil needs a live pid for the lock content
        terminated = False

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args[0] if args else kwargs.get("args")

        # subprocess.run (the "playwright-cli kill-all" cleanup) uses Popen as a context manager
        def __enter__(self) -> "_FakeProcess":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def communicate(self, *args: Any, **kwargs: Any) -> tuple[bytes, bytes]:
            return b"", b""

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            _FakeProcess.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    _link_autoforge_outside(project, outside)
    monkeypatch.setattr(process_manager.subprocess, "Popen", _FakeProcess)
    monkeypatch.setattr("registry.get_effective_sdk_env", lambda: {})
    monkeypatch.setattr("registry.get_testing_model_setting", lambda: None)
    manager = _process_manager(project)

    ok, message = asyncio.run(manager.start(yolo_mode=True))

    assert ok is False and _FakeProcess.terminated
    assert "Another agent instance" not in message
    assert message.startswith("Cannot create the agent lock file") and "symlink" in message
    assert _outside_names(outside) == set()


def test_graceful_pause_refuses_symlinked_autoforge_dir(project: Path, outside: Path) -> None:
    class _Proc:
        pid = 4242

    _link_autoforge_outside(project, outside)
    manager = _process_manager(project)
    manager.process = _Proc()
    manager._status = "running"

    ok, _message = asyncio.run(manager.graceful_pause())

    assert ok is False
    assert _outside_names(outside) == set()


def test_dev_server_lock_not_written_through_symlink(project: Path, victim: Path) -> None:
    from server.services.dev_server_manager import DevServerProcessManager

    lock = project / ".autoforge" / ".devserver.lock"
    lock.symlink_to(victim)
    manager = DevServerProcessManager("demo", project)

    class _Proc:
        pid = 4242

    manager.process = _Proc()  # type: ignore[assignment]
    manager._create_lock()

    assert victim.read_text(encoding="utf-8") == "keep"
    assert lock.read_text() == "4242"


def test_dev_server_lock_refusal_is_logged_not_raised(project: Path, outside: Path) -> None:
    from server.services.dev_server_manager import DevServerProcessManager

    _link_autoforge_outside(project, outside)
    manager = DevServerProcessManager("demo", project)
    manager.lock_file = project / ".autoforge" / ".devserver.lock"

    class _Proc:
        pid = 4242

    manager.process = _Proc()  # type: ignore[assignment]
    manager._create_lock()  # must not raise: the dev server is already running

    assert _outside_names(outside) == set()


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


def test_apply_template_not_written_through_symlink(project: Path, victim: Path) -> None:
    from server.routers import templates as templates_router

    prompts_dir = project / ".autoforge" / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "app_spec.txt").symlink_to(victim)
    request = templates_router.ApplyRequest(
        template_id="blog-cms", project_name="demo", project_dir=str(project), customizations=None
    )

    asyncio.run(templates_router.apply_template(request))

    assert victim.read_text(encoding="utf-8") == "keep"
    assert "<name>demo</name>" in (prompts_dir / "app_spec.txt").read_text(encoding="utf-8")


def test_update_project_prompts_not_written_through_symlink(
    project: Path, victim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routers import projects
    from server.schemas import ProjectPromptsUpdate

    prompts_dir = project / ".autoforge" / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coding_prompt.md").symlink_to(victim)
    registry_functions = (None, None, lambda _name: project, None, None, None, None, None, None)
    monkeypatch.setattr(projects, "_get_registry_functions", lambda: registry_functions)

    asyncio.run(projects.update_project_prompts("demo", ProjectPromptsUpdate(coding_prompt="new")))

    assert victim.read_text(encoding="utf-8") == "keep"
    assert (prompts_dir / "coding_prompt.md").read_text(encoding="utf-8") == "new"


# ---------------------------------------------------------------------------
# Agent runner: progress cache, JSON migration
# ---------------------------------------------------------------------------


def test_progress_cache_not_written_through_symlink(
    project: Path, victim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import progress

    cache = project / ".autoforge" / ".progress_cache"
    cache.symlink_to(victim)
    monkeypatch.setattr(progress, "WEBHOOK_URL", "http://127.0.0.1:9/hook")
    monkeypatch.setattr(progress, "get_all_passing_features", lambda _d: [{"id": 1, "name": "a"}])
    monkeypatch.setattr(progress.urllib.request, "urlopen", lambda *a, **k: None)

    progress.send_progress_webhook(1, 2, project)

    assert victim.read_text(encoding="utf-8") == "keep"
    assert json.loads(cache.read_text()) == {"count": 1, "passing_ids": [1]}


def test_progress_cache_refusal_does_not_raise(
    project: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import progress

    _link_autoforge_outside(project, outside)
    monkeypatch.setattr(progress, "WEBHOOK_URL", "http://127.0.0.1:9/hook")
    monkeypatch.setattr(progress, "get_all_passing_features", lambda _d: [])

    progress.send_progress_webhook(0, 2, project)  # initial-state branch

    assert _outside_names(outside) == set()


def test_export_to_json_not_written_through_symlink(project: Path, victim: Path) -> None:
    from api.database import create_database

    (project / "feature_list_export.json").symlink_to(victim)
    _engine, session_maker = create_database(project)
    migration.export_to_json(project, session_maker)

    assert victim.read_text(encoding="utf-8") == "keep"
    assert json.loads((project / "feature_list_export.json").read_text(encoding="utf-8")) == []


def test_json_backup_not_moved_into_symlinked_dir(
    project: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.database import create_database

    class _FixedDatetime:
        @staticmethod
        def now() -> datetime:
            return datetime(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr(migration, "datetime", _FixedDatetime)
    (project / "feature_list.json").write_text(
        json.dumps([{"category": "c", "description": "d", "steps": [], "passes": False}]), encoding="utf-8"
    )
    (project / "feature_list.json.backup.20260102_030405").symlink_to(outside, target_is_directory=True)
    _engine, session_maker = create_database(project)

    assert migration.migrate_json_to_sqlite(project, session_maker) is True
    assert _outside_names(outside) == set()


# ---------------------------------------------------------------------------
# Project layout migration (autoforge_paths)
# ---------------------------------------------------------------------------


def test_ensure_autoforge_dir_refuses_symlinked_dir(project: Path, outside: Path) -> None:
    _link_autoforge_outside(project, outside)
    with pytest.raises(sio.UnsafeProjectPathError):
        autoforge_paths.ensure_autoforge_dir(project)
    assert _outside_names(outside) == set()


def test_layout_migration_skips_symlinked_autoforge_dir(project: Path, outside: Path) -> None:
    _link_autoforge_outside(project, outside)
    (project / ".progress_cache").write_text("{}")
    (project / "prompts").mkdir()
    (project / "prompts" / "app_spec.txt").write_text("spec")

    assert autoforge_paths.migrate_project_layout(project) == []
    assert _outside_names(outside) == set()
    assert (project / ".progress_cache").exists()


def test_layout_migration_db_copy_not_written_through_dangling_symlink(project: Path, outside: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(project / "features.db")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    (project / ".autoforge" / "features.db").symlink_to(outside / "created.db")

    autoforge_paths.migrate_project_layout(project)

    assert _outside_names(outside) == set()
    # The database check refuses the planted link (logged); the root database stays put
    assert (project / "features.db").exists()


def test_layout_migration_does_not_copy_linked_prompt_contents(project: Path, outside: Path) -> None:
    (outside / "id_rsa").write_text("secret")
    (project / "prompts").mkdir()
    (project / "prompts" / "app_spec.txt").write_text("spec")
    (project / "prompts" / "coding_prompt.md").symlink_to(outside / "id_rsa")

    autoforge_paths.migrate_project_layout(project)

    copied = project / ".autoforge" / "prompts" / "coding_prompt.md"
    assert (project / ".autoforge" / "prompts" / "app_spec.txt").read_text() == "spec"
    assert copied.is_symlink()  # the link itself, never the outside file's contents


def test_layout_migration_does_not_launder_hard_linked_prompt(project: Path, outside: Path) -> None:
    import os

    (outside / "secret.md").write_text("OUTSIDE SECRET")
    (project / "prompts").mkdir()
    (project / "prompts" / "app_spec.txt").write_text("spec")
    os.link(outside / "secret.md", project / "prompts" / "coding_prompt.md")

    assert autoforge_paths.migrate_project_layout(project) == []  # refused (logged), retried next start

    assert not (project / ".autoforge" / "prompts").exists()
    assert (project / "prompts" / "coding_prompt.md").exists()  # nothing moved or deleted


# ---------------------------------------------------------------------------
# Scaffolding and prompt migrations (prompts.py)
# ---------------------------------------------------------------------------


def test_scaffold_does_not_write_through_planted_links(project: Path, outside: Path, victim: Path) -> None:
    prompts_dir = project / ".autoforge" / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coding_prompt.md").symlink_to(outside / "coding.md")  # dangling
    (project / ".autoforge" / "allowed_commands.yaml").symlink_to(outside / "allowed.yaml")  # dangling
    (project / ".claude").mkdir()
    (project / ".claude" / "skills").symlink_to(outside, target_is_directory=True)
    (project / ".gitignore").symlink_to(victim)
    (project / ".playwright").mkdir()
    (project / ".playwright" / "cli.config.json").symlink_to(outside / "cli.json")  # dangling

    prompts.scaffold_project_prompts(project)

    assert victim.read_text(encoding="utf-8") == "keep"
    assert _outside_names(outside) == {"victim.txt"}
    assert "STEP" in (prompts_dir / "coding_prompt.md").read_text(encoding="utf-8")
    assert (project / ".autoforge" / ".migration_version").read_text() == str(prompts.CURRENT_MIGRATION_VERSION)


def test_scaffold_refuses_symlinked_prompts_dir(project: Path, outside: Path) -> None:
    (project / ".autoforge" / "prompts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        prompts.scaffold_project_prompts(project)
    assert _outside_names(outside) == set()


def test_migration_version_not_written_through_symlink(project: Path, victim: Path) -> None:
    (project / ".autoforge" / ".migration_version").symlink_to(victim)
    prompts._set_migration_version(project, 4)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert (project / ".autoforge" / ".migration_version").read_text() == "4"


def _legacy_prompt_project(project: Path) -> Path:
    prompts_dir = project / ".autoforge" / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "app_spec.txt").write_text("<project_specification>x</project_specification>", encoding="utf-8")
    (prompts_dir / "coding_prompt.md").write_text("Read app_spec.txt first.", encoding="utf-8")
    return prompts_dir


def test_copy_spec_to_project_not_written_through_dangling_symlink(project: Path, outside: Path) -> None:
    _legacy_prompt_project(project)
    (project / "app_spec.txt").symlink_to(outside / "created.txt")

    prompts.copy_spec_to_project(project)

    assert _outside_names(outside) == set()


def test_copy_spec_to_project_does_not_copy_linked_source(project: Path, outside: Path) -> None:
    prompts_dir = _legacy_prompt_project(project)
    (outside / "id_rsa").write_text("secret")
    (prompts_dir / "app_spec.txt").unlink()
    (prompts_dir / "app_spec.txt").symlink_to(outside / "id_rsa")

    prompts.copy_spec_to_project(project)

    assert not (project / "app_spec.txt").exists()


def test_v0_migration_does_not_write_through_planted_links(project: Path, outside: Path, victim: Path) -> None:
    prompts_dir = project / ".autoforge" / "prompts"
    prompts_dir.mkdir()
    mcp_prompt = outside / "coding.md"
    mcp_prompt.write_text("Use Playwright MCP browser_navigate", encoding="utf-8")
    (prompts_dir / "coding_prompt.md").symlink_to(mcp_prompt)
    (project / ".gitignore").symlink_to(victim)
    (project / ".claude").mkdir()
    (project / ".claude" / "skills").symlink_to(outside, target_is_directory=True)
    (project / ".playwright").mkdir()
    (project / ".playwright" / "cli.config.json").symlink_to(outside / "cli.json")  # dangling

    prompts._migrate_v0_to_v1(project)

    assert victim.read_text(encoding="utf-8") == "keep"
    assert mcp_prompt.read_text(encoding="utf-8") == "Use Playwright MCP browser_navigate"
    assert _outside_names(outside) == {"victim.txt", "coding.md"}


def test_v2_migration_does_not_write_into_symlinked_autoforge_dir(project: Path, outside: Path) -> None:
    _link_autoforge_outside(project, outside)

    prompts._migrate_v2_to_v3(project)  # refusal is printed as a warning, not raised

    assert _outside_names(outside) == set()


# ---------------------------------------------------------------------------
# Reports, generated docs/tokens/workflows, visual snapshots, log exports
# ---------------------------------------------------------------------------


def test_review_and_security_reports_refuse_symlinked_autoforge_dir(project: Path, outside: Path) -> None:
    from review_agent import ReviewAgent, ReviewReport
    from security_scanner import ScanResult, SecurityScanner

    _link_autoforge_outside(project, outside)
    with pytest.raises(sio.UnsafeProjectPathError):
        ReviewAgent(project).save_report(ReviewReport(project_dir=str(project), review_time="t"))
    with pytest.raises(sio.UnsafeProjectPathError):
        SecurityScanner(project)._save_report(ScanResult(project_dir=str(project), scan_time="t"))
    assert _outside_names(outside) == set()


def test_design_tokens_not_written_through_symlinks(project: Path, outside: Path, victim: Path) -> None:
    import design_tokens

    (project / ".autoforge" / "design-tokens.json").symlink_to(victim)
    (project / "src").symlink_to(outside, target_is_directory=True)
    manager = design_tokens.DesignTokensManager(project)
    tokens = manager.load()

    manager.save(tokens)
    with pytest.raises(sio.UnsafeProjectPathError):
        manager.generate_all()  # default output: src/styles/

    assert victim.read_text(encoding="utf-8") == "keep"
    assert _outside_names(outside) == {"victim.txt"}


def test_generate_design_tokens_not_written_through_symlink(project: Path, victim: Path) -> None:
    import design_tokens

    (project / ".autoforge" / "design-tokens.json").symlink_to(victim)
    path = design_tokens.generate_design_tokens(project, "neobrutalism")

    assert victim.read_text(encoding="utf-8") == "keep"
    assert path is not None and not path.is_symlink()


def test_generated_docs_not_written_through_symlinks(project: Path, outside: Path, victim: Path) -> None:
    from auto_documentation import DocumentationGenerator, ProjectDocs

    (project / "README.md").symlink_to(victim)
    (project / "docs").symlink_to(outside, target_is_directory=True)
    generator = DocumentationGenerator(project)
    docs = ProjectDocs("demo", "d", {}, [], [], [], [], [], {})

    generator.write_readme(docs)
    with pytest.raises(sio.UnsafeProjectPathError):
        generator.write_setup_guide(docs)

    assert victim.read_text(encoding="utf-8") == "keep"
    assert _outside_names(outside) == {"victim.txt"}


def test_github_workflow_refuses_symlinked_github_dir(project: Path, outside: Path) -> None:
    from integrations.ci.github_actions import GitHubWorkflow

    (project / ".github").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        GitHubWorkflow(name="ci", filename="ci.yml", on={}, jobs={}).save(project)
    assert _outside_names(outside) == set()


def test_visual_snapshots_refuse_symlinked_dir(project: Path, outside: Path) -> None:
    from visual_regression import VisualRegressionTester

    (project / ".visual-snapshots").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        VisualRegressionTester(project)
    assert _outside_names(outside) == set()


def test_visual_baseline_update_does_not_copy_linked_screenshot(project: Path, outside: Path, victim: Path) -> None:
    from visual_regression import VisualRegressionTester

    tester = VisualRegressionTester(project)
    (tester.current_dir / "home_desktop.png").symlink_to(victim)
    (tester.baselines_dir / "other_desktop.png").symlink_to(outside / "created.png")  # dangling
    (tester.current_dir / "other_desktop.png").write_bytes(b"png")

    with pytest.raises(sio.UnsafeProjectPathError):
        tester.update_baseline("home", "desktop")
    assert tester.update_baseline("other", "desktop") is True

    assert not (tester.baselines_dir / "home_desktop.png").exists()
    assert _outside_names(outside) == {"victim.txt"}
    assert (tester.baselines_dir / "other_desktop.png").read_bytes() == b"png"


def test_visual_report_refuses_symlinked_reports_dir(project: Path, outside: Path) -> None:
    from visual_regression import TestReport, VisualRegressionTester

    tester = VisualRegressionTester(project)
    (tester.snapshots_dir / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        tester.save_report(TestReport(project_dir=str(project), test_time="t"))
    assert _outside_names(outside) == set()


def _export_logs(project: Path, monkeypatch: pytest.MonkeyPatch, fmt: str) -> Any:
    from server.routers import logs

    monkeypatch.setattr(logs, "_get_project_path", lambda _name: project)
    return asyncio.run(logs.export_logs(logs.ExportRequest(project_name="demo", format=fmt)))  # type: ignore[arg-type]


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv"])
def test_log_export_endpoint_writes_into_project(project: Path, monkeypatch: pytest.MonkeyPatch, fmt: str) -> None:
    from structured_logging import get_logger

    get_logger(project, agent_id="a1", console_output=False).info("hello")
    result = _export_logs(project, monkeypatch, fmt)

    exported = project / ".autocoder" / "exports" / result.filename
    assert result.count == 1 and result.format == fmt
    assert "hello" in exported.read_text()


def test_log_export_refuses_symlinked_exports_dir(
    project: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    (project / ".autocoder").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException):
        _export_logs(project, monkeypatch, "jsonl")
    assert _outside_names(outside) == set()


def test_layout_migration_skips_windows_junctions(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A junction at .autoforge/.autocoder/prompts is treated like a symlink (is_symlink() misses it)."""
    junctions = {".autoforge", ".autocoder"}
    monkeypatch.setattr(
        sio, "_reparse_tag", lambda path: sio._IO_REPARSE_TAG_MOUNT_POINT if Path(path).name in junctions else 0,
        raising=False,
    )
    (project / "features.db").write_text("db")  # would be copied into .autoforge/

    assert autoforge_paths.migrate_project_layout(project) == []
    assert list((project / ".autoforge").iterdir()) == []

    junctions.discard(".autoforge")
    (project / ".autocoder").mkdir()
    (project / ".autocoder" / "id_rsa").write_text("secret")  # stands for the junction's outside target
    autoforge_paths.migrate_project_layout(project)
    assert (project / ".autocoder" / "id_rsa").exists() and not (project / ".autoforge" / "id_rsa").exists()


def test_layout_migration_does_not_copy_a_junction_nested_in_prompts(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prompts/ itself is checked; a junction inside it must not copy its outside target either."""
    monkeypatch.setattr(
        sio, "_reparse_tag", lambda path: sio._IO_REPARSE_TAG_MOUNT_POINT if Path(path).name == "junction" else 0,
        raising=False,
    )
    (project / "prompts" / "sub" / "junction").mkdir(parents=True)  # nested: sub/ is copied first
    (project / "prompts" / "sub" / "junction" / "id_rsa").write_text("secret")  # the junction's outside target
    (project / "prompts" / "app_spec.txt").write_text("spec")

    autoforge_paths.migrate_project_layout(project)
    assert not (project / ".autoforge" / "prompts").exists()  # nothing half-copied
    assert (project / "prompts" / "sub" / "junction" / "id_rsa").exists()  # legacy layout left in place
