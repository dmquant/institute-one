"""Phase 1a vectors: embed→store→top-k, rebuild-by-source, and every
degradation path.

The degradation matrix under test (ollama × sqlite-vec):
- flag off (default reality)                → FTS only, zero vector tasks
- flag on, sqlite-vec missing               → FTS only, embed never called
- flag on, sqlite-vec ok, embed fails       → FTS only (Ollama down),
                                              negative-cached, stale hidden
- flag on, sqlite-vec ok, embed ok          → vector top-k + FTS merge,
                                              per-path folding, backfill
"""
from __future__ import annotations

import asyncio
import logging
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.institute import archive, sessions, vectors

# Tests exercising the real vec0 virtual table need sqlite-vec importable;
# the degradation tests must run (and pass) without it.
try:
    import sqlite_vec  # noqa: F401
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

needs_vec = pytest.mark.skipif(not HAS_SQLITE_VEC, reason="sqlite-vec not installed")

KEYWORD_DIMS = {"gpu": 0, "cpu": 1, "memory": 2, "zebra": 3}


def fake_embed_vector(text: str) -> list[float]:
    """Deterministic bag-of-keywords embedding (cosine-friendly)."""
    vec = [0.0] * vectors.EMBED_DIM
    for token in text.lower().split():
        token = token.strip(".,;:#*`\"'()[]")
        dim = KEYWORD_DIMS.get(token, 4)
        vec[dim] += 1.0
    if not any(vec):
        vec[4] = 1.0
    return vec


def _reset_vector_state(monkeypatch) -> None:
    """Neutralize module-level caches; monkeypatch restores originals after."""
    monkeypatch.setattr(vectors, "_ready_conn", None)
    monkeypatch.setattr(vectors, "_vec_unavailable", False)
    monkeypatch.setattr(vectors, "_ollama_down_until", 0.0)
    monkeypatch.setattr(vectors, "_ollama_warned", False)


@pytest.fixture
def vectors_enabled(monkeypatch):
    """Force the enable flag on and reset per-process negative caches."""
    monkeypatch.setattr(vectors, "_enabled", lambda: True)
    _reset_vector_state(monkeypatch)


@pytest.fixture
def fake_embedder(vectors_enabled, monkeypatch):
    """Deterministic embedder — no Ollama involved."""
    async def _fake_embed(text: str) -> list[float] | None:
        return fake_embed_vector(text)

    monkeypatch.setattr(vectors, "embed", _fake_embed)


async def _snapshot_md(files: dict[str, str], ref_id: str, session: dict | None = None) -> dict:
    if session is None:
        session = await sessions.create_session(f"vec test {ref_id}", kind="research")
    ws = sessions.workspace_path(session)
    for name, content in files.items():
        (ws / name).write_text(content, encoding="utf-8")
    archived = await archive.snapshot_session(session["id"], "research", ref_id)
    await archive.flush_vector_indexing()
    return {"session": session, "archived": archived}


# ---- the live path (fake embedder, real sqlite-vec) ----------------------

@needs_vec
async def test_index_and_topk_ordering(fake_embedder):
    await _snapshot_md(
        {"pure.md": "gpu gpu gpu gpu", "mixed.md": "gpu cpu cpu cpu", "other.md": "zebra zebra"},
        "r1",
    )
    rows = await db.query("SELECT path, sha256, model FROM vector_chunks ORDER BY path")
    assert {r["path"] for r in rows} == {
        "research/r1/pure.md", "research/r1/mixed.md", "research/r1/other.md",
    }
    assert all(r["sha256"] and r["model"] == "bge-m3" for r in rows)

    hits = await vectors.search("gpu", k=5)
    assert [h["path"] for h in hits[:2]] == ["research/r1/pure.md", "research/r1/mixed.md"]
    assert hits[0]["distance"] <= hits[1]["distance"]
    assert all(h["source"] == "vector" for h in hits)

    # k is honored by the underlying KNN + fold, not just by list slicing
    top1 = await vectors.search("gpu", k=1)
    assert len(top1) == 1 and top1[0]["path"] == "research/r1/pure.md"

    hybrid = await archive.search_hybrid("gpu")
    assert hybrid["mode"] == "vector+fts"
    assert hybrid["results"][0]["path"] == "research/r1/pure.md"


