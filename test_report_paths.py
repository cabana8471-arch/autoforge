"""
Review and security reports are listed where the generators write them
======================================================================

review_agent / security_scanner save reports through autoforge_paths
(.autoforge/... for current projects), but the routers read a hard-coded
.autocoder/... path, so reports never showed up for current projects.
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autoforge_paths import get_review_reports_dir, get_security_reports_dir
from server.routers import review, security


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project_dir = tmp_path / "demo"
    (project_dir / ".autoforge").mkdir(parents=True)
    monkeypatch.setattr(review, "get_project_dir", lambda name: project_dir)
    monkeypatch.setattr(security, "_get_project_path", lambda name: project_dir)
    return project_dir


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(review.router)
    app.include_router(security.router)
    return TestClient(app)


def test_review_reports_are_listed_and_served_from_the_generator_dir(project: Path) -> None:
    reports_dir = get_review_reports_dir(project)
    assert ".autoforge" in reports_dir.parts
    reports_dir.mkdir(parents=True)
    report = {"project_dir": str(project), "review_time": "2026-11-08T12:00:00", "commits_reviewed": [],
              "files_reviewed": [], "issues": [], "summary": {}}
    (reports_dir / "review_20261108_120000.json").write_text(json.dumps(report), encoding="utf-8")

    client = _client()
    listed = client.get("/api/review/reports/demo").json()
    assert listed["count"] == 1
    assert client.get("/api/review/reports/demo/review_20261108_120000.json").status_code == 200


def test_security_reports_are_listed_from_the_generator_dir(project: Path) -> None:
    reports_dir = get_security_reports_dir(project)
    assert ".autoforge" in reports_dir.parts
    reports_dir.mkdir(parents=True)
    (reports_dir / "security_scan_20261108_120000.json").write_text(json.dumps({"vulnerabilities": []}))

    listed = _client().get("/api/security/reports/demo").json()
    assert listed["count"] == 1
