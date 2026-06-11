"""Analyst roster — loaded from catalog/analysts.json (the source of truth).

The roster is configuration, not state: no DB table. CRUD writes back to the
catalog file atomically and reloads the cache, so edits made through the API
survive restarts and live in version control alongside the code.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from functools import lru_cache

from ..config import get_settings

# Known roles (free-form values are allowed; this list feeds UI dropdowns)
ROLES = ["strategy", "macro", "policy", "equity", "industry", "fixed-income", "ops"]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

REQUIRED_FIELDS = ("id", "name", "name_en", "category", "emoji", "focus", "persona")


@dataclass(frozen=True)
class Analyst:
    id: str
    name: str          # zh display name
    name_en: str
    category: str      # the analyst's role: strategy|macro|policy|equity|industry|fixed-income|ops|…
    emoji: str
    focus: str         # one-line coverage statement (zh)
    persona: str       # the persona paragraph injected into prompts (zh)
    hand: str | None = None    # preferred hand; None -> settings.default_hand
    model: str | None = None


@lru_cache(maxsize=1)
def _load() -> list[Analyst]:
    raw = json.loads(get_settings().catalog_path.read_text(encoding="utf-8"))
    return [Analyst(**a) for a in raw["analysts"]]


def roster() -> list[Analyst]:
    return list(_load())


def get_analyst(analyst_id: str) -> Analyst | None:
    for a in _load():
        if a.id == analyst_id:
            return a
    return None


def reload() -> None:
    _load.cache_clear()


# ---- CRUD (persists to catalog/analysts.json) -----------------------------

def _save(analysts: list[Analyst]) -> None:
    path = get_settings().catalog_path
    payload = json.dumps({"analysts": [asdict(a) for a in analysts]}, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    reload()


def validate(data: dict) -> Analyst:
    """Validate an analyst payload. Raises ValueError with a readable message."""
    missing = [f for f in REQUIRED_FIELDS if not str(data.get(f, "")).strip()]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    analyst_id = str(data["id"]).strip()
    if not _ID_RE.match(analyst_id):
        raise ValueError("id must be a 3-40 char lowercase slug (a-z, 0-9, -)")
    hand = (str(data.get("hand") or "").strip() or None)
    model = (str(data.get("model") or "").strip() or None)
    return Analyst(
        id=analyst_id,
        name=str(data["name"]).strip(),
        name_en=str(data["name_en"]).strip(),
        category=str(data["category"]).strip(),
        emoji=str(data["emoji"]).strip(),
        focus=str(data["focus"]).strip(),
        persona=str(data["persona"]).strip(),
        hand=hand,
        model=model,
    )


def create_analyst(data: dict) -> Analyst:
    analyst = validate(data)
    current = roster()
    if any(a.id == analyst.id for a in current):
        raise KeyError(f"analyst '{analyst.id}' already exists")
    _save(current + [analyst])
    return analyst


def update_analyst(analyst_id: str, data: dict) -> Analyst:
    current = roster()
    if not any(a.id == analyst_id for a in current):
        raise LookupError(f"unknown analyst '{analyst_id}'")
    analyst = validate({**data, "id": analyst_id})  # id is immutable
    _save([analyst if a.id == analyst_id else a for a in current])
    return analyst


def delete_analyst(analyst_id: str) -> bool:
    current = roster()
    remaining = [a for a in current if a.id != analyst_id]
    if len(remaining) == len(current):
        return False
    if not remaining:
        raise ValueError("cannot delete the last analyst")
    _save(remaining)
    return True