@needs_vec
async def test_reindex_replaces_chunks_for_path(fake_embedder):
    out = await _snapshot_md({"doc.md": "gpu gpu"}, "r2")
    session = out["session"]
    before = await db.query("SELECT id FROM vector_chunks WHERE path = ?", ("research/r2/doc.md",))
    assert before

    await _snapshot_md({"doc.md": "cpu cpu"}, "r2", session=session)

    after = await db.query("SELECT id, text FROM vector_chunks WHERE path = ?", ("research/r2/doc.md",))
    assert len(after) == 1 and after[0]["text"] == "cpu cpu"
    assert after[0]["id"] != before[0]["id"]
    # the vec rows follow: only the fresh rowid remains for this path
    old_vec = await db.query("SELECT rowid FROM vec_search WHERE rowid = ?", (before[0]["id"],))
    assert old_vec == []
    # search serves only the fresh text (KNN top-k has no relevance floor, so
    # the path may still appear for "gpu" — but never with the old content)
    for hit in await vectors.search("gpu"):
        assert "gpu" not in hit["snippet"]
    hits = await vectors.search("cpu")
    assert hits[0]["path"] == "research/r2/doc.md" and "cpu" in hits[0]["snippet"]


@needs_vec
async def test_backfill_indexes_unchanged_file_after_degraded_snapshot(monkeypatch):
    """Snapshot with vectors off, enable later, snapshot again (file
    unchanged) → the projection appears. REVIEW-A8 blocking path 1."""
    out = await _snapshot_md({"doc.md": "gpu gpu"}, "rbf")  # default: flag off
    assert await db.query("SELECT id FROM vector_chunks") == []

    monkeypatch.setattr(vectors, "_enabled", lambda: True)
    _reset_vector_state(monkeypatch)

    async def _fake_embed(text: str) -> list[float] | None:
        return fake_embed_vector(text)

    monkeypatch.setattr(vectors, "embed", _fake_embed)

    archived = await archive.snapshot_session(out["session"]["id"], "research", "rbf")
    await archive.flush_vector_indexing()
    assert archived == []  # nothing re-archived: the file did not change
    rows = await db.query("SELECT path FROM vector_chunks")
    assert {r["path"] for r in rows} == {"research/rbf/doc.md"}

    # …and the now-current projection is NOT rebuilt on the next snapshot
    async def _fail_embed(text: str) -> None:  # would wipe the index if called
        raise AssertionError("index_file must no-op on a current projection")

    monkeypatch.setattr(vectors, "embed", _fail_embed)
    await archive.snapshot_session(out["session"]["id"], "research", "rbf")
    await archive.flush_vector_indexing()
    assert len(await db.query("SELECT id FROM vector_chunks")) == len(rows)


@needs_vec
async def test_stale_vectors_hidden_then_recovered(vectors_enabled, monkeypatch):
    """Refresh fails → old vectors are hidden from search (not served stale);
    embed recovers → next snapshot rebuilds. REVIEW-A8 blocking path 2."""
    embed_ok = {"on": True}

    async def _toggle_embed(text: str) -> list[float] | None:
        return fake_embed_vector(text) if embed_ok["on"] else None

    monkeypatch.setattr(vectors, "embed", _toggle_embed)

    out = await _snapshot_md({"doc.md": "gpu gpu"}, "r5")
    assert any(h["path"] == "research/r5/doc.md" for h in await vectors.search("gpu"))

    embed_ok["on"] = False
    await _snapshot_md({"doc.md": "cpu cpu"}, "r5", session=out["session"])
    # old rows still exist (kept for retry) but must not be served
    assert await db.query_one("SELECT id FROM vector_chunks WHERE path = ?", ("research/r5/doc.md",))
    assert await vectors.search("gpu") == []
    hybrid = await archive.search_hybrid("gpu")
    assert hybrid["mode"] == "fts" and hybrid["results"] == []  # FTS has cpu now

    embed_ok["on"] = True
    await _snapshot_md({"doc.md": "cpu cpu"}, "r5", session=out["session"])  # unchanged
    hits = await vectors.search("cpu")
    assert hits and hits[0]["path"] == "research/r5/doc.md"
    # the rebuilt projection reflects the new content only (KNN top-k has no
    # relevance floor, so assert on content rather than absence of the path)
    rows = await db.query("SELECT text FROM vector_chunks WHERE path = ?", ("research/r5/doc.md",))
    assert [r["text"] for r in rows] == ["cpu cpu"]


