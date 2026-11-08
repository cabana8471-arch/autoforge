"""
Tests for token/cost usage tracking and cost limits (usage_tracker.py).

Run with: python -m pytest test_usage_tracker.py
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

import registry
import usage_tracker
from api.database import dispose_engine


@dataclass
class FakeResultMessage:
    total_cost_usd: float | None = 0.0
    usage: dict | None = None
    duration_ms: int = 0
    num_turns: int = 0


@pytest.fixture
def project_dir(tmp_path: Path):
    yield tmp_path
    dispose_engine(tmp_path)


@pytest.fixture
def cost_limit(monkeypatch):
    """Set the max_cost_usd setting without touching the real registry."""
    settings: dict[str, str] = {}

    def fake_get_setting(key, default=None):
        return settings.get(key, default)

    monkeypatch.setattr(registry, "get_setting", fake_get_setting)

    def set_limit(value: str | None):
        if value is None:
            settings.pop(usage_tracker.COST_LIMIT_SETTING, None)
        else:
            settings[usage_tracker.COST_LIMIT_SETTING] = value

    return set_limit


def test_extract_usage_full():
    msg = FakeResultMessage(
        total_cost_usd=0.1234,
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 2000,
        },
        duration_ms=4200,
        num_turns=7,
    )
    assert usage_tracker.extract_usage(msg) == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_tokens": 10,
        "cache_read_tokens": 2000,
        "cost_usd": 0.1234,
        "duration_ms": 4200,
        "num_turns": 7,
    }


def test_extract_usage_tolerates_missing_and_bad_values():
    msg = FakeResultMessage(total_cost_usd=None, usage={"input_tokens": "oops"})
    usage = usage_tracker.extract_usage(msg)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cost_usd"] == 0.0

    assert usage_tracker.extract_usage(object())["cost_usd"] == 0.0


def test_totals_empty_without_database(project_dir):
    totals = usage_tracker.get_usage_totals(project_dir)
    assert totals["sessions"] == 0
    assert totals["cost_usd"] == 0.0
    assert usage_tracker.get_recent_sessions(project_dir) == []


def test_record_and_sum_usage(project_dir):
    usage_tracker.record_usage(
        project_dir,
        {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5},
        agent_type="coding",
        feature_ids=[1, 2],
        model="claude-opus-4-7",
    )
    usage_tracker.record_usage(
        project_dir,
        {"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3, "cost_usd": 0.25},
        agent_type="testing",
    )

    totals = usage_tracker.get_usage_totals(project_dir)
    assert totals["sessions"] == 2
    assert totals["input_tokens"] == 11
    assert totals["output_tokens"] == 7
    assert totals["cache_read_tokens"] == 3
    assert totals["cost_usd"] == pytest.approx(0.75)

    recent = usage_tracker.get_recent_sessions(project_dir)
    assert [r["agent_type"] for r in recent] == ["testing", "coding"]
    assert recent[1]["feature_ids"] == [1, 2]
    assert recent[1]["model"] == "claude-opus-4-7"


def test_reset_usage(project_dir):
    usage_tracker.record_usage(project_dir, {"cost_usd": 1.0})
    assert usage_tracker.reset_usage(project_dir) == 1
    assert usage_tracker.get_usage_totals(project_dir)["cost_usd"] == 0.0


@pytest.mark.parametrize("raw", [None, "", "0", "-5", "abc"])
def test_no_limit_when_unset_or_invalid(project_dir, cost_limit, raw):
    cost_limit(raw)
    usage_tracker.record_usage(project_dir, {"cost_usd": 100.0})
    assert usage_tracker.get_cost_limit() is None
    assert usage_tracker.check_cost_limit(project_dir) == (False, 0.0, None)


def test_limit_enforced(project_dir, cost_limit):
    cost_limit("1.5")
    usage_tracker.record_usage(project_dir, {"cost_usd": 1.0})
    reached, total, limit = usage_tracker.check_cost_limit(project_dir)
    assert (reached, limit) == (False, 1.5)
    assert total == pytest.approx(1.0)

    usage_tracker.record_usage(project_dir, {"cost_usd": 0.5})
    reached, total, limit = usage_tracker.check_cost_limit(project_dir)
    assert reached is True
    assert total == pytest.approx(1.5)

    msg = usage_tracker.format_cost_limit_message(total, limit)
    assert "$1.50" in msg


# ---------------------------------------------------------------------------
# run_agent_session records the SDK's ResultMessage (merged with #294's handler)
# ---------------------------------------------------------------------------


class _Client:
    def __init__(self, messages):
        self.messages = messages

    async def query(self, message):
        pass

    async def receive_response(self):
        for msg in self.messages:
            yield msg

    async def interrupt(self):
        pass


def _result(cost: float = 0.42) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1500, duration_api_ms=1200, is_error=False, num_turns=3,
        session_id="s1", total_cost_usd=cost,
        usage={"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 900,
               "cache_creation_input_tokens": 40},
    )


def test_agent_session_records_the_result_message(project_dir):
    import asyncio

    from agent import run_agent_session

    status, _ = asyncio.run(run_agent_session(
        _Client([_result()]), "prompt", project_dir,
        usage_context={"agent_type": "coding", "feature_ids": [7], "model": "claude-opus-4-7"},
    ))

    assert status == "continue"
    [row] = usage_tracker.get_recent_sessions(project_dir)
    assert row["cost_usd"] == pytest.approx(0.42)
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (120, 30, 900)
    assert (row["agent_type"], row["feature_ids"], row["num_turns"]) == ("coding", [7], 3)


def test_agent_session_without_result_message_records_nothing(project_dir):
    import asyncio

    from agent import run_agent_session

    asyncio.run(run_agent_session(_Client([]), "prompt", project_dir))
    assert usage_tracker.get_recent_sessions(project_dir) == []


# ---------------------------------------------------------------------------
# A refused features.db (planted link) is an error, never a silent $0 spend
# ---------------------------------------------------------------------------


def _plant_linked_db(project_dir: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside") / "features.db"
    usage_tracker.record_usage(outside.parent, {"cost_usd": 9.0})  # a real DB elsewhere
    dispose_engine(outside.parent)
    real = outside.parent / ".autoforge" / "features.db"
    (project_dir / ".autoforge").mkdir(parents=True, exist_ok=True)
    (project_dir / ".autoforge" / "features.db").symlink_to(real)


def test_refused_database_is_not_reported_as_zero_spend(project_dir, tmp_path_factory, cost_limit):
    from api.safe_project_io import UnsafeProjectPathError

    _plant_linked_db(project_dir, tmp_path_factory)
    cost_limit("1")
    with pytest.raises(UnsafeProjectPathError):
        usage_tracker.get_usage_totals(project_dir)
    with pytest.raises(UnsafeProjectPathError):
        usage_tracker.get_recent_sessions(project_dir)
    with pytest.raises(UnsafeProjectPathError):
        usage_tracker.check_cost_limit(project_dir)


def test_ui_start_refused_once_the_project_limit_is_reached(tmp_path, monkeypatch, cost_limit):
    import asyncio
    from unittest.mock import patch

    from server.services import process_manager

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    (project / ".autoforge").mkdir(parents=True)
    cost_limit("2")
    usage_tracker.record_usage(project, {"cost_usd": 2.5})
    manager = process_manager.AgentProcessManager("demo", project, Path(process_manager.__file__).parents[2])
    try:
        with patch.object(process_manager.subprocess, "Popen") as popen:
            ok, message = asyncio.run(manager.start())
    finally:
        dispose_engine(project)

    assert not ok
    assert "Cost limit reached: $2.50 spent of $2.00" in message
    popen.assert_not_called()
