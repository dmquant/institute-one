"""Security master (card M2-001).

Canonical ids carry a market suffix (``600519.CN_A``, ``0700.HK``, ``NVDA.US``)
so one ticker can exist on several exchanges without ambiguity. Aliases map
Chinese names, unsuffixed tickers, and bundle ids back to the canonical row.
market-thesis-data market labels normalize via :func:`normalize_market`.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .. import bus, db

MARKETS = {"CN_A", "HK", "US", "KR", "JP"}
INSTRUMENT_TYPES = {"stock", "etf", "adr"}
ALIAS_KINDS = {"name_zh", "name_en", "ticker", "bundle_id", "other"}

# market-thesis-data market labels -> (market, instrument_type)
_MARKET_LABELS = {
    "US": ("US", "stock"),
    "US ETF": ("US", "etf"),
    "US ADR": ("US", "adr"),
    "A-share": ("CN_A", "stock"),
    "A-share ETF": ("CN_A", "etf"),
    "HK": ("HK", "stock"),
    "HK ETF": ("HK", "etf"),
    "Korea": ("KR", "stock"),
    "Japan": ("JP", "stock"),
}


class SecurityError(ValueError):
    """Validation failure (the API maps this to 400)."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def normalize_market(label: str) -> tuple[str, str]:
    """Map a market-thesis-data market label to (market, instrument_type)."""
    normalized = " ".join(str(label or "").split())
    if normalized in _MARKET_LABELS:
        return _MARKET_LABELS[normalized]
    if normalized.upper() in MARKETS:
        return normalized.upper(), "stock"
    raise SecurityError(f"unknown market label {label!r}")


def canonical_id(ticker: str, market: str) -> str:
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise SecurityError("a security needs a ticker")
    if market not in MARKETS:
        raise SecurityError(f"unknown market {market!r}; allowed: {', '.join(sorted(MARKETS))}")
    return f"{ticker}.{market}"


def _out(row: dict[str, Any]) -> dict[str, Any]:
    sec = dict(row)
    try:
        meta = json.loads(sec.pop("meta_json", None) or "{}")
    except ValueError:
        meta = {}
    sec["meta"] = meta if isinstance(meta, dict) else {}
    return sec


# ---- CRUD ----------------------------------------------------------------

