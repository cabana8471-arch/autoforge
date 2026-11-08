"""
Test Evidence Helpers
=====================

Agents can attach evidence to a feature when they mark it passing or failing:
a short summary of findings, a step-by-step log of how they tested it, and
paths to screenshots captured during verification. The evidence is stored in
the feature's ``test_evidence`` JSON column and shown on the feature card so
a human can quickly review how the feature was verified.

Screenshots are copied into ``.autoforge/evidence/feature_<id>/`` so they
survive later browser sessions overwriting or cleaning ``.playwright-cli/``.
Paths are stored relative to the project directory and are always re-validated
before being served, so a stored path can never be used to read files outside
the project.
"""

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from api import safe_project_io

MAX_SUMMARY_LENGTH = 4000
MAX_LOG_ENTRIES = 50
MAX_LOG_ENTRY_LENGTH = 1000
MAX_SCREENSHOTS = 10

SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def resolve_screenshot_path(project_dir: Path, raw_path: str) -> Path | None:
    """Resolve a screenshot path, ensuring it is an image inside the project.

    Args:
        project_dir: The project root directory
        raw_path: Absolute path or path relative to the project directory

    Returns:
        The resolved absolute path, or None if the path is outside the
        project directory or isn't a supported image type.
    """
    if not raw_path or "\x00" in raw_path:
        return None
    root = project_dir.resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if resolved.suffix.lower() not in SCREENSHOT_EXTENSIONS:
        return None
    return resolved


def get_evidence_dir(project_dir: Path, feature_id: int) -> Path:
    """Directory where screenshots for a feature's evidence are kept."""
    from autoforge_paths import get_autoforge_dir
    return get_autoforge_dir(project_dir.resolve()) / "evidence" / f"feature_{feature_id}"


# Evidence files are created, read and deleted through api.safe_project_io where
# the OS supports dir_fd: this code runs outside the agent's sandbox, and the agent
# can plant symlinks inside the project at any moment (also between a check and
# its use), so checking path names first is not enough.
_DIR_FD_SUPPORTED = safe_project_io.DIR_FD_SUPPORTED


def _open_evidence_dir(project_dir: Path, feature_id: int, create: bool) -> int | None:
    """Open the feature's evidence directory without following any symlink below the project root."""
    root = project_dir.resolve()
    parts = get_evidence_dir(project_dir, feature_id).relative_to(root).parts
    return safe_project_io.open_dir(root, parts, create=create)


def _safe_evidence_dir(project_dir: Path, feature_id: int) -> Path | None:
    """Path-based fallback for platforms without dir_fd support (e.g. Windows).

    Accepts the directory only when resolving it changes nothing below the project root.
    """
    expected = get_evidence_dir(project_dir, feature_id)
    if expected.resolve() != expected:
        return None
    return expected


def prune_evidence_dir(project_dir: Path, feature_id: int, keep: list[str]) -> None:
    """Delete copies in the feature's evidence directory that ``keep`` no longer references.

    Args:
        project_dir: The project root directory
        feature_id: The feature whose evidence directory is pruned
        keep: Project-relative screenshot paths of the evidence currently stored
    """
    root = project_dir.resolve()
    evidence_dir = get_evidence_dir(project_dir, feature_id)
    kept = {Path(path).name for path in keep if isinstance(path, str) and (root / path).parent == evidence_dir}

    if _DIR_FD_SUPPORTED:
        fd = _open_evidence_dir(project_dir, feature_id, create=False)
        if fd is None:
            return
        try:
            with os.scandir(fd) as entries:
                names = [e.name for e in entries if not e.is_dir(follow_symlinks=False)]
            for name in names:
                if name not in kept:
                    try:
                        os.unlink(name, dir_fd=fd)  # removes a symlink itself, never its target
                    except OSError:
                        pass
        finally:
            os.close(fd)
        return

    safe_dir = _safe_evidence_dir(project_dir, feature_id)
    if safe_dir is None or not safe_dir.is_dir():
        return
    for entry in safe_dir.iterdir():
        if entry.name not in kept and (entry.is_symlink() or entry.is_file()):
            try:
                entry.unlink()
            except OSError:
                pass


