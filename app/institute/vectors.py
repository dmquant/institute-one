"""Embeddings substrate (Phase 1a): Ollama bge-m3 → sqlite-vec, degrade-first.

Degradation contract — which on this machine is the DEFAULT reality (no
ollama binary installed; sqlite-vec is an optional dependency):

- ``embed()`` returns ``None`` when vectors are disabled, Ollama is
  unreachable/times out, or the reply is malformed. It never raises. After a
  failure it negative-caches for ``OLLAMA_RETRY_S`` so degraded searches do
  not re-probe (or re-wait the full timeout) on every request; the first
  failure logs at WARNING, repeats at DEBUG.
- ``ensure_ready()`` returns ``False`` when the sqlite-vec extension cannot
  be imported or loaded — that verdict is final for the process (probed and
  warned once); every entry point then no-ops (``index_file`` → 0,
  ``search`` → ``[]``).
- Callers (the archive snapshot hook, the search APIs) treat None/0/[] as
  "vectors unavailable" and fall back to FTS5. Nothing in this module ever
  propagates an exception to a caller.

Index consistency (rebuild-by-source):

- ``vector_chunks`` rows carry the source file's sha256 AND the embedding
  model; ``index_file`` no-ops when a projection for (path, sha, model)
  already exists, purges the projection when the source became empty, and
  re-checks ``archive_files``' CURRENT sha inside the replace transaction so
  a slow stale job can never overwrite a newer snapshot.
- ``search`` joins on current sha + model, so rows whose source has moved on
  (failed refresh, model switch) are hidden instead of served stale — and
  the missing (path, sha, model) projection is backfilled by the next
  snapshot of the source, even if the file itself did not change.

The ``vec_search`` vec0 virtual table is created HERE at runtime, not in a
migration: virtual tables need the extension loaded on the live connection,
and ``db.migrate()`` runs executescript with no extension present.
"""
from __future__ import annotations

import hashlib
import logging
import re
import struct
import time
import weakref
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any

import httpx

from .. import bus, db
from ..config import get_settings

log = logging.getLogger("institute.vectors")

EMBED_DIM = 1024          # bge-m3 output dimension
CHUNK_MAX_CHARS = 1200    # hard-wrap threshold for a single chunk
EMBED_TIMEOUT_S = 20.0
OLLAMA_RETRY_S = 60.0     # negative-cache TTL after an embed failure
HEALTH_PROBE_TTL_S = 30.0
HEALTH_PROBE_TIMEOUT_S = 2.0
EMBED_CACHE_MAX = 256
DEFAULT_MODEL = "bge-m3"

# Public reason vocabulary used by hybrid-search and health responses.
REASON_HEALTHY = "healthy"
REASON_DISABLED = "vectors_disabled"
REASON_VEC_MISSING = "vec_ext_missing"
REASON_OLLAMA_DOWN = "ollama_unreachable"
REASON_MODEL_MISSING = "model_missing"
REASON_VECTOR_ERROR = "vector_error"

# Connection the extension + virtual table were prepared on. Tests reopen the
# DB per test, so readiness is tracked per connection object (weakref: a dead
# connection must trigger a reload on its successor).
_ready_conn: weakref.ref | None = None

# Process-level negative caches (tests reset these via their fixtures):
_vec_unavailable = False   # sqlite-vec import/load failed — final for this process
_ollama_down_until = 0.0   # monotonic deadline; embed() short-circuits before it
_ollama_warned = False     # first embed failure logs WARNING, repeats DEBUG
_ollama_failure_reason: str | None = None
_ollama_down_key: tuple[str, str] | None = None

# Successful embeddings are content-addressed by (model, sha256(text)). This
# deliberately stays process-local because migration 0007 has no content_hash
# column and this card may not add a migration.
_embed_cache: OrderedDict[tuple[str, str], tuple[float, ...]] = OrderedDict()

# Lightweight /api/tags health probe cache. The key includes host + model so a
# config/model switch never inherits the previous target's verdict.
_health_probe_until = 0.0
_health_probe_key: tuple[str, str] | None = None
_health_probe_result: dict[str, Any] | None = None
_search_reason: ContextVar[str] = ContextVar(
    "vector_search_reason", default=REASON_DISABLED
)