@needs_vec
async def test_empty_file_update_purges_projection(fake_embedder):
    out = await _snapshot_md({"doc.md": "gpu gpu"}, "r6")
    assert await db.query_one("SELECT id FROM vector_chunks WHERE path = ?", ("research/r6/doc.md",))

    await _snapshot_md({"doc.md": ""}, "r6", session=out["session"])
    assert await db.query("SELECT id FROM vector_chunks WHERE path = ?", ("research/r6/doc.md",)) == []
    assert await vectors.search("gpu") == []


@needs_vec
async def test_stale_concurrent_job_cannot_overwrite_newer_snapshot(fake_embedder):
    out = await _snapshot_md({"doc.md": "gpu gpu"}, "r7")
    sha_old = (await db.query_one(
        "SELECT sha256 FROM archive_files WHERE path = ?", ("research/r7/doc.md",)
    ))["sha256"]
    await _snapshot_md({"doc.md": "cpu cpu"}, "r7", session=out["session"])

    # a slow job from the first snapshot finishes last: must be discarded
    stored = await vectors.index_file(
        "research/r7/doc.md", "research", "r7", out["session"]["id"], "gpu gpu", sha256=sha_old,
    )
    assert stored == 0
    rows = await db.query("SELECT text FROM vector_chunks WHERE path = ?", ("research/r7/doc.md",))
    assert [r["text"] for r in rows] == ["cpu cpu"]


@needs_vec
async def test_multi_chunk_document_folds_to_one_hit(fake_embedder):
    long_md = "# A\ngpu gpu gpu\n\n# B\ngpu gpu\n"  # two chunks, both match "gpu"
    await _snapshot_md({"long.md": long_md, "other.md": "gpu"}, "r8")
    n_chunks = await db.query(
        "SELECT count(*) AS n FROM vector_chunks WHERE path = ?", ("research/r8/long.md",)
    )
    assert n_chunks[0]["n"] == 2

    hits = await vectors.search("gpu", k=5)
    paths = [h["path"] for h in hits]
    assert len(paths) == len(set(paths)), f"duplicate paths in {paths}"
    assert set(paths) == {"research/r8/long.md", "research/r8/other.md"}

    hybrid = await archive.search_hybrid("gpu")
    hpaths = [r["path"] for r in hybrid["results"]]
    assert len(hpaths) == len(set(hpaths))


@needs_vec
async def test_txt_files_are_fts_only(fake_embedder):
    await _snapshot_md({"note.txt": "gpu gpu", "doc.md": "gpu"}, "r9")
    rows = await db.query("SELECT path FROM vector_chunks WHERE path LIKE ?", ("research/r9/%",))
    assert {r["path"] for r in rows} == {"research/r9/doc.md"}


@needs_vec
async def test_api_search_endpoints_vector_mode(fake_embedder):
    await _snapshot_md({"pure.md": "gpu gpu gpu", "mixed.md": "gpu cpu"}, "r10")
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/archive/search", params={"q": "gpu"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "vector+fts"
        assert body["results"][0]["path"] == "research/r10/pure.md"

        r = await client.post("/api/search", json={"query": "gpu", "k": 2})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "vector+fts"
        assert len(body["results"]) <= 2


# ---- shutdown drain --------------------------------------------------------

async def test_drain_cancels_inflight_embed_task(vectors_enabled, monkeypatch):
    """An in-flight embedding task is registered and cancelled by the A1
    shutdown drain (REVIEW-A8 blocking issue 2)."""
    from app import main as app_main

    started = asyncio.Event()

    async def _hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()  # parks forever until cancelled

    monkeypatch.setattr(vectors, "index_file", _hang)
    session = await sessions.create_session("drain", kind="research")
    (sessions.workspace_path(session) / "doc.md").write_text("gpu", encoding="utf-8")
    await archive.snapshot_session(session["id"], "research", "rdrain")
    await asyncio.wait_for(started.wait(), timeout=5)
    assert archive._bg_tasks, "embed task should be registered while in flight"
    task = next(iter(archive._bg_tasks))

    await app_main._drain_background(timeout_s=5)
    assert task.cancelled()
    assert not archive._bg_tasks


# ---- degradation paths ----------------------------------------------------

async def test_flag_off_is_pure_fts():
    """Default reality: no flag in config → zero vector tasks, pure FTS."""
    await _snapshot_md({"doc.md": "zebrafish99 marker"}, "r11")
    assert archive._bg_tasks == set()
    assert await db.query("SELECT id FROM vector_chunks") == []

    hybrid = await archive.search_hybrid("zebrafish99")
    assert hybrid["mode"] == "fts"
    assert any(r["path"] == "research/r11/doc.md" for r in hybrid["results"])
    assert all(r["source"] == "fts" for r in hybrid["results"])


