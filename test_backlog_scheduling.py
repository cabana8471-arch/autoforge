#!/usr/bin/env python3
"""
Backlog features outside progress and scheduling
================================================

Backlog features (#326) are not committed work: agents never pick them up, so
they must not count toward progress or keep a run "unfinished", and a feature
that depends on one must say so instead of claiming it waits on unfinished
work (plan Change 6.4).

Run with: python -m pytest test_backlog_scheduling.py
"""

import json

import pytest

from api.database import Feature, create_database
from api.dependency_resolver import build_graph_data, explain_stalled_features, get_blocked_features

FEATURES = [
    {"id": 1, "priority": 1, "category": "a", "name": "one", "dependencies": [], "passes": True},
    {"id": 2, "priority": 2, "category": "a", "name": "parked", "dependencies": [], "passes": False,
     "backlog": True},
    {"id": 3, "priority": 3, "category": "a", "name": "needs parked", "dependencies": [2], "passes": False},
]


def _make_project(tmp_path, rows):
    engine, SessionLocal = create_database(tmp_path)
    session = SessionLocal()
    session.add_all(rows)
    session.commit()
    session.close()
    return engine, SessionLocal


def test_progress_total_excludes_backlog(tmp_path):
    from progress import count_passing_tests

    engine, _ = _make_project(tmp_path, [
        Feature(id=1, priority=1, category="c", name="done", description="d", steps=[], passes=True),
        Feature(id=2, priority=2, category="c", name="parked", description="d", steps=[], backlog=True),
    ])
    engine.dispose()
    passing, _in_progress, total, _nhi = count_passing_tests(tmp_path)
    assert (passing, total) == (1, 1)


def test_stall_reasons_name_the_backlog_dependency():
    reasons = explain_stalled_features(FEATURES)
    assert 2 not in reasons  # a Backlog feature is not stalled work
    assert "Backlog" in reasons[3] and "#2" in reasons[3]


def test_backlog_feature_is_not_listed_as_blocked():
    parked_dependent = {"id": 4, "priority": 4, "category": "a", "name": "four", "dependencies": [3],
                        "passes": False, "backlog": True}
    features = [*FEATURES, parked_dependent]
    assert [f["id"] for f in get_blocked_features(features)] == [3]
    statuses = {n["id"]: n["status"] for n in build_graph_data(features)["nodes"]}
    assert statuses[2] == "backlog"
    assert statuses[4] == "backlog"


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    import parallel_orchestrator

    monkeypatch.setattr(parallel_orchestrator.debug_log, "log_file", tmp_path / "debug.log")
    orch = parallel_orchestrator.ParallelOrchestrator(tmp_path, testing_agent_ratio=1, batch_size=3)
    yield orch
    orch.cleanup()


def test_dependent_of_backlog_stops_with_a_clear_report(orchestrator, capsys):
    assert not orchestrator.get_all_complete(FEATURES)
    assert orchestrator.get_stall_state(FEATURES) == "blocked"
    orchestrator._report_stalled_queue(FEATURES)
    out = capsys.readouterr().out
    assert "1 unfinished feature(s)" in out
    assert "Feature #3" in out and "Backlog" in out
    assert "Feature #2" not in out


def test_completion_message_mentions_backlog(orchestrator):
    done_and_parked = FEATURES[:2]
    assert orchestrator.get_all_complete(done_and_parked)
    message = orchestrator.completion_message(done_and_parked)
    assert message != "All features complete!"
    assert "1 feature(s) stay in the Backlog" in message


def test_mcp_stats_exclude_backlog(tmp_path, monkeypatch):
    feature_mcp = pytest.importorskip("mcp_server.feature_mcp")

    engine, SessionLocal = _make_project(tmp_path, [
        Feature(id=1, priority=1, category="c", name="done", description="d", steps=[], passes=True),
        Feature(id=2, priority=2, category="c", name="parked", description="d", steps=[], backlog=True),
    ])
    monkeypatch.setattr(feature_mcp, "_session_maker", SessionLocal)
    stats = json.loads(feature_mcp.feature_get_stats())
    engine.dispose()
    assert stats["total"] == 1
    assert stats["percentage"] == 100.0
    assert stats["backlog"] == 1
