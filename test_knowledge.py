"""
Tests for project knowledge documents (issue #22).

Covers the prompt pointer added for every agent session and the REST API
used by the UI to upload/list/read/delete documents.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api import safe_project_io as sio
from prompts import get_knowledge_files, with_project_knowledge
from server.routers import knowledge as knowledge_router

# ---------------------------------------------------------------------------
# Prompt integration
# ---------------------------------------------------------------------------

def test_prompt_unchanged_without_knowledge(tmp_path: Path) -> None:
    assert with_project_knowledge("BASE", tmp_path) == "BASE"
    assert with_project_knowledge("BASE", None) == "BASE"


def test_prompt_lists_knowledge_files(tmp_path: Path) -> None:
    kdir = tmp_path / ".autoforge" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "requirements.md").write_text("# Reqs", encoding="utf-8")
    (kdir / "Research.txt").write_text("notes", encoding="utf-8")
    (kdir / "image.png").write_bytes(b"\x89PNG")

    names = [p.name for p in get_knowledge_files(tmp_path)]
    assert names == ["requirements.md", "Research.txt"]

    prompt = with_project_knowledge("BASE", tmp_path)
    assert prompt.startswith("## PROJECT KNOWLEDGE")
    assert "`.autoforge/knowledge/requirements.md`" in prompt
    assert "`.autoforge/knowledge/Research.txt`" in prompt
    assert "image.png" not in prompt
    assert prompt.endswith("BASE")


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        knowledge_router, "_get_project_path",
        lambda name: tmp_path if name == "demo" else None,
    )
    app = FastAPI()
    app.include_router(knowledge_router.router)
    # Same refusal mapping as server/main.py: a planted link answers 409, never a 500
    app.add_exception_handler(
        sio.UnsafeProjectPathError,
        lambda _req, exc: JSONResponse(status_code=409, content={"detail": str(exc)}),
    )
    return TestClient(app)


def test_upload_list_read_delete(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/api/projects/demo/knowledge").json() == {"files": []}

    res = client.post("/api/projects/demo/knowledge", json={"filename": "prd.md", "content": "# PRD\nDetails"})
    assert res.status_code == 201
    assert res.json()["name"] == "prd.md"
    assert (tmp_path / ".autoforge" / "knowledge" / "prd.md").read_text(encoding="utf-8") == "# PRD\nDetails"

    files = client.get("/api/projects/demo/knowledge").json()["files"]
    assert [f["name"] for f in files] == ["prd.md"]

    res = client.get("/api/projects/demo/knowledge/prd.md")
    assert res.json() == {"name": "prd.md", "content": "# PRD\nDetails"}

    # Re-uploading overwrites
    client.post("/api/projects/demo/knowledge", json={"filename": "prd.md", "content": "v2"})
    assert client.get("/api/projects/demo/knowledge/prd.md").json()["content"] == "v2"

    assert client.delete("/api/projects/demo/knowledge/prd.md").status_code == 204
    assert client.get("/api/projects/demo/knowledge/prd.md").status_code == 404


@pytest.mark.parametrize("filename", [
    "../evil.md",
    ".hidden.md",
    "notes.exe",
    "sub/dir.md",
    "a..md",
])
def test_rejects_bad_filenames(client: TestClient, filename: str) -> None:
    res = client.post("/api/projects/demo/knowledge", json={"filename": filename, "content": "x"})
    assert res.status_code == 400


def test_rejects_oversized_file(client: TestClient) -> None:
    content = "x" * (knowledge_router.MAX_KNOWLEDGE_FILE_SIZE + 1)
    res = client.post("/api/projects/demo/knowledge", json={"filename": "big.md", "content": content})
    assert res.status_code == 413


def test_unknown_project(client: TestClient) -> None:
    assert client.get("/api/projects/other/knowledge").status_code == 404


# ---------------------------------------------------------------------------
# Links the agent planted in .autoforge/knowledge/ (agent-writable) are never followed
# ---------------------------------------------------------------------------

needs_dir_fd = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    path = outside / "secret.md"
    path.write_text("TOP SECRET", encoding="utf-8")
    return path


def _knowledge_dir(project: Path) -> Path:
    kdir = project / ".autoforge" / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    return kdir


@needs_dir_fd
def test_read_refuses_a_symlinked_document(client: TestClient, tmp_path: Path, secret: Path) -> None:
    (_knowledge_dir(tmp_path) / "prd.md").symlink_to(secret)
    res = client.get("/api/projects/demo/knowledge/prd.md")
    assert res.status_code in (404, 409)
    assert "TOP SECRET" not in res.text


@needs_dir_fd
def test_read_refuses_a_hard_linked_document(client: TestClient, tmp_path: Path, secret: Path) -> None:
    (_knowledge_dir(tmp_path) / "prd.md").hardlink_to(secret)
    res = client.get("/api/projects/demo/knowledge/prd.md")
    assert res.status_code == 409
    assert "TOP SECRET" not in res.text


@needs_dir_fd
def test_upload_does_not_write_through_a_symlinked_document(client: TestClient, tmp_path: Path, secret: Path) -> None:
    (_knowledge_dir(tmp_path) / "prd.md").symlink_to(secret)
    res = client.post("/api/projects/demo/knowledge", json={"filename": "prd.md", "content": "overwritten"})
    assert res.status_code == 201
    assert secret.read_text(encoding="utf-8") == "TOP SECRET"
    assert not (tmp_path / ".autoforge" / "knowledge" / "prd.md").is_symlink()


@needs_dir_fd
def test_upload_refuses_a_symlinked_knowledge_dir(client: TestClient, tmp_path: Path, secret: Path) -> None:
    (tmp_path / ".autoforge").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".autoforge" / "knowledge").symlink_to(secret.parent, target_is_directory=True)
    res = client.post("/api/projects/demo/knowledge", json={"filename": "new.md", "content": "x"})
    assert res.status_code in (400, 409)
    assert not (secret.parent / "new.md").exists()


@needs_dir_fd
def test_delete_removes_the_link_not_its_target(client: TestClient, tmp_path: Path, secret: Path) -> None:
    (tmp_path / ".autoforge").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".autoforge" / "knowledge").symlink_to(secret.parent, target_is_directory=True)
    res = client.delete("/api/projects/demo/knowledge/secret.md")
    assert res.status_code in (400, 404, 409)
    assert secret.read_text(encoding="utf-8") == "TOP SECRET"


@needs_dir_fd
def test_list_and_prompt_skip_linked_documents(client: TestClient, tmp_path: Path, secret: Path) -> None:
    kdir = _knowledge_dir(tmp_path)
    (kdir / "real.md").write_text("ok", encoding="utf-8")
    (kdir / "linked.md").symlink_to(secret)
    assert [f["name"] for f in client.get("/api/projects/demo/knowledge").json()["files"]] == ["real.md"]
    assert "linked.md" not in with_project_knowledge("BASE", tmp_path)
