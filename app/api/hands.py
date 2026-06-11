from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..hands.registry import get_registry

router = APIRouter(prefix="/api/hands", tags=["hands"])


@router.get("")
async def hands_status():
    return get_registry().status_snapshot()


@router.post("/{name}/cooldown/clear")
async def clear_cooldown(name: str):
    registry = get_registry()
    if registry.get(name) is None:
        raise HTTPException(404, f"unknown hand {name}")
    registry.clear_cooldown(name)
    return {"ok": True}


@router.get("/{name}/health")
async def hand_health(name: str):
    registry = get_registry()
    hand = registry.get(name)
    if hand is None:
        raise HTTPException(404, f"unknown hand {name}")
    return {"name": name, "healthy": await hand.health_check(), "available": registry.is_available(name)}
