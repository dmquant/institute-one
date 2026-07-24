"""Favorites API round-trip and validation (ROADMAP Phase 7)."""
from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db


def _app() -> FastAPI:
    from app.api import favorites as api_favorites

    app = FastAPI()
    app.include_router(api_favorites.router)
    return app


async def _seed_research(item_id: str = "research-favorite-1") -> str:
    await db.execute(
        "INSERT INTO research_queue "
        "(id, topic, priority, status, source, created_at, finished_at) "
        "VALUES (?,?,0,'completed','test',?,?)",
        (item_id, "固态电池量产进度", bus.now_iso(), bus.now_iso()),
    )
    return item_id


async def test_api_round_trip_is_idempotent_and_enriched():
    item_id = await _seed_research()
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        first = await client.post(
            "/api/favorites",
            json={"ref_kind": "research", "ref_id": item_id, "note": "继续跟踪"},
        )
        assert first.status_code == 200
        assert first.json()["title"] == "固态电池量产进度"
        assert first.json()["status"] == "completed"

        again = await client.post(
            "/api/favorites",
            json={"ref_kind": "research", "ref_id": item_id, "note": "继续跟踪"},
        )
        assert again.status_code == 200
        assert again.json()["id"] == first.json()["id"]

        listed = await client.get("/api/favorites", params={"kind": "research"})
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        assert listed.json()[0]["ref_id"] == item_id
        assert (await db.query_one("SELECT COUNT(*) AS n FROM favorites"))["n"] == 1

        removed = await client.delete(f"/api/favorites/research/{item_id}")
        assert removed.status_code == 200
        assert removed.json() == {"removed": True}
        assert (await client.get("/api/favorites")).json() == []


async def test_unknown_ref_kind_is_400():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/favorites",
            json={"ref_kind": "not-a-real-kind", "ref_id": "x"},
        )
        assert response.status_code == 400
        assert "unknown ref_kind" in response.json()["detail"]
