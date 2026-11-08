"""
Project Skills
==============

Manage the Claude Code skills installed in a generated project's
``.claude/skills/`` directory.

Agents run with ``setting_sources=["project"]``, so only skills that live
inside the project are visible to them. This module lets users pick skills
from a library when a project is created (or later) and copies them in.

Skill library sources, in precedence order (first match for a name wins):

1. Bundled AutoForge skills: ``.claude/skills/`` in AutoForge (excluding
   skills that only make sense for AutoForge itself)
2. User AutoForge library: ``~/.autoforge/skills/``

The user's global ``~/.claude/skills/`` is deliberately not a source: those
are the user's own skills and may hold private data (user decision 2A).

The agent can write the project's ``.claude/``, so project-side reads, writes
and deletes go through ``api.safe_project_io`` and never follow a planted link.
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml

from api.safe_project_io import (
    UnsafeProjectPathError,
    is_link_or_junction,
    make_project_dirs,
    read_project_text,
    remove_project_tree,
    unlink_project_file,
    write_project_file,
)

AUTOFORGE_ROOT = Path(__file__).parent

# Skills that are always installed by scaffolding and cannot be removed,
# because the prompts depend on them.
REQUIRED_SKILLS = frozenset({"playwright-cli"})

# Bundled skills that are meant for working on AutoForge, not generated apps
_EXCLUDED_BUNDLED_SKILLS = frozenset({"gsd-to-autoforge-spec"})

# Skill names become directory names, so keep them strict
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")

_MAX_DESCRIPTION_LENGTH = 500


def _library_sources() -> list[tuple[str, Path]]:
    """Return (source_label, directory) pairs in precedence order."""
    return [
        ("bundled", AUTOFORGE_ROOT / ".claude" / "skills"),
        ("user", Path.home() / ".autoforge" / "skills"),
    ]


def is_valid_skill_name(name: str) -> bool:
    """Check that a skill name is safe to use as a directory name."""
    return bool(_SKILL_NAME_RE.match(name)) and ".." not in name


def get_project_skills_dir(project_dir: Path) -> Path:
    """Return the project's ``.claude/skills`` directory."""
    return project_dir / ".claude" / "skills"


def parse_skill_metadata(skill_md: Path, project_dir: Path | None = None) -> dict[str, str]:
    """Parse ``name`` and ``description`` from a SKILL.md YAML frontmatter.

    With ``project_dir`` the file is read without following links (project side).
    Returns an empty dict if the file has no readable frontmatter.
    """
    try:
        if project_dir is None:
            text = skill_md.read_text(encoding="utf-8")
        else:
            text = read_project_text(project_dir, skill_md)
    except (OSError, UnicodeDecodeError):
        return {}

    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not match:
        return {}

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}

    result: dict[str, str] = {}
    for key in ("name", "description"):
        value = data.get(key)
        if isinstance(value, str):
            result[key] = value.strip()
    if "description" in result:
        result["description"] = result["description"][:_MAX_DESCRIPTION_LENGTH]
    return result


def _iter_skill_dirs(root: Path):
    """Yield skill directories (containing SKILL.md) directly under root."""
    if not root.is_dir():
        return
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return
    for entry in entries:
        if (
            not is_link_or_junction(entry)
            and entry.is_dir()
            and is_valid_skill_name(entry.name)
            and (entry / "SKILL.md").is_file()
        ):
            yield entry


def _skill_info(skill_dir: Path, source: str, project_dir: Path | None = None) -> dict[str, Any]:
    meta = parse_skill_metadata(skill_dir / "SKILL.md", project_dir)
    return {
        "name": skill_dir.name,
        "description": meta.get("description", ""),
        "source": source,
        "required": skill_dir.name in REQUIRED_SKILLS,
    }


