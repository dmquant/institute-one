"""Optional bearer auth (app/api/auth.py, ROADMAP Phase 0).

The middleware is exercised on a self-contained probe app via install_auth()
— app/main.py's mount is a PATCH-NOTES-E6 line owned by the main agent, and
these tests must hold with or without it. Matrix:

- token unset      → zero change: everything answers with no header.
- token set        → /api/* demands ``Authorization: Bearer <token>`` (401
                     otherwise, with WWW-Authenticate); /health and non-API
                     paths stay open.
- non-loopback host with no token → one startup warning from install_auth().
"""
from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI

from app.api.auth import configured_token, install_auth
from app.config import get_settings

# dummy bearer fixture (guards nothing; auth reads INSTITUTE_TOKEN at runtime)
BEARER_FIXTURE = "dummy-test-bearer-fixture"


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/probe")
    async def probe():
        return {"hit": True}

    @app.post("/api/probe")
    async def probe_post():
        return {"hit": "post"}

    @app.get("/other")
    async def other():
        return {"open": True}

    install_auth(app)
    return app


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_probe_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture()
def no_token(monkeypatch):
    monkeypatch.delenv("INSTITUTE_TOKEN", raising=False)


@pytest.fixture()
def with_token(monkeypatch):
    monkeypatch.setenv("INSTITUTE_TOKEN", BEARER_FIXTURE)


async def test_token_unset_is_zero_change(no_token):
    assert configured_token() is None
    async with _client() as client:
        for path in ("/api/probe", "/health", "/other"):
            resp = await client.get(path)
            assert resp.status_code == 200, path
        resp = await client.post("/api/probe")
        assert resp.status_code == 200


async def test_empty_token_counts_as_unset(monkeypatch):
    monkeypatch.setenv("INSTITUTE_TOKEN", "")
    assert configured_token() is None
    async with _client() as client:
        resp = await client.get("/api/probe")
    assert resp.status_code == 200


async def test_token_set_demands_bearer_on_api(with_token):
    async with _client() as client:
        resp = await client.get("/api/probe")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        assert resp.json() == {"detail": "unauthorized"}

        resp = await client.get("/api/probe", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

        # scheme matters: a bare token without "Bearer " is rejected
        resp = await client.get("/api/probe", headers={"Authorization": BEARER_FIXTURE})
        assert resp.status_code == 401

        resp = await client.get("/api/probe", headers={"Authorization": f"Bearer {BEARER_FIXTURE}"})
        assert resp.status_code == 200
        assert resp.json() == {"hit": True}

        # every method under /api/* is enforced, not just GET
        resp = await client.post("/api/probe")
        assert resp.status_code == 401
        resp = await client.post("/api/probe", headers={"Authorization": f"Bearer {BEARER_FIXTURE}"})
        assert resp.status_code == 200


async def test_health_and_non_api_paths_stay_open(with_token):
    async with _client() as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        resp = await client.get("/other")
        assert resp.status_code == 200


def test_warning_when_bound_beyond_loopback_without_token(no_token, monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "host", "0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="institute.auth"):
        _probe_app()
    assert any("INSTITUTE_TOKEN" in r.getMessage() for r in caplog.records)


def test_no_warning_on_loopback_or_with_token(with_token, monkeypatch, caplog):
    assert get_settings().host == "127.0.0.1"
    with caplog.at_level(logging.WARNING, logger="institute.auth"):
        _probe_app()  # loopback: quiet (token set or not)
    monkeypatch.setattr(get_settings(), "host", "0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="institute.auth"):
        _probe_app()  # non-loopback but token set: quiet
    assert not [r for r in caplog.records if "INSTITUTE_TOKEN" in r.getMessage()]