def _enabled() -> bool:
    # Defensive read: the setting may not exist until config.py grows it
    # (see PATCH-NOTES-A8.md). Missing == disabled == degrade.
    return bool(getattr(get_settings(), "enable_vectors", False))


def _model() -> str:
    return str(getattr(get_settings(), "embed_model", "") or DEFAULT_MODEL)


def model_name() -> str:
    """Public alias: the embedding model identifier consumers may persist
    alongside vectors (e.g. whiteboard board vectors, cache fingerprints)."""
    return _model()


def last_search_reason() -> str:
    """Reason emitted by the latest vector search in this request/task."""
    return _search_reason.get()


def _target(model: str | None = None) -> tuple[str, str]:
    return str(get_settings().ollama_host).rstrip("/"), model or _model()


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


async def ensure_ready() -> bool:
    """Load sqlite-vec into the live connection and create ``vec_search``.

    Returns False — never raises — when the extension is missing or fails to
    load; that failure is cached for the rest of the process (same import,
    same wheel path: re-probing only re-logs). Safe to call repeatedly.
    """
    global _ready_conn, _vec_unavailable
    if _vec_unavailable:
        return False
    try:
        conn = db.conn()
    except RuntimeError:
        return False
    if _ready_conn is not None and _ready_conn() is conn:
        return True
    try:
        import sqlite_vec  # optional dependency — absence means degrade
    except Exception as exc:  # noqa: BLE001 - ImportError or a broken install
        log.warning("sqlite-vec unavailable — vector layer off for this process: %s", exc)
        _vec_unavailable = True
        return False
    try:
        await conn.enable_load_extension(True)
        try:
            await conn.load_extension(sqlite_vec.loadable_path())
        finally:
            await conn.enable_load_extension(False)
        await db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_search "
            f"USING vec0(embedding float[{EMBED_DIM}] distance_metric=cosine)"
        )
        _ready_conn = weakref.ref(conn)
        return True
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        log.warning("sqlite-vec load failed — vector layer off for this process: %s", exc)
        _vec_unavailable = True
        return False


def _note_embed_failure(
    detail: str, reason: str = REASON_OLLAMA_DOWN, target: tuple[str, str] | None = None,
) -> None:
    global _ollama_down_until, _ollama_warned, _ollama_failure_reason, _ollama_down_key
    _ollama_down_until = time.monotonic() + OLLAMA_RETRY_S
    _ollama_failure_reason = reason
    _ollama_down_key = target or _target()
    if _ollama_warned:
        log.debug("embed failed (negative-cached %.0fs): %s", OLLAMA_RETRY_S, detail)
    else:
        _ollama_warned = True
        log.warning("embed failed — vector layer degraded, retrying after %.0fs: %s",
                    OLLAMA_RETRY_S, detail)


def _failure_reason() -> str:
    return _ollama_failure_reason or REASON_OLLAMA_DOWN


def _cache_get(key: tuple[str, str]) -> list[float] | None:
    cached = _embed_cache.get(key)
    if cached is None:
        return None
    _embed_cache.move_to_end(key)
    return list(cached)


def _cache_put(key: tuple[str, str], vec: list[float]) -> None:
    _embed_cache[key] = tuple(vec)
    _embed_cache.move_to_end(key)
    while len(_embed_cache) > EMBED_CACHE_MAX:
        _embed_cache.popitem(last=False)


