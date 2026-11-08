"""
Safe project I/O tests
======================

The coding agent can create symlinks and hard links anywhere inside the project,
at any time. Code that runs outside its sandbox (server, feature MCP server) must
not be redirected outside the project by them.

Run with: python -m pytest test_safe_project_io.py
"""

import os
from pathlib import Path

import pytest

from api import safe_project_io as sio

pytestmark = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")


@pytest.fixture
def root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project.resolve()


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    return folder


def test_open_dir_walks_and_creates(root):
    fd = sio.open_dir(root, ("a", "b"), create=True)
    assert fd is not None
    os.close(fd)
    assert (root / "a" / "b").is_dir()


@pytest.mark.parametrize("level", [0, 1])
def test_open_dir_refuses_symlinked_component(root, outside, level):
    (outside / "b").mkdir()
    if level == 0:
        (root / "a").symlink_to(outside, target_is_directory=True)
    else:
        (root / "a").mkdir()
        (root / "a" / "b").symlink_to(outside / "b", target_is_directory=True)
    assert sio.open_dir(root, ("a", "b"), create=True) is None


def test_open_regular_file_reads_inside(root):
    (root / "shots").mkdir()
    (root / "shots" / "a.png").write_bytes(b"png")
    fd = sio.open_regular_file(root, Path("shots/a.png"))
    assert fd is not None
    with os.fdopen(fd, "rb") as f:
        assert f.read() == b"png"


def test_open_regular_file_refuses_symlinked_dir_and_file(root, outside):
    (outside / "a.png").write_bytes(b"secret")
    (root / "shots").symlink_to(outside, target_is_directory=True)
    assert sio.open_regular_file(root, Path("shots/a.png")) is None
    (root / "b.png").symlink_to(outside / "a.png")
    assert sio.open_regular_file(root, Path("b.png")) is None


def test_open_regular_file_refuses_non_regular(root):
    (root / "dir.png").mkdir()
    os.mkfifo(root / "pipe.png")
    assert sio.open_regular_file(root, Path("dir.png")) is None
    assert sio.open_regular_file(root, Path("pipe.png")) is None  # must not block on a FIFO


def test_write_file_creates_dirs_and_replaces(root):
    assert sio.write_file(root, Path(".autoforge/prompts/out.md"), b"one")
    assert sio.write_file(root, Path(".autoforge/prompts/out.md"), b"two")
    assert (root / ".autoforge" / "prompts" / "out.md").read_bytes() == b"two"
    assert [p.name for p in (root / ".autoforge" / "prompts").iterdir()] == ["out.md"]  # no temp left


def test_write_file_refuses_symlinked_parent(root, outside):
    (root / ".autoforge").mkdir()
    (root / ".autoforge" / "prompts").symlink_to(outside, target_is_directory=True)
    assert not sio.write_file(root, Path(".autoforge/prompts/out.md"), b"x")
    assert list(outside.iterdir()) == []


def test_write_file_does_not_write_through_final_symlink_or_hardlink(root, outside):
    target = outside / "victim.txt"
    target.write_bytes(b"keep")
    (root / "link.md").symlink_to(target)
    os.link(target, root / "hard.md")

    assert sio.write_file(root, Path("link.md"), b"x")
    assert sio.write_file(root, Path("hard.md"), b"x")

    assert target.read_bytes() == b"keep"
    assert (root / "link.md").read_bytes() == b"x" and not (root / "link.md").is_symlink()


# ---------------------------------------------------------------------------
# Project-level helpers (target path below a project directory)
# ---------------------------------------------------------------------------


