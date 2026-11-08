#!/usr/bin/env python3
"""
Test Evidence Tests
===================

Tests for recording test evidence on features (issue #69): evidence building,
screenshot path validation, the feature MCP tools, and the screenshot endpoint.
Run with: python -m pytest test_test_evidence.py -v
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from api.database import Feature, create_database, dispose_engine
from api.test_evidence import build_test_evidence, prune_evidence_dir, resolve_screenshot_path

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    yield project
    dispose_engine(project)


@pytest.fixture
def screenshot(project_dir):
    shots = project_dir / ".playwright-cli"
    shots.mkdir()
    path = shots / "page.png"
    path.write_bytes(PNG_BYTES)
    return path


@pytest.fixture
def session_maker(project_dir):
    _, session_maker = create_database(project_dir)
    session = session_maker()
    session.add_all([
        Feature(id=1, priority=1, category="core", name="Login", description="d", steps=["s"]),
        Feature(id=2, priority=2, category="core", name="Logout", description="d", steps=["s"], passes=True),
    ])
    session.commit()
    session.close()
    return session_maker


@pytest.fixture
def mcp_module(project_dir, session_maker, monkeypatch):
    from mcp_server import feature_mcp
    monkeypatch.setattr(feature_mcp, "PROJECT_DIR", project_dir.resolve())
    monkeypatch.setattr(feature_mcp, "_session_maker", session_maker)
    return feature_mcp


def _evidence(session_maker, feature_id):
    session = session_maker()
    try:
        return session.query(Feature).filter(Feature.id == feature_id).first().test_evidence
    finally:
        session.close()


# =============================================================================
# resolve_screenshot_path
# =============================================================================


class TestResolveScreenshotPath:
    def test_relative_image_inside_project(self, project_dir, screenshot):
        assert resolve_screenshot_path(project_dir, ".playwright-cli/page.png") == screenshot.resolve()

    def test_absolute_image_inside_project(self, project_dir, screenshot):
        assert resolve_screenshot_path(project_dir, str(screenshot)) == screenshot.resolve()

    def test_rejects_traversal(self, project_dir):
        assert resolve_screenshot_path(project_dir, "../outside.png") is None

    def test_rejects_absolute_outside(self, project_dir, tmp_path):
        assert resolve_screenshot_path(project_dir, str(tmp_path / "outside.png")) is None

    def test_rejects_non_image(self, project_dir):
        assert resolve_screenshot_path(project_dir, ".autoforge/features.db") is None

    def test_rejects_empty_and_null_byte(self, project_dir):
        assert resolve_screenshot_path(project_dir, "") is None
        assert resolve_screenshot_path(project_dir, "a\x00.png") is None


# =============================================================================
# build_test_evidence
# =============================================================================


class TestBuildTestEvidence:
    def test_nothing_supplied_returns_none(self, project_dir):
        evidence, warnings = build_test_evidence(project_dir, 1, "passing")
        assert evidence is None
        assert warnings == []

    def test_builds_and_copies_screenshot(self, project_dir, screenshot):
        evidence, warnings = build_test_evidence(
            project_dir, 7, "passing",
            summary="  works  ",
            test_log=["step 1 -> ok", "", "  step 2 -> ok "],
            screenshots=[".playwright-cli/page.png", ".playwright-cli/page.png"],
        )
        assert warnings == []
        assert evidence["result"] == "passing"
        assert evidence["summary"] == "works"
        assert evidence["test_log"] == ["step 1 -> ok", "step 2 -> ok"]
        assert evidence["recorded_at"]
        # Duplicate collapsed, copied into the evidence folder
        assert len(evidence["screenshots"]) == 1
        stored = evidence["screenshots"][0]
        assert stored.startswith(".autoforge/evidence/feature_7/")
        assert (project_dir / stored).read_bytes() == PNG_BYTES

    def test_rejects_bad_screenshots_with_warnings(self, project_dir, tmp_path):
        outside = tmp_path / "secret.png"
        outside.write_bytes(PNG_BYTES)
        evidence, warnings = build_test_evidence(
            project_dir, 1, "failing",
            summary="broken",
            screenshots=[str(outside), "missing.png", "notes.txt"],
        )
        assert evidence["screenshots"] == []
        assert len(warnings) == 3

    def test_limits_applied(self, project_dir):
        evidence, _ = build_test_evidence(
            project_dir, 1, "passing", summary="x" * 10000, test_log=["s"] * 200,
        )
        assert len(evidence["summary"]) == 4000
        assert len(evidence["test_log"]) == 50

    def test_screenshot_already_in_evidence_dir_not_recopied(self, project_dir):
        evidence_dir = project_dir / ".autoforge" / "evidence" / "feature_3"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "shot.png").write_bytes(PNG_BYTES)
        evidence, _ = build_test_evidence(
            project_dir, 3, "passing", screenshots=[".autoforge/evidence/feature_3/shot.png"],
        )
        assert evidence["screenshots"] == [".autoforge/evidence/feature_3/shot.png"]
        assert len(list(evidence_dir.iterdir())) == 1

    def test_does_not_copy_through_symlinked_evidence_dir(self, project_dir, screenshot, tmp_path):
        # The agent can plant symlinks inside the project; the (unsandboxed) MCP
        # server must not follow one out of the project when copying screenshots.
        outside = tmp_path / "outside"
        outside.mkdir()
        (project_dir / ".autoforge").mkdir()
        (project_dir / ".autoforge" / "evidence").symlink_to(outside, target_is_directory=True)

        evidence, warnings = build_test_evidence(
            project_dir, 5, "passing", screenshots=[".playwright-cli/page.png"],
        )

        assert list(outside.iterdir()) == []
        assert evidence["screenshots"] == [".playwright-cli/page.png"]
        assert len(warnings) == 1

    def test_symlink_swapped_in_after_the_check_is_not_followed(self, project_dir, screenshot, tmp_path, monkeypatch):
        # Race: a background process of the agent replaces the evidence folder with a
        # symlink after any up-front path check. Writes must still stay inside the project.
        import api.test_evidence as te

        outside = tmp_path / "outside"
        outside.mkdir()
        evidence_root = project_dir / ".autoforge" / "evidence"
        evidence_root.mkdir(parents=True)
        real_resolve = te.resolve_screenshot_path

        def resolve_then_swap(project, raw):
            result = real_resolve(project, raw)
            if evidence_root.is_dir() and not evidence_root.is_symlink():
                evidence_root.rmdir()
                evidence_root.symlink_to(outside, target_is_directory=True)
            return result

        monkeypatch.setattr(te, "resolve_screenshot_path", resolve_then_swap)
        evidence, warnings = build_test_evidence(project_dir, 5, "passing", screenshots=[".playwright-cli/page.png"])

        assert list(outside.iterdir()) == []
        assert evidence["screenshots"] == [".playwright-cli/page.png"]
        assert warnings

    def test_source_dir_swapped_after_resolution_is_not_read(self, project_dir, screenshot, tmp_path, monkeypatch):
        # Race: the screenshot's folder is replaced by a symlink to outside data after
        # its path was validated. The copy must not pull the outside file into evidence.
        import api.test_evidence as te

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "page.png").write_bytes(b"SECRET")
        shots = project_dir / ".playwright-cli"
        real_resolve = te.resolve_screenshot_path

        def resolve_then_swap(project, raw):
            result = real_resolve(project, raw)
            if not shots.is_symlink():
                (shots / "page.png").unlink()
                shots.rmdir()
                shots.symlink_to(outside, target_is_directory=True)
            return result

        monkeypatch.setattr(te, "resolve_screenshot_path", resolve_then_swap)
        evidence, warnings = build_test_evidence(project_dir, 8, "passing", screenshots=[".playwright-cli/page.png"])

        copies = list((project_dir / ".autoforge" / "evidence" / "feature_8").glob("*"))
        assert all(c.read_bytes() != b"SECRET" for c in copies)
        assert warnings

    @pytest.mark.parametrize("level", ["autoforge", "evidence", "feature"])
    def test_prune_never_deletes_through_a_symlinked_component(self, project_dir, tmp_path, level):
        outside = tmp_path / "outside"
        target = outside / "evidence" / "feature_4" if level == "autoforge" else (
            outside / "feature_4" if level == "evidence" else outside)
        target.mkdir(parents=True)
        (target / "keep.png").write_bytes(PNG_BYTES)
        link = {"autoforge": project_dir / ".autoforge",
                "evidence": project_dir / ".autoforge" / "evidence",
                "feature": project_dir / ".autoforge" / "evidence" / "feature_4"}[level]
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside if level != "feature" else outside, target_is_directory=True)

        prune_evidence_dir(project_dir, 4, [])

        assert (target / "keep.png").exists()

    @pytest.mark.parametrize("dir_fd", [True, False])
    def test_hard_linked_screenshot_is_not_copied(self, project_dir, tmp_path, monkeypatch, dir_fd):
        # A copy has a single link: it would serve the outside file's bytes as evidence
        import api.test_evidence as te

        monkeypatch.setattr(te, "_DIR_FD_SUPPORTED", dir_fd and te._DIR_FD_SUPPORTED)
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"SECRET-BYTES")
        (project_dir / ".playwright-cli").mkdir()
        os.link(outside, project_dir / ".playwright-cli" / "hard.png")

        evidence, warnings = build_test_evidence(project_dir, 8, "passing", screenshots=[".playwright-cli/hard.png"])

        assert evidence["screenshots"] == [".playwright-cli/hard.png"]  # not copied into evidence
        assert warnings
        copies = project_dir / ".autoforge" / "evidence" / "feature_8"
        assert not copies.exists() or list(copies.iterdir()) == []

    def test_path_fallback_copies_and_refuses_symlinked_dir(self, project_dir, screenshot, tmp_path, monkeypatch):
        # Platforms without dir_fd support (Windows) use the path-based check
        import api.test_evidence as te

        monkeypatch.setattr(te, "_DIR_FD_SUPPORTED", False)
        evidence, warnings = build_test_evidence(project_dir, 6, "passing", screenshots=[".playwright-cli/page.png"])
        assert warnings == []
        assert evidence["screenshots"][0].startswith(".autoforge/evidence/feature_6/")

        outside = tmp_path / "outside"
        outside.mkdir()
        (project_dir / ".autoforge" / "evidence" / "feature_7").symlink_to(outside, target_is_directory=True)
        evidence, warnings = build_test_evidence(project_dir, 7, "passing", screenshots=[".playwright-cli/page.png"])
        assert list(outside.iterdir()) == []
        assert evidence["screenshots"] == [".playwright-cli/page.png"]
        prune_evidence_dir(project_dir, 6, [])
        assert list((project_dir / ".autoforge" / "evidence" / "feature_6").iterdir()) == []

    def test_prune_keeps_only_referenced_copies(self, project_dir):
        evidence_dir = project_dir / ".autoforge" / "evidence" / "feature_4"
        evidence_dir.mkdir(parents=True)
        for name in ("old.png", "new.png"):
            (evidence_dir / name).write_bytes(PNG_BYTES)

        prune_evidence_dir(project_dir, 4, [".autoforge/evidence/feature_4/new.png", ".playwright-cli/x.png"])

        assert sorted(f.name for f in evidence_dir.iterdir()) == ["new.png"]

    def test_prune_ignores_symlinked_evidence_dir(self, project_dir, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.png").write_bytes(PNG_BYTES)
        (project_dir / ".autoforge" / "evidence").mkdir(parents=True)
        (project_dir / ".autoforge" / "evidence" / "feature_4").symlink_to(outside, target_is_directory=True)

        prune_evidence_dir(project_dir, 4, [])

        assert (outside / "keep.png").exists()


# =============================================================================
# Feature MCP tools
# =============================================================================


class TestMcpTools:
    def test_mark_passing_records_evidence(self, mcp_module, session_maker, screenshot):
        result = json.loads(mcp_module.feature_mark_passing(
            1, summary="verified", test_log=["a -> ok"], screenshots=[".playwright-cli/page.png"],
        ))
        assert result["success"] is True
        assert result["evidence_recorded"] is True
        evidence = _evidence(session_maker, 1)
        assert evidence["result"] == "passing"
        assert evidence["summary"] == "verified"
        assert len(evidence["screenshots"]) == 1

    def test_mark_passing_without_evidence_still_works(self, mcp_module, session_maker):
        result = json.loads(mcp_module.feature_mark_passing(1))
        assert result["success"] is True
        assert result["evidence_recorded"] is False
        assert _evidence(session_maker, 1) is None

    def test_mark_passing_reports_screenshot_warnings(self, mcp_module):
        result = json.loads(mcp_module.feature_mark_passing(1, summary="ok", screenshots=["../x.png"]))
        assert result["success"] is True
        assert len(result["warnings"]) == 1

    def test_mark_passing_already_passing_keeps_evidence_untouched(self, mcp_module, session_maker):
        result = json.loads(mcp_module.feature_mark_passing(2, summary="should not be stored"))
        assert "error" in result
        assert _evidence(session_maker, 2) is None

    def test_mark_failing_records_evidence(self, mcp_module, session_maker):
        result = json.loads(mcp_module.feature_mark_failing(2, summary="button does nothing"))
        assert result["evidence_recorded"] is True
        assert "test_evidence" not in result["feature"]  # kept out of agent context
        evidence = _evidence(session_maker, 2)
        assert evidence["result"] == "failing"
        assert evidence["summary"] == "button does nothing"

    def test_mark_failing_without_evidence_preserves_previous(self, mcp_module, session_maker):
        mcp_module.feature_mark_passing(1, summary="first")
        mcp_module.feature_mark_failing(1)
        assert _evidence(session_maker, 1)["summary"] == "first"

    def test_record_evidence(self, mcp_module, session_maker):
        result = json.loads(mcp_module.feature_record_evidence(2, test_log=["re-tested -> ok"]))
        assert result["success"] is True
        evidence = _evidence(session_maker, 2)
        assert evidence["result"] == "passing"
        assert evidence["test_log"] == ["re-tested -> ok"]

    def test_record_evidence_requires_content(self, mcp_module):
        assert "error" in json.loads(mcp_module.feature_record_evidence(2))

    def test_record_evidence_unknown_feature(self, mcp_module):
        assert "error" in json.loads(mcp_module.feature_record_evidence(999, summary="x"))

    def test_new_evidence_removes_unreferenced_copies(self, mcp_module, session_maker, project_dir, screenshot):
        mcp_module.feature_record_evidence(2, screenshots=[".playwright-cli/page.png"])
        mcp_module.feature_record_evidence(2, screenshots=[".playwright-cli/page.png"])
        evidence_dir = project_dir / ".autoforge" / "evidence" / "feature_2"
        stored = _evidence(session_maker, 2)["screenshots"]
        assert [f".autoforge/evidence/feature_2/{f.name}" for f in evidence_dir.iterdir()] == stored

    def test_rejected_mark_passing_leaves_no_copy(self, mcp_module, session_maker, project_dir, screenshot):
        # Feature 2 is already passing: the call fails and must not leave an orphan copy
        result = json.loads(mcp_module.feature_mark_passing(2, screenshots=[".playwright-cli/page.png"]))
        assert "error" in result
        evidence_dir = project_dir / ".autoforge" / "evidence" / "feature_2"
        assert not evidence_dir.exists() or list(evidence_dir.iterdir()) == []

    def test_get_by_id_excludes_evidence(self, mcp_module):
        mcp_module.feature_mark_passing(1, summary="verified")
        assert "test_evidence" not in json.loads(mcp_module.feature_get_by_id(1))


# =============================================================================
# REST API
# =============================================================================


@pytest.fixture
def client(project_dir, session_maker, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.routers import features as features_router

    monkeypatch.setattr(features_router, "_get_project_path", lambda name: project_dir)
    app = FastAPI()
    app.include_router(features_router.router)
    return TestClient(app)


def _set_evidence(session_maker, feature_id, evidence):
    session = session_maker()
    feature = session.query(Feature).filter(Feature.id == feature_id).first()
    feature.test_evidence = evidence
    session.commit()
    session.close()


class TestApi:
    def test_feature_response_includes_evidence(self, client, session_maker):
        _set_evidence(session_maker, 2, {"result": "passing", "summary": "ok", "test_log": [],
                                         "screenshots": [], "recorded_at": "2026-01-01T00:00:00+00:00"})
        body = client.get("/api/projects/proj/features/2").json()
        assert body["test_evidence"]["summary"] == "ok"
        assert client.get("/api/projects/proj/features/1").json()["test_evidence"] is None

    def test_serves_screenshot(self, client, session_maker, screenshot):
        _set_evidence(session_maker, 2, {"screenshots": [".playwright-cli/page.png"]})
        resp = client.get("/api/projects/proj/features/2/evidence/screenshots/0")
        assert resp.status_code == 200
        assert resp.content == PNG_BYTES
        assert resp.headers["content-type"] == "image/png"

    def test_index_out_of_range(self, client, session_maker, screenshot):
        _set_evidence(session_maker, 2, {"screenshots": [".playwright-cli/page.png"]})
        assert client.get("/api/projects/proj/features/2/evidence/screenshots/1").status_code == 404
        assert client.get("/api/projects/proj/features/1/evidence/screenshots/0").status_code == 404

    def test_hard_linked_screenshot_is_not_served(self, client, session_maker, project_dir, tmp_path):
        outside = tmp_path / "secret.png"
        outside.write_bytes(PNG_BYTES)
        (project_dir / ".playwright-cli").mkdir()
        os.link(outside, project_dir / ".playwright-cli" / "hard.png")
        _set_evidence(session_maker, 2, {"screenshots": [".playwright-cli/hard.png"]})
        assert client.get("/api/projects/proj/features/2/evidence/screenshots/0").status_code == 404

    def test_tampered_path_is_not_served(self, client, session_maker, project_dir, tmp_path):
        outside = tmp_path / "secret.png"
        outside.write_bytes(PNG_BYTES)
        (project_dir / "notes.txt").write_text("hi")
        _set_evidence(session_maker, 2, {"screenshots": [str(outside), "../secret.png", "notes.txt"]})
        for i in range(3):
            assert client.get(f"/api/projects/proj/features/2/evidence/screenshots/{i}").status_code == 404
