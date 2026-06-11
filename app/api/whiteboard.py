from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..institute import whiteboard

router = APIRouter(prefix="/api/whiteboard", tags=["whiteboard"])


class BoardBody(BaseModel):
    topic: str
    question: str = ""
    max_cards: int = Field(default=5, ge=1, le=12)


class TopicBody(BaseModel):
    topic: str
    question: str = ""
    score: float = 1.0


@router.get("/boards")
async def list_boards(status: str | None = None, limit: int = 50):
    return await whiteboard.list_boards(status=status, limit=limit)


@router.post("/boards")
async def create_board(body: BoardBody):
    return await whiteboard.create_board(body.topic, body.question, max_cards=body.max_cards)


@router.get("/boards/{board_id}")
async def get_board(board_id: str):
    board = await whiteboard.get_board(board_id)
    if board is None:
        raise HTTPException(404, "board not found")
    return board


@router.post("/boards/{board_id}/stop")
async def stop_board(board_id: str):
    board = await whiteboard.get_board(board_id)
    if board is None:
        raise HTTPException(404, "board not found")
    stopped = await whiteboard.stop_board(board_id)
    return {"stopped": stopped, "board": await whiteboard.get_board(board_id)}


@router.post("/tick")
async def tick():
    await whiteboard.tick()
    return {"ok": True}


@router.post("/kickoff")
async def kickoff():
    board_id = await whiteboard.kickoff()
    return {"board_id": board_id}


@router.get("/topics")
async def list_topics(status: str | None = "pending"):
    return await whiteboard.list_topics(status=status)


@router.post("/topics")
async def add_topic(body: TopicBody):
    return await whiteboard.add_topic(body.topic, body.question, source="api", score=body.score)


@router.delete("/topics/{topic_id}")
async def expire_topic(topic_id: int):
    ok = await whiteboard.expire_topic(topic_id)
    if not ok:
        raise HTTPException(404, "topic not found or not pending")
    return {"expired": True}
