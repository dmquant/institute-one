"""API-route smoke over the WHOLE mounted surface (ROADMAP Phase 8).

The route list is enumerated programmatically from ``create_app().routes`` —
a router added to app/main.py automatically enters this smoke, and a mutating
route that fits no rule fails the classification test until it is explicitly
accounted for. Rules per (method, path):

- GET without path params      -> real request; must not 5xx (core list/detail
                                  endpoints additionally assert 200 + shape)
- GET with path params         -> request with fake id 999999; must not 5xx
- mutating with a REQUIRED body-> send {} and assert 422 (the validation face;
                                  pydantic rejects before any handler runs, so
                                  nothing executes and no model is burned)
- mutating with path params    -> fake id 999999; "look up, then act" endpoints
                                  4xx before any side effect
- mutating, no params, body
  optional/absent              -> must be listed in SAFE_EMPTY_BODY (empty-db
                                  no-op, called for real) or EXEMPT (reason)

Everything runs on the empty per-test database with the echo hand — zero quota.
"""
from __future__ import annotations

import re

from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

# ---- explicit tables --------------------------------------------------------

# (method, path) -> reason. These are NOT smoked here.
EXEMPT: dict[tuple[str, str], str] = {
    ("GET", "/api/events/stream"): "SSE long-poll — the request never completes",
    ("POST", "/api/mcp"): "JSON-RPC envelope; covered end-to-end by test_mcp_roundtrip",
    ("POST", "/api/analysts/daily/run-now"): "spawns the WHOLE roster's daily runs in background",
    ("POST", "/api/workflows/daily/briefing/run-now"): "runs a real (echo) workflow; covered by test_daily",
    ("POST", "/api/workflows/daily/daily/run-now"): "runs a real (echo) workflow; covered by test_daily",
}

# mutating routes with no path params whose empty/default body is a safe
# empty-database no-op: called for real, must answer 2xx.
SAFE_EMPTY_BODY: dict[tuple[str, str], str] = {
    ("POST", "/api/operator/observe"): "empty db -> zero-count observation snapshot, no model call",
    ("POST", "/api/operator/proposals/generate"): "no observations -> proposes nothing (deterministic)",
    ("POST", "/api/operator/effects/measure"): "no due effects -> no-op",
    ("POST", "/api/whiteboard/tick"): "no boards -> no-op",
    ("POST", "/api/whiteboard/kickoff"): "empty topic pool -> no board, no model call",
    ("POST", "/api/mailbox/sweep"): "no pending dispatches -> no-op",
    ("POST", "/api/research/tick"): "empty queue -> no-op",
    ("POST", "/api/vault/doctor"): "read-only ledger-vs-disk audit",
    ("POST", "/api/research/seed-from-theses"): "no theses -> matched 0, enqueues nothing",
    ("POST", "/api/roadmap/import"): "idempotent seed import into the throwaway test db",
    ("PUT", "/api/whiteboard/similarity-config"): "partial update; {} changes nothing",
    ("POST", "/api/chain/reproject"): "empty vault_index -> zero notes walked, no-op",
}

# GET endpoints (no path params) whose empty-db answer must be 200 + a list
LIST_200 = {
    "/api/tasks", "/api/analysts", "/api/sessions", "/api/workflows",
    "/api/workflows/runs/recent", "/api/whiteboard/boards", "/api/whiteboard/topics",
    "/api/whiteboard/category-weights", "/api/mailbox/threads", "/api/research/queue",
    "/api/research/log", "/api/research/trees", "/api/projects",
    "/api/events", "/api/factcheck/cards", "/api/forecasts",
    "/api/chain/nodes", "/api/chain/candidates", "/api/book/positions", "/api/book/nav",
    "/api/hands", "/api/hands/weights", "/api/vault/index", "/api/archive/files",
    "/api/theses", "/api/roadmap/cards", "/api/roadmap/sessions", "/api/roadmap/decisions",
    "/api/roadmap/release-gates", "/api/market/benchmarks", "/api/market/suspensions",
}

# GET endpoints (no path params) whose empty-db answer must be 200 + an object
DICT_200 = {
    "/health", "/api/meta", "/api/admin/state", "/api/cron/health", "/api/contract",
    "/api/tasks/queue", "/api/operator/actions", "/api/operator/triage",
    "/api/vault/status", "/api/whiteboard/similarity-config", "/api/roadmap/process",
    "/api/roadmap/export", "/api/mcp/health", "/api/hands/scorecard", "/api/hands/stats",
    "/api/analysts/roles", "/api/forecasts/stats",
}

