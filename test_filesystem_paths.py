"""
Tests for pasted/typed path handling in the folder browser API (#16).

Run with: python -m pytest test_filesystem_paths.py
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers import filesystem
from server.routers.filesystem import normalize_input_path


@pytest.mark.parametrize("raw, expected", [
    ('"/srv/my app"', "/srv/my app"),
    ("'/srv/my app'", "/srv/my app"),
    ("  /srv/app  ", "/srv/app"),
    ('  " /srv/app "  ', "/srv/app"),
    ('"/srv/app', '"/srv/app'),  # unbalanced quote is kept
])
def test_normalize_strips_whitespace_and_quotes(raw, expected):
    assert normalize_input_path(raw) == expected


def test_normalize_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert normalize_input_path("~/projects") == str(tmp_path / "projects")


def test_normalized_unc_path_is_still_blocked():
    app = FastAPI()
    app.include_router(filesystem.router)
    client = TestClient(app)

    resp = client.get("/api/filesystem/list", params={"path": '"\\\\server\\share"'})

    assert resp.status_code == 403


def test_pasted_quoted_path_lists_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "is_path_blocked", lambda _p: False)
    (tmp_path / "with space").mkdir()
    (tmp_path / "with space" / "child").mkdir()
    app = FastAPI()
    app.include_router(filesystem.router)
    client = TestClient(app)

    resp = client.get("/api/filesystem/list", params={"path": f'  "{tmp_path / "with space"}"  '})

    assert resp.status_code == 200
    assert Path(resp.json()["current_path"]) == (tmp_path / "with space").resolve()
