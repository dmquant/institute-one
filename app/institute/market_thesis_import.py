"""market-thesis-data bundle importer (card M1-003).

Bootstraps the local thesis registry from a ``researchos.market_thesis_export``
bundle directory (default: ``market-thesis-data/`` at the repo root — a local,
intentionally untracked dataset):

- ``manifest.json`` — schema + counts (validated when present, warn-only);
- ``bundle.json`` — lanes, theses, stocks, thesis-stock edges.

Mapping: lanes -> ``theses`` rows (kind='lane'); theses -> rows under their
lane; stocks -> ``securities`` (+ aliases for the bundle id, zh name, and
unsuffixed ticker); thesis-stock edges -> ``thesis_security_edges``. Practical
metadata is preserved verbatim in ``meta_json``. Direction defaults to
``conflicting`` (imported theses are hypotheses to validate, not conclusions).

Contract (roadmap/07-market-thesis-data-kickoff.md): dry-run by default,
apply writes everything in ONE transaction with deterministic ids so re-runs
are idempotent; every batch lands in ``market_thesis_import_batches`` and every
applied item in ``market_thesis_import_items``. Field access tolerates both
camelCase and snake_case (the export format is external to this repo); errors
are reserved for structural problems, per-item issues become warnings + skips.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from .securities import SecurityError, canonical_id, normalize_market
from .theses import DIRECTIONS, _SLUG_RE

SCHEMA_PREFIX = "researchos.market_thesis_export"
MAX_WARNINGS = 200
# appended to `role` to park a moving edge row so its unique key frees up
# mid-import (\x1f cannot appear in real roles); always resolved before commit
_DETACH_MARK = "\x1fmoving"


class BundleError(ValueError):
    """Structural import failure (the API maps this to 400)."""


class _DryRunRollback(Exception):
    """Internal: unwinds the dry-run transaction so nothing persists."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _det_id(*parts: str) -> str:
    """Deterministic 12-hex id so re-imports update instead of duplicating."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def _get(d: dict[str, Any], *names: str, default: Any = None) -> Any:
    """First present key among camelCase/snake_case candidates."""
    for name in names:
        if name in d:
            return d[name]
    return default


def _slugify(text: str, fallback: str) -> str:
    out = []
    for ch in str(text or "").strip().lower():
        if ch.isascii() and (ch.isalnum()):
            out.append(ch)
        elif ch in (" ", "-", "_", "/", "."):
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug[:60].strip("-")
    if len(slug) < 2 or not _SLUG_RE.match(slug):
        return fallback
    return slug


def default_bundle_dir() -> Path:
    return get_settings().repo_root / "market-thesis-data"


# ---- load + structural validation -------------------------------------------

def _load_bundle(src: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Returns (bundle, manifest, warnings). Raises BundleError on structure."""
    warnings: list[str] = []
    bundle_path = src / "bundle.json"
    if not bundle_path.is_file():
        raise BundleError(f"bundle file not found: {bundle_path}")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BundleError(f"bundle.json is not valid JSON: {exc}") from None
    if not isinstance(bundle, dict):
        raise BundleError("bundle.json must be a JSON object")

    manifest: dict[str, Any] = {}
    manifest_path = src / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise BundleError(f"manifest.json is not valid JSON: {exc}") from None
        schema = str(_get(manifest, "schema", default="") or "")
        if not schema.startswith(SCHEMA_PREFIX):
            raise BundleError(
                f"manifest schema {schema!r} does not match expected prefix {SCHEMA_PREFIX!r}"
            )
    else:
        warnings.append("manifest.json missing; skipping schema/count validation")
    return bundle, manifest, warnings


def _sections(bundle: dict[str, Any]) -> tuple[list, list, list, list]:
    lanes = _get(bundle, "lanes", default=None)
    theses_raw = _get(bundle, "theses", default=None)
    stocks = _get(bundle, "stocks", "securities", default=None)
    edges = _get(bundle, "thesisStockEdges", "thesis_stock_edges", "thesisSecurityEdges", default=None)
    missing = [
        label for label, val in
        (("lanes", lanes), ("theses", theses_raw), ("stocks", stocks), ("thesisStockEdges", edges))
        if not isinstance(val, list)
    ]
    if missing:
        raise BundleError(
            f"bundle.json is missing list sections: {', '.join(missing)} "
            f"(top-level keys found: {', '.join(sorted(bundle))})"
        )
    return lanes, theses_raw, stocks, edges


