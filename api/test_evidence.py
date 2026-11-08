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

import shutil
from datetime import datetime, timezone
from pathlib import Path

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


def _safe_evidence_dir(project_dir: Path, feature_id: int) -> Path | None:
    """The feature's evidence directory, or None when reaching it would follow a symlink.

    The coding agent can create symlinks inside the project; this server runs
    outside its sandbox, so it must never write or delete through one. The path
    is accepted only when resolving it changes nothing below the project root.
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
    evidence_dir = _safe_evidence_dir(project_dir, feature_id)
    if evidence_dir is None or not evidence_dir.is_dir():
        return
    root = project_dir.resolve()
    kept = {(root / path) for path in keep if isinstance(path, str)}
    for entry in evidence_dir.iterdir():
        if entry not in kept and (entry.is_symlink() or entry.is_file()):
            try:
                entry.unlink()
            except OSError:
                pass


def _persist_screenshot(source: Path, dest_dir: Path, stamp: str, index: int) -> Path:
    """Copy a screenshot into the evidence directory (no-op if it already lives there)."""
    try:
        source.relative_to(dest_dir)
        return source
    except ValueError:
        pass
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stamp}_{index}_{source.name}"
    shutil.copy2(source, dest)
    return dest


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
    dest_dir = _safe_evidence_dir(project_dir, feature_id)
    accepted: list[str] = []
    seen: set[Path] = set()
    warnings: list[str] = []
    if dest_dir is None and screenshots:
        warnings.append("Screenshots were not copied: the evidence folder path contains a symlink")
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
        if dest_dir is not None:
            try:
                stored = _persist_screenshot(resolved, dest_dir, stamp, len(accepted))
            except OSError as e:
                warnings.append(f"Could not copy screenshot '{raw}' into evidence folder: {e}")
        accepted.append(stored.relative_to(root).as_posix())

    if not summary and not log and not accepted:
        return None, warnings

    return {
        "result": result,
        "summary": summary,
        "test_log": log,
        "screenshots": accepted,
        "recorded_at": now.isoformat(),
    }, warnings