async def embed(text: str) -> list[float] | None:
    """Embed text via Ollama, reusing successful (model, content-hash) hits."""
    global _ollama_down_until, _ollama_warned
    global _ollama_failure_reason, _ollama_down_key
    if not _enabled() or not (text or "").strip():
        return None
    host, model = _target()
    key = (model, hashlib.sha256(text.encode("utf-8")).hexdigest())
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if time.monotonic() < _ollama_down_until and _ollama_down_key == (host, model):
        return None  # negative-cached: don't re-probe (or re-wait) every call
    try:
        # trust_env=False: this machine exports global SOCKS proxy env vars
        # that break httpx loopback requests (same rationale as api_hands.py).
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_S, trust_env=False) as client:
            resp = await client.post(
                f"{host}/api/embeddings", json={"model": model, "prompt": text}
            )
        if resp.status_code != 200:
            try:
                error = str(resp.json().get("error", ""))
            except Exception:  # noqa: BLE001 - error body is diagnostic only
                error = str(getattr(resp, "text", ""))
            missing = resp.status_code == 404 or (
                "model" in error.lower() and "not found" in error.lower()
            )
            reason = REASON_MODEL_MISSING if missing else REASON_OLLAMA_DOWN
            _note_embed_failure(
                f"ollama returned HTTP {resp.status_code}: {error[:160]}",
                reason,
                (host, model),
            )
            return None
        vec = resp.json().get("embedding")
        if not isinstance(vec, list) or len(vec) != EMBED_DIM:
            _note_embed_failure(
                "unexpected embedding shape from ollama",
                REASON_OLLAMA_DOWN,
                (host, model),
            )
            return None
        _ollama_down_until = 0.0
        _ollama_warned = False
        _ollama_failure_reason = None
        _ollama_down_key = None
        out = [float(x) for x in vec]
        _cache_put(key, out)
        return list(out)
    except Exception as exc:  # noqa: BLE001 - unreachable/timeout/bad JSON → degrade
        _note_embed_failure(str(exc), REASON_OLLAMA_DOWN, (host, model))
        return None