def _find_library_skill(name: str) -> tuple[str, Path] | None:
    """Locate a skill by name in the library. Returns (source, path) or None."""
    if not is_valid_skill_name(name):
        return None
    for source, root in _library_sources():
        if source == "bundled" and name in _EXCLUDED_BUNDLED_SKILLS:
            continue
        candidate = root / name
        if candidate.is_dir() and (candidate / "SKILL.md").is_file():
            return source, candidate
    return None


def list_library_skills() -> list[dict[str, Any]]:
    """List every skill available for installation into projects."""
    seen: set[str] = set()
    skills: list[dict[str, Any]] = []
    for source, root in _library_sources():
        for skill_dir in _iter_skill_dirs(root):
            name = skill_dir.name
            if name in seen:
                continue
            if source == "bundled" and name in _EXCLUDED_BUNDLED_SKILLS:
                continue
            seen.add(name)
            skills.append(_skill_info(skill_dir, source))
    return skills


def list_project_skills(project_dir: Path) -> list[dict[str, Any]]:
    """List the skills installed in a project (planted links are skipped, never read through)."""
    skills_dir = get_project_skills_dir(project_dir)
    if is_link_or_junction(skills_dir.parent) or is_link_or_junction(skills_dir):
        return []
    return [_skill_info(d, "project", project_dir) for d in _iter_skill_dirs(skills_dir)]


def _copy_skill(project_dir: Path, source: Path, dest: Path) -> None:
    """Copy a library skill into the project.

    Symlinks and caches in the source are skipped, so a skill in the user's
    library cannot pull files from elsewhere into the project; on the project
    side nothing is written through a planted link.
    """
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(
            d for d in dirnames if d not in ("__pycache__", ".git") and not os.path.islink(os.path.join(dirpath, d))
        )
        target_dir = dest / Path(dirpath).relative_to(source)
        make_project_dirs(project_dir, target_dir)
        for name in sorted(filenames):
            file_path = Path(dirpath) / name
            if not file_path.is_symlink():
                write_project_file(project_dir, target_dir / name, file_path.read_bytes())


def install_skills(project_dir: Path, names: list[str]) -> tuple[list[str], list[str]]:
    """Copy skills from the library into the project.

    Skills that are already installed are left untouched (so agent-made
    edits, e.g. to project-learnings, are preserved).

    Returns:
        (installed, skipped) - names newly installed, and names that were
        unknown or already present.

    Raises:
        ValueError: If a name is not a valid skill name.
        OSError: The copy failed (``UnsafeProjectPathError`` for a planted link).
    """
    for name in names:
        if not is_valid_skill_name(name):
            raise ValueError(f"Invalid skill name: {name!r}")

    dest_root = get_project_skills_dir(project_dir)
    installed: list[str] = []
    skipped: list[str] = []
    for name in dict.fromkeys(names):  # dedupe, keep order
        found = _find_library_skill(name)
        dest = dest_root / name
        if found is None or os.path.lexists(dest):
            skipped.append(name)
            continue
        _, src = found
        _copy_skill(project_dir, src.resolve(), dest)
        installed.append(name)
    return installed, skipped


def remove_skill(project_dir: Path, name: str) -> bool:
    """Remove an installed skill from the project.

    Returns:
        True if removed, False if it was not installed.

    Raises:
        ValueError: If the name is invalid or the skill is required.
        OSError: The delete failed (``UnsafeProjectPathError`` when a parent is a planted link).
    """
    if not is_valid_skill_name(name):
        raise ValueError(f"Invalid skill name: {name!r}")
    if name in REQUIRED_SKILLS:
        raise ValueError(f"Skill '{name}' is required by AutoForge and cannot be removed")

    target = get_project_skills_dir(project_dir) / name
    if is_link_or_junction(target.parent.parent) or is_link_or_junction(target.parent):
        raise UnsafeProjectPathError(f"Refusing to remove {name!r}: the project's skills directory is a link")
    if not os.path.lexists(target):
        return False
    if is_link_or_junction(target) or not target.is_dir():
        unlink_project_file(project_dir, target)  # removes the link, never its target
    else:
        remove_project_tree(project_dir, target)
    return True
