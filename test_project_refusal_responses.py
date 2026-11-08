"""
A refused project path is reported, not turned into a generic 500 or false data
===============================================================================

When safe_project_io / the database check refuses a path because of a link the
agent planted, the API answers 409 with the reason (server.main exception
handler), and the progress counters raise instead of reporting 0/0.

Run with: python -m pytest test_project_refusal_responses.py
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import database
from api import safe_project_io as sio

pytestmark = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")


@pytest.fixture
def project(tmp_path: Path) -> Any:
    folder = tmp_path / "project"
    (folder / ".autoforge").mkdir(parents=True)
    yield folder
    database.dispose_engine(folder)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    return folder


def _client(monkeypatch: pytest.MonkeyPatch, project: Path) -> TestClient:
    """The real routers plus the real handler from server.main."""
    from server import main
    from server.routers import features, projects

    registry_functions = (
        None,  # register_project
        None,  # unregister_project
        lambda _name: project,  # get_project_path
        lambda: {"demo": {"path": str(project)}},  # list_registered_projects
        lambda _path: (True, ""),  # validate_project_path
        lambda _name: 3,  # get_project_concurrency
        None, None, None,
    )
    monkeypatch.setattr(projects, "_get_registry_functions", lambda: registry_functions)
    monkeypatch.setattr(features, "_get_project_path", lambda _name: project)

    app = FastAPI()
    app.add_exception_handler(sio.UnsafeProjectPathError, main._unsafe_project_path_handler)
    app.include_router(projects.router)
    app.include_router(features.router)
    return TestClient(app, raise_server_exceptions=False)


def _plant_db(project: Path, outside: Path) -> None:
    (outside / "other.db").write_bytes(b"")
    (project / ".autoforge" / "features.db").symlink_to(outside / "other.db")


def test_main_app_registers_the_handler() -> None:
    from server.main import app

    assert sio.UnsafeProjectPathError in app.exception_handlers


def test_prompt_update_through_planted_link_is_409(project: Path, outside: Path, monkeypatch) -> None:
    (project / ".autoforge" / "prompts").symlink_to(outside, target_is_directory=True)
    client = _client(monkeypatch, project)

    response = client.put("/api/projects/demo/prompts", json={"coding_prompt": "x"})

    assert response.status_code == 409
    assert "symlink" in response.json()["detail"]
    assert list(outside.iterdir()) == []


def test_refused_features_db_is_409_not_database_error(project: Path, outside: Path, monkeypatch) -> None:
    _plant_db(project, outside)
    client = _client(monkeypatch, project)

    for url in ("/api/projects/demo/features", "/api/projects/demo/stats"):
        response = client.get(url)
        assert response.status_code == 409, url
        assert "Refusing to open the project database" in response.json()["detail"]


def test_project_list_survives_one_refused_database(project: Path, outside: Path, monkeypatch) -> None:
    _plant_db(project, outside)
    client = _client(monkeypatch, project)

    response = client.get("/api/projects")

    assert response.status_code == 200
    summary = response.json()[0]
    assert summary["stats"]["total"] == 0
    # The reason reaches the UI instead of a silent 0/0
    assert "Refusing to open the project database" in summary["error"]


def test_project_list_has_no_error_for_a_healthy_project(project: Path, monkeypatch) -> None:
    client = _client(monkeypatch, project)

    assert client.get("/api/projects").json()[0]["error"] is None


def test_count_passing_tests_raises_instead_of_zeros(project: Path, outside: Path) -> None:
    import progress

    _plant_db(project, outside)
    with pytest.raises(sio.UnsafeProjectPathError):
        progress.count_passing_tests(project)