_FAKE_ID = "999999"
_PARAM_RE = re.compile(r"\{[^}]+\}")

# GETs may legitimately answer 4xx here (required query params -> 422; fake
# ids -> 404/400) but must never 5xx; 202 covers background-kicker responses.
_GET_OK = {200, 202, 400, 404, 422}
# fake-id mutations: 4xx, or a 200 no-op envelope ("cancelled": false style)
_MUTATE_FAKE_OK = {200, 400, 404, 409, 422}


def _api_routes() -> list[APIRoute]:
    from app.main import create_app

    app = create_app()
    out: list[APIRoute] = []
    stack = list(app.routes)
    while stack:
        r = stack.pop()
        if isinstance(r, APIRoute):
            if r.include_in_schema:
                out.append(r)
        elif hasattr(r, "original_router"):
            # fastapi >= 0.139 mounts include_router() lazily as _IncludedRouter;
            # the wrapped APIRouter carries the real APIRoute objects
            stack.extend(r.original_router.routes)
    return out


def _keys(route: APIRoute) -> list[tuple[str, str]]:
    return [(m, route.path) for m in sorted(route.methods - {"HEAD", "OPTIONS"})]


def _empty_body_would_422(route: APIRoute) -> bool:
    """True when sending ``{}`` MUST fail pydantic validation (a required body
    model with at least one required field). A required body whose fields all
    carry defaults accepts {} and would EXECUTE — those routes go through
    SAFE_EMPTY_BODY / EXEMPT instead."""
    import pydantic

    field = getattr(route, "body_field", None)
    if field is None:
        return False
    info = field.field_info
    if not info.is_required():
        return False  # optional body (e.g. `Body | None = None`)
    model = getattr(field, "type_", None) or getattr(info, "annotation", None)
    if isinstance(model, type) and issubclass(model, pydantic.BaseModel):
        return any(f.is_required() for f in model.model_fields.values())
    return True


def _fill(path: str) -> str:
    return _PARAM_RE.sub(_FAKE_ID, path)


def _client() -> AsyncClient:
    from app.main import create_app

    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


# ---- (a) classification is total --------------------------------------------

def test_every_route_is_classified_and_tables_have_no_dead_entries():
    keys = {k for route in _api_routes() for k in _keys(route)}

    dead = (set(EXEMPT) | set(SAFE_EMPTY_BODY)) - keys
    assert not dead, f"stale table entries for routes that no longer exist: {sorted(dead)}"
    dead_gets = (LIST_200 | DICT_200) - {p for m, p in keys if m == "GET"}
    assert not dead_gets, f"stale shape-whitelist entries: {sorted(dead_gets)}"

    unclassified = []
    for route in _api_routes():
        for method, path in _keys(route):
            if method == "GET" or (method, path) in EXEMPT or (method, path) in SAFE_EMPTY_BODY:
                continue
            if _empty_body_would_422(route) or "{" in path:
                continue
            unclassified.append((method, path))
    assert not unclassified, (
        f"mutating routes with no required body and no path params: {sorted(unclassified)} — "
        "add them to SAFE_EMPTY_BODY (empty-db no-op) or EXEMPT (with a reason)"
    )


# ---- (b) GET smoke ------------------------------------------------------------

async def test_get_routes_never_500_and_core_lists_have_shape():
    seen_get = 0
    async with _client() as client:
        for route in _api_routes():
            for method, path in _keys(route):
                if method != "GET" or (method, path) in EXEMPT:
                    continue
                seen_get += 1
                r = await client.get(_fill(path))
                assert r.status_code in _GET_OK, (
                    f"GET {path} -> {r.status_code}: {r.text[:300]}"
                )
                if "{" in path:
                    continue
                if path in LIST_200:
                    assert r.status_code == 200, f"GET {path} -> {r.status_code}: {r.text[:300]}"
                    assert isinstance(r.json(), list), f"GET {path}: expected a JSON list"
                elif path in DICT_200:
                    assert r.status_code == 200, f"GET {path} -> {r.status_code}: {r.text[:300]}"
                    assert isinstance(r.json(), dict), f"GET {path}: expected a JSON object"
                elif path.startswith("/api/institute/"):
                    # curl-back digests: markdown documents, placeholders included
                    assert r.status_code == 200, f"GET {path} -> {r.status_code}"
                    assert "markdown" in r.headers.get("content-type", "")
    assert seen_get >= 60, f"route enumeration looks broken: only {seen_get} GETs seen"


# ---- (c) mutating smoke: the 422 validation face + fake-id 4xx ------------------

