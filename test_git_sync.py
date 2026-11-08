"""
Git auto-push (opt-in, per project)
===================================

The orchestrator pushes outside the agent sandbox, and the agent may run any
``git`` command in the project, so a push must only reach the URL the user
trusted when enabling auto-push (plan Change 6.5):

- the push goes to the stored URL, never to whatever the named remote says now;
- a changed remote URL, or repo-local config that can redirect the push or run
  a program (``url.*.insteadOf``/``pushInsteadOf``, ``core.sshCommand``,
  ``credential.*``, ``include.*``, ...), refuses the push;
- hooks never run (``core.hooksPath=/dev/null`` + ``--no-verify``);
- never a force push; the push runs off the orchestrator's main path.

Run with: python -m pytest test_git_sync.py
"""

import subprocess
import threading
import time
from pathlib import Path

import pytest

import git_sync


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def settings_store(monkeypatch):
    """Registry settings in memory (never the real ~/.autoforge)."""
    import registry

    store: dict[str, str] = {}
    monkeypatch.setattr(registry, "get_setting", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(registry, "set_setting", lambda key, value: store.__setitem__(key, value))
    return store


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "commit.gpgsign", "false")
    (project / "README.md").write_text("hello\n")
    _git(project, "add", ".")
    _git(project, "commit", "-q", "-m", "initial")
    return project


def _bare(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    return bare


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    return _bare(tmp_path, "remote.git")


@pytest.fixture
def enabled(repo: Path, remote: Path) -> Path:
    _git(repo, "remote", "add", "origin", str(remote))
    git_sync.enable_auto_push(repo, "origin")
    return repo


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _branches(bare: Path) -> str:
    return _git(bare, "for-each-ref", "--format=%(refname)")


# --- enabling ---------------------------------------------------------------


def test_off_by_default(repo: Path, remote: Path):
    _git(repo, "remote", "add", "origin", str(remote))
    assert git_sync.get_push_target(repo) is None
    assert git_sync.push_current_branch(repo) == (False, "auto-push is off for this project")
    assert _branches(remote) == ""


def test_enabling_stores_the_remote_url(repo: Path, remote: Path):
    _git(repo, "remote", "add", "origin", str(remote))
    target = git_sync.enable_auto_push(repo, "origin")
    assert target == {"remote": "origin", "url": str(remote)}
    assert git_sync.get_push_target(repo) == target
    git_sync.disable_auto_push(repo)
    assert git_sync.get_push_target(repo) is None


def test_enabling_refuses_unknown_remote(repo: Path):
    with pytest.raises(git_sync.GitPushError, match="no remote 'origin'"):
        git_sync.enable_auto_push(repo, "origin")


def test_enabling_refuses_unsafe_local_config(repo: Path, remote: Path):
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "config", "core.sshCommand", "touch /tmp/x; ssh")
    with pytest.raises(git_sync.GitPushError, match="core.sshcommand"):
        git_sync.enable_auto_push(repo, "origin")


# --- pushing ----------------------------------------------------------------


