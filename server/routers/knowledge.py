"""
Knowledge Router
================

API endpoints for managing per-project knowledge documents.

Knowledge documents are markdown/text files (requirements docs, research
notes, ...) stored in ``{project}/.autoforge/knowledge/``. Every agent
session is told to read them before starting work (see
``prompts.with_project_knowledge``).

The agent can write inside ``.autoforge/``, so every file access goes through
``api.safe_project_io``: a link planted there is never followed (a refusal
answers 409 via the server's ``UnsafeProjectPathError`` handler).
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.safe_project_io import read_project_text, unlink_project_file, write_project_file

from ..schemas import (
    KnowledgeFileContent,
    KnowledgeFileInfo,
    KnowledgeFileUpload,
    KnowledgeListResponse,
)
from ..utils.project_helpers import get_project_path as _get_project_path
from ..utils.validation import validate_project_name

# Limits to keep prompts and disk usage sane
MAX_KNOWLEDGE_FILES = 50
MAX_KNOWLEDGE_FILE_SIZE = 1024 * 1024  # 1 MB of text per document

# Plain filename with a markdown/text extension: no path separators, no
# leading dot (hidden files), no "..".
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,99}\.(md|markdown|txt)$", re.IGNORECASE)

router = APIRouter(
    prefix="/api/projects/{project_name}/knowledge",
    tags=["knowledge"],
)


def _get_project_dir(project_name: str) -> Path:
    """Resolve a registered project's directory."""
    project_name = validate_project_name(project_name)
    project_path = _get_project_path(project_name)
    if not project_path:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in registry")
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project directory not found: {project_path}")
    return Path(project_path)


def _knowledge_path(project_dir: Path, filename: str) -> Path:
    """Path of a knowledge document, refused when it leaves the project."""
    from autoforge_paths import get_knowledge_dir

    path = get_knowledge_dir(project_dir) / _validate_filename(filename)
    # The file itself may be a planted link: the safe_project_io calls handle it
    if path.name != filename or not path.parent.resolve().is_relative_to(project_dir.resolve()):
        raise HTTPException(status_code=400, detail="Invalid filename: path traversal detected")
    return path


def _validate_filename(filename: str) -> str:
    if ".." in filename or not _FILENAME_RE.match(filename):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid filename. Use letters, numbers, spaces, '.', '_' or '-' "
                "with a .md, .markdown or .txt extension."
            ),
        )
    return filename


def _file_info(path: Path) -> KnowledgeFileInfo:
    stat = path.lstat()
    return KnowledgeFileInfo(
        name=path.name,
        size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


@router.get("", response_model=KnowledgeListResponse)
async def list_knowledge_files(project_name: str) -> KnowledgeListResponse:
    """List the project's knowledge documents (links are skipped)."""
    from prompts import get_knowledge_files

    files = get_knowledge_files(_get_project_dir(project_name))
    return KnowledgeListResponse(files=[_file_info(p) for p in files])


@router.get("/{filename}", response_model=KnowledgeFileContent)
async def get_knowledge_file(project_name: str, filename: str) -> KnowledgeFileContent:
    """Return a knowledge document's content."""
    project_dir = _get_project_dir(project_name)
    path = _knowledge_path(project_dir, filename)
    try:
        content = read_project_text(project_dir, path, errors="replace")  # never through a planted link
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Knowledge file '{filename}' not found")
    return KnowledgeFileContent(name=path.name, content=content)


@router.post("", response_model=KnowledgeFileInfo, status_code=201)
async def upload_knowledge_file(project_name: str, upload: KnowledgeFileUpload) -> KnowledgeFileInfo:
    """Create or overwrite a knowledge document."""
    from prompts import get_knowledge_files

    project_dir = _get_project_dir(project_name)
    path = _knowledge_path(project_dir, upload.filename)

    data = upload.content.encode("utf-8")
    if len(data) > MAX_KNOWLEDGE_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(data) / (1024 * 1024):.1f} MB). Maximum is 1 MB.",
        )

    existing = get_knowledge_files(project_dir)
    if path not in existing and len(existing) >= MAX_KNOWLEDGE_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum of {MAX_KNOWLEDGE_FILES} knowledge files per project reached",
        )

    # Creates .autoforge/knowledge/; a link at the target is replaced, never written through
    write_project_file(project_dir, path, data)
    return _file_info(path)


@router.delete("/{filename}", status_code=204)
async def delete_knowledge_file(project_name: str, filename: str) -> None:
    """Delete a knowledge document (a link is removed, never its target)."""
    project_dir = _get_project_dir(project_name)
    path = _knowledge_path(project_dir, filename)
    try:
        unlink_project_file(project_dir, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Knowledge file '{filename}' not found")
