"""
Denied-commands log: written outside the sandbox, never through a planted link
===============================================================================

The Bash security hook runs in the agent runner, outside the agent's sandbox,
and logs the refused command (text the agent chose) into
``.autoforge/denied_commands.jsonl``. So the log is appended without following
a symlink or hard link the agent planted, rotated with an atomic rename
(#322's approach: ``denied_commands.jsonl`` -> ``.1``), and the agent's
settings make it read-only for the agent so it cannot forge entries.

Run with: python -m pytest test_denied_commands_log.py
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import security
from api import safe_project_io as sio
from autoforge_paths import get_denied_commands_path

needs_dir_fd = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    (folder / ".autoforge").mkdir(parents=True)
    return folder


def _deny(project: Path, command: str) -> dict:
    result: dict = security.validate_bash_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": command}}, {"project_dir": str(project)}
    )
    return result


def test_hook_output_has_only_decision_and_reason(project: Path):
    result = _deny(project, "openssl rand -hex 8")
    assert set(result) == {"decision", "reason"}
    assert _deny(project, "ls -la") == {}


def test_kinds_and_commands_are_recorded(project: Path):
    _deny(project, "openssl rand -hex 8")
    _deny(project, "shutdown now")
    _deny(project, "chmod 777 x.sh")
    _deny(project, "bash -c 'openssl version'")
    _deny(project, "echo x >> .autoforge/allowed_commands.yaml")
    entries = security.load_denied_commands(project)
    assert [(e["command"], e["kind"]) for e in entries] == [
        ("openssl", "not_allowed"),
        ("shutdown", "blocklisted"),
        ("chmod", "validation"),
        ("openssl", "not_allowed"),
        (None, "validation"),
    ]
    assert entries[3]["full_command"] == "bash -c 'openssl version'"


@needs_dir_fd
def test_log_is_not_appended_through_a_symlink(project: Path, tmp_path: Path):
    victim = tmp_path / "bashrc"
    victim.write_text("# user file\n")
    get_denied_commands_path(project).symlink_to(victim)
    result = _deny(project, "openssl rand -hex 8")
    assert result["decision"] == "block"  # the decision never depends on the log
    assert victim.read_text() == "# user file\n"


@needs_dir_fd
def test_log_is_not_appended_through_a_hard_link(project: Path, tmp_path: Path):
    victim = tmp_path / "notes.txt"
    victim.write_text("keep\n")
    get_denied_commands_path(project).hardlink_to(victim)
    _deny(project, "openssl version")
    assert victim.read_text() == "keep\n"


@needs_dir_fd
def test_log_is_not_written_into_a_symlinked_autoforge_dir(tmp_path: Path):
    project = tmp_path / "p"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".autoforge").symlink_to(outside, target_is_directory=True)
    _deny(project, "openssl version")
    assert list(outside.iterdir()) == []


def test_rotation_renames_to_dot_one_and_reads_both(project: Path, monkeypatch):
    monkeypatch.setattr(security, "DENIED_LOG_MAX_BYTES", 300)
    for i in range(6):
        _deny(project, f"openssl rand -hex {i}")
    log = get_denied_commands_path(project)
    rotated = log.with_name(log.name + ".1")
    assert rotated.exists()
    assert log.stat().st_size <= 300 + 1000
    commands = [e["full_command"] for e in security.load_denied_commands(project)]
    # Oldest entries may be dropped with an older .1, but order is kept and the newest is present
    assert commands == sorted(commands)
    assert commands[-1] == "openssl rand -hex 5"


def test_clear_removes_both_files(project: Path, monkeypatch):
    monkeypatch.setattr(security, "DENIED_LOG_MAX_BYTES", 200)
    for i in range(5):
        _deny(project, f"openssl rand -hex {i}")
    security.clear_denied_commands(project)
    log = get_denied_commands_path(project)
    assert not log.exists() and not log.with_name(log.name + ".1").exists()
    assert security.load_denied_commands(project) == []


@needs_dir_fd
def test_reading_does_not_follow_a_symlinked_log(project: Path, tmp_path: Path):
    secret = tmp_path / "secret.jsonl"
    secret.write_text(json.dumps({"command": "TOP SECRET", "kind": "not_allowed"}) + "\n")
    get_denied_commands_path(project).symlink_to(secret)
    with pytest.raises(OSError):
        security.load_denied_commands(project)


@needs_dir_fd
def test_api_answers_409_for_a_planted_log_link(project: Path, tmp_path: Path):
    from fastapi.testclient import TestClient

    from server.main import app
    from server.routers import projects

    secret = tmp_path / "secret.jsonl"
    secret.write_text(json.dumps({"command": "TOP SECRET", "kind": "not_allowed"}) + "\n")
    get_denied_commands_path(project).symlink_to(secret)
    funcs = list(projects._get_registry_functions())
    funcs[2] = lambda name: project if name == "demo" else None
    with patch.object(projects, "_get_registry_functions", return_value=tuple(funcs)):
        client = TestClient(app, client=("127.0.0.1", 50000))
        res = client.get("/api/projects/demo/denied-commands")
    assert res.status_code == 409
    assert "TOP SECRET" not in res.text


def test_agent_settings_make_the_log_read_only(project: Path):
    import client

    class _Abort:
        def __init__(self, options=None, **_kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("stop")

        async def __aexit__(self, *args):
            return None

    with (
        patch.object(client, "ClaudeSDKClient", _Abort),
        patch("registry.get_effective_sdk_env", return_value={}),
        patch("registry.get_effort_setting", return_value=None),
    ):
        client.create_client(project, "claude-opus-4-6")
    settings = json.loads((project / ".autoforge" / ".claude_settings.json").read_text(encoding="utf-8"))
    log = str(get_denied_commands_path(project.resolve()))
    for path in (log, log + ".1"):
        assert path in settings["sandbox"]["filesystem"]["denyWrite"]
        assert security.cli_absolute_rule("Edit", Path(path)) in settings["permissions"]["deny"]