async def upsert_security(
    ticker: str,
    market: str,
    *,
    instrument_type: str = "stock",
    name: str = "",
    name_en: str = "",
    meta: dict[str, Any] | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Create the canonical row, or update name/meta on an existing one."""
    if instrument_type not in INSTRUMENT_TYPES:
        raise SecurityError(
            f"unknown instrument_type {instrument_type!r}; allowed: {', '.join(sorted(INSTRUMENT_TYPES))}"
        )
    sec_id = canonical_id(ticker, market)
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    now = bus.now_iso()
    created = False
    # check + write under one write lock so concurrent upserts cannot both insert
    async with db.transaction() as conn:
        cur = await conn.execute("SELECT * FROM securities WHERE id = ?", (sec_id,))
        existing = await cur.fetchone()
        await cur.close()
        if existing is None:
            created = True
            await conn.execute(
                "INSERT INTO securities (id, ticker, market, instrument_type, name, name_en, meta_json, "
                "source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sec_id, str(ticker).strip().upper(), market, instrument_type,
                 name.strip(), name_en.strip(), meta_json, source, now, now),
            )
        else:
            changed = (
                (name.strip() and name.strip() != existing["name"])
                or (name_en.strip() and name_en.strip() != existing["name_en"])
                or (meta is not None and meta_json != existing["meta_json"])
                or instrument_type != existing["instrument_type"]
            )
            if changed:
                await conn.execute(
                    "UPDATE securities SET instrument_type=?, name=?, name_en=?, meta_json=?, updated_at=? WHERE id=?",
                    (instrument_type, name.strip() or existing["name"],
                     name_en.strip() or existing["name_en"],
                     meta_json if meta is not None else existing["meta_json"], now, sec_id),
                )
    if created:  # emit outside the transaction (emit writes the events table)
        await bus.emit("security.created", "security", sec_id, {"market": market, "type": instrument_type})
    return _out(await db.query_one("SELECT * FROM securities WHERE id = ?", (sec_id,)))  # type: ignore[arg-type]


async def get_security(security_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM securities WHERE id = ?", (security_id,))
    if row is None:
        return None
    sec = _out(row)
    sec["aliases"] = await db.query(
        "SELECT * FROM security_aliases WHERE security_id = ? ORDER BY kind, alias", (security_id,)
    )
    return sec


async def list_securities(
    market: str | None = None, instrument_type: str | None = None, search: str | None = None
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if market:
        clauses.append("market = ?")
        params.append(market)
    if instrument_type:
        clauses.append("instrument_type = ?")
        params.append(instrument_type)
    if search:
        like = f"%{search}%"
        clauses.append("(id LIKE ? OR ticker LIKE ? OR name LIKE ? OR name_en LIKE ?)")
        params.extend([like, like, like, like])
    sql = "SELECT * FROM securities"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY market, ticker"
    return [_out(r) for r in await db.query(sql, params)]


async def add_alias(security_id: str, alias: str, kind: str = "other") -> dict[str, Any] | None:
    if kind not in ALIAS_KINDS:
        raise SecurityError(f"unknown alias kind {kind!r}; allowed: {', '.join(sorted(ALIAS_KINDS))}")
    alias = str(alias or "").strip()
    if not alias:
        raise SecurityError("alias cannot be empty")
    sec = await db.query_one("SELECT id FROM securities WHERE id = ?", (security_id,))
    if sec is None:
        return None
    await db.execute(
        "INSERT OR IGNORE INTO security_aliases (id, security_id, alias, kind, created_at) VALUES (?,?,?,?,?)",
        (_new_id(), security_id, alias, kind, bus.now_iso()),
    )
    return await db.query_one(
        "SELECT * FROM security_aliases WHERE security_id = ? AND kind = ? AND alias = ?",
        (security_id, kind, alias),
    )


async def find_security(query: str) -> dict[str, Any] | None:
    """Resolve a canonical id, ticker, name, or alias to one security row."""
    query = str(query or "").strip()
    if not query:
        return None
    row = await db.query_one("SELECT * FROM securities WHERE id = ?", (query.upper(),))
    if row:
        return _out(row)
    row = await db.query_one(
        "SELECT s.* FROM securities s JOIN security_aliases a ON a.security_id = s.id "
        "WHERE a.alias = ? ORDER BY s.id LIMIT 1",
        (query,),
    )
    if row:
        return _out(row)
    rows = await db.query(
        "SELECT * FROM securities WHERE ticker = ? OR name = ? OR name_en = ? ORDER BY id",
        (query.upper(), query, query),
    )
    return _out(rows[0]) if rows else None


# ---- thesis <-> security edges ---------------------------------------------

async def upsert_edge(
    thesis_id: str,
    security_id: str,
    *,
    role: str = "exposure",
    exposure: str = "",
    confidence: float | None = None,
    rationale: str = "",
    meta: dict[str, Any] | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    if "\x1f" in str(role or ""):  # checked pre-strip: Python strips \x1f as whitespace
        raise SecurityError("role contains a reserved control character")
    role = str(role or "exposure").strip() or "exposure"
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        raise SecurityError("confidence must be between 0 and 1")
    thesis = await db.query_one("SELECT id FROM theses WHERE id = ?", (thesis_id,))
    if thesis is None:
        raise SecurityError(f"unknown thesis {thesis_id!r}")
    sec = await db.query_one("SELECT id FROM securities WHERE id = ?", (security_id,))
    if sec is None:
        raise SecurityError(f"unknown security {security_id!r}")

    now = bus.now_iso()
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    created = False
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT * FROM thesis_security_edges WHERE thesis_id = ? AND security_id = ? AND role = ?",
            (thesis_id, security_id, role),
        )
        existing = await cur.fetchone()
        await cur.close()
        if existing is None:
            created = True
            edge_id = _new_id()
            await conn.execute(
                "INSERT INTO thesis_security_edges (id, thesis_id, security_id, role, exposure, confidence, "
                "rationale, meta_json, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (edge_id, thesis_id, security_id, role, exposure, confidence, rationale, meta_json, source, now, now),
            )
        else:
            edge_id = existing["id"]
            changed = (
                exposure != existing["exposure"] or confidence != existing["confidence"]
                or rationale != existing["rationale"]
                or (meta is not None and meta_json != existing["meta_json"])
            )
            if changed:
                # a changed edge belongs to whoever changed it: an operator edit
                # flips source to 'manual', which shields the row from later
                # bundle imports (see market_thesis_import._upsert_edge_row)
                await conn.execute(
                    "UPDATE thesis_security_edges SET exposure=?, confidence=?, rationale=?, meta_json=?, "
                    "source=?, updated_at=? WHERE id=?",
                    (exposure, confidence, rationale,
                     meta_json if meta is not None else existing["meta_json"], source, now, edge_id),
                )
    if created:
        await bus.emit("thesis.edge_added", "thesis", thesis_id, {"security_id": security_id, "role": role})
    return await db.query_one("SELECT * FROM thesis_security_edges WHERE id = ?", (edge_id,))  # type: ignore[return-value]


async def remove_edge(thesis_id: str, security_id: str, role: str | None = None) -> int:
    sql = "DELETE FROM thesis_security_edges WHERE thesis_id = ? AND security_id = ?"
    params: list[Any] = [thesis_id, security_id]
    if role:
        sql += " AND role = ?"
        params.append(role)
    return await db.execute(sql, params)