def test_pushes_current_branch_to_the_stored_url(enabled: Path, remote: Path):
    head = _commit(enabled, "feature.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert ok, msg
    assert _git(remote, "rev-parse", "main") == head


def test_pushes_a_feature_branch(enabled: Path, remote: Path):
    _git(enabled, "checkout", "-q", "-b", "feature/login")
    head = _commit(enabled, "login.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert ok, msg
    assert "feature/login" in msg
    assert _git(remote, "rev-parse", "feature/login") == head


def test_changed_remote_url_blocks_the_push(enabled: Path, remote: Path, tmp_path: Path):
    evil = _bare(tmp_path, "evil.git")
    _git(enabled, "remote", "set-url", "origin", str(evil))
    _commit(enabled, "x.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert not ok
    assert "changed" in msg and str(evil) in msg
    assert _branches(evil) == "" and _branches(remote) == ""


def test_stored_url_is_used_not_the_branch_upstream(enabled: Path, remote: Path, tmp_path: Path):
    """With allowed keys pointing the branch at another remote, the push still goes to the stored URL."""
    evil = _bare(tmp_path, "evil.git")
    _git(enabled, "remote", "add", "upstream", str(evil))
    _git(enabled, "config", "branch.main.remote", "upstream")
    _git(enabled, "config", "branch.main.merge", "refs/heads/main")
    head = _commit(enabled, "x.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert ok, msg
    assert _git(remote, "rev-parse", "main") == head
    assert _branches(evil) == ""


@pytest.mark.parametrize(
    "key,value",
    [
        ("url.{evil}.pushInsteadOf", "{remote}"),
        ("url.{evil}.insteadOf", "{remote}"),
        ("core.sshCommand", "sh -c 'touch {marker}'"),
        ("credential.helper", "!touch {marker}"),
        ("include.path", "{evil}/config"),
        ("http.sslVerify", "false"),
        ("gpg.program", "{marker}"),
    ],
)
def test_unsafe_local_config_blocks_the_push(enabled: Path, remote: Path, tmp_path: Path, key: str, value: str):
    evil = _bare(tmp_path, "evil.git")
    marker = tmp_path / "marker"
    fmt = {"evil": str(evil), "remote": str(remote), "marker": str(marker)}
    _git(enabled, "config", key.format(**fmt), value.format(**fmt))
    _commit(enabled, "x.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert not ok
    assert "refusing" in msg.lower() and key.split(".")[0] in msg.lower()
    assert _branches(remote) == "" and _branches(evil) == ""
    assert not marker.exists()


def test_worktree_scoped_config_blocks_the_push(enabled: Path, remote: Path, tmp_path: Path):
    """`git config --local` does not list .git/config.worktree; the check must see that scope too."""
    _git(enabled, "config", "extensions.worktreeConfig", "true")
    _git(enabled, "config", "--worktree", "core.sshCommand", f"touch {tmp_path / 'marker'}")
    _commit(enabled, "x.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert not ok
    assert "core.sshcommand" in msg.lower()
    assert _branches(remote) == ""


@pytest.mark.parametrize("key,value", [
    ("core.alternateRefsCommand", "touch /tmp/x"),
    ("remote.origin.receivepack", "touch /tmp/x; git-receive-pack"),
    ("push.recurseSubmodules", "on-demand"),
    ("remote.origin.pushurl", "/somewhere/else.git"),
])
def test_any_key_outside_the_safe_set_blocks_the_push(enabled: Path, remote: Path, key: str, value: str):
    """Only plain repository, identity, branch and remote url/fetch keys are accepted (allowlist)."""
    _git(enabled, "config", key, value)
    _commit(enabled, "x.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert not ok
    assert key.lower() in msg.lower()
    assert _branches(remote) == ""


def test_pre_push_hook_does_not_run(enabled: Path, remote: Path, tmp_path: Path):
    marker = tmp_path / "hook-ran"
    hook = enabled / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    head = _commit(enabled, "x.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert ok, msg
    assert not marker.exists()
    assert _git(remote, "rev-parse", "main") == head


def test_never_force_pushes(enabled: Path, remote: Path, tmp_path: Path):
    assert git_sync.push_current_branch(enabled)[0]
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "Other")
    _commit(other, "other.txt")
    _git(other, "push", "-q")
    remote_head = _git(remote, "rev-parse", "main")

    _commit(enabled, "mine.txt")
    ok, msg = git_sync.push_current_branch(enabled)
    assert not ok
    assert "failed" in msg
    assert _git(remote, "rev-parse", "main") == remote_head


def test_detached_head_skips(enabled: Path):
    _git(enabled, "checkout", "-q", "--detach")
    ok, msg = git_sync.push_current_branch(enabled)
    assert not ok
    assert "detached" in msg


def test_not_a_repo(tmp_path: Path, settings_store):
    folder = tmp_path / "plain"
    folder.mkdir()
    settings_store[git_sync.GIT_PUSH_TARGETS_SETTING] = (
        '{"%s": {"remote": "origin", "url": "x"}}' % folder.resolve().as_posix()
    )
    ok, msg = git_sync.push_current_branch(folder)
    assert not ok
    assert "not a git repository" in msg


# --- orchestrator -----------------------------------------------------------


def test_orchestrator_pushes_off_the_main_path(tmp_path: Path, monkeypatch):
    """The push never delays the completion signal (it runs in a background thread)."""
    import parallel_orchestrator

    monkeypatch.setattr(parallel_orchestrator.debug_log, "log_file", tmp_path / "debug.log")
    orch = parallel_orchestrator.ParallelOrchestrator(tmp_path, testing_agent_ratio=0)
    release = threading.Event()
    pushed = threading.Event()

    def slow_push(_project_dir):
        release.wait(5)
        pushed.set()
        return True, "pushed"

    monkeypatch.setattr(git_sync, "get_push_target", lambda _p: {"remote": "origin", "url": "u"})
    monkeypatch.setattr(git_sync, "push_current_branch", slow_push)
    try:
        started = time.monotonic()
        orch._maybe_push_to_remote()
        assert time.monotonic() - started < 1
        assert not pushed.is_set()
        release.set()
        assert pushed.wait(5)
    finally:
        release.set()
        orch.cleanup()


# --- REST API ---------------------------------------------------------------


@pytest.fixture
def api(repo: Path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.routers import git_push

    monkeypatch.setattr(git_push, "_get_project_path", lambda name: repo if name == "demo" else None)
    app = FastAPI()
    app.include_router(git_push.router)
    return TestClient(app)


def test_api_enable_shows_and_stores_the_url(api, repo: Path, remote: Path):
    _git(repo, "remote", "add", "origin", str(remote))
    assert api.get("/api/projects/demo/git-push").json()["enabled"] is False
    res = api.put("/api/projects/demo/git-push", json={"enabled": True})
    assert res.status_code == 200, res.text
    assert res.json()["url"] == str(remote) == res.json()["current_url"]
    assert git_sync.get_push_target(repo) == {"remote": "origin", "url": str(remote)}
    assert api.put("/api/projects/demo/git-push", json={"enabled": False}).json()["enabled"] is False


def test_api_enable_refuses_unsafe_config(api, repo: Path, remote: Path):
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "config", "url.https://evil.example/.pushInsteadOf", str(remote))
    res = api.put("/api/projects/demo/git-push", json={"enabled": True})
    assert res.status_code == 400
    assert "pushinsteadof" in res.json()["detail"].lower()
    assert git_sync.get_push_target(repo) is None


def test_api_rejects_option_like_remote_name(api):
    assert api.put("/api/projects/demo/git-push", json={"enabled": True, "remote": "--upload-pack=x"}).status_code == 422
