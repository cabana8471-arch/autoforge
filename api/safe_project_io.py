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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

DIR_FD_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.symlink in os.supports_dir_fd
    and os.readlink in os.supports_dir_fd
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
        file (FIFOs and devices are refused without blocking) with a single link:
        a copy of a hard-linked file would carry the outside file's content under
        a fresh, single-link name that every later check accepts.
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
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
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


# A Windows junction (directory mount point) redirects like a symlink, but
# Path.is_symlink() is False for it. Path.is_junction() (Python 3.12+) checks this
# reparse tag; reading it from lstat() works on Python 3.11 too. The constant is the
# documented Windows value (``stat`` only defines it on Windows).
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def _reparse_tag(path: Path) -> int:
    """The Windows reparse tag of ``path`` itself (0 elsewhere or when missing)."""
    try:
        return int(getattr(os.lstat(path), "st_reparse_tag", 0))
    except OSError:
        return 0


def is_link_or_junction(path: Path) -> bool:
    """True when ``path`` itself is a symlink or a Windows junction."""
    return path.is_symlink() or _reparse_tag(path) == _IO_REPARSE_TAG_MOUNT_POINT


def refuse_nested_junctions(directory: str, names: list[str]) -> list[str]:
    """``shutil.copytree`` ``ignore`` callback: refuse a junction at any level of the tree.

    ``copytree(symlinks=True)`` copies symlinks as links but treats a junction as a
    directory and would copy its target, which may lie outside the project.
    """
    for name in names:
        if _reparse_tag(Path(directory) / name) == _IO_REPARSE_TAG_MOUNT_POINT:
            raise UnsafeProjectPathError(f"{Path(directory) / name} is a junction")
    return []


