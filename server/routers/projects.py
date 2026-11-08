"""
Projects Router
===============

API endpoints for project management.
Uses project registry for path lookups instead of fixed generations/ directory.
"""

import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from api.safe_project_io import UnsafeProjectPathError

from ..schemas import (
    DetachResponse,
    DetachStatusResponse,
    ProjectCreate,
    ProjectDetail,
    ProjectImport,
    ProjectPrompts,
    ProjectPromptsUpdate,
    ProjectSettingsUpdate,
    ProjectStats,
    ProjectSummary,
    ProjectUsageResponse,
    ReattachResponse,
    UsageSession,
    UsageTotals,
)

logger = logging.getLogger(__name__)

# Lazy imports to avoid circular dependencies
# These are initialized by _init_imports() before first use.
_imports_initialized = False
_check_spec_exists: Callable[..., Any] | None = None
_scaffold_project_prompts: Callable[..., Any] | None = None
_get_project_prompts_dir: Callable[..., Any] | None = None
_count_passing_tests: Callable[..., Any] | None = None
_detach_module: Any = None


def _init_imports():
    """Lazy import of project-level modules."""
    global _imports_initialized, _check_spec_exists
    global _scaffold_project_prompts, _get_project_prompts_dir
    global _count_passing_tests, _detach_module

    if _imports_initialized:
        return

    import sys
    root = Path(__file__).parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from progress import count_passing_tests
    from prompts import get_project_prompts_dir, scaffold_project_prompts
    from start import check_spec_exists

    _check_spec_exists = check_spec_exists
    _scaffold_project_prompts = scaffold_project_prompts
    _get_project_prompts_dir = get_project_prompts_dir
    _count_passing_tests = count_passing_tests

    import detach as detach_mod
    _detach_module = detach_mod

    _imports_initialized = True


def _get_registry_functions():
    """Get registry functions with lazy import."""
    import sys
    root = Path(__file__).parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from registry import (
        get_project_auto_improve,
        get_project_concurrency,
        get_project_path,
        list_registered_projects,
        register_project,
        set_project_auto_improve,
        set_project_concurrency,
        unregister_project,
        validate_project_path,
    )
    return (
        register_project,
        unregister_project,
        get_project_path,
        list_registered_projects,
        validate_project_path,
        get_project_concurrency,
        set_project_concurrency,
        get_project_auto_improve,
        set_project_auto_improve,
    )


router = APIRouter(prefix="/api/projects", tags=["projects"])


def validate_project_name(name: str) -> str:
    """Validate and sanitize project name to prevent path traversal."""
    if not re.match(r'^[a-zA-Z0-9_-]{1,50}$', name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name. Use only letters, numbers, hyphens, and underscores (1-50 chars)."
        )
    return name


def get_project_stats(project_dir: Path) -> ProjectStats:
    """Get statistics for a project."""
    _init_imports()
    assert _count_passing_tests is not None  # guaranteed by _init_imports()
    passing, in_progress, total, _needs_human_input = _count_passing_tests(project_dir)
    percentage = (passing / total * 100) if total > 0 else 0.0
    return ProjectStats(
        passing=passing,
        in_progress=in_progress,
        total=total,
        percentage=round(percentage, 1)
    )


def _ensure_not_registered(name: str, project_path: Path) -> None:
    """Raise 409 if the name or the path is already in the registry."""
    (_, _, get_project_path, list_registered_projects,
     _, _, _, _, _) = _get_registry_functions()

    # Check if project name already registered
    existing = get_project_path(name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' already exists at {existing}"
        )

    # Check if path already registered under a different name
    all_projects = list_registered_projects()
    for existing_name, info in all_projects.items():
        existing_path = Path(info["path"]).resolve()
        # Case-insensitive comparison on Windows
        if sys.platform == "win32":
            paths_match = str(existing_path).lower() == str(project_path).lower()
        else:
            paths_match = existing_path == project_path

        if paths_match:
            raise HTTPException(
                status_code=409,
                detail=f"Path '{project_path}' is already registered as project '{existing_name}'"
            )


