"""
Orchestrator Rate-Limit Pause Tests
===================================

The orchestrator pauses all agents when an agent reports Claude's usage limit
through the [RATE_LIMIT] marker (#19).

Run with: python -m pytest test_orchestrator_rate_limit.py
"""

import asyncio

import pytest

import parallel_orchestrator
from parallel_orchestrator import ParallelOrchestrator
from rate_limit_utils import MAX_LIMIT_RESET_SECONDS


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    monkeypatch.setattr(parallel_orchestrator.debug_log, "log_file", tmp_path / "debug.log")
    orch = ParallelOrchestrator(tmp_path)
    yield orch
    orch.cleanup()


def test_note_rate_limit_pauses_for_reported_time(orchestrator):
    orchestrator._note_rate_limit(120)

    assert 119 <= orchestrator._rate_limit_remaining() <= 120


def test_oversized_retry_in_is_clamped(orchestrator):
    # A marker value far beyond any real reset time (or one large enough to
    # overflow datetime arithmetic) must not freeze the orchestrator for years.
    orchestrator._note_rate_limit(10**12)

    assert orchestrator._rate_limit_remaining() <= MAX_LIMIT_RESET_SECONDS


def test_tracker_reports_rate_limit_pause_until_resume():
    from server.websocket import OrchestratorTracker

    async def scenario():
        tracker = OrchestratorTracker()
        paused = await tracker.process_line(
            "Claude rate limit reached - Pausing all agents for 3600s (until 2026-11-08 18:00 EET)"
        )
        resumed = await tracker.process_line("Rate limit pause over - resuming agents")
        return paused, resumed

    paused, resumed = asyncio.run(scenario())

    assert paused is not None and paused["state"] == "rate_limited"
    assert paused["rateLimitedUntil"] == "2026-11-08 18:00 EET"
    assert resumed is not None and resumed["state"] != "rate_limited"
    assert resumed["rateLimitedUntil"] is None