def copy2_refusing_hard_links(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
    """``shutil.copy2`` for ``copytree(copy_function=...)``: refuse a source with a second link.

    A copy has a single link, so a hard link the agent planted to an outside file
    would come out of the copy as a plain project file. ``copytree(symlinks=True)``
    hands only regular files to the copy function; the source is opened without
    following a symlink and checked on the open descriptor.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(src, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise UnsafeProjectPathError(f"{src} is not a regular file with a single link")
        with os.fdopen(os.dup(fd), "rb") as source, open(dst, "xb") as target:
            shutil.copyfileobj(source, target)
        # Same metadata as shutil.copy2: permission bits and timestamps
        os.chmod(dst, stat.S_IMODE(st.st_mode))
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    finally:
        os.close(fd)
    return dst


def _refuse_symlinks(project_dir: Path, relative: Path) -> Path:
    """Path-based check for platforms without dir_fd: no symlink or junction below the project."""
    current = project_dir
    for name in relative.parts:
        current = current / name
        if is_link_or_junction(current):
            raise UnsafeProjectPathError(f"{current} is a symlink or junction")
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
    with open_project_file_for_write(project_dir, target) as f:
        f.write(data)


@contextmanager
def open_project_file_for_write(project_dir: Path, target: Path) -> Iterator[BinaryIO]:
    """Stream a new version of ``target`` (below ``project_dir``); like ``write_project_file``.

    Yields a binary file on a fresh temp file next to the target, renamed over the
    target when the block completes; on an exception the temp file is removed and
    the target is left as it was.

    Raises:
        UnsafeProjectPathError: the target is outside the project or a parent is a symlink
        OSError: the write failed
    """
    relative = _project_relative(project_dir, target)
    tmp_name = f".{relative.name}.{uuid.uuid4().hex}.tmp"
    if DIR_FD_SUPPORTED:
        dir_fd = _walk_dirs(project_dir.resolve(), relative.parts[:-1], create=True)
        try:
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
            try:
                with os.fdopen(fd, "wb") as f:
                    yield f
                os.rename(tmp_name, relative.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                try:
                    os.unlink(tmp_name, dir_fd=dir_fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(dir_fd)
        return
    path = _refuse_symlinks(project_dir, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(tmp_name)
    try:
        with open(tmp, "xb") as f:
            yield f
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


def check_project_file(project_dir: Path, target: Path) -> bool:
    """Check that ``target`` (below ``project_dir``) is a plain project file, if it exists.

    For files that another library opens by path (SQLite): no component below the
    project may be a symlink, and an existing target must be a regular file with a
    single link. The check cannot hold the file open for that library, so a link
    planted between this check and its open is not caught.

    Returns:
        True when the target exists, False when it (or one of its parents) is missing.

    Raises:
        UnsafeProjectPathError: outside the project, a symlinked component, or the
            target is a symlink, a non-regular file or has more than one link
    """
    relative = _project_relative(project_dir, target)
    if DIR_FD_SUPPORTED:
        try:
            dir_fd = _walk_dirs(project_dir.resolve(), relative.parts[:-1], create=False)
        except FileNotFoundError:
            return False
        try:
            st = os.stat(relative.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        finally:
            os.close(dir_fd)
    else:
        _refuse_symlinks(project_dir, relative.parent)
        try:
            st = (project_dir / relative).lstat()
        except FileNotFoundError:
            return False
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeProjectPathError(f"{target} is a symlink")
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        raise UnsafeProjectPathError(f"{target} is not a regular file with a single link")
    return True


def read_project_text(project_dir: Path, target: Path, errors: str = "strict") -> str:
    """``read_project_file`` decoded as UTF-8, newlines normalized like ``Path.read_text``.

    For project files whose content is sent to an agent prompt or to the UI: a
    planted symlink or hard link would otherwise send a file from outside the project.

    Raises:
        OSError: as ``read_project_file``
        UnicodeDecodeError: the content is not UTF-8 (with ``errors="strict"``)
    """
    text = read_project_file(project_dir, target).decode("utf-8", errors=errors)
    return text.replace("\r\n", "\n").replace("\r", "\n")


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


def _remove_entry_at(dir_fd: int, name: str) -> None:
    """Remove ``name`` in ``dir_fd`` if present: a directory tree, or a file or symlink."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def _copy_entry_at(src_dir: int, name: str, dst_dir: int, dst_name: str) -> None:
    """Copy ``name`` (symlink, regular file or directory tree) to a new ``dst_name``, like copytree(symlinks=True)."""
    st = os.stat(name, dir_fd=src_dir, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(name, dir_fd=src_dir), dst_name, dir_fd=dst_dir)
    elif stat.S_ISDIR(st.st_mode):
        os.mkdir(dst_name, 0o700, dir_fd=dst_dir)
        src_fd = os.open(name, _DIR_FLAGS, dir_fd=src_dir)
        try:
            dst_fd = os.open(dst_name, _DIR_FLAGS, dir_fd=dst_dir)
            try:
                for child in sorted(os.listdir(src_fd)):
                    _copy_entry_at(src_fd, child, dst_fd, child)
            finally:
                os.close(dst_fd)
        finally:
            os.close(src_fd)
        os.chmod(dst_name, stat.S_IMODE(st.st_mode), dir_fd=dst_dir)
    elif stat.S_ISREG(st.st_mode):
        src_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=src_dir)
        try:
            src_st = os.fstat(src_fd)
            if not stat.S_ISREG(src_st.st_mode):  # swapped since the lstat
                raise UnsafeProjectPathError(f"{name!r} is not a regular file")
            if src_st.st_nlink > 1:  # the copy would launder an outside file into the project
                raise UnsafeProjectPathError(f"{name!r} has more than one hard link")
            dst_fd = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_dir)
            with os.fdopen(dst_fd, "wb") as dst, os.fdopen(os.dup(src_fd), "rb") as src:
                shutil.copyfileobj(src, dst)
                dst.flush()
                # Same metadata as shutil.copy2: permission bits and timestamps
                os.fchmod(dst.fileno(), stat.S_IMODE(st.st_mode))
                os.utime(dst.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
        finally:
            os.close(src_fd)
    else:
        raise UnsafeProjectPathError(f"{name!r} is not a regular file, directory or symlink")


def copy_project_entry(project_dir: Path, source: Path, target: Path) -> None:
    """Replace ``target`` with a copy of ``source``; both are paths below ``project_dir``.

    Copies a file, a symlink (as a symlink) or a directory tree (links inside it
    as links), like ``shutil.copytree(..., symlinks=True)`` / ``shutil.copy2``,
    but without following a symlinked parent on either side. The copy is built
    under a temporary name next to ``target`` and renamed over it once complete;
    an existing ``target`` (file, symlink or tree) is removed just before.

    Raises:
        UnsafeProjectPathError: either path is outside the project or has a symlinked parent
        OSError: ``source`` is missing or the copy failed
    """
    src_rel = _project_relative(project_dir, source)
    dst_rel = _project_relative(project_dir, target)
    tmp_name = f".{dst_rel.name}.{uuid.uuid4().hex}.tmp"
    if not DIR_FD_SUPPORTED:
        src_path = _refuse_symlinks(project_dir, src_rel.parent) / src_rel.name
        dst_path = _refuse_symlinks(project_dir, dst_rel)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dst_path.with_name(tmp_name)
        try:
            if _reparse_tag(src_path) == _IO_REPARSE_TAG_MOUNT_POINT:
                # copytree would copy the junction's (outside) target
                raise UnsafeProjectPathError(f"{src_path} is a junction")
            if src_path.is_symlink():
                tmp_path.symlink_to(os.readlink(src_path))
            elif src_path.is_dir():
                shutil.copytree(
                    src_path, tmp_path, symlinks=True, ignore=refuse_nested_junctions,
                    copy_function=copy2_refusing_hard_links,
                )
            else:
                copy2_refusing_hard_links(str(src_path), str(tmp_path))
            if dst_path.is_dir() and not dst_path.is_symlink():
                shutil.rmtree(dst_path)
            elif tmp_path.is_dir() and not tmp_path.is_symlink():
                dst_path.unlink(missing_ok=True)
            os.replace(tmp_path, dst_path)
        except BaseException:
            if tmp_path.is_dir() and not tmp_path.is_symlink():
                shutil.rmtree(tmp_path, ignore_errors=True)
            else:
                tmp_path.unlink(missing_ok=True)
            raise
        return

    root = project_dir.resolve()
    src_dir = _walk_dirs(root, src_rel.parts[:-1], create=False)
    try:
        dst_dir = _walk_dirs(root, dst_rel.parts[:-1], create=True)
        try:
            try:
                _copy_entry_at(src_dir, src_rel.name, dst_dir, tmp_name)
                copied_dir = stat.S_ISDIR(os.stat(tmp_name, dir_fd=dst_dir, follow_symlinks=False).st_mode)
                if copied_dir or _is_dir_at(dst_dir, dst_rel.name):
                    # rename() only replaces a file with a file: clear the old entry first
                    _remove_entry_at(dst_dir, dst_rel.name)
                os.rename(tmp_name, dst_rel.name, src_dir_fd=dst_dir, dst_dir_fd=dst_dir)
            except BaseException:
                _remove_entry_at(dst_dir, tmp_name)
                raise
        finally:
            os.close(dst_dir)
    finally:
        os.close(src_dir)


def _is_dir_at(dir_fd: int, name: str) -> bool:
    """True when ``name`` in ``dir_fd`` is a real directory (not a symlink to one)."""
    try:
        return stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False
