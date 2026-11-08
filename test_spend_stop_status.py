"""
A spend stop is not a crash
===========================

Agents exit with code 3 (billing error: credits exhausted) or 4 (spending cap or
project cost limit reached). The process manager reports these as ``stopped``
with the reason in the log, so the scheduler (which restarts ``crashed`` runs in
its window) never restarts a run that ran out of credit or budget.

Run with: python -m pytest test_spend_stop_status.py
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from billing_utils import BILLING_ERROR_EXIT_CODE, BUDGET_EXCEEDED_EXIT_CODE
from server.services import process_manager


def _finished_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    (project / ".autoforge").mkdir(parents=True)
    manager = process_manager.AgentProcessManager("demo", project, Path(process_manager.__file__).parents[2])
    proc = MagicMock(returncode=exit_code, pid=999999)
    proc.poll.return_value = exit_code
    proc.stdout.readline.return_value = b""
    manager.process = proc
    manager._status = "running"
    lines: list[str] = []
    statuses: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    async def on_status(status: str) -> None:
        statuses.append(status)

    manager.add_output_callback(on_line)
    manager.add_status_callback(on_status)
    return manager, lines, statuses


@pytest.mark.parametrize(
    ("exit_code", "reason"),
    [(BUDGET_EXCEEDED_EXIT_CODE, "spending limit"), (BILLING_ERROR_EXIT_CODE, "credit balance")],
)
def test_spend_exit_is_stopped_with_a_reason(tmp_path, monkeypatch, exit_code, reason) -> None:
    manager, lines, statuses = _finished_manager(tmp_path, monkeypatch, exit_code)

    async def run() -> None:
        await manager._stream_output()
        await asyncio.sleep(0)  # let status callbacks run

    asyncio.run(run())
    assert manager.status == "stopped"
    assert "crashed" not in statuses  # the scheduler restarts only on "crashed"
    assert any(reason in line.lower() for line in lines), lines


def test_healthcheck_after_a_spend_exit_is_stopped(tmp_path, monkeypatch) -> None:
    manager, lines, statuses = _finished_manager(tmp_path, monkeypatch, BUDGET_EXCEEDED_EXIT_CODE)
    asyncio.run(manager.healthcheck())
    assert manager.status == "stopped"
    assert "crashed" not in statuses
    assert any("spending limit" in line.lower() for line in lines), lines


def test_other_failures_are_still_crashes(tmp_path, monkeypatch) -> None:
    manager, _lines, _statuses = _finished_manager(tmp_path, monkeypatch, 1)
    asyncio.run(manager._stream_output())
    assert manager.status == "crashed"
