"""
Editing the project allowlist (.autoforge/allowed_commands.yaml) from the UI (user decision 3B).

The agent cannot write the file (settings deny, sandbox denyWrite, Bash hook),
so the user edits it through the server: the text is validated with the same
rules the security hook applies, and it is read and written without following
a link planted in the project.
"""

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import api.safe_project_io as sio
from security import load_project_commands, validate_project_config_text
from server.routers import allowed_commands as allowed_commands_router

VALID = """version: 1
commands:
  - name: swift
    description: Swift compiler
  - name: ./scripts/build.sh
pkill_processes:
  - vite
"""


# --- validate_project_config_text -------------------------------------------


def test_valid_config_has_no_errors():
    assert validate_project_config_text(VALID) == []


def test_comment_only_file_is_valid():
    """The shipped example is all comments: it means 'no project commands'."""
    assert validate_project_config_text("# version: 1\n# commands: []\n") == []


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("version: 1\ncommands: [\n", "YAML"),
        ("- a\n- b\n", "mapping"),
        ("commands: []\n", "version"),
        ("version: 1\ncommands: swift\n", "list"),
        ("version: 1\ncommands:\n  - swift\n", "commands[0]"),
        ("version: 1\ncommands:\n  - name: sudo\n", "sudo"),
        ("version: 1\ncommands:\n  - name: '*'\n", "wildcard"),
        ("version: 1\ncommands:\n  - name: swift\n    description: 3\n", "Description"),
        ("version: 1\npkill_processes: ['a.*']\n", "pkill_processes"),
    ],
)
def test_invalid_config_is_reported(text, fragment):
    errors = validate_project_config_text(text)
    assert errors, text
    assert any(fragment in e for e in errors), errors


def test_blank_name_is_refused():
    """The loader drops the whole file for a blank name, so the editor must not save one."""
    text = 'version: 1\ncommands:\n  - name: swift\n  - name: "   "\n'
    assert any("commands[1]" in e for e in validate_project_config_text(text))


def test_more_than_100_commands_is_refused():
    text = "version: 1\ncommands:\n" + "".join(f"  - name: tool{i}\n" for i in range(101))
    assert any("100" in e for e in validate_project_config_text(text))


# --- REST API ---------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".autoforge").mkdir()
    return tmp_path


@pytest.fixture
def client(project: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        allowed_commands_router, "_get_project_path",
        lambda name: project if name == "demo" else None,
    )
    app = FastAPI()
    app.include_router(allowed_commands_router.router)
    app.add_exception_handler(
        sio.UnsafeProjectPathError,
        lambda _req, exc: JSONResponse(status_code=409, content={"detail": str(exc)}),
    )
    return TestClient(app)


URL = "/api/projects/demo/allowed-commands"


def test_get_without_a_file(client: TestClient):
    r = client.get(URL)
    assert r.status_code == 200
    assert r.json() == {"exists": False, "content": "", "path": ".autoforge/allowed_commands.yaml"}


def test_put_writes_a_file_the_security_hook_loads(client: TestClient, project: Path):
    r = client.put(URL, json={"content": VALID})
    assert r.status_code == 200, r.text
    assert r.json()["exists"] is True
    assert (project / ".autoforge" / "allowed_commands.yaml").read_text(encoding="utf-8") == VALID
    config = load_project_commands(project)
    assert config is not None
    assert [c["name"] for c in config["commands"]] == ["swift", "./scripts/build.sh"]
    assert client.get(URL).json()["content"] == VALID


def test_invalid_put_is_refused_and_keeps_the_file(client: TestClient, project: Path):
    target = project / ".autoforge" / "allowed_commands.yaml"
    target.write_text(VALID, encoding="utf-8")
    r = client.put(URL, json={"content": "version: 1\ncommands:\n  - name: sudo\n"})
    assert r.status_code == 400
    assert "sudo" in r.json()["detail"]
    assert target.read_text(encoding="utf-8") == VALID


def test_get_shows_the_legacy_file_until_one_is_saved(client: TestClient, project: Path):
    legacy = project / ".autocoder"
    legacy.mkdir()
    (legacy / "allowed_commands.yaml").write_text(VALID, encoding="utf-8")
    r = client.get(URL).json()
    assert r == {"exists": True, "content": VALID, "path": ".autocoder/allowed_commands.yaml"}
    assert client.put(URL, json={"content": "version: 1\ncommands: []\n"}).status_code == 200
    assert client.get(URL).json()["path"] == ".autoforge/allowed_commands.yaml"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_planted_link_is_never_followed(client: TestClient, project: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "victim.yaml"
    outside.write_text("secret: 1\n", encoding="utf-8")
    (project / ".autoforge" / "allowed_commands.yaml").symlink_to(outside)

    assert client.get(URL).status_code == 409
    r = client.put(URL, json={"content": VALID})
    assert r.status_code in (200, 409)
    assert outside.read_text(encoding="utf-8") == "secret: 1\n"


def test_oversized_content_is_refused(client: TestClient, project: Path):
    r = client.put(URL, json={"content": "#" * 70_000})
    assert r.status_code == 422
    assert not (project / ".autoforge" / "allowed_commands.yaml").exists()


def test_unknown_project_is_404(client: TestClient):
    assert client.get("/api/projects/other/allowed-commands").status_code == 404