@router.get("", response_model=list[ProjectSummary])
async def list_projects():
    """List all registered projects."""
    _init_imports()
    assert _check_spec_exists is not None  # guaranteed by _init_imports()
    (_, _, _, list_registered_projects, validate_project_path,
     get_project_concurrency, _, _, _) = _get_registry_functions()

    projects = list_registered_projects()
    result = []

    for name, info in projects.items():
        project_dir = Path(info["path"])

        # Skip if path no longer exists
        is_valid, _ = validate_project_path(project_dir)
        if not is_valid:
            continue

        has_spec = _check_spec_exists(project_dir)
        error = None
        try:
            stats = get_project_stats(project_dir)
        except UnsafeProjectPathError as e:
            # One project with a refused database must not break the whole list;
            # the reason is shown in the UI and its own endpoints answer 409
            logger.warning("No stats for project %s: %s", name, e)
            stats = ProjectStats(passing=0, in_progress=0, total=0, percentage=0.0)
            error = str(e)

        result.append(ProjectSummary(
            name=name,
            path=info["path"],
            has_spec=has_spec,
            stats=stats,
            default_concurrency=info.get("default_concurrency", 3),
            auto_improve_enabled=bool(info.get("auto_improve_enabled", False)),
            auto_improve_interval_minutes=int(
                info.get("auto_improve_interval_minutes", 10) or 10
            ),
            is_detached=_detach_module.is_project_detached(project_dir),
            error=error,
        ))

    return result


@router.post("", response_model=ProjectSummary)
async def create_project(project: ProjectCreate):
    """Create a new project at the specified path."""
    _init_imports()
    assert _scaffold_project_prompts is not None  # guaranteed by _init_imports()
    (register_project, _, _, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(project.name)
    project_path = Path(project.path).resolve()

    _ensure_not_registered(name, project_path)

    # Security: Check if path is in a blocked location
    from .filesystem import is_path_blocked
    if is_path_blocked(project_path):
        raise HTTPException(
            status_code=403,
            detail="Cannot create project in system or sensitive directory"
        )

    # Validate the path is usable
    if project_path.exists():
        if not project_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail="Path exists but is not a directory"
            )
    else:
        # Create the directory
        try:
            project_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create directory: {e}"
            )

    # Bring an existing (e.g. AutoCoder-era) project up to the current layout
    # before scaffolding, so legacy files are moved into .autoforge/ rather
    # than shadowed by freshly scaffolded ones.
    from autoforge_paths import migrate_project_layout
    migrate_project_layout(project_path)

    # Scaffold prompts
    _scaffold_project_prompts(project_path)

    # Install skills selected at set-up time
    # (names are validated by the schema; a copy failure should not abort creation)
    if project.skills:
        from project_skills import install_skills
        try:
            install_skills(project_path, project.skills)
        except (OSError, ValueError) as e:
            print(f"  Warning: Could not install skills: {e}")

    # Register in registry
    try:
        register_project(name, project_path)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to register project: {e}"
        )

    return ProjectSummary(
        name=name,
        path=project_path.as_posix(),
        has_spec=False,  # Just created, no spec yet
        stats=ProjectStats(passing=0, total=0, percentage=0.0),
        default_concurrency=3,
    )


@router.post("/import", response_model=ProjectSummary)
async def import_project(project: ProjectImport):
    """Register an existing AutoForge project folder (e.g. after a reinstall).

    Unlike project creation, this never creates directories or scaffolds
    prompt templates: the folder must already contain AutoForge data
    (``features.db`` and/or ``prompts/app_spec.txt``) so its features,
    progress and spec are picked up as-is.
    """
    _init_imports()
    assert _check_spec_exists is not None  # guaranteed by _init_imports()
    (register_project, _, _, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(project.name)
    project_path = Path(project.path).resolve()

    # Security: Check if path is in a blocked location
    from .filesystem import is_path_blocked
    if is_path_blocked(project_path):
        raise HTTPException(
            status_code=403,
            detail="Cannot import project from system or sensitive directory"
        )

    if not project_path.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Folder does not exist: {project_path}"
        )

    from autoforge_paths import is_autoforge_project
    if not is_autoforge_project(project_path):
        raise HTTPException(
            status_code=400,
            detail=(
                "This folder does not look like an AutoForge project "
                "(no .autoforge/features.db or prompts/app_spec.txt found). "
                "Use 'New Project' to start a new project in it instead."
            )
        )

    _ensure_not_registered(name, project_path)

    try:
        register_project(name, project_path)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to register project: {e}"
        )

    return ProjectSummary(
        name=name,
        path=project_path.as_posix(),
        has_spec=_check_spec_exists(project_path),
        stats=get_project_stats(project_path),
        default_concurrency=3,
    )


