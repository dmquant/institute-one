from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..institute import analysts as roster_mod
from ..institute.analysts import ROLES, get_analyst, roster

router = APIRouter(prefix="/api/analysts", tags=["analysts"])


class AnalystBody(BaseModel):
    id: str | None = None
    name: str
    name_en: str
    category: str
    emoji: str = "🧑‍💼"
    focus: str
    persona: str
    hand: str | None = None
    model: str | None = None


@router.get("")
async def list_analysts():
    return [asdict(a) for a in roster()]


@router.get("/roles")
async def roles():
    in_use = sorted({a.category for a in roster()})
    return {"roles": ROLES, "in_use": in_use}


@router.get("/daily/status")
async def daily_status(date: str | None = None):
    from ..institute import analyst_daily

    return await analyst_daily.status(date)


@router.post("/daily/run-now", status_code=202)
async def daily_run_all():
    """Kick the whole analyst-daily sweep in the background."""
    from ..institute import analyst_daily

    analyst_daily.spawn_all()
    return {"started": "analyst-dailies", "status_url": "/api/analysts/daily/status"}


@router.post("", status_code=201)
async def create(body: AnalystBody):
    try:
        return asdict(roster_mod.create_analyst(body.model_dump()))
    except KeyError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{analyst_id}")
async def one(analyst_id: str):
    a = get_analyst(analyst_id)
    if a is None:
        raise HTTPException(404, "unknown analyst")
    return asdict(a)


@router.put("/{analyst_id}")
async def update(analyst_id: str, body: AnalystBody):
    try:
        return asdict(roster_mod.update_analyst(analyst_id, body.model_dump()))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{analyst_id}/daily/run", status_code=202)
async def daily_run_one(analyst_id: str):
    """Run (or re-run) one analyst's daily report in the background."""
    if get_analyst(analyst_id) is None:
        raise HTTPException(404, "unknown analyst")
    from ..institute import analyst_daily

    analyst_daily.spawn_one(analyst_id)
    return {"started": analyst_id, "status_url": "/api/analysts/daily/status"}


@router.delete("/{analyst_id}")
async def delete(analyst_id: str):
    try:
        ok = roster_mod.delete_analyst(analyst_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, "unknown analyst")
    return {"deleted": analyst_id}
