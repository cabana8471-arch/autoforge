#!/usr/bin/env python3
"""
Testing Model Tests
===================

Verifies that coding and testing agents can be launched with different
models (issue #146).

Run with: python -m pytest test_testing_model.py -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from parallel_orchestrator import ParallelOrchestrator


def _model_arg(cmd: list[str]) -> str | None:
    """Return the value following --model in a subprocess command, if any."""
    if "--model" not in cmd:
        return None
    return cmd[cmd.index("--model") + 1]


class TestOrchestratorTestingModel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, **kwargs) -> ParallelOrchestrator:
        orch = ParallelOrchestrator(project_dir=self.project_dir, max_concurrency=2, **kwargs)
        orch._get_test_batch = MagicMock(return_value=[1, 2])  # type: ignore[method-assign]
        return orch

    def _spawn_cmds(self, orch: ParallelOrchestrator) -> tuple[list[str], list[str]]:
        """Spawn one testing and one coding agent, returning their commands."""
        fake_proc = MagicMock()
        fake_proc.pid = 4242
        fake_proc.stdout = iter(())
        with patch("parallel_orchestrator.subprocess.Popen", return_value=fake_proc) as popen, \
                patch("parallel_orchestrator.threading.Thread"):
            ok, msg = orch._spawn_testing_agent()
            self.assertTrue(ok, msg)
            orch._spawn_coding_agent(7)
        testing_cmd = popen.call_args_list[0].args[0]
        coding_cmd = popen.call_args_list[1].args[0]
        return testing_cmd, coding_cmd

    def test_testing_model_defaults_to_coding_model(self):
        orch = self._make(model="claude-opus-4-7")
        self.assertEqual(orch.testing_model, "claude-opus-4-7")
        testing_cmd, coding_cmd = self._spawn_cmds(orch)
        self.assertEqual(_model_arg(testing_cmd), "claude-opus-4-7")
        self.assertEqual(_model_arg(coding_cmd), "claude-opus-4-7")

    def test_separate_testing_model(self):
        orch = self._make(model="claude-opus-4-7", testing_model="claude-sonnet-4-6")
        testing_cmd, coding_cmd = self._spawn_cmds(orch)
        self.assertEqual(_model_arg(testing_cmd), "claude-sonnet-4-6")
        self.assertEqual(_model_arg(coding_cmd), "claude-opus-4-7")


class TestTestingModelSetting(unittest.TestCase):
    def _resolve(self, settings: dict[str, str]) -> str | None:
        import registry

        with patch.object(registry, "get_setting", side_effect=lambda k, d=None: settings.get(k, d)):
            return registry.get_testing_model_setting()

    def test_unset_returns_none(self):
        self.assertIsNone(self._resolve({}))

    def test_empty_string_means_same_as_coding(self):
        self.assertIsNone(self._resolve({"testing_model": ""}))

    def test_valid_model_returned(self):
        self.assertEqual(self._resolve({"testing_model": "claude-sonnet-4-6"}), "claude-sonnet-4-6")

    def test_legacy_model_migrated(self):
        self.assertEqual(self._resolve({"testing_model": "claude-sonnet-4-5"}), "claude-sonnet-4-6")

    def test_unknown_model_ignored(self):
        self.assertIsNone(self._resolve({"testing_model": "not-a-model"}))

    def test_ignored_for_alternative_provider(self):
        self.assertIsNone(self._resolve({"testing_model": "claude-sonnet-4-6", "api_provider": "glm"}))


class TestSettingsSchemas(unittest.TestCase):
    def test_settings_update_accepts_clear_and_valid(self):
        from server.schemas import SettingsUpdate

        self.assertEqual(SettingsUpdate(testing_model="").testing_model, "")
        self.assertEqual(SettingsUpdate(testing_model="claude-sonnet-4-6").testing_model, "claude-sonnet-4-6")

    def test_settings_update_rejects_unknown_model(self):
        from pydantic import ValidationError

        from server.schemas import SettingsUpdate

        with self.assertRaises(ValidationError):
            SettingsUpdate(testing_model="gpt-4")


def _ui_start_cmd(tmp_path, monkeypatch, global_setting, **start_kwargs) -> list[str]:
    """Start the UI agent process with Popen mocked and return the agent command."""
    import asyncio
    import os

    import registry
    from server.services import process_manager

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # start() reads the registry under ~/.autoforge
    monkeypatch.setattr(registry, "get_testing_model_setting", lambda: global_setting)
    project_dir = tmp_path / "project"
    (project_dir / ".autoforge").mkdir(parents=True)
    manager = process_manager.AgentProcessManager("demo", project_dir, Path(process_manager.__file__).parents[2])
    fake_proc = MagicMock(pid=os.getpid(), returncode=0)
    fake_proc.poll.return_value = None
    fake_proc.communicate.return_value = ("", "")
    fake_proc.__enter__.return_value = fake_proc

    async def no_stream():
        return None

    with patch.object(process_manager.subprocess, "Popen", return_value=fake_proc) as popen, \
            patch.object(manager, "_stream_output", no_stream):
        ok, msg = asyncio.run(manager.start(**start_kwargs))
    assert ok, msg
    agent_calls = [c for c in popen.call_args_list if "PYTHONUNBUFFERED" in (c.kwargs.get("env") or {})]
    assert len(agent_calls) == 1
    return list(agent_calls[0].args[0])


def _flag(cmd: list[str], name: str) -> str | None:
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_ui_start_uses_global_testing_model(tmp_path, monkeypatch):
    cmd = _ui_start_cmd(tmp_path, monkeypatch, "claude-sonnet-4-6", model="claude-opus-4-7")
    assert _flag(cmd, "--model") == "claude-opus-4-7"
    assert _flag(cmd, "--testing-model") == "claude-sonnet-4-6"


def test_ui_start_request_overrides_global_testing_model(tmp_path, monkeypatch):
    cmd = _ui_start_cmd(tmp_path, monkeypatch, "claude-sonnet-4-6",
                        model="claude-opus-4-7", testing_model="claude-haiku-4-5")
    assert _flag(cmd, "--testing-model") == "claude-haiku-4-5"


def test_ui_start_omits_flag_when_testing_model_matches_or_unset(tmp_path, monkeypatch):
    assert "--testing-model" not in _ui_start_cmd(tmp_path, monkeypatch, None, model="claude-opus-4-7")
    assert "--testing-model" not in _ui_start_cmd(
        tmp_path / "b", monkeypatch, "claude-opus-4-7", model="claude-opus-4-7")


if __name__ == "__main__":
    unittest.main()