@router.get("/{name}", response_model=ProjectDetail)
async def get_project(name: str):
    """Get detailed information about a project."""
    _init_imports()
    assert _check_spec_exists is not None  # guaranteed by _init_imports()
    assert _get_project_prompts_dir is not None  # guaranteed by _init_imports()
    (_, _, get_project_path, _, _, get_project_concurrency, _,
     get_project_auto_improve, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found in registry")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail=f"Project directory no longer exists: {project_dir}")

    has_spec = _check_spec_exists(project_dir)
    stats = get_project_stats(project_dir)
    prompts_dir = _get_project_prompts_dir(project_dir)
    ai_enabled, ai_interval = get_project_auto_improve(name)

    return ProjectDetail(
        name=name,
        path=project_dir.as_posix(),
        has_spec=has_spec,
        stats=stats,
        prompts_dir=str(prompts_dir),
        default_concurrency=get_project_concurrency(name),
        auto_improve_enabled=ai_enabled,
        auto_improve_interval_minutes=ai_interval,
        is_detached=_detach_module.is_project_detached(project_dir),
    )


@router.delete("/{name}")
async def delete_project(name: str, delete_files: bool = False):
    """
    Delete a project from the registry.

    Args:
        name: Project name to delete
        delete_files: If True, also delete the project directory and files
    """
    _init_imports()
    (_, unregister_project, get_project_path, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    # Check if agent is running
    from autoforge_paths import has_agent_running
    if has_agent_running(project_dir):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete project while agent is running. Stop the agent first."
        )

    # Optionally delete files
    if delete_files and project_dir.exists():
        try:
            shutil.rmtree(project_dir)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete project files: {e}")

    # Unregister from registry
    unregister_project(name)

    return {
        "success": True,
        "message": f"Project '{name}' deleted" + (" (files removed)" if delete_files else " (files preserved)")
    }


@router.get("/{name}/prompts", response_model=ProjectPrompts)
async def get_project_prompts(name: str):
    """Get the content of project prompt files."""
    _init_imports()
    assert _get_project_prompts_dir is not None  # guaranteed by _init_imports()
    (_, _, get_project_path, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    prompts_dir: Path = _get_project_prompts_dir(project_dir)

    def read_file(filename: str) -> str:
        filepath = prompts_dir / filename
        if filepath.exists():
            try:
                from api.safe_project_io import read_project_text
                return read_project_text(project_dir, filepath)  # never through a planted link
            except Exception:
                return ""
        return ""

    return ProjectPrompts(
        app_spec=read_file("app_spec.txt"),
        initializer_prompt=read_file("initializer_prompt.md"),
        coding_prompt=read_file("coding_prompt.md"),
    )


@router.put("/{name}/prompts")
async def update_project_prompts(name: str, prompts: ProjectPromptsUpdate):
    """Update project prompt files."""
    _init_imports()
    assert _get_project_prompts_dir is not None  # guaranteed by _init_imports()
    (_, _, get_project_path, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    from api.safe_project_io import write_project_file

    prompts_dir = _get_project_prompts_dir(project_dir)

    def write_file(filename: str, content: str | None):
        if content is not None:
            # Never through a link the agent planted in the project
            write_project_file(project_dir, prompts_dir / filename, content.encode("utf-8"))

    write_file("app_spec.txt", prompts.app_spec)
    write_file("initializer_prompt.md", prompts.initializer_prompt)
    write_file("coding_prompt.md", prompts.coding_prompt)

    return {"success": True, "message": "Prompts updated"}


@router.get("/{name}/stats", response_model=ProjectStats)
async def get_project_stats_endpoint(name: str):
    """Get current progress statistics for a project."""
    _init_imports()
    (_, _, get_project_path, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    return get_project_stats(project_dir)


def _resolve_existing_project_dir(name: str) -> Path:
    (_, _, get_project_path, _, _, _, _, _, _) = _get_registry_functions()
    name = validate_project_name(name)
    project_dir = get_project_path(name)
    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")
    return Path(project_dir)


@router.get("/{name}/usage", response_model=ProjectUsageResponse)
async def get_project_usage(name: str):
    """Get cumulative token usage and cost for a project, plus the active cost limit."""
    project_dir = _resolve_existing_project_dir(name)

    from usage_tracker import get_cost_limit, get_recent_sessions, get_usage_totals

    totals = get_usage_totals(project_dir)
    limit = get_cost_limit()
    return ProjectUsageResponse(
        totals=UsageTotals(**totals),
        cost_limit_usd=limit,
        limit_reached=limit is not None and totals["cost_usd"] >= limit,
        recent_sessions=[UsageSession(**r) for r in get_recent_sessions(project_dir)],
    )


@router.delete("/{name}/usage")
async def reset_project_usage(name: str):
    """Clear recorded usage for a project (resets its spend against the cost limit)."""
    project_dir = _resolve_existing_project_dir(name)

    from usage_tracker import reset_usage

    deleted = reset_usage(project_dir)
    return {"success": True, "deleted": deleted}


@router.post("/{name}/reset")
async def reset_project(name: str, full_reset: bool = False):
    """
    Reset a project to its initial state.

    Args:
        name: Project name to reset
        full_reset: If True, also delete prompts/ directory (triggers setup wizard)

    Returns:
        Dictionary with list of deleted files and reset type
    """
    _init_imports()
    (_, _, get_project_path, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    # Check if agent is running
    from autoforge_paths import has_agent_running
    if has_agent_running(project_dir):
        raise HTTPException(
            status_code=409,
            detail="Cannot reset project while agent is running. Stop the agent first."
        )

    # Dispose of database engines to release file locks (required on Windows)
    # Import here to avoid circular imports
    from api.database import dispose_engine as dispose_features_engine
    from server.services.assistant_database import dispose_engine as dispose_assistant_engine

    dispose_features_engine(project_dir)
    dispose_assistant_engine(project_dir)

    deleted_files: list[str] = []

    from autoforge_paths import (
        get_assistant_db_path,
        get_claude_assistant_settings_path,
        get_claude_settings_path,
        get_features_db_path,
    )

    # Build list of files to delete using path helpers (finds files at current location)
    # Plus explicit old-location fallbacks for backward compatibility
    db_path = get_features_db_path(project_dir)
    asst_path = get_assistant_db_path(project_dir)
    reset_files: list[Path] = [
        db_path,
        db_path.with_suffix(".db-wal"),
        db_path.with_suffix(".db-shm"),
        asst_path,
        asst_path.with_suffix(".db-wal"),
        asst_path.with_suffix(".db-shm"),
        get_claude_settings_path(project_dir),
        get_claude_assistant_settings_path(project_dir),
        # Also clean old root-level locations if they exist
        project_dir / "features.db",
        project_dir / "features.db-wal",
        project_dir / "features.db-shm",
        project_dir / "assistant.db",
        project_dir / "assistant.db-wal",
        project_dir / "assistant.db-shm",
        project_dir / ".claude_settings.json",
        project_dir / ".claude_assistant_settings.json",
    ]

    # Never delete through a symlinked directory the agent planted in the project
    from api.safe_project_io import remove_project_tree, unlink_project_file

    for file_path in reset_files:
        if file_path.exists():
            try:
                relative = file_path.relative_to(project_dir)
                unlink_project_file(project_dir, file_path)
                deleted_files.append(str(relative))
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to delete {file_path.name}: {e}")

    # Full reset: also delete prompts directory
    if full_reset:
        from autoforge_paths import get_prompts_dir
        # Delete prompts from both possible locations
        for prompts_dir in [get_prompts_dir(project_dir), project_dir / "prompts"]:
            if prompts_dir.exists():
                try:
                    relative = prompts_dir.relative_to(project_dir)
                    remove_project_tree(project_dir, prompts_dir)
                    deleted_files.append(f"{relative}/")
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Failed to delete prompts: {e}")

    return {
        "success": True,
        "reset_type": "full" if full_reset else "quick",
        "deleted_files": deleted_files,
        "message": f"Project '{name}' has been reset" + (" (full reset)" if full_reset else " (quick reset)")
    }


@router.patch("/{name}/settings", response_model=ProjectDetail)
async def update_project_settings(name: str, settings: ProjectSettingsUpdate):
    """Update project-level settings (concurrency, auto-improve, etc.)."""
    _init_imports()
    assert _check_spec_exists is not None  # guaranteed by _init_imports()
    assert _get_project_prompts_dir is not None  # guaranteed by _init_imports()
    (_, _, get_project_path, _, _, get_project_concurrency,
     set_project_concurrency, get_project_auto_improve,
     set_project_auto_improve) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    # Update concurrency if provided
    if settings.default_concurrency is not None:
        success = set_project_concurrency(name, settings.default_concurrency)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update concurrency")

    # Update auto-improve config if either field was provided
    auto_improve_touched = (
        settings.auto_improve_enabled is not None
        or settings.auto_improve_interval_minutes is not None
    )
    if auto_improve_touched:
        try:
            success = set_project_auto_improve(
                name,
                enabled=settings.auto_improve_enabled,
                interval_minutes=settings.auto_improve_interval_minutes,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update auto-improve settings")

        # Sync the scheduler with the new state.
        from ..services.scheduler_service import get_scheduler
        scheduler = get_scheduler()
        ai_enabled, ai_interval = get_project_auto_improve(name)
        if ai_enabled:
            try:
                await scheduler.register_auto_improve(name, project_dir, ai_interval)
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to register auto-improve schedule: {e}",
                )
        else:
            scheduler.remove_auto_improve(name)

    # Return updated project details
    has_spec = _check_spec_exists(project_dir)
    stats = get_project_stats(project_dir)
    prompts_dir = _get_project_prompts_dir(project_dir)
    ai_enabled, ai_interval = get_project_auto_improve(name)

    return ProjectDetail(
        name=name,
        path=project_dir.as_posix(),
        has_spec=has_spec,
        stats=stats,
        prompts_dir=str(prompts_dir),
        default_concurrency=get_project_concurrency(name),
        auto_improve_enabled=ai_enabled,
        auto_improve_interval_minutes=ai_interval,
        is_detached=_detach_module.is_project_detached(project_dir),
    )


@router.get("/{name}/detach-status", response_model=DetachStatusResponse)
async def get_detach_status(name: str):
    """Get detach status for a project."""
    _init_imports()
    (_, _, get_project_path, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    status = _detach_module.get_detach_status(name)
    return DetachStatusResponse(
        is_detached=status["is_detached"],
        backup_exists=status["backup_exists"],
        backup_size=status.get("backup_size"),
        detached_at=status.get("detached_at"),
        file_count=status.get("file_count"),
    )


@router.post("/{name}/detach", response_model=DetachResponse)
async def detach_project(name: str):
    """Detach a project by moving Autocoder files to backup."""
    _init_imports()
    (_, _, get_project_path, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    # Check if agent is running
    from autoforge_paths import has_agent_running
    if has_agent_running(project_dir):
        raise HTTPException(
            status_code=409,
            detail="Cannot detach project while agent is running. Stop the agent first."
        )

    success, message, manifest, user_files_restored = _detach_module.detach_project(name, force=True)

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return DetachResponse(
        success=True,
        files_moved=manifest["file_count"] if manifest else 0,
        backup_size=manifest["total_size_bytes"] if manifest else 0,
        backup_path=".autoforge-backup",
        message=message,
        user_files_restored=user_files_restored,
    )


@router.post("/{name}/reattach", response_model=ReattachResponse)
async def reattach_project(name: str):
    """Reattach a project by restoring Autocoder files from backup."""
    _init_imports()
    (_, _, get_project_path, _, _, _, _, _, _) = _get_registry_functions()

    name = validate_project_name(name)
    project_dir = get_project_path(name)

    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    # Check if agent is running
    from autoforge_paths import has_agent_running
    if has_agent_running(project_dir):
        raise HTTPException(
            status_code=409,
            detail="Cannot reattach project while agent is running. Stop the agent first."
        )

    success, message, files_restored, conflicts = _detach_module.reattach_project(name)

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return ReattachResponse(
        success=True,
        files_restored=files_restored,
        message=message,
        conflicts=conflicts,
        conflicts_backup_path=".autoforge-pre-reattach-backup" if conflicts else None,
    )
