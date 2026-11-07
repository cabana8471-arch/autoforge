"""
Tests for start_ui.install_python_deps (#168: pip3-only and uv-based setups).

Run with: python -m pytest test_start_ui.py
"""

from types import SimpleNamespace

import pytest

import start_ui


@pytest.fixture
def commands(monkeypatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(start_ui, "run_command", lambda cmd, **_kw: ran.append([str(c) for c in cmd]) or True)
    return ran


def _venv_pip(monkeypatch, available: bool) -> None:
    monkeypatch.setattr(start_ui.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0 if available else 1))


def test_venv_without_pip_installs_through_uv(monkeypatch, commands):
    _venv_pip(monkeypatch, available=False)
    monkeypatch.setattr(start_ui.shutil, "which", lambda name: "/opt/bin/uv" if name == "uv" else None)

    assert start_ui.install_python_deps() is True

    assert len(commands) == 1
    assert commands[0][:5] == ["/opt/bin/uv", "pip", "install", "--python", str(start_ui.get_venv_python())]


def test_venv_with_pip_uses_pip_even_when_uv_exists(monkeypatch, commands):
    _venv_pip(monkeypatch, available=True)
    monkeypatch.setattr(start_ui.shutil, "which", lambda name: "/opt/bin/uv")

    assert start_ui.install_python_deps() is True

    assert all(cmd[1:3] == ["-m", "pip"] for cmd in commands)
    assert len(commands) == 2


def test_venv_without_pip_and_without_uv_falls_back_to_pip(monkeypatch, commands):
    _venv_pip(monkeypatch, available=False)
    monkeypatch.setattr(start_ui.shutil, "which", lambda name: None)

    start_ui.install_python_deps()

    assert commands and commands[0][1:3] == ["-m", "pip"]