async def _probe_ollama_uncached(host: str, model: str) -> dict[str, Any]:
    """One lightweight model-list probe; callers own caching."""
    try:
        async with httpx.AsyncClient(timeout=HEALTH_PROBE_TIMEOUT_S, trust_env=False) as client:
            resp = await client.get(f"{host}/api/tags")
        if resp.status_code != 200:
            return {
                "reachable": False,
                "model_available": False,
                "reason": REASON_OLLAMA_DOWN,
            }
        payload = resp.json()
        names: set[str] = set()
        for item in payload.get("models", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            for field in ("name", "model"):
                if item.get(field):
                    names.add(str(item[field]))
        base = model.split(":", 1)[0]
        available = any(name == model or name.split(":", 1)[0] == base for name in names)
        return {
            "reachable": True,
            "model_available": available,
            "reason": REASON_HEALTHY if available else REASON_MODEL_MISSING,
        }
    except Exception:  # noqa: BLE001 - health is observational, never raises
        return {
            "reachable": False,
            "model_available": False,
            "reason": REASON_OLLAMA_DOWN,
        }


async def _probe_ollama() -> dict[str, Any]:
    """Cached Ollama/model reachability used by the health endpoint."""
    global _health_probe_until, _health_probe_key, _health_probe_result
    host, model = _target()
    target = (host, model)
    if not _enabled():
        return {
            "reachable": None,
            "model_available": None,
            "reason": REASON_DISABLED,
            "cached": False,
        }
    now = time.monotonic()
    if (
        _health_probe_key == target
        and _health_probe_result is not None
        and now < _health_probe_until
    ):
        return {**_health_probe_result, "cached": True}
    if _ollama_down_key == target and now < _ollama_down_until:
        result = {
            "reachable": False,
            "model_available": False,
            "reason": _failure_reason(),
        }
        _health_probe_key = target
        _health_probe_result = result
        _health_probe_until = min(_ollama_down_until, now + HEALTH_PROBE_TTL_S)
        return {**result, "cached": True}
    result = await _probe_ollama_uncached(host, model)
    _health_probe_key = target
    _health_probe_result = result
    _health_probe_until = now + HEALTH_PROBE_TTL_S
    return {**result, "cached": False}


async def get_health() -> dict[str, Any]:
    """Vector substrate health without an embedding call."""
    enabled = _enabled()
    extension_available = await ensure_ready()
    ollama = await _probe_ollama()
    try:
        rows = await db.query(
            "SELECT model, COUNT(*) AS chunks FROM vector_chunks GROUP BY model ORDER BY model"
        )
        chunk_counts = {str(row["model"]): int(row["chunks"]) for row in rows}
    except Exception:  # noqa: BLE001 - pre-migration/closed DB still gets a health envelope
        chunk_counts = {}
    if not enabled:
        reason = REASON_DISABLED
    elif not extension_available:
        reason = REASON_VEC_MISSING
    else:
        reason = str(ollama["reason"])
    return {
        "enabled": enabled,
        "reason": reason,
        "extension_available": extension_available,
        "ollama_reachable": ollama["reachable"],
        "model_available": ollama["model_available"],
        "probe_cached": ollama["cached"],
        "current_model": _model(),
        "chunk_counts": chunk_counts,
    }


_HEADING_SPLIT = re.compile(r"(?m)^(?=#{1,6}\s)")


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Markdown-aware chunking: split on headings, hard-wrap oversized blocks."""
    chunks: list[str] = []
    for block in _HEADING_SPLIT.split(text or ""):
        block = block.strip()
        while len(block) > max_chars:
            chunks.append(block[:max_chars])
            block = block[max_chars:].strip()
        if block:
            chunks.append(block)
    return chunks


async def _delete_path_rows(conn, path: str) -> None:
    """Remove a path's rows from both tables (must run inside a transaction)."""
    cur = await conn.execute("SELECT id FROM vector_chunks WHERE path = ?", (path,))
    old_ids = [row[0] for row in await cur.fetchall()]
    for i in range(0, len(old_ids), 500):
        batch = old_ids[i : i + 500]
        marks = ",".join("?" * len(batch))
        await conn.execute(f"DELETE FROM vec_search WHERE rowid IN ({marks})", batch)
    await conn.execute("DELETE FROM vector_chunks WHERE path = ?", (path,))


async def gc_stale_models(keep_model: str) -> dict[str, Any]:
    """Explicitly delete chunks and vec0 projections not using ``keep_model``."""
    keep_model = (keep_model or "").strip()
    if not keep_model:
        raise ValueError("keep_model must not be empty")
    if not await ensure_ready():
        return {
            "keep_model": keep_model,
            "deleted_chunks": 0,
            "deleted_projections": 0,
            "reason": REASON_VEC_MISSING,
        }
    try:
        async with db.transaction() as conn:
            cur = await conn.execute(
                "SELECT id FROM vector_chunks WHERE model != ? ORDER BY id", (keep_model,)
            )
            stale_ids = [int(row[0]) for row in await cur.fetchall()]
            await cur.close()
            deleted_projections = 0
            for i in range(0, len(stale_ids), 500):
                batch = stale_ids[i : i + 500]
                marks = ",".join("?" * len(batch))
                cur = await conn.execute(
                    f"DELETE FROM vec_search WHERE rowid IN ({marks})", batch
                )
                if cur.rowcount and cur.rowcount > 0:
                    deleted_projections += cur.rowcount
                await cur.close()
            cur = await conn.execute(
                "DELETE FROM vector_chunks WHERE model != ?", (keep_model,)
            )
            deleted_chunks = max(cur.rowcount, 0)
            await cur.close()
        return {
            "keep_model": keep_model,
            "deleted_chunks": deleted_chunks,
            "deleted_projections": deleted_projections,
            "reason": REASON_HEALTHY,
        }
    except Exception as exc:  # noqa: BLE001 - maintenance endpoint gets a stable envelope
        log.warning("stale-model vector GC failed: %s", exc)
        return {
            "keep_model": keep_model,
            "deleted_chunks": 0,
            "deleted_projections": 0,
            "reason": REASON_VECTOR_ERROR,
        }


async def index_file(
    path: str,
    ref_kind: str,
    ref_id: str,
    session_id: str | None,
    text: str,
    sha256: str | None = None,
) -> int:
    """Project one archived file into the vector index, idempotently.

    Rebuild-by-source semantics (all failure modes covered):
    - no-op when a projection for (path, sha256, model) already exists —
      safe to call on every snapshot, which is what backfills after a
      first-run degradation or a model switch;
    - empty source text purges any previous projection for the path;
    - embed failure keeps the old rows (they are hidden at query time by the
      current-sha join in ``search``) so a later call retries;
    - the replace transaction re-checks ``archive_files``' current sha, so a
      slower stale job never overwrites a newer snapshot's projection.

    Returns chunks stored; 0 = degraded / already current / superseded.
    Never raises.
    """
    try:
        # _enabled() first: the default path (flag unset) must cost nothing.
        if not _enabled() or not await ensure_ready():
            return 0
        model = _model()
        chunks = chunk_text(text)
        if not chunks:
            stale = await db.query_one(
                "SELECT id FROM vector_chunks WHERE path = ? LIMIT 1", (path,)
            )
            if stale:
                async with db.transaction() as conn:
                    await _delete_path_rows(conn, path)
            return 0
        current = await db.query_one(
            "SELECT id FROM vector_chunks WHERE path = ? AND sha256 IS ? AND model = ? LIMIT 1",
            (path, sha256, model),
        )
        if current:
            return 0  # replace txn is atomic: one matching row == complete projection
        vecs: list[list[float]] = []
        for chunk in chunks:
            vec = await embed(chunk)
            if vec is None:
                return 0
            vecs.append(vec)

        now = bus.now_iso()
        async with db.transaction() as conn:
            # Concurrency guard: embedding happened outside the lock, so only
            # commit if the archive still holds the sha this job embedded.
            cur = await conn.execute(
                "SELECT sha256 FROM archive_files WHERE path = ?", (path,)
            )
            row = await cur.fetchone()
            if row is None or (sha256 is not None and row[0] != sha256):
                return 0  # superseded by a newer snapshot; empty txn is harmless
            await _delete_path_rows(conn, path)
            for idx, (chunk, vec) in enumerate(zip(chunks, vecs)):
                cur = await conn.execute(
                    "INSERT INTO vector_chunks "
                    "(path, ref_kind, ref_id, session_id, chunk_index, text, sha256, model, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (path, ref_kind, str(ref_id), session_id, idx, chunk, sha256, model, now),
                )
                await conn.execute(
                    "INSERT INTO vec_search (rowid, embedding) VALUES (?,?)",
                    (cur.lastrowid, _pack(vec)),
                )
        return len(chunks)
    except Exception as exc:  # noqa: BLE001 - indexing must never break callers
        log.warning("vector index failed for %s: %s", path, exc)
        return 0


def _snippet(text: str, max_chars: int = 200) -> str:
    flat = " ".join((text or "").split())
    return flat[:max_chars] + ("…" if len(flat) > max_chars else "")


async def search_with_status(query: str, k: int = 10) -> dict[str, Any]:
    """Cosine top-k plus a machine-readable health reason.

    Stale rows are hidden by joining on the archive's CURRENT sha and the
    current model; chunk rows are folded to the best (nearest) chunk per
    path, oversampling the KNN so folding can still fill k distinct paths.
    A healthy empty result is therefore distinguishable from degradation.
    """
    try:
        if not _enabled():
            return {"reason": REASON_DISABLED, "results": []}
        if not await ensure_ready():
            return {"reason": REASON_VEC_MISSING, "results": []}
        if not (query or "").strip():
            return {"reason": REASON_HEALTHY, "results": []}
        qvec = await embed(query)
        if qvec is None:
            return {"reason": _failure_reason(), "results": []}
        k = min(max(k, 1), 50)
        fetch = min(k * 4, 200)
        rows = await db.query(
            "SELECT c.path, c.ref_kind, c.ref_id, c.session_id, c.chunk_index, c.text, v.distance "
            "FROM (SELECT rowid, distance FROM vec_search WHERE embedding MATCH ? AND k = ?) v "
            "JOIN vector_chunks c ON c.id = v.rowid "
            "JOIN archive_files a ON a.path = c.path AND a.sha256 = c.sha256 "
            "WHERE c.model = ? ORDER BY v.distance",
            (_pack(qvec), fetch, _model()),
        )
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows:
            if r["path"] in seen:
                continue
            seen.add(r["path"])
            out.append(
                {
                    "path": r["path"],
                    "ref_kind": r["ref_kind"],
                    "ref_id": r["ref_id"],
                    "session_id": r["session_id"],
                    "chunk_index": r["chunk_index"],
                    "snippet": _snippet(r["text"]),
                    "distance": round(r["distance"], 4),
                    "source": "vector",
                }
            )
            if len(out) >= k:
                break
        return {"reason": REASON_HEALTHY, "results": out}
    except Exception as exc:  # noqa: BLE001 - search must never break callers
        log.warning("vector search failed for %r: %s", query, exc)
        return {"reason": REASON_VECTOR_ERROR, "results": []}


async def search(query: str, k: int = 10) -> list[dict[str, Any]]:
    """Backward-compatible rows-only facade over :func:`search_with_status`."""
    status = await search_with_status(query, k=k)
    _search_reason.set(status["reason"])
    return status["results"]
