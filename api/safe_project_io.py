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
Windows. The ``*_project_*`` helpers take a project directory plus a target path
below it and fall back to a path-based check there (refuse when the target or any
component below the project directory is a symlink); they raise ``OSError``
(``UnsafeProjectPathError`` for a refused link) so callers keep the error
handling they already have for a failed write.
"""

import errno
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

DIR_FD_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
# What os.open reports for a symlink opened with O_NOFOLLOW (ELOOP on Linux;
# ENOTDIR / EMLINK on some BSDs when O_DIRECTORY is set as well)
_LINK_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})


class UnsafeProjectPathError(OSError):
    """A project path was refused: it leaves the project or goes through a link."""


def _walk_dirs(root: Path, parts: Sequence[str], create: bool) -> int:
    """Open ``root/parts...`` without following symlinks below ``root``.

    Raises:
        UnsafeProjectPathError: a component is a symlink, not a directory or invalid
        OSError: any other failure (e.g. a missing component when not creating)
    """
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for name in parts:
            if name in ("", ".", ".."):
                raise UnsafeProjectPathError(f"invalid path component {name!r}")
            if create:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                if e.errno in _LINK_ERRNOS:
                    raise UnsafeProjectPathError(f"{name!r} is a symlink or not a directory") from e
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


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
        return _walk_dirs(root, parts, create)
    except OSError:
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


def _replace_file(dir_fd: int, name: str, data: bytes) -> None:
    """Write ``data`` to a fresh temp file in ``dir_fd`` and rename it over ``name``."""
    tmp_name = f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def write_file(root: Path, relative: Path, data: bytes) -> bool:
    """Atomically write ``data`` to a file below ``root``, creating parent directories.

    A symlink or hard link at the target name is replaced, never written through.

    Returns:
        True when written, False when a parent component is a symlink or the
        write failed.
    """
    try:
        dir_fd = _walk_dirs(root, relative.parts[:-1], create=True)
    except OSError:
        return False
    try:
        _replace_file(dir_fd, relative.name, data)
        return True
    except OSError:
        return False
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# Project-level helpers: a target path below a project directory, any platform
# ---------------------------------------------------------------------------

def _project_relative(project_dir: Path, target: Path) -> Path:
    """Return ``target`` relative to ``project_dir`` (as given or resolved)."""
    for base in (project_dir, project_dir.resolve()):
        try:
            relative = target.relative_to(base)
        except ValueError:
            continue
        if relative.parts and ".." not in relative.parts:
            return relative
    raise UnsafeProjectPathError(f"{target} is not inside the project {project_dir}")


def _refuse_symlinks(project_dir: Path, relative: Path) -> Path:
    """Path-based check for platforms without dir_fd: no symlink below the project."""
    current = project_dir
    for name in relative.parts:
        current = current / name
        if current.is_symlink():
            raise UnsafeProjectPathError(f"{current} is a symlink")
    return current


def _open_single_link_file(dir_fd: int, name: str, flags: int) -> int:
    """Open ``name`` in ``dir_fd`` without following a symlink; refuse non-regular or hard-linked files."""
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), 0o644, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in _LINK_ERRNOS:
            raise UnsafeProjectPathError(f"{name!r} is a symlink") from e
        raise
    st = os.fstat(fd)
    # A second hard link means the same inode may also live outside the project
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        os.close(fd)
        raise UnsafeProjectPathError(f"{name!r} is not a regular file with a single link")
    return fd


def write_project_file(project_dir: Path, target: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``target`` (below ``project_dir``), creating parents.

    A symlink or hard link at the target is replaced, never written through.

    Raises:
        UnsafeProjectPathError: the target is outside the project or a parent is a symlink
        OSError: the write failed
    """
    relative = _project_relative(project_dir, target)
    if DIR_FD_SUPPORTED:
        dir_fd = _walk_dirs(project_dir.resolve(), relative.parts[:-1], create=True)
        try:
            _replace_file(dir_fd, relative.name, data)
        finally:
            os.close(dir_fd)
        return
    path = _refuse_symlinks(project_dir, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def append_project_file(project_dir: Path, target: Path, data: bytes) -> None:
    """Append ``data`` to ``target`` (below ``project_dir``), creating it and its parents.

    Raises:
        UnsafeProjectPathError: outside the project, a symlinked component, or the
            target is not a regular file with a single link
        OSError: the write failed
    """
    relative = _project_relative(project_dir, target)
    if DIR_FD_SUPPORTED:
        dir_fd = _walk_dirs(project_dir.resolve(), relative.parts[:-1], create=True)
        try:
            fd = _open_single_link_file(dir_fd, relative.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        finally:
            os.close(dir_fd)
        with os.fdopen(fd, "ab") as f:
            f.write(data)
        return
    path = _refuse_symlinks(project_dir, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as f:
        f.write(data)


def create_project_file_exclusive(project_dir: Path, target: Path, data: bytes) -> None:
    """Create ``target`` (below ``project_dir``) with ``data``; fail if the name exists.

    Raises:
        FileExistsError: the target exists (a symlink at the target counts)
        UnsafeProjectPathError: outside the project or a parent is a symlink
        OSError: the write failed
    """
    relative = _project_relative(project_dir, target)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if DIR_FD_SUPPORTED:
        dir_fd = _walk_dirs(project_dir.resolve(), relative.parts[:-1], create=True)
        try:
            fd = os.open(relative.name, flags | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    else:
        path = _refuse_symlinks(project_dir, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def make_project_dirs(project_dir: Path, target: Path) -> None:
    """Create the directory ``target`` (below ``project_dir``) and its parents.

    Raises:
        UnsafeProjectPathError: outside the project or a component is a symlink
        OSError: creating a directory failed
    """
    relative = _project_relative(project_dir, target)
    if DIR_FD_SUPPORTED:
        os.close(_walk_dirs(project_dir.resolve(), relative.parts, create=True))
        return
    _refuse_symlinks(project_dir, relative).mkdir(parents=True, exist_ok=True)


def read_project_file(project_dir: Path, target: Path) -> bytes:
    """Read ``target`` (below ``project_dir``) without following links.

    For project files whose content is copied or rewritten elsewhere in the
    project: a symlink or hard link would otherwise copy a file from outside it.

    Raises:
        UnsafeProjectPathError: outside the project, a symlinked component, or the
            target is not a regular file with a single link
        OSError: the target is missing or unreadable
    """
    relative = _project_relative(project_dir, target)
    if DIR_FD_SUPPORTED:
        dir_fd = _walk_dirs(project_dir.resolve(), relative.parts[:-1], create=False)
        try:
            fd = _open_single_link_file(dir_fd, relative.name, os.O_RDONLY)
        finally:
            os.close(dir_fd)
        with os.fdopen(fd, "rb") as f:
            return f.read()
    return _refuse_symlinks(project_dir, relative).read_bytes()


def copy_tree_into_project(project_dir: Path, source_dir: Path, target: Path) -> None:
    """Copy the trusted directory ``source_dir`` (outside the project) to ``target``.

    Files are written with ``write_project_file``, so a link planted anywhere on
    the destination side is never written through.

    Raises:
        OSError: as ``write_project_file`` / ``make_project_dirs``
    """
    for dirpath, dirnames, filenames in os.walk(source_dir):
        dirnames.sort()
        destination = target / Path(dirpath).relative_to(source_dir)
        make_project_dirs(project_dir, destination)
        for name in sorted(filenames):
            write_project_file(project_dir, destination / name, (Path(dirpath) / name).read_bytes())


def unlink_project_file(project_dir: Path, target: Path, missing_ok: bool = False) -> None:
    """Delete the file ``target`` (below ``project_dir``) without following a symlinked parent.

    ``Path.unlink`` only leaves the last component alone: with a symlinked parent
    (e.g. ``.autoforge`` -> elsewhere) it deletes the file at the link's target.
    A symlink at ``target`` itself is removed, never what it points to.

    With ``missing_ok`` a target whose parent is a symlink counts as absent (it
    cannot be inside the project): the refusal is logged instead of raised.

    Raises:
        FileNotFoundError: the target is missing (unless ``missing_ok``)
        UnsafeProjectPathError: outside the project or a parent is a symlink (unless ``missing_ok``)
        OSError: the delete failed
    """
    try:
        relative = _project_relative(project_dir, target)
        if DIR_FD_SUPPORTED:
            dir_fd = _walk_dirs(project_dir.resolve(), relative.parts[:-1], create=False)
            try:
                os.unlink(relative.name, dir_fd=dir_fd)
            finally:
                os.close(dir_fd)
        else:
            _refuse_symlinks(project_dir, relative.parent)
            (project_dir / relative).unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise
    except UnsafeProjectPathError as e:
        if not missing_ok:
            raise
        logger.warning("Not deleting %s: %s", target, e)


def remove_project_tree(project_dir: Path, target: Path) -> None:
    """Delete the directory tree ``target`` (below ``project_dir``) without following links.

    ``shutil.rmtree`` refuses a symlink at the path itself, but follows a symlinked
    parent; here the parents are walked with ``O_NOFOLLOW`` and the tree is removed
    relative to the last directory descriptor.

    Raises:
        UnsafeProjectPathError: outside the project or a parent is a symlink
        OSError: the target is missing, is a symlink, or the delete failed
    """
    relative = _project_relative(project_dir, target)
    if DIR_FD_SUPPORTED:
        dir_fd = _walk_dirs(project_dir.resolve(), relative.parts[:-1], create=False)
        try:
            shutil.rmtree(relative.name, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        return
    shutil.rmtree(_refuse_symlinks(project_dir, relative))