def _check_counts(manifest: dict[str, Any], plan: dict[str, int], warnings: list[str]) -> None:
    counts = _get(manifest, "counts", default=None)
    if not isinstance(counts, dict):
        return
    for key, names in (
        ("lanes", ("lanes",)),
        ("theses", ("theses",)),
        ("stocks", ("stocks", "securities")),
        ("edges", ("thesisStockEdges", "thesis_stock_edges", "thesisSecurityEdges")),
    ):
        declared = _get(counts, *names, default=None)
        if isinstance(declared, int) and declared != plan[key]:
            warnings.append(f"manifest declares {declared} {key} but bundle has {plan[key]}")


# ---- per-item normalization ----------------------------------------------------

def _warn(warnings: list[str], msg: str) -> None:
    if len(warnings) < MAX_WARNINGS:
        warnings.append(msg)
    elif len(warnings) == MAX_WARNINGS:
        warnings.append(f"… further warnings suppressed (cap {MAX_WARNINGS})")


def _dedupe_plans(
    plans: list[dict[str, Any]], label: str, warnings: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split plans into (winners, losers) when several target the same row id.

    The first source wins the payload. Without this, bundle entries collapsing
    onto one row would rewrite it in bundle order on EVERY re-import (updated
    forever, order-dependent payload — the opposite of idempotent).
    """
    winner_by_id: dict[str, str] = {}
    winners: list[dict[str, Any]] = []
    losers: list[dict[str, Any]] = []
    for p in plans:
        winner = winner_by_id.get(p["id"])
        if winner is None:
            winner_by_id[p["id"]] = p["source_id"]
            winners.append(p)
            continue
        _warn(warnings, f"{label} {p['source_id']}: duplicate of {winner} (target {p['id']}); values skipped")
        losers.append(dict(p, duplicate_of=winner))
    return winners, losers


def _plan_lane(raw: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    src_id = str(_get(raw, "id", "laneId", "lane_id", default="") or "").strip()
    title = str(_get(raw, "name", "title", default="") or "").strip()
    if not src_id or not title:
        _warn(warnings, f"lane skipped (needs id and name): {json.dumps(raw, ensure_ascii=False)[:120]}")
        return None
    local_id = _det_id("mti", "lane", src_id)
    return {
        "source_id": src_id,
        "id": local_id,
        "slug": _slugify(title, f"lane-{local_id[:8]}"),
        "title": title,
        "view": str(_get(raw, "description", "summary", "view", default="") or ""),
        "meta": {"bundle_id": src_id, **({"raw": raw.get("meta")} if raw.get("meta") else {})},
    }


def _plan_thesis(
    raw: dict[str, Any], lane_ids: dict[str, str], warnings: list[str]
) -> dict[str, Any] | None:
    src_id = str(_get(raw, "id", "thesisId", "thesis_id", default="") or "").strip()
    title = str(_get(raw, "title", "name", "claim", default="") or "").strip()
    if not src_id or not title:
        _warn(warnings, f"thesis skipped (needs id and title): {json.dumps(raw, ensure_ascii=False)[:120]}")
        return None
    direction = str(_get(raw, "direction", default="conflicting") or "conflicting").strip().lower()
    if direction not in DIRECTIONS:
        _warn(warnings, f"thesis {src_id}: unknown direction {direction!r} -> conflicting")
        direction = "conflicting"
    lane_src = str(_get(raw, "laneId", "lane_id", "lane", default="") or "").strip()
    parent_id = lane_ids.get(lane_src)
    if lane_src and parent_id is None:
        _warn(warnings, f"thesis {src_id}: unknown lane {lane_src!r} -> imported top-level")
    meta: dict[str, Any] = {"bundle_id": src_id}
    practical = _get(raw, "practical", default=None)
    if practical is not None:
        meta["practical"] = practical
    for extra in ("tags", "conviction", "horizon", "updatedAt", "updated_at"):
        if raw.get(extra) is not None:
            meta.setdefault("extra", {})[extra] = raw[extra]
    local_id = _det_id("mti", "thesis", src_id)
    return {
        "source_id": src_id,
        "id": local_id,
        "slug": _slugify(title, f"thesis-{local_id[:8]}"),
        "parent_id": parent_id,
        "title": title,
        "view": str(_get(raw, "view", "statement", "summary", "description", default="") or ""),
        "direction": direction,
        "meta": meta,
    }


def _plan_stock(raw: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    src_id = str(_get(raw, "id", "stockId", "stock_id", default="") or "").strip()
    ticker = str(_get(raw, "ticker", "symbol", "code", default="") or "").strip()
    market_label = str(_get(raw, "market", "exchange", default="") or "").strip()
    if not src_id or not ticker or not market_label:
        _warn(warnings, f"stock skipped (needs id, ticker, market): {json.dumps(raw, ensure_ascii=False)[:120]}")
        return None
    try:
        market, instrument_type = normalize_market(market_label)
        sec_id = canonical_id(ticker, market)
    except SecurityError as exc:
        _warn(warnings, f"stock {src_id} skipped: {exc}")
        return None
    name = str(_get(raw, "name", "nameZh", "name_zh", default="") or "").strip()
    name_en = str(_get(raw, "nameEn", "name_en", default="") or "").strip()
    meta: dict[str, Any] = {"bundle_id": src_id, "bundle_market": market_label}
    for extra in ("sector", "industry", "theme", "notes"):
        if raw.get(extra) is not None:
            meta[extra] = raw[extra]
    return {
        "source_id": src_id,
        "id": sec_id,
        "ticker": ticker.upper(),
        "market": market,
        "instrument_type": instrument_type,
        "name": name,
        "name_en": name_en,
        "meta": meta,
    }


def _plan_edge(
    raw: dict[str, Any],
    thesis_ids: dict[str, str],
    stock_ids: dict[str, str],
    warnings: list[str],
) -> dict[str, Any] | None:
    thesis_src = str(_get(raw, "thesisId", "thesis_id", "thesis", default="") or "").strip()
    stock_src = str(_get(raw, "stockId", "stock_id", "stock", "securityId", default="") or "").strip()
    thesis_id = thesis_ids.get(thesis_src)
    security_id = stock_ids.get(stock_src)
    if thesis_id is None or security_id is None:
        _warn(warnings, f"edge skipped (unknown refs thesis={thesis_src!r} stock={stock_src!r})")
        return None
    role = str(_get(raw, "role", "relation", default="exposure") or "exposure").strip() or "exposure"
    role = role.replace("\x1f", " ")  # \x1f is reserved for the in-flight detach mark
    confidence = _get(raw, "confidence", "score", default=None)
    if confidence is not None:
        try:
            confidence = min(max(float(confidence), 0.0), 1.0)
        except (TypeError, ValueError):
            _warn(warnings, f"edge {thesis_src}->{stock_src}: bad confidence {confidence!r} -> dropped")
            confidence = None
    meta = {k: raw[k] for k in ("weight", "updatedAt", "updated_at") if raw.get(k) is not None}
    return {
        "source_id": f"{thesis_src}->{stock_src}",
        "id": _det_id("mti", "edge", thesis_src, stock_src, role),
        "thesis_id": thesis_id,
        "security_id": security_id,
        "role": role,
        "exposure": str(_get(raw, "exposure", "direction", default="") or ""),
        "confidence": confidence,
        "rationale": str(_get(raw, "rationale", "reason", "note", default="") or ""),
        "meta": meta,
    }


# ---- import ----------------------------------------------------------------------

async def import_bundle(path: str | Path | None = None, *, apply: bool = False) -> dict[str, Any]:
    """Validate (and optionally apply) a market-thesis-data bundle.

    Dry-run (default) writes only a batch provenance row. Apply upserts all
    rows in one transaction — deterministic ids keyed on bundle ids make the
    import idempotent: unchanged rows are counted ``unchanged`` and untouched.
    """
    src = Path(path) if path else default_bundle_dir()
    if not src.is_absolute():
        src = get_settings().repo_root / src
    bundle, manifest, warnings = _load_bundle(src)
    lanes_raw, theses_raw, stocks_raw, edges_raw = _sections(bundle)

    # plan: normalize every item, collect id maps (BEFORE dedupe so loser
    # source ids still resolve to the winning row), then split off duplicates
    lanes = [p for p in (_plan_lane(r, warnings) for r in lanes_raw if isinstance(r, dict)) if p]
    lane_ids = {p["source_id"]: p["id"] for p in lanes}
    theses_plan = [
        p for p in (_plan_thesis(r, lane_ids, warnings) for r in theses_raw if isinstance(r, dict)) if p
    ]
    thesis_ids = {p["source_id"]: p["id"] for p in theses_plan}
    stocks = [p for p in (_plan_stock(r, warnings) for r in stocks_raw if isinstance(r, dict)) if p]
    stock_ids = {p["source_id"]: p["id"] for p in stocks}
    edges = [
        p for p in (_plan_edge(r, thesis_ids, stock_ids, warnings) for r in edges_raw if isinstance(r, dict)) if p
    ]
    plan_counts = {
        "lanes": len(lanes_raw), "theses": len(theses_raw),
        "stocks": len(stocks_raw), "edges": len(edges_raw),
    }
    _check_counts(manifest, plan_counts, warnings)

    # first source wins a contested row id — duplicate lanes/theses/edges are
    # dropped as skipped; duplicate stocks still run (after the winners) for
    # stale-canonical migration + bundle_id alias only
    lanes, _ = _dedupe_plans(lanes, "lane", warnings)
    theses_plan, _ = _dedupe_plans(theses_plan, "thesis", warnings)
    stocks, stock_dupes = _dedupe_plans(stocks, "stock", warnings)
    stocks = stocks + stock_dupes
    edges, _ = _dedupe_plans(edges, "edge", warnings)

    counts: dict[str, dict[str, int]] = {
        kind: {"created": 0, "updated": 0, "unchanged": 0, "skipped": skipped}
        for kind, skipped in (
            ("lane", len(lanes_raw) - len(lanes)),
            ("thesis", len(theses_raw) - len(theses_plan)),
            ("stock", len(stocks_raw) - len(stocks)),  # stock dupes count at runtime
            ("edge", len(edges_raw) - len(edges)),
        )
    }

    batch_id = _new_id()
    now = bus.now_iso()
    items: list[tuple[str, str, str, str, str]] = []  # (item_type, source_id, target_id, action, detail)

    # one code path for both modes: dry-run runs the same upserts against the
    # live rows for accurate created/updated/unchanged counts, then rolls back
    try:
        async with db.transaction() as conn:
            # batch row goes first: import_items rows reference it (FK)
            await conn.execute(
                "INSERT INTO market_thesis_import_batches (id, source, mode, status, counts_json, "
                "warnings_json, manifest_json, created_at, finished_at) VALUES (?,?,?,?,'{}','[]',?,?,NULL)",
                (batch_id, str(src), "apply" if apply else "dry_run", "completed",
                 json.dumps(manifest, ensure_ascii=False), now),
            )
            for lane in lanes:
                action = await _upsert_thesis_row(conn, lane, kind="lane", direction="neutral", now=now)
                counts["lane"][action] += 1
                items.append(("lane", lane["source_id"], lane["id"], action, lane["slug"]))
            for thesis in theses_plan:
                action = await _upsert_thesis_row(
                    conn, thesis, kind="thesis", direction=thesis["direction"], now=now
                )
                counts["thesis"][action] += 1
                items.append(("thesis", thesis["source_id"], thesis["id"], action, thesis["slug"]))
            planned_sec_ids = {s["id"] for s in stocks}
            for stock in stocks:
                action = await _upsert_security_row(conn, stock, now, warnings, planned_sec_ids)
                counts["stock"][action] += 1
                items.append(("stock", stock["source_id"], stock["id"], action, stock["ticker"]))
            planned_keys = {e["id"]: (e["thesis_id"], e["security_id"], e["role"]) for e in edges}
            for edge in edges:
                action, target_id = await _upsert_edge_row(conn, edge, now, warnings, planned_keys)
                counts["edge"][action] += 1
                items.append(("edge", edge["source_id"], target_id, action, edge["role"]))
            await _resolve_parked_edges(conn, warnings)
            if not apply:
                raise _DryRunRollback
            for item_type, source_id, target_id, action, detail in items:
                await conn.execute(
                    "INSERT INTO market_thesis_import_items (id, batch_id, item_type, source_id, target_id, "
                    "action, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (_new_id(), batch_id, item_type, source_id, target_id, action, detail, now),
                )
    except _DryRunRollback:
        # dry-run rolled back everything including the batch row: re-record it
        await db.execute(
            "INSERT INTO market_thesis_import_batches (id, source, mode, status, counts_json, warnings_json, "
            "manifest_json, created_at, finished_at) VALUES (?,?,'dry_run','completed','{}','[]',?,?,NULL)",
            (batch_id, str(src), json.dumps(manifest, ensure_ascii=False), now),
        )
    except BundleError as exc:
        # Domain conflicts deliberately abort the apply transaction, which also
        # rolls back its provisional batch row. Recreate a failed provenance
        # record so the operator can see and resolve the blocked import.
        _warn(warnings, str(exc))
        finished_at = bus.now_iso()
        await db.execute(
            "INSERT INTO market_thesis_import_batches (id, source, mode, status, counts_json, warnings_json, "
            "manifest_json, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                batch_id, str(src), "apply" if apply else "dry_run", "failed",
                json.dumps(counts, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False),
                json.dumps(manifest, ensure_ascii=False), now, finished_at,
            ),
        )
        await bus.emit(
            "thesis.import_failed", "import", batch_id,
            {"mode": "apply" if apply else "dry_run", "error": str(exc)},
        )
        raise

    # finalize the batch row with the counts/warnings gathered above
    await db.execute(
        "UPDATE market_thesis_import_batches SET counts_json=?, warnings_json=?, finished_at=? WHERE id=?",
        (json.dumps(counts, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False),
         bus.now_iso(), batch_id),
    )
    result = {
        "batch_id": batch_id, "mode": "apply" if apply else "dry_run",
        "counts": counts, "warnings": warnings,
    }
    await bus.emit("thesis.import_completed", "import", batch_id, {
        "mode": result["mode"],
        "warnings": len(warnings),
        **{k: v["created"] + v["updated"] for k, v in counts.items()},
    })
    return result


# ---- row upserts (inside the apply transaction) --------------------------------

async def _resolve_parked_edges(conn, warnings: list[str]) -> None:
    """Safety net: no row may leave the transaction with the detach mark.

    A parked row whose plan never landed (skipped for another reason) is
    restored to its original key, or dropped if that key found a new owner.
    """
    # only import rows can be parked (manual rows are skipped before parking,
    # and the domain layer rejects \x1f in operator-written roles)
    cur = await conn.execute(
        "SELECT * FROM thesis_security_edges WHERE role LIKE ? AND source = 'import'",
        (f"%{_DETACH_MARK}",),
    )
    parked = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    for row in parked:
        restored = row["role"][: -len(_DETACH_MARK)]
        owner = await _fetch_one(
            conn,
            "SELECT id FROM thesis_security_edges WHERE thesis_id = ? AND security_id = ? AND role = ?",
            (row["thesis_id"], row["security_id"], restored),
        )
        if owner is None:
            await conn.execute(
                "UPDATE thesis_security_edges SET role = ? WHERE id = ?", (restored, row["id"])
            )
        else:
            _warn(warnings, f"edge {row['id']} dropped: key reassigned to {owner['id']} during import")
            await conn.execute("DELETE FROM thesis_security_edges WHERE id = ?", (row["id"],))

async def _fetch_one(conn, sql: str, params: tuple) -> dict[str, Any] | None:
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row is not None else None


async def _free_slug(conn, slug: str, row_id: str) -> str:
    """Keep the planned slug unless a DIFFERENT row already owns it."""
    owner = await _fetch_one(conn, "SELECT id FROM theses WHERE slug = ?", (slug,))
    if owner is None or owner["id"] == row_id:
        return slug
    return f"{slug[:47]}-{row_id[:6]}"


async def _upsert_thesis_row(conn, plan: dict[str, Any], *, kind: str, direction: str, now: str) -> str:
    row = await _fetch_one(conn, "SELECT * FROM theses WHERE id = ?", (plan["id"],))
    meta_json = json.dumps(plan["meta"], ensure_ascii=False)
    view = plan["view"]
    title = plan["title"]
    if row is None:
        slug = await _free_slug(conn, plan["slug"], plan["id"])
        await conn.execute(
            "INSERT INTO theses (id, slug, parent_id, kind, title, view, direction, status, tags_json, "
            "meta_json, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'candidate','[]',?,'import',?,?)",
            (plan["id"], slug, plan.get("parent_id"), kind, title, view, direction, meta_json, now, now),
        )
        await conn.execute(
            "INSERT INTO thesis_versions (id, thesis_id, version, title, view, direction, status, author, "
            "created_at) VALUES (?,?,1,?,?,?,'candidate','import',?)",
            (_new_id(), plan["id"], title, view, direction, now),
        )
        return "created"
    changed = (
        title != row["title"] or view != row["view"] or direction != row["direction"]
        or plan.get("parent_id") != row["parent_id"] or meta_json != row["meta_json"]
    )
    if not changed:
        return "unchanged"
    await conn.execute(
        "UPDATE theses SET parent_id=?, title=?, view=?, direction=?, meta_json=?, updated_at=? WHERE id=?",
        (plan.get("parent_id"), title, view, direction, meta_json, now, plan["id"]),
    )
    if title != row["title"] or view != row["view"] or direction != row["direction"]:
        nxt = await _fetch_one(
            conn, "SELECT COALESCE(MAX(version),0) AS v FROM thesis_versions WHERE thesis_id = ?",
            (plan["id"],),
        )
        await conn.execute(
            "INSERT INTO thesis_versions (id, thesis_id, version, title, view, direction, status, author, "
            "created_at) VALUES (?,?,?,?,?,?,?,'import',?)",
            (_new_id(), plan["id"], nxt["v"] + 1, title, view, direction, row["status"], now),
        )
    return "updated"


async def _recanonicalize_security(conn, old_id: str, new_id: str, warnings: list[str]) -> None:
    """A corrected ticker/market changed the canonical id: move children, drop the stale row.

    Edges move one by one so conflicts resolve by ownership, not by accident:
    a manual edge displaces an import-owned occupant of the same natural key,
    an import edge yields to any occupant (the bundle re-upserts its payload
    later by natural key), and dropping a manual edge is warned loudly.
    Aliases carry no payload, so identical tuples merge set-wise.
    """
    cur = await conn.execute("SELECT * FROM thesis_security_edges WHERE security_id = ?", (old_id,))
    old_edges = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    for edge in old_edges:
        owner = await _fetch_one(
            conn,
            "SELECT * FROM thesis_security_edges WHERE thesis_id = ? AND security_id = ? AND role = ?",
            (edge["thesis_id"], new_id, edge["role"]),
        )
        if owner is None:
            await conn.execute(
                "UPDATE thesis_security_edges SET security_id = ? WHERE id = ?", (new_id, edge["id"])
            )
        elif edge["source"] != "import" and owner["source"] == "import":
            await conn.execute("DELETE FROM thesis_security_edges WHERE id = ?", (owner["id"],))
            await conn.execute(
                "UPDATE thesis_security_edges SET security_id = ? WHERE id = ?", (new_id, edge["id"])
            )
        elif edge["source"] != "import" and owner["source"] != "import":
            # Two operator-owned rows collapse onto the same natural key.
            # Choosing either one would silently destroy manual work, so stop
            # the whole import transaction and require explicit resolution.
            raise BundleError(
                f"manual edge collision while recanonicalizing {old_id} -> {new_id}: "
                f"{edge['id']} conflicts with {owner['id']}"
            )
        else:
            await conn.execute("DELETE FROM thesis_security_edges WHERE id = ?", (edge["id"],))
    await conn.execute(
        "UPDATE OR IGNORE security_aliases SET security_id = ? WHERE security_id = ?", (new_id, old_id)
    )
    await conn.execute("DELETE FROM security_aliases WHERE security_id = ?", (old_id,))
    await conn.execute("DELETE FROM securities WHERE id = ?", (old_id,))


async def _upsert_security_row(
    conn, plan: dict[str, Any], now: str, warnings: list[str], planned_ids: set[str]
) -> str:
    row = await _fetch_one(conn, "SELECT * FROM securities WHERE id = ?", (plan["id"],))
    meta_json = json.dumps(plan["meta"], ensure_ascii=False)
    action = "created"

    # reconcile by bundle source id: if this source was previously imported
    # under DIFFERENT canonical id(s) (ticker/market corrected upstream), every
    # old row must be migrated, not left behind as a stale duplicate
    cur = await conn.execute(
        "SELECT DISTINCT security_id FROM security_aliases WHERE kind = 'bundle_id' AND alias = ? "
        "AND security_id != ?",
        (plan["source_id"], plan["id"]),
    )
    stale_ids = [r["security_id"] for r in await cur.fetchall()]
    await cur.close()
    migrated = bool(stale_ids)
    if migrated:
        if row is None:
            await conn.execute(
                "INSERT INTO securities (id, ticker, market, instrument_type, name, name_en, meta_json, "
                "source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'import',?,?)",
                (plan["id"], plan["ticker"], plan["market"], plan["instrument_type"],
                 plan["name"], plan["name_en"], meta_json, now, now),
            )
        for stale_id in stale_ids:
            _warn(warnings, f"stock {plan['source_id']}: canonical id {stale_id} -> {plan['id']}")
            if stale_id in planned_ids:
                # the old row now belongs to ANOTHER source in this bundle —
                # deleting it would orphan that winner. Only this source's
                # alias moves (re-inserted against plan['id'] below); the
                # row's edges self-heal through their deterministic edge ids.
                await conn.execute(
                    "DELETE FROM security_aliases WHERE security_id = ? AND kind = 'bundle_id' AND alias = ?",
                    (stale_id, plan["source_id"]),
                )
            else:
                await _recanonicalize_security(conn, stale_id, plan["id"], warnings)
        row = await _fetch_one(conn, "SELECT * FROM securities WHERE id = ?", (plan["id"],))

    if plan.get("duplicate_of"):
        # a duplicate source for a row another plan owns: keep only the
        # bundle_id alias so this source id still resolves — never the payload
        # (bundle order must not decide the row's content)
        await conn.execute(
            "INSERT OR IGNORE INTO security_aliases (id, security_id, alias, kind, created_at) VALUES (?,?,?,?,?)",
            (_det_id("mti", "alias", plan["id"], "bundle_id", plan["source_id"]),
             plan["id"], plan["source_id"], "bundle_id", now),
        )
        return "skipped"

    if row is None:
        await conn.execute(
            "INSERT INTO securities (id, ticker, market, instrument_type, name, name_en, meta_json, source, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,'import',?,?)",
            (plan["id"], plan["ticker"], plan["market"], plan["instrument_type"],
             plan["name"], plan["name_en"], meta_json, now, now),
        )
    else:
        changed = (
            plan["name"] != row["name"] or plan["name_en"] != row["name_en"]
            or plan["instrument_type"] != row["instrument_type"] or meta_json != row["meta_json"]
        )
        if changed:
            await conn.execute(
                "UPDATE securities SET instrument_type=?, name=?, name_en=?, meta_json=?, updated_at=? WHERE id=?",
                (plan["instrument_type"], plan["name"], plan["name_en"], meta_json, now, plan["id"]),
            )
        action = "updated" if (changed or migrated) else "unchanged"
    for alias, alias_kind in (
        (plan["source_id"], "bundle_id"),
        (plan["name"], "name_zh"),
        (plan["ticker"], "ticker"),
    ):
        if alias:
            await conn.execute(
                "INSERT OR IGNORE INTO security_aliases (id, security_id, alias, kind, created_at) "
                "VALUES (?,?,?,?,?)",
                (_det_id("mti", "alias", plan["id"], alias_kind, alias), plan["id"], alias, alias_kind, now),
            )
    return action


async def _upsert_edge_row(
    conn, plan: dict[str, Any], now: str, warnings: list[str],
    planned_keys: dict[str, tuple[str, str, str]],
) -> tuple[str, str | None]:
    """Returns (action, actual_row_id) — the survivor may not be the planned det id."""
    # resolve by natural key first (a recanonicalization may have merged this
    # edge onto a row with a different id), then by det id
    by_key = await _fetch_one(
        conn,
        "SELECT * FROM thesis_security_edges WHERE thesis_id = ? AND security_id = ? AND role = ?",
        (plan["thesis_id"], plan["security_id"], plan["role"]),
    )
    by_id = await _fetch_one(conn, "SELECT * FROM thesis_security_edges WHERE id = ?", (plan["id"],))

    # conflict: another row occupies the plan's target key
    if by_key is not None and by_id is not None and by_key["id"] != by_id["id"]:
        occupant_plan = planned_keys.get(by_key["id"])
        occupant_key = (by_key["thesis_id"], by_key["security_id"], by_key["role"])
        if by_key["source"] == "import" and occupant_plan is not None and occupant_plan != occupant_key:
            # the import occupant is scheduled to move to another key later in
            # this import (e.g. two securities swapped canonical ids): park it
            # so the slot frees up — its own plan lands it on its final key.
            # Manual rows are NEVER parked: if their own move later fails the
            # mark would leak into a committed row, so they simply hold their
            # key and this plan yields below.
            await conn.execute(
                "UPDATE thesis_security_edges SET role = ? WHERE id = ?",
                (by_key["role"] + _DETACH_MARK, by_key["id"]),
            )
            by_key = None
        elif by_id["source"] != "import":
            # target key is held while the plan's own det row is operator-
            # owned: leave BOTH rows alone (never delete manual work)
            _warn(warnings, f"edge {plan['source_id']}: manual edge {by_id['id']} edited by the operator "
                            f"stays put; key owner {by_key['id']} unchanged; bundle values skipped")
            return "skipped", by_id["id"]
        else:
            # the plan's det row was re-pointed elsewhere while another row
            # holds the target key (an import row staying put, or any manual
            # row): the holder is the survivor, the import det row is a stale
            # duplicate — the bundle converges on the next apply if the
            # holder later moves
            await conn.execute("DELETE FROM thesis_security_edges WHERE id = ?", (by_id["id"],))
            by_id = None

    row = by_key or by_id
    meta_json = json.dumps(plan["meta"], ensure_ascii=False)
    if row is None:
        await conn.execute(
            "INSERT INTO thesis_security_edges (id, thesis_id, security_id, role, exposure, confidence, "
            "rationale, meta_json, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,'import',?,?)",
            (plan["id"], plan["thesis_id"], plan["security_id"], plan["role"], plan["exposure"],
             plan["confidence"], plan["rationale"], meta_json, now, now),
        )
        return "created", plan["id"]

    if row["source"] != "import":
        # operator-owned: endpoints may migrate structurally (the row follows
        # its source through canonical changes) but payload NEVER comes from
        # the bundle
        struct_changed = (
            plan["thesis_id"] != row["thesis_id"] or plan["security_id"] != row["security_id"]
            or plan["role"] != row["role"]
        )
        if struct_changed and row["id"] == plan["id"]:
            await conn.execute(
                "UPDATE thesis_security_edges SET thesis_id=?, security_id=?, role=?, updated_at=? WHERE id=?",
                (plan["thesis_id"], plan["security_id"], plan["role"], now, row["id"]),
            )
            _warn(warnings, f"edge {plan['source_id']}: manual edge {row['id']} migrated structurally; "
                            "operator payload kept")
            return "updated", row["id"]
        _warn(warnings, f"edge {plan['source_id']}: manual edge {row['id']} edited by the operator "
                        f"owns ({plan['thesis_id']},{plan['security_id']},{plan['role']}); bundle values skipped")
        return "skipped", row["id"]

    if by_key is not None and by_key["id"] != plan["id"]:
        # two bundle sources collapse onto one natural key: the first import
        # owner keeps the payload — deterministic and idempotent instead of
        # letting bundle order flip-flop the row on every re-import
        _warn(warnings, f"edge {plan['source_id']}: duplicate of import edge {by_key['id']}; values skipped")
        return "skipped", by_key["id"]

    changed = (
        plan["thesis_id"] != row["thesis_id"] or plan["security_id"] != row["security_id"]
        or plan["role"] != row["role"]  # a parked row lands here (mark stripped)
        or plan["exposure"] != row["exposure"] or plan["confidence"] != row["confidence"]
        or plan["rationale"] != row["rationale"] or meta_json != row["meta_json"]
    )
    if not changed:
        return "unchanged", row["id"]
    # row is the plan's own det row: either the natural-key owner (endpoint
    # writes are no-ops on the index) or its target key is free — plain UPDATE
    await conn.execute(
        "UPDATE thesis_security_edges SET thesis_id=?, security_id=?, role=?, exposure=?, confidence=?, "
        "rationale=?, meta_json=?, updated_at=? WHERE id=?",
        (plan["thesis_id"], plan["security_id"], plan["role"], plan["exposure"], plan["confidence"],
         plan["rationale"], meta_json, now, row["id"]),
    )
    return "updated", row["id"]


async def list_batches(limit: int = 20) -> list[dict[str, Any]]:
    rows = await db.query(
        "SELECT * FROM market_thesis_import_batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (min(max(limit, 1), 100),),
    )
    out = []
    for r in rows:
        batch = dict(r)
        for field in ("counts", "warnings", "manifest"):
            try:
                batch[field] = json.loads(batch.pop(f"{field}_json") or "null")
            except ValueError:
                batch[field] = None
        out.append(batch)
    return out


def _cli() -> None:
    """Operator entry point (ported from the remote wave-3 line):

    .venv/bin/python -m app.institute.market_thesis_import market-thesis-data --dry-run
    .venv/bin/python -m app.institute.market_thesis_import market-thesis-data --apply
    """
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Import a market-thesis-data bundle")
    parser.add_argument("bundle", nargs="?", default=None,
                        help="bundle DIRECTORY (default: market-thesis-data/ in the repo)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="validate + report only (default)")
    group.add_argument("--apply", action="store_true", help="write lanes/theses/securities/edges")
    args = parser.parse_args()

    async def _run() -> dict[str, Any]:
        await db.init()
        try:
            return await import_bundle(args.bundle, apply=args.apply)
        finally:
            await db.close()

    print(json.dumps(asyncio.run(_run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
