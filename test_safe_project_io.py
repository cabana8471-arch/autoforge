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
