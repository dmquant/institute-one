from __future__ import annotations

from fastapi import APIRouter

from .. import db
from ..config import VERSION, get_settings
from ..hands.registry import get_registry
from ..institute.prompts import now_sgt, work_date
from ..router import executor

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health():
    return {"ok": True, "version": VERSION, "time_sgt": now_sgt().isoformat(timespec="seconds")}


@router.get("/api/meta")
async def meta():
    settings = get_settings()
    return {
        "version": VERSION,
        "timezone": settings.timezone,
        "work_date": work_date(),
        "hands": get_registry().status_snapshot(),
        "vault_configured": settings.vault_dir is not None,
        "queue": await executor.queue_stats(),
        "limits": {
            "max_concurrent": settings.max_concurrent,
            "default_timeout_s": settings.default_timeout_s,
            "output_cap_bytes": settings.output_cap_bytes,
        },
    }


@router.get("/api/admin/state")
async def admin_state():
    rows = await db.query("SELECT key, value FROM admin_state")
    return {r["key"]: r["value"] for r in rows}
