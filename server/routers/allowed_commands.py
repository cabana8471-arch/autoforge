"""
Project Allowlist Router
========================

Read and save a project's ``.autoforge/allowed_commands.yaml`` from the UI
(user decision 3B). The agent cannot change this file, so the user edits it
here: the text is checked with the security hook's own rules before it is
saved, and the file is never read or written through a link planted in the
project (a planted link answers 409 through the global handler).
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.safe_project_io import read_project_text, write_project_file
from security import validate_project_config_text

from ..utils.project_helpers import get_project_path as _get_project_path
from ..utils.validation import validate_project_name

router = APIRouter(prefix="/api/projects/{project_name}/allowed-commands", tags=["security"])

CONFIG_PATH = Path(".autoforge") / "allowed_commands.yaml"
LEGACY_CONFIG_PATH = Path(".autocoder") / "allowed_commands.yaml"
MAX_CONFIG_CHARS = 64_000


class AllowedCommandsFile(BaseModel):
    exists: bool
    content: str
    path: str  # Project-relative file the security hook reads


class AllowedCommandsUpdate(BaseModel):
    content: str = Field(max_length=MAX_CONFIG_CHARS)


def _project_dir(project_name: str) -> Path:
    project_name = validate_project_name(project_name)
    project_dir = _get_project_path(project_name)
    if not project_dir:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in registry")
    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")
    return Path(project_dir).resolve()


def _read(project_dir: Path) -> AllowedCommandsFile:
    # Same lookup order as security.load_project_commands: new location, then legacy
    for relative in (CONFIG_PATH, LEGACY_CONFIG_PATH):
        try:
            content = read_project_text(project_dir, project_dir / relative, errors="replace")
        except FileNotFoundError:
            continue
        return AllowedCommandsFile(exists=True, content=content, path=relative.as_posix())
    return AllowedCommandsFile(exists=False, content="", path=CONFIG_PATH.as_posix())


@router.get("", response_model=AllowedCommandsFile)
async def get_allowed_commands(project_name: str) -> AllowedCommandsFile:
    return _read(_project_dir(project_name))


@router.put("", response_model=AllowedCommandsFile)
async def update_allowed_commands(project_name: str, body: AllowedCommandsUpdate) -> AllowedCommandsFile:
    project_dir = _project_dir(project_name)
    errors = validate_project_config_text(body.content)
    if errors:
        raise HTTPException(status_code=400, detail="Invalid allowlist: " + "; ".join(errors))
    write_project_file(project_dir, project_dir / CONFIG_PATH, body.content.encode("utf-8"))
    return _read(project_dir)