def test_write_project_file_writes_and_replaces_final_links(root, outside):
    victim = outside / "victim.txt"
    victim.write_bytes(b"keep")
    (root / ".autoforge").mkdir()
    (root / ".autoforge" / "link.json").symlink_to(victim)
    os.link(victim, root / ".autoforge" / "hard.json")

    sio.write_project_file(root, root / ".autoforge" / "link.json", b"x")
    sio.write_project_file(root, root / ".autoforge" / "hard.json", b"x")
    sio.write_project_file(root, root / "new" / "dir" / "file.txt", b"y")

    assert victim.read_bytes() == b"keep"
    assert (root / ".autoforge" / "link.json").read_bytes() == b"x"
    assert (root / "new" / "dir" / "file.txt").read_bytes() == b"y"


def test_write_project_file_refuses_symlinked_parent_and_outside_target(root, outside):
    (root / ".autoforge").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.write_project_file(root, root / ".autoforge" / "settings.json", b"x")
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.write_project_file(root, outside / "settings.json", b"x")
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.write_project_file(root, root / ".." / "outside" / "settings.json", b"x")
    assert list(outside.iterdir()) == []


def test_write_project_file_accepts_unresolved_project_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)  # the project root itself is trusted
    sio.write_project_file(alias, alias / "a.txt", b"1")
    sio.write_project_file(alias, real.resolve() / "b.txt", b"2")
    assert (real / "a.txt").read_bytes() == b"1" and (real / "b.txt").read_bytes() == b"2"


def test_append_project_file_appends_and_creates(root):
    target = root / ".gitignore"
    sio.append_project_file(root, target, b"a\n")
    sio.append_project_file(root, target, b"b\n")
    assert target.read_bytes() == b"a\nb\n"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "parent"])
def test_append_project_file_refuses_links(root, outside, kind):
    victim = outside / "victim.txt"
    victim.write_bytes(b"keep")
    if kind == "symlink":
        target = root / ".gitignore"
        target.symlink_to(victim)
    elif kind == "hardlink":
        target = root / ".gitignore"
        os.link(victim, target)
    else:
        (root / "sub").symlink_to(outside, target_is_directory=True)
        target = root / "sub" / "victim.txt"
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.append_project_file(root, target, b"x")
    assert victim.read_bytes() == b"keep"


def test_create_project_file_exclusive(root, outside):
    lock = root / ".autoforge" / ".agent.lock"
    sio.create_project_file_exclusive(root, lock, b"123")
    assert lock.read_bytes() == b"123"
    with pytest.raises(FileExistsError):
        sio.create_project_file_exclusive(root, lock, b"456")

    dangling = root / ".autoforge" / ".other.lock"
    dangling.symlink_to(outside / "created-outside")
    with pytest.raises(FileExistsError):
        sio.create_project_file_exclusive(root, dangling, b"x")
    assert list(outside.iterdir()) == []


def test_make_project_dirs(root, outside):
    sio.make_project_dirs(root, root / ".autoforge" / "validation")
    assert (root / ".autoforge" / "validation").is_dir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.make_project_dirs(root, root / "linked" / "validation")
    assert list(outside.iterdir()) == []


def test_read_project_file_refuses_links(root, outside):
    (outside / "id_rsa").write_bytes(b"secret")
    (root / "spec.txt").write_bytes(b"spec")
    assert sio.read_project_file(root, root / "spec.txt") == b"spec"
    (root / "sym.txt").symlink_to(outside / "id_rsa")
    os.link(outside / "id_rsa", root / "hard.txt")
    for name in ("sym.txt", "hard.txt"):
        with pytest.raises(sio.UnsafeProjectPathError):
            sio.read_project_file(root, root / name)
    with pytest.raises(FileNotFoundError):
        sio.read_project_file(root, root / "missing.txt")


def test_copy_tree_into_project(root, outside, tmp_path):
    source = tmp_path / "source"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_bytes(b"skill")
    (source / "references" / "a.md").write_bytes(b"a")

    sio.copy_tree_into_project(root, source, root / ".claude" / "skills" / "tool")
    assert (root / ".claude" / "skills" / "tool" / "SKILL.md").read_bytes() == b"skill"
    assert (root / ".claude" / "skills" / "tool" / "references" / "a.md").read_bytes() == b"a"

    (root / ".claude" / "skills" / "other").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.copy_tree_into_project(root, source, root / ".claude" / "skills" / "other")
    assert list(outside.iterdir()) == []