@needs_vec
async def test_ollama_unreachable_degrades_to_fts(vectors_enabled, monkeypatch):
    """Flag on, sqlite-vec fine, but embed() → None (Ollama down)."""
    async def _no_embed(text: str) -> None:
        return None

    monkeypatch.setattr(vectors, "embed", _no_embed)
    await _snapshot_md({"doc.md": "zebrafish77 marker"}, "r12")
    assert await db.query("SELECT id FROM vector_chunks") == []

    hybrid = await archive.search_hybrid("zebrafish77")
    assert hybrid["mode"] == "fts"
    assert any(r["path"] == "research/r12/doc.md" for r in hybrid["results"])


async def test_real_embed_returns_none_when_ollama_down(vectors_enabled, monkeypatch):
    """The actual HTTP wrapper: connection refused → None, no raise."""
    monkeypatch.setattr(
        vectors, "get_settings",
        lambda: type("S", (), {"ollama_host": "http://127.0.0.1:9", "enable_vectors": True})(),
    )
    assert await vectors.embed("hello") is None


async def test_embed_failure_negative_cache_and_single_warning(
    vectors_enabled, monkeypatch, caplog,
):
    """After one failure: no re-probe within the TTL, exactly one WARNING."""
    calls: list[int] = []

    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            calls.append(1)
            raise vectors.httpx.ConnectError("refused")

    monkeypatch.setattr(vectors.httpx, "AsyncClient", FailingClient)
    with caplog.at_level(logging.DEBUG, logger="institute.vectors"):
        assert await vectors.embed("one") is None
        assert await vectors.embed("two") is None
        assert await vectors.embed("three") is None

    assert len(calls) == 1, "second call must hit the negative cache, not the network"
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "embed failed" in r.message]
    assert len(warnings) == 1


async def test_sqlite_vec_missing_degrades(vectors_enabled, monkeypatch):
    """Flag on but the dependency is absent → FTS; embed is NEVER reached."""
    embed_calls: list[str] = []

    async def _spy_embed(text: str) -> list[float]:
        embed_calls.append(text)
        return fake_embed_vector(text)

    monkeypatch.setattr(vectors, "embed", _spy_embed)
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)  # import raises ImportError
    assert await vectors.ensure_ready() is False
    assert vectors._vec_unavailable is True  # verdict cached for the process

    await _snapshot_md({"doc.md": "zebrafish55 marker"}, "r13")
    hybrid = await archive.search_hybrid("zebrafish55")
    assert hybrid["mode"] == "fts"
    assert any(r["path"] == "research/r13/doc.md" for r in hybrid["results"])
    assert embed_calls == [], "sqlite-vec short-circuit must precede any Ollama call"


async def test_snapshot_survives_indexing_crash(vectors_enabled, monkeypatch):
    """A blowing-up vector hook must not fail (or slow) the snapshot."""
    async def _boom(*args, **kwargs):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(vectors, "index_file", _boom)
    out = await _snapshot_md({"doc.md": "gpu content"}, "r14")
    assert out["archived"] == ["research/r14/doc.md"]
    # FTS indexing still happened
    hits = await archive.search("gpu")
    assert any(h["path"] == "research/r14/doc.md" for h in hits)


@needs_vec
async def test_search_never_raises_on_internal_failure(fake_embedder, monkeypatch):
    """vectors.search degrades to [] even when the vec query itself blows up."""
    await _snapshot_md({"doc.md": "gpu"}, "r15")

    async def _boom(*args, **kwargs):
        raise RuntimeError("vec query exploded")

    monkeypatch.setattr(vectors.db, "query", _boom)
    assert await vectors.search("gpu") == []


# ---- chunking -------------------------------------------------------------

def test_chunk_text_splits_headings_and_wraps():
    md = "# One\nalpha\n\n## Two\nbeta\n"
    chunks = vectors.chunk_text(md)
    assert len(chunks) == 2
    assert chunks[0].startswith("# One") and chunks[1].startswith("## Two")

    long_block = "x" * (vectors.CHUNK_MAX_CHARS * 2 + 10)
    wrapped = vectors.chunk_text(long_block)
    assert len(wrapped) == 3
    assert all(len(c) <= vectors.CHUNK_MAX_CHARS for c in wrapped)
    assert vectors.chunk_text("") == []
