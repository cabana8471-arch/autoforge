"""
Spec Creation Router
====================

WebSocket and REST endpoints for interactive spec creation with Claude.
"""

import json
import logging
from typing import AsyncGenerator, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from ..schemas import FileAttachment
from ..services.spec_chat_session import (
    SpecChatSession,
    create_session,
    get_session,
    list_sessions,
    remove_session,
)
from ..utils.project_helpers import get_project_path as _get_project_path
from ..utils.validation import is_valid_project_name, validate_project_name
from ..utils.ws_inbox import WebSocketInbox
from ..utils.ws_stream import forward_chunks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/spec", tags=["spec-creation"])


# ============================================================================
# REST Endpoints
# ============================================================================

class SpecSessionStatus(BaseModel):
    """Status of a spec creation session."""
    project_name: str
    is_active: bool
    is_complete: bool
    message_count: int


@router.get("/sessions", response_model=list[str])
async def list_spec_sessions():
    """List all active spec creation sessions."""
    return list_sessions()


@router.get("/sessions/{project_name}", response_model=SpecSessionStatus)
async def get_session_status(project_name: str):
    """Get status of a spec creation session."""
    if not is_valid_project_name(project_name):
        raise HTTPException(status_code=400, detail="Invalid project name")

    session = get_session(project_name)
    if not session:
        raise HTTPException(status_code=404, detail="No active session for this project")

    return SpecSessionStatus(
        project_name=project_name,
        is_active=True,
        is_complete=session.is_complete(),
        message_count=len(session.get_messages()),
    )


@router.delete("/sessions/{project_name}")
async def cancel_session(project_name: str):
    """Cancel and remove a spec creation session."""
    if not is_valid_project_name(project_name):
        raise HTTPException(status_code=400, detail="Invalid project name")

    session = get_session(project_name)
    if not session:
        raise HTTPException(status_code=404, detail="No active session for this project")

    await remove_session(project_name)
    return {"success": True, "message": "Session cancelled"}


class SpecFileStatus(BaseModel):
    """Status of spec files on disk (from .spec_status.json)."""
    exists: bool
    status: str  # "complete" | "in_progress" | "not_started"
    feature_count: Optional[int] = None
    timestamp: Optional[str] = None
    files_written: list[str] = []


@router.get("/status/{project_name}", response_model=SpecFileStatus)
async def get_spec_file_status(project_name: str):
    """
    Get spec creation status by reading .spec_status.json from the project.

    This is used for polling to detect when Claude has finished writing spec files.
    Claude writes this status file as the final step after completing all spec work.
    """
    if not is_valid_project_name(project_name):
        raise HTTPException(status_code=400, detail="Invalid project name")

    project_dir = _get_project_path(project_name)
    if not project_dir:
        raise HTTPException(status_code=404, detail="Project not found in registry")

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")

    from autoforge_paths import get_prompts_dir
    status_file = get_prompts_dir(project_dir) / ".spec_status.json"

    if not status_file.exists():
        return SpecFileStatus(
            exists=False,
            status="not_started",
            feature_count=None,
            timestamp=None,
            files_written=[],
        )

    try:
        from api.safe_project_io import read_project_text
        data = json.loads(read_project_text(project_dir, status_file))  # never through a planted link
        return SpecFileStatus(
            exists=True,
            status=data.get("status", "unknown"),
            feature_count=data.get("feature_count"),
            timestamp=data.get("timestamp"),
            files_written=data.get("files_written", []),
        )
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid JSON in spec status file: {e}")
        return SpecFileStatus(
            exists=True,
            status="error",
            feature_count=None,
            timestamp=None,
            files_written=[],
        )
    except Exception as e:
        logger.error(f"Error reading spec status file: {e}")
        raise HTTPException(status_code=500, detail="Failed to read status file")


# ============================================================================
# WebSocket Endpoint
# ============================================================================

async def _forward_or_cancel(
    websocket: WebSocket,
    inbox: WebSocketInbox,
    project_name: str,
    session: SpecChatSession,
    chunks: AsyncGenerator[dict, None],
) -> bool:
    """Forward a turn; if the client closed the chat on purpose, stop the turn and close the session."""
    if await forward_chunks(websocket, chunks, cancel=inbox.client_left, interrupt=session.interrupt):
        return True
    if inbox.client_left.is_set():
        await remove_session(project_name)
    return False


async def _with_completion(chunks: AsyncIterator[dict]) -> AsyncGenerator[dict, None]:
    """Pass chunks through, adding {"type": "complete"} after the response_done of a turn that wrote the spec."""
    spec_path = None
    async for chunk in chunks:
        if chunk.get("type") == "spec_complete":
            spec_path = chunk.get("path")
        yield chunk
        if chunk.get("type") == "response_done" and spec_path is not None:
            yield {"type": "complete", "path": spec_path}


