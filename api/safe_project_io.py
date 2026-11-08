"""
Safe Project I/O
================

The coding agent can create symlinks and hard links anywhere inside the project
directory, at any moment, including between a path check and its use. Code that
runs outside the agent's sandbox (the server, the feature MCP server) uses these
helpers so a planted link can never redirect a read or write outside the project.

Every path is walked from a descriptor on the (resolved) project root, one
component at a time with ``O_NOFOLLOW``; files are then opened or created
relative to the last directory descriptor. Writes go to a fresh ``O_EXCL`` temp
file that is renamed over the target, so a symlink or hard link at the target
name is replaced instead of written through.

These helpers need ``dir_fd`` support (POSIX). ``DIR_FD_SUPPORTED`` is False on
Windows; callers keep a path-based check there.
"""

import os
import stat
import uuid
from collections.abc import Sequence
from pathlib import Path

DIR_FD_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def open_dir(root: Path, parts: Sequence[str], create: bool = False) -> int | None:
    """Open ``root/parts...`` without following any symlink below ``root``.

    Args:
        root: The resolved project root (trusted; the agent cannot replace it)
        parts: Directory names below the root
        create: Create missing directories on the way

    Returns:
        A directory file descriptor the caller must close, or None when a
        component is missing (and not created), is a symlink or is not a directory.
    """
    try:
        fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return None
    try:
        for name in parts:
            if name in ("", ".", ".."):
                raise OSError(f"invalid path component {name!r}")
            if create:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        os.close(fd)
        return None


def open_regular_file(root: Path, relative: Path) -> int | None:
    """Open a regular file below ``root`` for reading without following symlinks.

    Returns:
        A read-only file descriptor the caller must close (e.g. via os.fdopen),
        or None when any component is a symlink or the target is not a regular
        file (FIFOs and devices are refused without blocking).
    """
    dir_fd = open_dir(root, relative.parts[:-1])
    if dir_fd is None:
        return None
    try:
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def write_file(root: Path, relative: Path, data: bytes) -> bool:
    """Atomically write ``data`` to a file below ``root``, creating parent directories.

    A symlink or hard link at the target name is replaced, never written through.

    Returns:
        True when written, False when a parent component is a symlink or the
        write failed.
    """
    dir_fd = open_dir(root, relative.parts[:-1], create=True)
    if dir_fd is None:
        return False
    tmp_name = f".{relative.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.rename(tmp_name, relative.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return True
    except OSError:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        return False
    finally:
        os.close(dir_fd)
