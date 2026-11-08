"""
Git Auto-Push Router
====================

Per-project opt-in for pushing the agent's commits after each successful
coding session (see ``git_sync``). Turning it on stores the remote's current
URL; pushes only ever go to that URL.
"""

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import git_sync

from ..utils.project_helpers import get_project_path as _get_project_path
from ..utils.validation import validate_project_name

router = APIRouter(prefix="/api/projects/{project_name}/git-push", tags=["git"])

_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")


class GitPushStatus(BaseModel):
    enabled: bool
    is_git_repo: bool = True
    remote: str | None = None
    url: str | None = None  # Stored (trusted) URL that pushes go to
    current_url: str | None = None  # What `git remote get-url <remote>` says now
    unsafe_config: list[str] = Field(default_factory=list)  # Repo config keys / legacy remote files that block it


class GitPushUpdate(BaseModel):
    enabled: bool
    remote: str = "origin"

    @field_validator("remote")
    @classmethod
    def validate_remote(cls, v: str) -> str:
        if not _REMOTE_NAME_RE.match(v):
            raise ValueError("Invalid remote name")
        return v


def _project_dir(project_name: str) -> Path:
    project_name = validate_project_name(project_name)
    project_dir = _get_project_path(project_name)
    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in registry")
    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")
    return Path(project_dir)


def _status(project_dir: Path) -> GitPushStatus:
    target = git_sync.get_push_target(project_dir)
    remote = target["remote"] if target else "origin"
    if not git_sync.is_git_repo(project_dir):
        # The repo checks fail closed outside a repository; that is not unsafe config
        return GitPushStatus(enabled=target is not None, is_git_repo=False, remote=remote,
                             url=target["url"] if target else None)
    return GitPushStatus(
        enabled=target is not None,
        remote=remote,
        url=target["url"] if target else None,
        current_url=git_sync.get_remote_url(project_dir, remote),
        unsafe_config=[*git_sync.find_unsafe_local_config(project_dir), *git_sync.find_legacy_remote_files(project_dir)],
    )


@router.get("", response_model=GitPushStatus)
async def get_git_push(project_name: str) -> GitPushStatus:
    """Auto-push state for a project, with the remote URL it would push to."""
    project_dir = _project_dir(project_name)
    return await asyncio.to_thread(_status, project_dir)


@router.put("", response_model=GitPushStatus)
async def update_git_push(project_name: str, update: GitPushUpdate) -> GitPushStatus:
    """Turn auto-push on (trusting the remote's current URL) or off."""
    project_dir = _project_dir(project_name)
    if update.enabled:
        try:
            await asyncio.to_thread(git_sync.enable_auto_push, project_dir, update.remote)
        except git_sync.GitPushError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        await asyncio.to_thread(git_sync.disable_auto_push, project_dir)
    return await asyncio.to_thread(_status, project_dir)