@router.websocket("/ws/{project_name}")
async def spec_chat_websocket(websocket: WebSocket, project_name: str):
    """
    WebSocket endpoint for interactive spec creation chat.

    Message protocol:

    Client -> Server:
    - {"type": "start"} - Start the spec creation session
    - {"type": "message", "content": "..."} - Send user message
    - {"type": "answer", "answers": {...}, "tool_id": "..."} - Answer structured question
    - {"type": "ping"} - Keep-alive ping

    Server -> Client:
    - {"type": "text", "content": "..."} - Text chunk from Claude
    - {"type": "question", "questions": [...], "tool_id": "..."} - Structured question
    - {"type": "spec_complete", "path": "..."} - Spec file created
    - {"type": "file_written", "path": "..."} - Other file written
    - {"type": "complete"} - Session complete
    - {"type": "error", "content": "..."} - Error message
    - {"type": "pong"} - Keep-alive pong
    """
    # Always accept WebSocket first to avoid opaque 403 errors
    await websocket.accept()

    try:
        project_name = validate_project_name(project_name)
    except HTTPException:
        await websocket.send_json({"type": "error", "content": "Invalid project name"})
        await websocket.close(code=4000, reason="Invalid project name")
        return

    # Look up project directory from registry
    project_dir = _get_project_path(project_name)
    if not project_dir:
        await websocket.send_json({"type": "error", "content": "Project not found in registry"})
        await websocket.close(code=4004, reason="Project not found in registry")
        return

    if not project_dir.exists():
        await websocket.send_json({"type": "error", "content": "Project directory not found"})
        await websocket.close(code=4004, reason="Project directory not found")
        return

    session: Optional[SpecChatSession] = None
    # Read the socket in the background so uvicorn keeps seeing keepalive pongs
    # while a long Claude turn is streamed from this loop.
    inbox = WebSocketInbox(websocket)
    inbox.start()

    try:
        while True:
            try:
                # Receive message from client
                data = await inbox.receive_text()
                message = json.loads(data)
                msg_type = message.get("type")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue

                elif msg_type == "start":
                    # Create and start a new session
                    session = await create_session(project_name, project_dir)

                    # Stream the initial greeting
                    if not await _forward_or_cancel(websocket, inbox, project_name, session, _with_completion(session.start())):
                        break

                elif msg_type == "message":
                    # User sent a message
                    if not session:
                        session = get_session(project_name)
                        if not session:
                            await websocket.send_json({
                                "type": "error",
                                "content": "No active session. Send 'start' first."
                            })
                            continue

                    user_content = message.get("content", "").strip()

                    # Parse attachments if present
                    attachments: list[FileAttachment] = []
                    raw_attachments = message.get("attachments", [])
                    if raw_attachments:
                        try:
                            for raw_att in raw_attachments:
                                attachments.append(FileAttachment(**raw_att))
                        except (ValidationError, Exception) as e:
                            logger.warning(f"Invalid attachment data: {e}")
                            await websocket.send_json({
                                "type": "error",
                                "content": f"Invalid attachment: {str(e)}"
                            })
                            continue

                    # Allow empty content if attachments are present
                    if not user_content and not attachments:
                        await websocket.send_json({
                            "type": "error",
                            "content": "Empty message"
                        })
                        continue

                    # Stream Claude's response (with attachments if present)
                    if not await _forward_or_cancel(websocket, inbox, project_name, session, _with_completion(session.send_message(user_content, attachments if attachments else None))):
                        break

                elif msg_type == "answer":
                    # User answered a structured question
                    if not session:
                        session = get_session(project_name)
                        if not session:
                            await websocket.send_json({
                                "type": "error",
                                "content": "No active session"
                            })
                            continue

                    # Format the answers as a natural response
                    answers = message.get("answers", {})
                    if isinstance(answers, dict):
                        # Convert structured answers to a message
                        response_parts = []
                        for question_idx, answer_value in answers.items():
                            if isinstance(answer_value, list):
                                response_parts.append(", ".join(answer_value))
                            else:
                                response_parts.append(str(answer_value))
                        user_response = "; ".join(response_parts) if response_parts else "OK"
                    else:
                        user_response = str(answers)

                    # Stream Claude's response
                    if not await _forward_or_cancel(websocket, inbox, project_name, session, _with_completion(session.send_message(user_response))):
                        break

                else:
                    await websocket.send_json({
                        "type": "error",
                        "content": f"Unknown message type: {msg_type}"
                    })

            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "content": "Invalid JSON"
                })

    except WebSocketDisconnect:
        logger.info(f"Spec chat WebSocket disconnected for {project_name}")

    except Exception as e:
        logger.exception(f"Spec chat WebSocket error for {project_name}")
        try:
            await websocket.send_json({
                "type": "error",
                "content": f"Server error: {str(e)}"
            })
        except Exception:
            pass

    finally:
        # Don't remove the session on disconnect - allow resume
        await inbox.aclose()
