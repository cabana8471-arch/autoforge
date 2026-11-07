"""
Agent Loop Completion Tests
===========================

Regression tests for #13 in agent.py: the session loop decides that the
project is complete from the feature database, not from the agent's reply.

Run with: python -m pytest test_agent_completion.py
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent
from rate_limit_utils import parse_rate_limit_marker


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def loop_env(monkeypatch):
    """Stub everything around the loop; tests set the DB counts and session result."""
    session = AsyncMock()
    counts = MagicMock()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(agent, "create_client", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(agent, "run_agent_session", session)
    monkeypatch.setattr(agent, "count_passing_tests", counts)
    monkeypatch.setattr(agent, "print_progress_summary", lambda *_: None)
    monkeypatch.setattr(agent, "get_coding_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(agent.asyncio, "sleep", no_sleep)
    return session, counts


def _run(project_dir: Path, max_iterations: int) -> None:
    asyncio.run(agent.run_autonomous_agent(
        project_dir, model="test-model", max_iterations=max_iterations, agent_type="coding",
    ))


def test_exits_when_database_shows_all_passing_after_failed_session(loop_env, tmp_path):
    session, counts = loop_env
    # Before the first session 1/2 pass; the session then completes the last feature but ends in error.
    counts.side_effect = [(1, 0, 2, 0), (2, 0, 2, 0)]
    session.return_value = ("error", "connection reset")

    _run(tmp_path, max_iterations=3)

    assert session.await_count == 1


def test_reply_text_alone_does_not_end_the_loop(loop_env, tmp_path):
    session, counts = loop_env
    counts.return_value = (1, 0, 2, 0)
    session.return_value = ("error", "All features are passing. No more work to do.")

    _run(tmp_path, max_iterations=2)

    assert session.await_count == 2


def test_feature_agent_hands_usage_limit_to_orchestrator(loop_env, tmp_path, monkeypatch, capsys):
    session, counts = loop_env
    counts.return_value = (1, 0, 2, 0)
    session.return_value = ("continue", "Limit reached · resets 6pm (America/New_York)")
    monkeypatch.setattr(agent, "get_single_feature_prompt", lambda *a, **k: "prompt")

    asyncio.run(agent.run_autonomous_agent(
        tmp_path, model="test-model", max_iterations=3, agent_type="coding", feature_id=2,
    ))

    assert session.await_count == 1
    marker_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[RATE_LIMIT] retry_in=")]
    assert len(marker_lines) == 1
    assert parse_rate_limit_marker(marker_lines[0])[0] is True