class _EvidenceWriter:
    """Copies screenshots into one feature's evidence directory, opened lazily at the first copy."""

    def __init__(self, project_dir: Path, feature_id: int):
        self.project_dir = project_dir
        self.feature_id = feature_id
        self.dir_path = get_evidence_dir(project_dir, feature_id)
        self._fd: int | None = None
        self._opened = False
        self.unsafe = False

    def copy(self, source: Path, name: str) -> Path | None:
        """Copy ``source`` into the directory as ``name``; None when it cannot be reached safely."""
        if not _DIR_FD_SUPPORTED:
            safe_dir = _safe_evidence_dir(self.project_dir, self.feature_id)
            if safe_dir is None:
                self.unsafe = True
                return None
            safe_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, safe_dir / name)
            return safe_dir / name

        if not self._opened:
            self._opened = True
            self._fd = _open_evidence_dir(self.project_dir, self.feature_id, create=True)
            self.unsafe = self._fd is None
        if self._fd is None:
            return None
        root = self.project_dir.resolve()
        src_fd = safe_project_io.open_regular_file(root, source.relative_to(root))
        if src_fd is None:
            raise OSError("screenshot path changed or is no longer a regular file inside the project")
        with os.fdopen(src_fd, "rb") as src:
            dst_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=self._fd)
            with os.fdopen(dst_fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return self.dir_path / name

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def build_test_evidence(
    project_dir: Path,
    feature_id: int,
    result: str,
    summary: str | None = None,
    test_log: list[str] | None = None,
    screenshots: list[str] | None = None,
) -> tuple[dict | None, list[str]]:
    """Build a normalized evidence record from agent-supplied values.

    Args:
        project_dir: The project root directory (used to validate screenshots)
        feature_id: The feature the evidence belongs to (used for the screenshot directory)
        result: "passing" or "failing"
        summary: Short description of what was verified and what was found
        test_log: Ordered list of the testing steps performed and their outcomes
        screenshots: Screenshot paths (absolute or relative to the project)

    Returns:
        Tuple of (evidence dict or None if nothing was supplied, list of warnings
        about rejected screenshots).
    """
    summary = (summary or "").strip()[:MAX_SUMMARY_LENGTH]
    log = [
        entry.strip()[:MAX_LOG_ENTRY_LENGTH]
        for entry in (test_log or [])
        if isinstance(entry, str) and entry.strip()
    ][:MAX_LOG_ENTRIES]

    root = project_dir.resolve()
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    writer = _EvidenceWriter(project_dir, feature_id)
    accepted: list[str] = []
    seen: set[Path] = set()
    warnings: list[str] = []
    try:
        for raw in (screenshots or [])[:MAX_SCREENSHOTS]:
            resolved = resolve_screenshot_path(project_dir, raw) if isinstance(raw, str) else None
            if resolved is None:
                warnings.append(f"Ignored screenshot '{raw}': must be an image file inside the project directory")
                continue
            if not resolved.is_file():
                warnings.append(f"Ignored screenshot '{raw}': file not found")
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            stored = resolved
            if resolved.parent != writer.dir_path:  # already in the evidence folder: keep as is
                try:
                    copied = writer.copy(resolved, f"{stamp}_{len(accepted)}_{resolved.name}")
                except OSError as e:
                    warnings.append(f"Could not copy screenshot '{raw}' into evidence folder: {e}")
                else:
                    if copied is not None:
                        stored = copied
            accepted.append(stored.relative_to(root).as_posix())
        if writer.unsafe:
            warnings.append("Screenshots were not copied: the evidence folder path contains a symlink")
    finally:
        writer.close()

    if not summary and not log and not accepted:
        return None, warnings

    return {
        "result": result,
        "summary": summary,
        "test_log": log,
        "screenshots": accepted,
        "recorded_at": now.isoformat(),
    }, warnings
