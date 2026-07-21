"""market-thesis-data bundle importer (card M1-003).

``import_bundle(path, mode)`` reads an exported ``bundle.json`` and turns it
into the local coverage universe. Contract: design/local-thesis-alpha/
10-market-thesis-data-bootstrap.md + roadmap/07-market-thesis-data-kickoff.md;
target schema: migrations/0003_theses.sql (theses/versions + provenance) and
0004_securities.sql (securities/aliases/edges).

Modes
  dry_run  validate + plan only: report per-entity counts, warnings, and what
           apply WOULD do; a provenance row (mode='dry_run', idempotency_key
           NULL) is still recorded, no domain rows and no item rows.
  apply    insert lanes (theses rows with kind='lane'), theses under their
           lanes (+ seeded thesis_versions v1: summary = coreView, stock_map =
           stockUniverse), securities + aliases, and tracks_stock edges into
           thesis_security_edges. Item provenance per bundle record.

Edge kinds (bundle ships 3; nothing is dropped silently)
  tracks_stock       -> thesis_security_edges rows (role/bucket/weight kept;
                        exposure = weight / 3 clamped to 0..1 — the bundle
                        weight scale is 1 watch/hedge, 2 peer, 3 core).
  belongs_to_lane    -> already represented by theses.parent_id: item rows are
                        'skipped', mismatches against thesis.laneId warn.
  lane_contains_stock-> no schema home (the edge table is the thesis-level
                        investable map): counted + warned, item rows 'skipped';
                        the lane universe survives in lane metadata_json
                        (stockTickers) so nothing is lost.

Field policy: every bundle field not mapped onto a real column lands verbatim
in the row's metadata_json (the importer must never drop fields). Imported
lanes and theses enter as status='active' (bootstrap doc allows active|watch);
directions stay as shipped — 'conflicting' rows are hypotheses, not calls.

Fail closed (import marked failed / nothing written): missing or unreadable
bundle, unknown schema version, duplicate lane/thesis id with a conflicting
title, slug conflicts. Warn but continue: alias collisions (the documented
cross-listed zh names — warn-and-skip, company_key groups the listings),
context-only/unknown markets (Korea/Japan -> GLOBAL_CONTEXT), malformed rows,
edges with missing refs (item status 'failed'), empty thesis titles, stats
mismatches.

Apply is transactional: domain rows + item rows + the running->completed
conditional claim commit together, so a mid-import failure rolls back to zero
domain writes and the completed-only idempotency index (idx_mti_idem) lets the
retry through. A completed apply of the same bundle refuses politely.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from .theses import VIEWS

BUNDLE_SCHEMAS = {"researchos.market_thesis_export.bundle.v1"}
MODES = {"dry_run", "apply"}
SOURCE = "market_thesis_import"
LANE_SCOPE = "Imported lane from market-thesis-data"

# bundle market string -> (local market, instrument_type); context-only markets
# keep their native vendor suffix and land as GLOBAL_CONTEXT with a warning
# (normalization table in 10-market-thesis-data-bootstrap.md).
_MARKET_MAP = {
    "A-share": ("CN_A", "stock"),
    "A-share ETF": ("CN_A", "ETF"),
    "HK": ("HK", "stock"),
    "HK ETF": ("HK", "ETF"),
    "US": ("US", "stock"),
    "US ETF": ("US", "ETF"),
    "US ADR": ("US", "ADR"),
}
_CONTEXT_MARKETS = {"Korea", "Japan"}
_CURRENCY = {"CN_A": "CNY", "HK": "HKD", "US": "USD"}
_CN_EXCHANGE = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
_CN_TICKER = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_HK_TICKER = re.compile(r"^\d+\.HK$")
_RESERVED_SUFFIXES = ("SH", "SZ", "BJ", "HK", "US")
_WEIGHT_SCALE = 3.0  # tracks_stock weight ceiling observed in the bundle (core=3)
_BUCKETS = {"core", "watch", "peer", "hedge"}  # CHECK set on thesis_security_edges

IMPORT_BATCH_DEFAULT_LIMIT = 50
IMPORT_BATCH_MAX_LIMIT = 200
_REDACTED = "[redacted]"
_SENSITIVE_MANIFEST_KEYS = (
    "password", "passwd", "secret", "token", "credential", "authorization",
    "apikey", "accesskey", "privatekey", "bundlepath", "localpath",
)
_SENSITIVE_INLINE_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|credential|authorization|"
    r"api[_-]?key|access[_-]?key|private[_-]?key)\b(\s*[:=]\s*)"
    r"(?:(?:Bearer|Basic)\s+)?"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,&;)\]}]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]+)")
_URL_USERINFO_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/@\s]+@"
)
_LOCAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_:/])(?:file://|~/|/|[A-Za-z]:[\\/])[^\s,;:)\]}]+"
)

# bundle keys that map onto real columns; everything else -> metadata_json
_LANE_MAPPED = {"id", "lane", "laneEn", "href", "firstSeen", "lastSeen"}
_THESIS_MAPPED = {"id", "title", "titleEn", "laneId", "coreView", "direction",
                  "conviction", "firstSeen", "lastSeen", "href", "networkHref"}
_STOCK_MAPPED = {"ticker", "name", "href"}
_EDGE_MAPPED = {"id", "source", "target", "role", "bucket", "weight"}

_EDGE_HANDLING = {
    "tracks_stock": "thesis_security_edges",
    "belongs_to_lane": "skipped: represented by theses.parent_id",
    "lane_contains_stock": ("skipped: no schema home; lane universe retained in "
                            "lane metadata_json.stockTickers"),
}


class MarketThesisImportError(ValueError):
    """Bundle validation / idempotency failure (fail closed)."""


# ---- helpers ---------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _dumps(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False)


def _has_cjk(text: str) -> bool:
    return any("㐀" <= ch <= "鿿" for ch in text or "")


def _num(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _tally(entries: list[dict[str, Any]]) -> dict[str, int]:
    out = {"inserted": 0, "skipped": 0, "failed": 0}
    for e in entries:
        out[e["status"]] += 1
    return out


def _safe_json(text: str | None, fallback: Any, expected: type) -> Any:
    """Decode provenance JSON without letting one damaged row break the list."""
    try:
        value = json.loads(text) if text else fallback
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, expected) else fallback


def _public_provenance_value(value: Any, *, key: str = "") -> Any:
    """Remove credentials and machine-local paths from persisted manifests.

    The importer intentionally stores the source manifest for auditability, but
    that JSON is an open object.  Treat it as untrusted at the API boundary: a
    future exporter may add a local path or credential-shaped field that does
    not belong in a read endpoint.
    """
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    if any(part in normalized for part in _SENSITIVE_MANIFEST_KEYS):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(child_key): _public_provenance_value(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_public_provenance_value(child) for child in value]
    if isinstance(value, str):
        redacted = _URL_USERINFO_RE.sub(
            lambda match: f"{match.group(1)}{_REDACTED}@", value
        )
        redacted = _SENSITIVE_INLINE_RE.sub(r"\1\2" + _REDACTED, redacted)
        redacted = _BEARER_RE.sub("Bearer " + _REDACTED, redacted)
        return _LOCAL_PATH_RE.sub(_REDACTED, redacted)
    return value


async def list_import_batches(
    limit: int = IMPORT_BATCH_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Return recent importer provenance without bundle contents or local paths."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise MarketThesisImportError("limit must be an integer")
    if not 1 <= limit <= IMPORT_BATCH_MAX_LIMIT:
        raise MarketThesisImportError(
            f"limit must be between 1 and {IMPORT_BATCH_MAX_LIMIT}"
        )

    rows = await db.query(
        "SELECT id, schema, generated_at, source_schema, source_generated_at, "
        "source_first_date, source_last_date, thesis_count, lane_count, stock_count, "
        "edge_count, thesis_stock_edge_count, bundle_sha256, idempotency_key, mode, "
        "status, manifest_json, warnings_json, error, imported_at, finished_at "
        "FROM market_thesis_imports ORDER BY imported_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    batches: list[dict[str, Any]] = []
    for row in rows:
        batch = dict(row)
        manifest = _safe_json(batch.pop("manifest_json", None), {}, dict)
        warnings = _safe_json(batch.pop("warnings_json", None), [], list)
        batch["manifest"] = _public_provenance_value(manifest)
        batch["warnings"] = _public_provenance_value(warnings)
        batch["error"] = _public_provenance_value(batch.get("error"))
        batches.append(batch)
    return batches


async def _load_existing() -> dict[str, Any]:
    """Pre-transaction snapshot for skip/conflict planning (advisory: a racing
    writer still surfaces as an IntegrityError inside the transaction, which
    rolls back and marks the batch failed — the retry then succeeds)."""
    return {
        "theses": {r["id"]: r for r in await db.query("SELECT id, name_zh, kind FROM theses")},
        "slugs": {r["slug"]: r["id"] for r in await db.query("SELECT slug, id FROM theses")},
        "securities": {r["id"]: r for r in await db.query("SELECT id, name_zh, name_en FROM securities")},
        "aliases": {(r["alias"], r["kind"]): r["security_id"]
                    for r in await db.query("SELECT alias, kind, security_id FROM security_aliases")},
        "edges": {(r["thesis_id"], r["security_id"], r["role"])
                  for r in await db.query("SELECT thesis_id, security_id, role FROM thesis_security_edges")},
    }


# ---- planning (shared by dry_run and apply) ---------------------------------

def _plan_lanes(
    records: list[Any], existing: dict[str, Any], warnings: list[str], now: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, rec in enumerate(records):
        ext = f"lane[{i}]"
        if not isinstance(rec, dict) or not str(rec.get("id") or "").strip():
            warnings.append(f"{ext}: malformed row (missing id); skipped")
            entries.append({"external_id": ext, "status": "failed", "local_id": None,
                            "message": "malformed row (missing id)", "row": None})
            continue
        lid = str(rec["id"]).strip()
        if lid in seen:
            warnings.append(f"lane {lid}: duplicate id in bundle; first occurrence wins")
            continue
        seen.add(lid)
        name_zh = str(rec.get("lane") or "").strip() or str(rec.get("laneEn") or "").strip() or lid
        if not str(rec.get("lane") or "").strip():
            warnings.append(f"lane {lid}: empty name; falls back to {name_zh!r}")
        held = existing["theses"].get(lid)
        if held is not None:
            if held["kind"] != "lane" or held["name_zh"] != name_zh:
                raise MarketThesisImportError(
                    f"lane {lid!r} conflicts with existing thesis row "
                    f"(kind={held['kind']!r}, name_zh={held['name_zh']!r})"
                )
            entries.append({"external_id": lid, "status": "skipped", "local_id": lid,
                            "message": "already present", "row": None})
            continue
        slug_holder = existing["slugs"].get(lid)
        if slug_holder is not None and slug_holder != lid:
            raise MarketThesisImportError(f"lane {lid!r}: slug already used by thesis {slug_holder!r}")
        metadata = {k: v for k, v in rec.items() if k not in _LANE_MAPPED}
        entries.append({
            "external_id": lid, "status": "inserted", "local_id": lid, "message": None,
            "row": (
                lid, None, "lane", lid, name_zh, rec.get("laneEn"), "active", LANE_SCOPE,
                "", None, 0, "medium", "unknown", None, None, rec.get("firstSeen"),
                rec.get("lastSeen"), SOURCE, rec.get("href"), None, _dumps(metadata), now, now,
            ),
        })
    return entries


def _plan_theses(
    records: list[Any], existing: dict[str, Any], known_lanes: set[str],
    warnings: list[str], now: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, rec in enumerate(records):
        ext = f"thesis[{i}]"
        if not isinstance(rec, dict) or not str(rec.get("id") or "").strip():
            warnings.append(f"{ext}: malformed row (missing id); skipped")
            entries.append({"external_id": ext, "status": "failed", "local_id": None,
                            "message": "malformed row (missing id)", "row": None})
            continue
        tid = str(rec["id"]).strip()
        if tid in seen:
            warnings.append(f"thesis {tid}: duplicate id in bundle; first occurrence wins")
            continue
        seen.add(tid)
        name_zh = str(rec.get("title") or "").strip() or str(rec.get("titleEn") or "").strip() or tid
        if not str(rec.get("title") or "").strip():
            warnings.append(f"thesis {tid}: empty title; name falls back to {name_zh!r}")
        held = existing["theses"].get(tid)
        if held is not None:
            if held["kind"] != "thesis" or held["name_zh"] != name_zh:
                raise MarketThesisImportError(
                    f"thesis {tid!r} conflicts with existing row "
                    f"(kind={held['kind']!r}, name_zh={held['name_zh']!r})"
                )
            entries.append({"external_id": tid, "status": "skipped", "local_id": tid,
                            "message": "already present", "row": None, "version_row": None})
            continue
        slug_holder = existing["slugs"].get(tid)
        if slug_holder is not None and slug_holder != tid:
            raise MarketThesisImportError(f"thesis {tid!r}: slug already used by thesis {slug_holder!r}")

        lane_id = str(rec.get("laneId") or "").strip() or None
        if lane_id and lane_id not in known_lanes:
            warnings.append(f"thesis {tid}: unknown lane {lane_id!r}; imported without parent")
            lane_id = None
        view = rec.get("direction") or "unknown"
        if view not in VIEWS:
            warnings.append(f"thesis {tid}: unknown direction {view!r}; stored as 'unknown'")
            view = "unknown"
        conviction = _num(rec.get("conviction"))
        if conviction is None and rec.get("conviction") is not None:
            warnings.append(f"thesis {tid}: malformed conviction {rec.get('conviction')!r}")
        practical = rec.get("practical") if isinstance(rec.get("practical"), dict) else {}
        score = _num(practical.get("score"))
        if score is None and practical.get("score") is not None:
            warnings.append(f"thesis {tid}: malformed practical.score {practical.get('score')!r}")
        stock_universe = rec.get("stockUniverse") if isinstance(rec.get("stockUniverse"), list) else []
        metadata = {k: v for k, v in rec.items() if k not in _THESIS_MAPPED}
        entries.append({
            "external_id": tid, "status": "inserted", "local_id": tid, "message": None,
            "row": (
                tid, lane_id, "thesis", tid, name_zh, rec.get("titleEn"), "active", "",
                "", None, score if score is not None else 0, "medium", view, conviction,
                score, rec.get("firstSeen"), rec.get("lastSeen"), SOURCE, rec.get("href"),
                rec.get("networkHref"), _dumps(metadata), now, now,
            ),
            "version_row": (
                _new_id(), tid, 1, None, None, view, "medium", str(rec.get("coreView") or ""),
                "[]", "[]", "[]", "[]", _dumps(stock_universe), now,
            ),
        })
    return entries


def _normalize_security(ticker: str, market_raw: str, warnings: list[str]) -> dict[str, Any] | None:
    """Canonical-id normalization (bootstrap doc): suffixed CN_A/HK as shipped,
    unsuffixed US gains .US, context markets keep their native vendor suffix.
    Returns None (malformed) when no valid canonical id can be built."""
    if market_raw in _MARKET_MAP:
        market, itype = _MARKET_MAP[market_raw]
    elif market_raw in _CONTEXT_MARKETS:
        market, itype = "GLOBAL_CONTEXT", "stock"
        warnings.append(f"stock {ticker}: context-only market {market_raw!r} -> GLOBAL_CONTEXT")
    else:
        market, itype = "GLOBAL_CONTEXT", "stock"
        warnings.append(f"stock {ticker}: unknown market {market_raw!r} -> GLOBAL_CONTEXT")

    if market == "CN_A":
        if not _CN_TICKER.match(ticker):
            warnings.append(f"stock {ticker}: malformed A-share ticker (want 6 digits + .SH/.SZ/.BJ)")
            return None
        sid, symbol = ticker, ticker[:6]
        exchange = _CN_EXCHANGE[ticker.rsplit(".", 1)[1]]
    elif market == "HK":
        if _HK_TICKER.match(ticker):
            sid, symbol = ticker, ticker[: -len(".HK")]
        elif ticker.isdigit():
            sid, symbol = f"{ticker}.HK", ticker
        else:
            warnings.append(f"stock {ticker}: malformed HK ticker")
            return None
        exchange = "HKEX"
    elif market == "US":
        symbol = ticker[: -len(".US")] if ticker.endswith(".US") else ticker
        if not symbol:
            warnings.append(f"stock {ticker}: malformed US ticker")
            return None
        sid, exchange = f"{symbol}.US", None
    else:  # GLOBAL_CONTEXT: native vendor suffix required, reserved suffixes excluded
        if "." not in ticker.strip(".") or ticker.rsplit(".", 1)[1] in _RESERVED_SUFFIXES:
            warnings.append(f"stock {ticker}: cannot derive a GLOBAL_CONTEXT id (no native suffix)")
            return None
        sid, symbol, exchange = ticker, ticker.rsplit(".", 1)[0], None
    return {"id": sid, "symbol": symbol, "market": market, "instrument_type": itype,
            "exchange": exchange, "currency": _CURRENCY.get(market)}


def _plan_stocks(
    records: list[Any], existing: dict[str, Any], warnings: list[str], now: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """Returns (stock entries, alias entries, bundle-ticker -> canonical id)."""
    entries: list[dict[str, Any]] = []
    alias_entries: list[dict[str, Any]] = []
    ticker_map: dict[str, str] = {}
    # cross-listings ship duplicate zh names (0004 IMPORTER WARNING: 中芯国际,
    # 中远海控) — company_key groups them; the duplicate alias is warn-and-skip
    name_counts: dict[str, int] = {}
    for rec in records:
        if isinstance(rec, dict) and str(rec.get("name") or "").strip():
            name = str(rec["name"]).strip()
            name_counts[name] = name_counts.get(name, 0) + 1
    occupied = dict(existing["aliases"])  # (alias, kind) -> security_id
    seen: set[str] = set()

    for i, rec in enumerate(records):
        ext = f"stock[{i}]"
        if not isinstance(rec, dict) or not str(rec.get("ticker") or "").strip():
            warnings.append(f"{ext}: malformed row (missing ticker); skipped")
            entries.append({"external_id": ext, "status": "failed", "local_id": None,
                            "message": "malformed row (missing ticker)", "row": None})
            continue
        ticker = str(rec["ticker"]).strip()
        if ticker in seen:
            warnings.append(f"stock {ticker}: duplicate ticker in bundle; first occurrence wins")
            continue
        seen.add(ticker)
        name = str(rec.get("name") or "").strip()
        if not name:
            warnings.append(f"stock {ticker}: malformed row (empty name); skipped")
            entries.append({"external_id": ticker, "status": "failed", "local_id": None,
                            "message": "malformed row (empty name)", "row": None})
            continue
        norm = _normalize_security(ticker, str(rec.get("market") or ""), warnings)
        if norm is None:
            entries.append({"external_id": ticker, "status": "failed", "local_id": None,
                            "message": "malformed ticker/market", "row": None})
            continue
        sid = norm["id"]
        ticker_map[ticker] = sid
        if sid in existing["securities"]:
            held = existing["securities"][sid]
            if name not in (held["name_zh"], held["name_en"]):
                warnings.append(f"stock {ticker}: {sid} already present with a different name; kept existing")
            entries.append({"external_id": ticker, "status": "skipped", "local_id": sid,
                            "message": "already present", "row": None})
            continue
        name_zh, name_en = (name, None) if _has_cjk(name) else (None, name)
        company_key = name if name_counts.get(name, 0) > 1 else None
        metadata = {k: v for k, v in rec.items() if k not in _STOCK_MAPPED}
        entries.append({
            "external_id": ticker, "status": "inserted", "local_id": sid, "message": None,
            "row": (
                sid, norm["symbol"], norm["market"], norm["instrument_type"], norm["exchange"],
                name_zh, name_en, norm["currency"], None, "active", company_key, SOURCE,
                rec.get("href"), _dumps(metadata), now, now,
            ),
        })
        for alias, kind in ((norm["symbol"], "ticker"), (name, "name_zh" if name_zh else "name_en")):
            holder = occupied.get((alias, kind))
            if holder is not None:
                warnings.append(f"alias {alias!r} ({kind}) for {sid} collides with {holder}; skipped")
                alias_entries.append({"alias": alias, "kind": kind, "security_id": sid,
                                      "status": "skipped"})
                continue
            occupied[(alias, kind)] = sid
            alias_entries.append({"alias": alias, "kind": kind, "security_id": sid,
                                  "status": "inserted",
                                  "row": (_new_id(), sid, alias, kind, SOURCE, now)})
    return entries, alias_entries, ticker_map


def _plan_edges(
    records: list[Any], existing: dict[str, Any], known_theses: set[str],
    known_lanes: set[str], thesis_lane: dict[str, str], ticker_map: dict[str, str],
    import_id: str, warnings: list[str], now: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    kinds: dict[str, dict[str, Any]] = {}
    claimed = set(existing["edges"])  # (thesis_id, security_id, role)
    unhomed = 0

    def _resolve_security(target: str) -> str | None:
        sid = ticker_map.get(target)
        if sid is None and target in existing["securities"]:
            sid = target  # already-canonical id known to the db
        return sid

    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            warnings.append(f"edge[{i}]: malformed row; skipped")
            entries.append({"external_id": f"edge[{i}]", "status": "failed", "local_id": None,
                            "message": "malformed row", "row": None, "kind": "unknown"})
            continue
        kind = str(rec.get("type") or "unknown")
        ext = str(rec.get("id") or "").strip() or f"edge[{i}]"
        stats = kinds.setdefault(
            kind, {"count": 0, "handling": _EDGE_HANDLING.get(kind, "skipped: unknown edge kind"),
                   "inserted": 0, "skipped": 0, "failed": 0})
        stats["count"] += 1
        source, target = str(rec.get("source") or ""), str(rec.get("target") or "")

        if kind == "tracks_stock":
            if source not in known_theses:
                warnings.append(f"edge {ext}: references unknown thesis {source!r}")
                entry = {"status": "failed", "message": f"unknown thesis {source!r}", "row": None}
            elif (sid := _resolve_security(target)) is None:
                warnings.append(f"edge {ext}: references unknown ticker {target!r}")
                entry = {"status": "failed", "message": f"unknown ticker {target!r}", "row": None}
            else:
                role = str(rec.get("role") or "").strip()
                if not role:
                    warnings.append(f"edge {ext}: missing role; defaulted to 'related'")
                    role = "related"
                bucket = rec.get("bucket")
                if bucket is not None and bucket not in _BUCKETS:
                    warnings.append(f"edge {ext}: unknown bucket {bucket!r}; kept in metadata only")
                    bucket = None
                weight = _num(rec.get("weight"))
                if weight is None and rec.get("weight") is not None:
                    warnings.append(f"edge {ext}: malformed weight {rec.get('weight')!r}")
                exposure = min(1.0, max(0.0, weight / _WEIGHT_SCALE)) if weight is not None else 0.5
                if (source, sid, role) in claimed:
                    entry = {"status": "skipped", "message": "edge already present", "row": None,
                             "local_id": None}
                else:
                    claimed.add((source, sid, role))
                    metadata = {k: v for k, v in rec.items() if k not in _EDGE_MAPPED}
                    local = ext if not ext.startswith("edge[") else _new_id()
                    entry = {
                        "status": "inserted", "message": None, "local_id": local,
                        "row": (local, source, sid, role, bucket, exposure, "medium", "",
                                weight, "active", None, import_id, _dumps(metadata), now, now),
                    }
        elif kind == "belongs_to_lane":
            if source not in known_theses or target not in known_lanes:
                warnings.append(f"edge {ext}: belongs_to_lane references unknown "
                                f"thesis/lane ({source!r} -> {target!r})")
                entry = {"status": "failed", "message": "unknown thesis/lane ref", "row": None}
            else:
                if thesis_lane.get(source) not in (None, target):
                    warnings.append(f"edge {ext}: belongs_to_lane target {target!r} disagrees "
                                    f"with thesis.laneId {thesis_lane[source]!r}")
                entry = {"status": "skipped", "row": None,
                         "message": "represented by theses.parent_id"}
        elif kind == "lane_contains_stock":
            if source not in known_lanes or _resolve_security(target) is None:
                warnings.append(f"edge {ext}: lane_contains_stock references unknown "
                                f"lane/ticker ({source!r} -> {target!r})")
                entry = {"status": "failed", "message": "unknown lane/ticker ref", "row": None}
            else:
                unhomed += 1
                entry = {"status": "skipped", "row": None,
                         "message": "no schema home; lane universe kept in lane metadata"}
        else:
            warnings.append(f"edge {ext}: unknown edge kind {kind!r}; skipped")
            entry = {"status": "skipped", "message": f"unknown edge kind {kind!r}", "row": None}

        stats[entry["status"]] += 1
        entry.setdefault("local_id", None)
        entries.append({"external_id": ext, "kind": kind, **entry})

    if unhomed:
        warnings.append(
            f"{unhomed} lane_contains_stock edges have no schema home; skipped "
            "(lane->stock universe retained in lane metadata_json.stockTickers)"
        )
    return entries, kinds


def _build_plan(bundle: dict[str, Any], existing: dict[str, Any], import_id: str,
                warnings: list[str]) -> dict[str, Any]:
    now = bus.now_iso()
    lanes = _plan_lanes(bundle["lanes"], existing, warnings, now)
    known_lanes = {e["external_id"] for e in lanes if e["status"] != "failed"} | {
        tid for tid, r in existing["theses"].items() if r["kind"] == "lane"
    }
    theses = _plan_theses(bundle["theses"], existing, known_lanes, warnings, now)
    directions = [r.get("direction") for r in bundle["theses"] if isinstance(r, dict)]
    if directions and all(d == "conflicting" for d in directions):
        warnings.append("all imported thesis directions are 'conflicting' — "
                        "treat them as hypotheses needing validation, not calls")
    stocks, aliases, ticker_map = _plan_stocks(bundle["stocks"], existing, warnings, now)
    known_theses = {e["external_id"] for e in theses if e["status"] != "failed"} | set(existing["theses"])
    thesis_lane = {str(r.get("id")): str(r.get("laneId") or "") or None
                   for r in bundle["theses"] if isinstance(r, dict) and r.get("id")}
    edges, edge_kinds = _plan_edges(
        bundle["edges"], existing, known_theses, known_lanes, thesis_lane,
        ticker_map, import_id, warnings, now,
    )
    return {"now": now, "lanes": lanes, "theses": theses, "stocks": stocks,
            "aliases": aliases, "edges": edges, "edge_kinds": edge_kinds}


# ---- apply -------------------------------------------------------------------

async def _apply_plan(import_id: str, plan: dict[str, Any], warnings: list[str]) -> None:
    """One transaction: domain rows + item provenance + the running->completed
    conditional claim. Any failure rolls back to zero domain writes."""
    now = plan["now"]
    items = []
    for item_type, entries in (("lane", plan["lanes"]), ("thesis", plan["theses"]),
                               ("stock", plan["stocks"]), ("edge", plan["edges"])):
        for e in entries:
            items.append((_new_id(), import_id, item_type, e["external_id"],
                          e["local_id"], e["status"], e["message"], now))

    thesis_sql = (
        "INSERT INTO theses (id, parent_id, kind, slug, name_zh, name_en, status, scope, "
        "exclusions, owner_analyst, priority, confidence, current_view, conviction_score, "
        "alpha_prior_score, first_seen, last_seen, source, source_href, source_network_href, "
        "metadata_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    try:
        # NB: transaction() holds the db write lock — use the yielded conn directly
        # (db.execute/insert or bus.emit in here would deadlock); events after commit.
        async with db.transaction() as conn:
            lane_rows = [e["row"] for e in plan["lanes"] if e["row"]]
            if lane_rows:
                await conn.executemany(thesis_sql, lane_rows)
            thesis_rows = [e["row"] for e in plan["theses"] if e["row"]]
            if thesis_rows:
                await conn.executemany(thesis_sql, thesis_rows)
            version_rows = [e["version_row"] for e in plan["theses"] if e.get("version_row")]
            if version_rows:
                await conn.executemany(
                    "INSERT INTO thesis_versions (id, thesis_id, version, supersedes_id, run_id, "
                    "view, confidence, summary, drivers_json, risks_json, kpis_json, "
                    "catalysts_json, stock_map_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    version_rows,
                )
            stock_rows = [e["row"] for e in plan["stocks"] if e["row"]]
            if stock_rows:
                await conn.executemany(
                    "INSERT INTO securities (id, symbol, market, instrument_type, exchange, "
                    "name_zh, name_en, currency, board, listing_status, company_key, source, "
                    "source_href, metadata_json, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    stock_rows,
                )
            alias_rows = [e["row"] for e in plan["aliases"] if e["status"] == "inserted"]
            if alias_rows:
                await conn.executemany(
                    "INSERT INTO security_aliases (id, security_id, alias, kind, source, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    alias_rows,
                )
            edge_rows = [e["row"] for e in plan["edges"] if e["row"]]
            if edge_rows:
                await conn.executemany(
                    "INSERT INTO thesis_security_edges (id, thesis_id, security_id, role, bucket, "
                    "exposure, confidence, rationale, weight, status, source_run_id, import_id, "
                    "metadata_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    edge_rows,
                )
            if items:
                await conn.executemany(
                    "INSERT INTO market_thesis_import_items (id, import_id, item_type, external_id, "
                    "local_id, status, message, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    items,
                )
            # completion claim commits WITH the domain rows: completed <=> imported
            cur = await conn.execute(
                "UPDATE market_thesis_imports SET status='completed', warnings_json=?, "
                "finished_at=? WHERE id=? AND status='running'",
                (_dumps(warnings), bus.now_iso(), import_id),
            )
            claimed = cur.rowcount
            await cur.close()
            if claimed != 1:
                raise MarketThesisImportError(f"import {import_id} was claimed concurrently")
    except sqlite3.IntegrityError as exc:
        # a racing writer slipped past the advisory pre-checks (or a completed
        # apply of the same bundle landed first — idx_mti_idem); zero domain writes
        raise MarketThesisImportError(
            f"import conflicted mid-apply and rolled back ({exc}); nothing was written — retry"
        ) from exc


# ---- entry point ---------------------------------------------------------------

async def import_bundle(path: str | Path, mode: str = "dry_run") -> dict[str, Any]:
    """Validate (and in apply mode, import) a market-thesis-data bundle.

    Returns the import report; raises MarketThesisImportError on fail-closed
    validation, idempotent refusal, or a rolled-back apply (the provenance row
    is marked failed when one exists).
    """
    if mode not in MODES:
        raise MarketThesisImportError(f"unknown mode {mode!r}; allowed: {', '.join(sorted(MODES))}")
    src = Path(path)
    if not src.is_absolute():
        src = get_settings().repo_root / src
    try:
        raw = src.read_bytes()
    except OSError:
        raise MarketThesisImportError(f"bundle file not found: {src}") from None
    sha = hashlib.sha256(raw).hexdigest()
    try:
        bundle = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MarketThesisImportError(f"bundle is not valid JSON: {exc}") from None

    # fail closed before any provenance row: missing manifest header / unknown schema
    if not isinstance(bundle, dict):
        raise MarketThesisImportError("bundle must be a JSON object")
    schema = str(bundle.get("schema") or "")
    if not schema:
        raise MarketThesisImportError("bundle has no schema (missing manifest header)")
    if schema not in BUNDLE_SCHEMAS:
        raise MarketThesisImportError(
            f"unknown bundle schema {schema!r}; supported: {', '.join(sorted(BUNDLE_SCHEMAS))}"
        )
    generated_at = str(bundle.get("generatedAt") or "")
    if not generated_at:
        raise MarketThesisImportError("bundle has no generatedAt")
    for key in ("lanes", "theses", "stocks", "edges"):
        if not isinstance(bundle.get(key), list):
            raise MarketThesisImportError(f"bundle {key!r} must be a list")

    warnings: list[str] = []
    counts = {
        "lanes": len(bundle["lanes"]),
        "theses": len(bundle["theses"]),
        "stocks": len(bundle["stocks"]),
        "edges": len(bundle["edges"]),
        "thesis_stock_edges": sum(
            1 for e in bundle["edges"] if isinstance(e, dict) and e.get("type") == "tracks_stock"
        ),
    }
    stats = bundle.get("stats") if isinstance(bundle.get("stats"), dict) else {}
    for stat_key, count_key in (("laneCount", "lanes"), ("thesisCount", "theses"),
                                ("stockCount", "stocks"), ("edgeCount", "edges"),
                                ("thesisStockEdgeCount", "thesis_stock_edges")):
        if stat_key in stats and stats[stat_key] != counts[count_key]:
            warnings.append(f"stats.{stat_key}={stats[stat_key]!r} but bundle "
                            f"ships {counts[count_key]} {count_key}")
    date_range = stats.get("sourceDateRange") if isinstance(stats.get("sourceDateRange"), dict) else {}

    idem_key = f"{schema}:{generated_at}:{sha}" if mode == "apply" else None
    if idem_key:
        prior = await db.query_one(
            "SELECT id, finished_at FROM market_thesis_imports "
            "WHERE idempotency_key = ? AND status = 'completed'",
            (idem_key,),
        )
        if prior:
            raise MarketThesisImportError(
                f"this bundle was already imported by {prior['id']} "
                f"(completed {prior['finished_at']}); refusing to re-apply"
            )

    import_id = _new_id()
    manifest = {k: v for k, v in bundle.items() if k not in ("lanes", "theses", "stocks", "edges")}
    await db.execute(
        "INSERT INTO market_thesis_imports (id, schema, generated_at, source_schema, "
        "source_generated_at, source_first_date, source_last_date, thesis_count, lane_count, "
        "stock_count, edge_count, thesis_stock_edge_count, bundle_sha256, idempotency_key, "
        "mode, status, manifest_json, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            import_id, schema, generated_at, bundle.get("sourceSchema"),
            bundle.get("sourceGeneratedAt"), date_range.get("first"), date_range.get("last"),
            counts["theses"], counts["lanes"], counts["stocks"], counts["edges"],
            counts["thesis_stock_edges"], sha, idem_key, mode, "running", _dumps(manifest),
            bus.now_iso(),
        ),
    )

    try:
        existing = await _load_existing()
        plan = _build_plan(bundle, existing, import_id, warnings)
        if mode == "apply":
            await _apply_plan(import_id, plan, warnings)
        else:
            claimed = await db.execute(
                "UPDATE market_thesis_imports SET status='completed', warnings_json=?, "
                "finished_at=? WHERE id=? AND status='running'",
                (_dumps(warnings), bus.now_iso(), import_id),
            )
            if claimed != 1:
                raise MarketThesisImportError(f"import {import_id} was claimed concurrently")
    except Exception as exc:
        await db.execute(
            "UPDATE market_thesis_imports SET status='failed', error=?, warnings_json=?, "
            "finished_at=? WHERE id=? AND status='running'",
            (str(exc), _dumps(warnings), bus.now_iso(), import_id),
        )
        if isinstance(exc, MarketThesisImportError):
            raise
        raise MarketThesisImportError(f"import failed: {exc}") from exc

    actions = {
        "lanes": _tally(plan["lanes"]),
        "theses": _tally(plan["theses"]),
        "stocks": _tally(plan["stocks"]),
        "aliases": _tally(plan["aliases"]),
        "edges": _tally(plan["edges"]),
    }
    await bus.emit(
        "market_thesis_import.completed", "market_thesis_import", import_id,
        {"mode": mode, "counts": counts,
         "inserted": {k: v["inserted"] for k, v in actions.items()},
         "warnings": len(warnings)},
    )
    return {
        "import_id": import_id,
        "mode": mode,
        "status": "completed",
        "bundle_sha256": sha,
        "idempotency_key": idem_key,
        "counts": counts,
        "actions": actions,
        "edge_kinds": plan["edge_kinds"],
        "warnings": warnings,
    }


# ---- CLI (10-market-thesis-data-bootstrap.md proposed commands) ----------------
# .venv/bin/python -m app.institute.market_thesis_import market-thesis-data/bundle.json --dry-run
# Writes to the configured INSTITUTE_HOME database — point INSTITUTE_HOME at a
# scratch dir first when experimenting.

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Import a market-thesis-data bundle")
    parser.add_argument("bundle", help="path to bundle.json")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="validate + report only (default)")
    group.add_argument("--apply", action="store_true", help="write lanes/theses/securities/edges")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"

    async def _run() -> dict[str, Any]:
        await db.init()
        try:
            return await import_bundle(args.bundle, mode=mode)
        finally:
            await db.close()

    print(_dumps(asyncio.run(_run())))


if __name__ == "__main__":
    _cli()