def test_project_helpers_path_fallback_refuses_symlinks(root, outside, monkeypatch):
    """Without dir_fd (Windows) the helpers fall back to a path-based symlink check."""
    monkeypatch.setattr(sio, "DIR_FD_SUPPORTED", False)
    sio.write_project_file(root, root / "a" / "file.txt", b"1")
    sio.append_project_file(root, root / "a" / "file.txt", b"2")
    assert sio.read_project_file(root, root / "a" / "file.txt") == b"12"

    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "final.txt").symlink_to(outside / "victim.txt")
    for target in (root / "linked" / "x.txt", root / "final.txt"):
        with pytest.raises(sio.UnsafeProjectPathError):
            sio.write_project_file(root, target, b"x")
        with pytest.raises(sio.UnsafeProjectPathError):
            sio.append_project_file(root, target, b"x")
    assert list(outside.iterdir()) == []


def test_unlink_project_file_deletes_and_removes_final_symlink_only(root, outside):
    victim = outside / "victim.txt"
    victim.write_bytes(b"keep")
    (root / "a.txt").write_bytes(b"a")
    (root / "link.txt").symlink_to(victim)

    sio.unlink_project_file(root, root / "a.txt")
    sio.unlink_project_file(root, root / "link.txt")

    assert not (root / "a.txt").exists() and not (root / "link.txt").is_symlink()
    assert victim.read_bytes() == b"keep"
    with pytest.raises(FileNotFoundError):
        sio.unlink_project_file(root, root / "a.txt")
    sio.unlink_project_file(root, root / "a.txt", missing_ok=True)
    sio.unlink_project_file(root, root / "no" / "dir.txt", missing_ok=True)


def test_unlink_project_file_refuses_symlinked_parent(root, outside, caplog):
    (outside / "features.db").write_bytes(b"keep")
    (root / ".autoforge").symlink_to(outside, target_is_directory=True)

    with pytest.raises(sio.UnsafeProjectPathError):
        sio.unlink_project_file(root, root / ".autoforge" / "features.db")
    sio.unlink_project_file(root, root / ".autoforge" / "features.db", missing_ok=True)

    assert (outside / "features.db").read_bytes() == b"keep"
    assert "Not deleting" in caplog.text


def test_remove_project_tree(root, outside):
    (root / ".autoforge" / "prompts" / "sub").mkdir(parents=True)
    (root / ".autoforge" / "prompts" / "sub" / "a.md").write_bytes(b"a")
    sio.remove_project_tree(root, root / ".autoforge" / "prompts")
    assert not (root / ".autoforge" / "prompts").exists()

    (outside / "prompts").mkdir()
    (outside / "prompts" / "a.md").write_bytes(b"keep")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "final").symlink_to(outside / "prompts", target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.remove_project_tree(root, root / "linked" / "prompts")
    with pytest.raises(OSError):
        sio.remove_project_tree(root, root / "final")
    assert (outside / "prompts" / "a.md").read_bytes() == b"keep"


def test_delete_helpers_path_fallback(root, outside, monkeypatch):
    monkeypatch.setattr(sio, "DIR_FD_SUPPORTED", False)
    (outside / "features.db").write_bytes(b"keep")
    (outside / "prompts").mkdir()
    (root / ".autoforge").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.unlink_project_file(root, root / ".autoforge" / "features.db")
    with pytest.raises(sio.UnsafeProjectPathError):
        sio.remove_project_tree(root, root / ".autoforge" / "prompts")
    assert {p.name for p in outside.iterdir()} == {"features.db", "prompts"}

    (root / "real").mkdir()
    (root / "real" / "x.txt").write_bytes(b"x")
    sio.unlink_project_file(root, root / "real" / "x.txt")
    sio.remove_project_tree(root, root / "real")
    assert not (root / "real").exists()