async def test_mutating_routes_validation_and_fake_id_surface():
    checked_422 = 0
    async with _client() as client:
        for route in _api_routes():
            for method, path in _keys(route):
                if method == "GET" or (method, path) in EXEMPT or (method, path) in SAFE_EMPTY_BODY:
                    continue
                if _empty_body_would_422(route):
                    r = await client.request(method, _fill(path), json={})
                    assert r.status_code == 422, (
                        f"{method} {path} with empty body -> {r.status_code} "
                        f"(expected 422): {r.text[:300]}"
                    )
                    checked_422 += 1
                else:  # path-parameterized, body optional: unknown id must 4xx before acting
                    r = await client.request(method, _fill(path), json={})
                    assert r.status_code in _MUTATE_FAKE_OK, (
                        f"{method} {_fill(path)} -> {r.status_code}: {r.text[:300]}"
                    )
    assert checked_422 >= 20, f"validation face looks under-enumerated: {checked_422} routes"


async def test_safe_empty_body_kickers_answer_2xx_on_empty_db():
    async with _client() as client:
        for (method, path), reason in sorted(SAFE_EMPTY_BODY.items()):
            r = await client.request(method, path, json={})
            assert 200 <= r.status_code < 300, (
                f"{method} {path} ({reason}) -> {r.status_code}: {r.text[:300]}"
            )


# ---- (d) a couple of seeded detail round-trips (list -> detail coherence) -------

async def test_detail_endpoints_follow_created_rows():
    """One representative create -> list -> detail chain per cheap domain, so the
    smoke also proves the happy 200 path of parameterized GETs (fake-id runs
    above only prove the 404 face)."""
    from app.institute import sessions as sessions_mod
    from app.institute import whiteboard as whiteboard_mod

    session = await sessions_mod.create_session("路由冒烟", kind="chat")
    topic = await whiteboard_mod.add_topic("路由冒烟话题", "形状如何", source="test")

    async with _client() as client:
        r = await client.get(f"/api/sessions/{session['id']}")
        assert r.status_code == 200
        assert r.json()["id"] == session["id"]

        r = await client.get(f"/api/sessions/{session['id']}/messages")
        assert r.status_code == 200
        assert r.json() == []

        r = await client.get("/api/whiteboard/topics")
        assert r.status_code == 200
        assert topic["id"] in {t["id"] for t in r.json()}

        r = await client.get("/api/tasks", params={"status": "completed"})
        assert r.status_code == 200
        assert r.json() == []

        # unknown-status filters answer as validation, never 500
        r = await client.get("/api/book/positions", params={"status": "bogus"})
        assert r.status_code == 400

        r = await client.get("/api/events", params={"types": "task.,research."})
        assert r.status_code == 200


async def test_workflow_run_missing_declared_variable_is_400():
    """A manual research trigger without TOPIC gets a clear 400 instead of a
    run that would feed the literal "${TOPIC}" placeholder to the model."""
    from app.institute import workflows as workflows_mod

    await workflows_mod.reconcile_from_disk()
    async with _client() as client:
        r = await client.post("/api/workflows/research/run", json={})
        assert r.status_code == 400
        assert "TOPIC" in r.json()["detail"]

        r = await client.post("/api/workflows/research/run", json={"variables": {"TOPIC": " "}})
        assert r.status_code == 400

        r = await client.post(
            "/api/workflows/research/run", json={"variables": {"TOPIC": "路由冒烟"}}
        )
        assert r.status_code == 200
    await workflows_mod.cancel_run(r.json()["run_id"])  # leave no driver running


# ---- guard: the enumeration itself stays meaningful ------------------------------

def test_enumeration_sees_all_mounted_routers():
    """If create_app() ever stops mounting a family this smoke silently loses
    coverage — pin the prefixes that must exist."""
    paths = {r.path for r in _api_routes()}
    for prefix in (
        "/api/tasks", "/api/analysts", "/api/sessions", "/api/workflows",
        "/api/whiteboard", "/api/mailbox", "/api/research", "/api/roadmap",
        "/api/theses", "/api/forecasts", "/api/chain", "/api/book",
        "/api/factcheck", "/api/operator", "/api/multi-agent", "/api/archive",
        "/api/vault", "/api/contract", "/api/mcp", "/api/institute",
        "/api/hands", "/api/events", "/api/projects", "/api/research/tree",
    ):
        assert any(p == prefix or p.startswith(prefix + "/") for p in paths), (
            f"no mounted route under {prefix} — router unmounted?"
        )
