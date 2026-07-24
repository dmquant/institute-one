from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..institute import memory, sessions
from ..institute.analysts import get_analyst
from ..router import executor

log = logging.getLogger("institute.api.sessions")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _require(session_id: str) -> dict[str, Any]:
    session = await sessions.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    return session


@router.get("")
async def list_sessions(kind: str | None = None, limit: int = 100):
    return await sessions.list_sessions(kind=kind, limit=limit)


class CreateSessionBody(BaseModel):
    title: str
    kind: str = "chat"
    analyst_id: str | None = None


@router.post("")
async def create_session(body: CreateSessionBody):
    if body.analyst_id and get_analyst(body.analyst_id) is None:
        raise HTTPException(404, f"unknown analyst {body.analyst_id}")
    return await sessions.create_session(body.title, kind=body.kind, analyst_id=body.analyst_id)


@router.get("/{session_id}")
async def get_session(session_id: str):
    return await _require(session_id)


@router.get("/{session_id}/messages")
async def list_messages(session_id: str):
    await _require(session_id)
    return await sessions.list_messages(session_id)


MAX_CONTENT_LEN = 16000    # chars; a chat turn, not a document


class MessageBody(BaseModel):
    content: str = Field(max_length=MAX_CONTENT_LEN)
    hand: str | None = None


@router.post("/{session_id}/messages")
async def post_message(session_id: str, body: MessageBody):
    """Chat turn: record the user message, run the hand, record the reply."""
    settings = get_settings()
    session = await _require(session_id)

    prompt = body.content
    hand = body.hand or settings.default_hand
    model: str | None = None
    if session["analyst_id"]:
        analyst = get_analyst(session["analyst_id"])
        if analyst is not None:
            prompt = await memory.prompt_with_memory(analyst, body.content)
            hand = body.hand or analyst.hand or settings.default_hand
            model = analyst.model

    await sessions.add_message(session_id, "user", body.content)
    task = await executor.submit(
        hand, prompt, source="api", model=model,
        session_id=session_id, workspace=sessions.workspace_path(session),
    )
    content = task.output if task.status == "completed" else (task.error or task.output or f"[{task.status}]")
    message_id = await sessions.add_message(session_id, "assistant", content, hand=task.hand, task_id=task.id)
    return {"message": await sessions.get_message(message_id), "task": task}


@router.get("/{session_id}/workspace")
async def list_workspace(session_id: str):
    await _require(session_id)
    return await sessions.list_workspace_files(session_id)


@router.get("/{session_id}/workspace/file")
async def read_workspace_file(session_id: str, path: str):
    await _require(session_id)
    try:
        text = await sessions.read_workspace_file(session_id, path)
    except FileNotFoundError:
        raise HTTPException(404, "file not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return PlainTextResponse(text)
