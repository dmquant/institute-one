"""Forecast ledger (card M5-001).

A forecast is a falsifiable call bound to a thesis: claim + direction +
horizon + a deterministic ``settlement_rule`` (canonical JSON, validated by
``parse_settlement_rule``). Settlement prices come exclusively from the local
PIT store (``market_data.get_bars_pit`` / ``get_marks_pit``, card M4-001) so
"what would we have known" stays answerable; adjusted closes
(close * adj_factor) are used so corporate actions do not fake returns.

Rule grammar (the only two types at launch; open set, domain-validated —
additive migrations cannot widen a CHECK):

    {"type": "absolute_move",      "threshold": 0.05}
    {"type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "CSI300"}

``threshold`` is a finite return fraction (> 0). The measured quantity is the
security's window return (absolute_move) or its excess over the benchmark's
window return (price_vs_benchmark), where the window is made_at → expires_at.
Verdict, given ``measured``:

    long     signed = measured;  short  signed = -measured
             hit if signed >= threshold, miss if signed <= 0, else partial
    neutral  hit if |measured| <= threshold, else miss (no partial band)

KNOWLEDGE-TIME SEMANTICS (the two legs deliberately differ):

    entry    the last value at or before made_at's calendar date **as known
             at made_at** (PIT ``as_of = made_at``). The basis is frozen at
             the forecaster's knowledge: a close published or a correction
             ingested after made_at can NEVER rewrite the entry — that would
             be look-ahead bias, judging the call against a price the
             forecaster could not have known.
    exit     the last value at or before expires_at's calendar date as known
             at the settlement's knowledge cutoff. The cutoff is FIXED BY THE
             SYSTEM at settle time (microsecond-precision now, the 0006
             version-key shape) and persisted to the settlement row as
             ``knowledge_as_of``; both exit-leg PIT reads receive it
             explicitly, so one settlement is computed from ONE consistent
             knowledge snapshot — a correction landing between the two reads
             can no longer split them. Callers cannot choose the cutoff of a
             state-changing settlement; historical replays go through the
             read-only ``preview_settlement`` (no writes, no status change).

BENCHMARK WINDOW ALIGNMENT: price_vs_benchmark measures the benchmark on
exactly the dates the security actually used — the marks whose mark_date
equals the security's entry/exit bar_date (entry leg as known at made_at,
exit leg at the knowledge cutoff). A benchmark that cannot be aligned to
those dates settles verdict='invalid' (fail closed), never a nearest-date
proxy series.

EVIDENCE CHAIN (0033): every settlement row records ``knowledge_as_of`` plus
the (date, as_known_at) version identity of each PIT row it used per leg, so
the verdict can be re-derived from the immutable version store at any time.
``preview_settlement(as_of=knowledge_as_of)`` reproduces it through the
PINNED path: the legs are fetched by their persisted version identities, NOT
by a PIT scan — a revision ingested later with a BACKDATED as_known_at can
rewrite what a PIT scan at that cutoff answers (the store accepts historical
as_known_at by design, for revision-stream backfills) but can never move a
pinned leg. Any other ``as_of`` (and every pre-0033 settlement, which has no
pinned identities) answers via the PIT scan — "what does the version store
currently say we knew at T" — and is therefore, deliberately and
documentedly, sensitive to backfilled revisions; the response's
``evidence_source`` field ('pinned' | 'pit') states which semantics
answered.

ORIGIN / BACKFILL (0033): ``origin`` marks how a row entered the ledger.
``create_forecast_public`` (the POST /api/forecasts boundary) only accepts a
made_at within now±24h; anything further must declare ``backfill=true`` and
is persisted with origin='backfill' — an accountability record excluded from
the DEFAULT ``list_forecasts`` scope (which the SPA/plugin hit-rate
aggregations consume) and never opened by the paper book.

FAILS CLOSED: any missing or unusable input — no security on the row, no
entry price known at made_at, no bar strictly after the entry date, an
entry/exit value that is not a positive finite number, a non-finite computed
return, unknown benchmark, missing/unusable/unalignable benchmark marks —
settles with verdict='invalid' and forecast status='invalid'. Settlement
never guesses and never substitutes a proxy series. Settling is a
conditional claim on status='open'; the claim and the settlement row commit
in one transaction, and UNIQUE(forecast_id) backstops double-settlement.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import bus, db
from ..util import new_id
from . import market_data

DIRECTIONS = {"long", "short", "neutral"}
RULE_TYPES = {"absolute_move", "price_vs_benchmark"}
STATUSES = ("open", "settled", "invalid")
VERDICTS = ("hit", "miss", "partial", "invalid")
ORIGINS = ("standard", "backfill")
# public-create tolerance: a made_at further than this from now needs the
# explicit backfill declaration (origin='backfill', excluded from stats)
MADE_AT_TOLERANCE = timedelta(hours=24)


class ForecastError(ValueError):
    """Validation failure (the API maps this to 400)."""


class TransitionConflict(ForecastError):
    """Conditional claim lost — the row changed under us (API maps to 409)."""


# ---- helpers ---------------------------------------------------------------

def _dumps(val: Any) -> str:
    # allow_nan=False: last line of defense for the canonical-JSON claim —
    # NaN/Infinity must never reach a stored settlement_rule
    return json.dumps(val, ensure_ascii=False, allow_nan=False)


def _norm_ts(val: Any, label: str) -> str:
    """Normalize an ISO timestamp (naive = UTC; bare date = 00:00 UTC) to the
    bus.now_iso() shape — seconds precision, +00:00 — so made_at/expires_at
    string comparisons equal time comparisons."""
    raw = str(val or "").strip()
    if not raw:
        raise ForecastError(f"{label} must be an ISO-8601 timestamp")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ForecastError(f"{label} {raw!r} is not ISO-8601") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_settlement_rule(rule: Any) -> dict[str, Any]:
    """Validate + normalize a settlement rule (JSON string or dict) into its
    canonical dict. Unknown types/keys and non-positive thresholds are
    rejected up front so an unsettleable rule can never enter the ledger."""
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except ValueError:
            raise ForecastError("settlement_rule must be valid JSON") from None
    if not isinstance(rule, dict):
        raise ForecastError("settlement_rule must be a JSON object")
    data = dict(rule)
    rtype = data.pop("type", None)
    if rtype not in RULE_TYPES:
        raise ForecastError(
            f"unknown settlement_rule type {rtype!r}; allowed: {', '.join(sorted(RULE_TYPES))}"
        )
    try:
        threshold_raw = data.pop("threshold")
    except KeyError:
        raise ForecastError("settlement_rule needs a threshold (return fraction > 0)") from None
    if isinstance(threshold_raw, bool):  # bool is an int subclass; True is not a threshold
        raise ForecastError("settlement_rule threshold must be a number")
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        raise ForecastError("settlement_rule threshold must be a number") from None
    # NaN/Inf slip past a bare `<= 0` (NaN compares false everywhere): whitelist
    # finite-and-positive so an unsettleable rule can never enter the ledger
    if not math.isfinite(threshold) or threshold <= 0:
        raise ForecastError("settlement_rule threshold must be a finite number > 0")
    out: dict[str, Any] = {"type": rtype, "threshold": threshold}
    if rtype == "price_vs_benchmark":
        benchmark_id = str(data.pop("benchmark_id", "") or "").strip()
        if not benchmark_id:
            raise ForecastError("price_vs_benchmark rule needs a benchmark_id")
        out["benchmark_id"] = benchmark_id
    if data:
        raise ForecastError(f"unknown settlement_rule fields: {', '.join(sorted(data))}")
    return out


def _forecast_out(row: dict[str, Any]) -> dict[str, Any]:
    fc = dict(row)
    try:
        fc["settlement_rule"] = json.loads(fc["settlement_rule"])
    except ValueError:  # defensive: rows are written canonical by create_forecast
        pass
    return fc


# ---- create ----------------------------------------------------------------

async def create_forecast_public(fields: dict[str, Any]) -> dict[str, Any]:
    """The POST /api/forecasts boundary: ``create_forecast`` plus the made_at
    integrity gate. A made_at further than MADE_AT_TOLERANCE from now is
    refused unless the caller declares ``backfill: true`` — the row is then
    persisted with origin='backfill' (excluded from the default list scope
    and the paper book). Trusted internal callers (extraction, tests) use
    ``create_forecast`` directly."""
    data = dict(fields or {})
    backfill = bool(data.pop("backfill", False))
    if backfill:
        data["origin"] = "backfill"
    elif data.get("made_at") is not None:
        made_at = _norm_ts(data["made_at"], "made_at")
        drift = abs(datetime.fromisoformat(made_at) - datetime.now(timezone.utc))
        if drift > MADE_AT_TOLERANCE:
            raise ForecastError(
                f"made_at {made_at} is outside now±24h; declare backfill=true to "
                "record it as a provenance-marked backfill (origin='backfill', "
                "excluded from performance stats)"
            )
    return await create_forecast(data)


async def create_forecast(fields: dict[str, Any], *, forecast_id: str | None = None) -> dict[str, Any]:
    """Record a forecast (M5-001 acceptance: thesis, claim, horizon, direction,
    and settlement_rule are all required). ``made_at`` defaults to now; an
    explicit value (import/backfill) is normalized to the house UTC shape and
    expires_at is always made_at + horizon_days. ``forecast_id`` lets the
    extraction pipeline pass its pre-generated deterministic id (exactly-once
    replay); it is a trusted kwarg, never a public field."""
    data = dict(fields or {})

    thesis_id = str(data.pop("thesis_id", "") or "").strip()
    if not thesis_id:
        raise ForecastError("a forecast needs a thesis_id")
    if not await db.query_one("SELECT id FROM theses WHERE id = ?", (thesis_id,)):
        raise ForecastError(f"thesis {thesis_id!r} not found")

    claim = str(data.pop("claim", "") or "").strip()
    if not claim:
        raise ForecastError("a forecast needs a claim")

    direction = data.pop("direction", None)
    if direction not in DIRECTIONS:
        raise ForecastError(
            f"unknown direction {direction!r}; allowed: {', '.join(sorted(DIRECTIONS))}"
        )

    horizon_raw = data.pop("horizon_days", None)
    try:
        horizon_f = float(horizon_raw)
    except (TypeError, ValueError):
        raise ForecastError("horizon_days must be a positive integer") from None
    if isinstance(horizon_raw, bool) or not math.isfinite(horizon_f):
        raise ForecastError("horizon_days must be a positive integer")
    horizon_days = int(horizon_f)
    if horizon_days != horizon_f or horizon_days <= 0:
        raise ForecastError("horizon_days must be a positive integer")

    if "settlement_rule" not in data:
        raise ForecastError("a forecast needs a settlement_rule")
    rule = parse_settlement_rule(data.pop("settlement_rule"))

    security_id = str(data.pop("security_id", "") or "").strip() or None
    # both launch rule types price the security itself, so they require one;
    # per-rule enforcement so a future thesis-level rule type can relax it
    if rule["type"] in ("absolute_move", "price_vs_benchmark") and not security_id:
        raise ForecastError(f"rule type {rule['type']!r} needs a security_id to price")
    if security_id and not await db.query_one(
        "SELECT id FROM securities WHERE id = ?", (security_id,)
    ):
        raise ForecastError(f"security {security_id!r} not found")

    conviction_raw = data.pop("conviction", None)
    conviction: float | None = None
    if conviction_raw is not None:
        if isinstance(conviction_raw, bool):
            raise ForecastError("conviction must be a number")
        try:
            conviction = float(conviction_raw)
        except (TypeError, ValueError):
            raise ForecastError("conviction must be a number") from None
        # NaN compares false against both bounds and would slip a bare range check
        if not math.isfinite(conviction) or not 0.0 <= conviction <= 1.0:
            raise ForecastError("conviction must be within 0..1")

    made_at = _norm_ts(data.pop("made_at", None) or bus.now_iso(), "made_at")

    origin = str(data.pop("origin", None) or "standard").strip()
    if origin not in ORIGINS:
        raise ForecastError(f"unknown origin {origin!r}; allowed: {', '.join(ORIGINS)}")

    if data:
        raise ForecastError(f"unknown forecast fields: {', '.join(sorted(data))}")

    expires_at = (
        datetime.fromisoformat(made_at) + timedelta(days=horizon_days)
    ).astimezone(timezone.utc).isoformat(timespec="seconds")

    fid = forecast_id or new_id()
    now = bus.now_iso()
    try:
        await db.execute(
            "INSERT INTO forecasts (id, thesis_id, security_id, claim, direction, conviction, "
            "horizon_days, settlement_rule, made_at, expires_at, status, origin, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?,?)",
            (fid, thesis_id, security_id, claim, direction, conviction, horizon_days,
             _dumps(rule), made_at, expires_at, origin, now, now),
        )
    except sqlite3.IntegrityError as exc:  # racing thesis/security delete slipped past the
        # pre-checks, or a replayed deterministic forecast_id already exists
        raise ForecastError(f"constraint failed: {exc}") from exc
    await bus.emit("forecast.created", "forecast", fid, {
        "thesis_id": thesis_id, "security_id": security_id, "direction": direction,
        "horizon_days": horizon_days, "rule_type": rule["type"], "origin": origin,
    })
    return await get_forecast(fid)  # type: ignore[return-value]


# ---- read ------------------------------------------------------------------

async def get_forecast(forecast_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM forecasts WHERE id = ?", (forecast_id,))
    if row is None:
        return None
    fc = _forecast_out(row)
    fc["settlement"] = await db.query_one(
        "SELECT * FROM forecast_settlements WHERE forecast_id = ?", (forecast_id,)
    )
    return fc


async def list_forecasts(
    status: str | None = None,
    thesis_id: str | None = None,
    limit: int = 100,
    origin: str | None = None,
) -> list[dict[str, Any]]:
    """List forecasts. ``origin=None`` (the default) is the PERFORMANCE scope:
    backfilled rows (origin='backfill') are excluded, so every consumer that
    aggregates hit rates over this list (SPA dashboard, Obsidian plugin) never
    counts backfill. Pass origin='backfill'/'standard' to filter exactly, or
    origin='all' for the unfiltered accountability view.

    Each row carries its ``settlement`` inlined (the full forecast_settlements
    row or None, the same shape as ``get_forecast``) via ONE batched IN query —
    list consumers (the SPA ledger's verdict badges, hit-rate aggregations)
    must not N+1 the detail endpoint."""
    if status and status not in STATUSES:
        raise ForecastError(f"unknown status {status!r}; allowed: {', '.join(STATUSES)}")
    if origin is not None and origin != "all" and origin not in ORIGINS:
        raise ForecastError(
            f"unknown origin {origin!r}; allowed: {', '.join(ORIGINS)}, all"
        )
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if thesis_id:
        clauses.append("thesis_id = ?")
        params.append(thesis_id)
    if origin is None:
        clauses.append("origin <> 'backfill'")  # default = performance scope
    elif origin != "all":
        clauses.append("origin = ?")
        params.append(origin)
    sql = "SELECT * FROM forecasts"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY made_at DESC, id LIMIT ?"
    params.append(min(max(limit, 1), 500))
    out = [_forecast_out(r) for r in await db.query(sql, params)]
    ids = [fc["id"] for fc in out]
    if ids:
        # UNIQUE(forecast_id) on forecast_settlements ⇒ at most one row per id
        marks = ",".join("?" for _ in ids)
        by_forecast = {
            s["forecast_id"]: s
            for s in await db.query(
                f"SELECT * FROM forecast_settlements WHERE forecast_id IN ({marks})", ids
            )
        }
        for fc in out:
            fc["settlement"] = by_forecast.get(fc["id"])
    return out


async def hit_rate_stats() -> dict[str, int]:
    """Settled-verdict counts over the PERFORMANCE scope (origin <> 'backfill',
    the same scope as the default list) — one aggregate for the SPA dashboard,
    which used to page every settled forecast and fetch each settlement row
    individually to compute this."""
    rows = await db.query(
        "SELECT s.verdict, COUNT(*) AS n "
        "FROM forecasts f JOIN forecast_settlements s ON s.forecast_id = f.id "
        "WHERE f.status = 'settled' AND f.origin <> 'backfill' GROUP BY s.verdict",
    )
    by_verdict = {str(r["verdict"]): int(r["n"]) for r in rows}
    return {
        "hits": by_verdict.get("hit", 0),
        "misses": by_verdict.get("miss", 0),
        "partial": by_verdict.get("partial", 0),
        "settled": sum(by_verdict.values()),
    }


# ---- vault projection --------------------------------------------------------

def _vault_inline(value: Any) -> str:
    return " ".join(str(value or "").split())


def _vault_return(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:+.2%}" if math.isfinite(number) else "—"


async def render_vault_history() -> tuple[str, int]:
    """Render the complete forecast ledger as deterministic markdown."""
    rows = await db.query(
        "SELECT f.*, t.name_zh AS thesis_name, "
        "s.name_zh AS security_name_zh, s.name_en AS security_name_en, "
        "fs.verdict AS settlement_verdict, fs.settled_at, "
        "fs.actual_return, fs.benchmark_return, fs.note AS settlement_note "
        "FROM forecasts f "
        "LEFT JOIN theses t ON t.id = f.thesis_id "
        "LEFT JOIN securities s ON s.id = f.security_id "
        "LEFT JOIN forecast_settlements fs ON fs.forecast_id = f.id "
        "ORDER BY f.made_at DESC, f.id"
    )
    lines = ["# 预测历史", "", f"> 共 {len(rows)} 条预测；按 made_at 倒序。", ""]
    for row in rows:
        try:
            rule = json.loads(row["settlement_rule"])
            rule_text = json.dumps(rule, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            rule_text = _vault_inline(row["settlement_rule"])
        security_name = row["security_name_zh"] or row["security_name_en"] or ""
        security = row["security_id"] or "—"
        if security_name:
            security += f"（{_vault_inline(security_name)}）"
        thesis_name = row["thesis_name"] or row["thesis_id"]
        conviction = "—" if row["conviction"] is None else f"{float(row['conviction']):.0%}"
        lines += [
            f"## {row['made_at'][:10]} · `{row['id']}`",
            "",
            _vault_inline(row["claim"]),
            "",
            f"- 状态：**{row['status']}**　方向：`{row['direction']}`　信念：{conviction}",
        ]
        if row["origin"] == "backfill":
            lines.append("- 来源：**backfill**（回填记录，不计入绩效统计）")
        lines += [
            f"- 标的：{security}",
            f"- 论点：{_vault_inline(thesis_name)}（`{row['thesis_id']}`）",
            f"- 时间：made_at `{row['made_at']}` → expires_at `{row['expires_at']}`"
            f"（{row['horizon_days']} 天）",
            f"- 结算规则：`{rule_text}`",
        ]
        if row["settlement_verdict"] is not None:
            lines += [
                f"- 结算：**{row['settlement_verdict']}** 于 `{row['settled_at']}`；"
                f"标的收益 {_vault_return(row['actual_return'])}；"
                f"基准收益 {_vault_return(row['benchmark_return'])}",
                f"- 结算说明：{_vault_inline(row['settlement_note']) or '—'}",
            ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", len(rows)


async def export_vault_history() -> dict[str, Any]:
    """Write ``Book/forecasts.md`` through VaultWriter managed regions."""
    from ..vault.writer import get_writer  # lazy: keep the ledger domain lightweight

    body, count = await render_vault_history()
    writer = get_writer()
    written = await writer.write_note(
        "Book/forecasts.md",
        {"type": "forecast-history"},
        body,
        artifact_kind="forecast-history",
        artifact_id="forecast-history",
        region=True,
    )
    return {"enabled": writer.enabled, "path": written, "count": count}


# ---- settlement ------------------------------------------------------------

def _adj_close(bar: dict[str, Any]) -> float:
    return float(bar["close"]) * float(bar.get("adj_factor") or 1.0)


def _usable_price(val: Any) -> bool:
    """Endpoint whitelist: a positive finite number. Zero/negative closes and
    marks are storable by design (0006 allows them — futures can print
    negative) but are NOT usable for a return denominator/numerator here;
    NaN/Infinity survive REAL columns and every comparison, so only an
    explicit isfinite gate keeps them out of a verdict."""
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return math.isfinite(val) and val > 0


def _window_return(
    entry_rows: list[dict[str, Any]],
    exit_rows: list[dict[str, Any]],
    date_key: str,
    value_of,
    label: str,
) -> tuple[float | None, str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Deterministic window return from two separately-frozen PIT snapshots:

    ``entry_rows``  dated <= made_date, **as known at made_at** — the entry is
                    the last row, i.e. the value standing at forecast time.
    ``exit_rows``   dated <= expires_date, as known at the settlement's
                    knowledge cutoff — the exit is the last row.

    Returns (return, why-invalid, entry_row, exit_row); the caller fails
    closed on a non-None ``why`` and records the selected endpoint rows as
    evidence whenever they exist (even for a fails-closed verdict). Every
    endpoint must pass the positive-finite whitelist and the computed return
    itself must be finite."""
    entry = entry_rows[-1] if entry_rows else None
    exit_row = exit_rows[-1] if exit_rows else None
    if entry is None:
        return None, f"{label}: no entry value known at made_at", entry, exit_row
    if exit_row is None:
        return None, f"{label}: no data in window", entry, exit_row
    if exit_row[date_key] <= entry[date_key]:
        return None, f"{label}: no value after entry date {entry[date_key]}", entry, exit_row
    try:
        entry_v, exit_v = value_of(entry), value_of(exit_row)
    except (TypeError, ValueError):
        return None, f"{label}: unusable entry/exit values", entry, exit_row
    if not _usable_price(entry_v):
        return None, f"{label}: unusable entry value at {entry[date_key]}", entry, exit_row
    if not _usable_price(exit_v):
        return None, f"{label}: unusable exit value at {exit_row[date_key]}", entry, exit_row
    ret = exit_v / entry_v - 1.0
    if not math.isfinite(ret):
        return None, f"{label}: computed return is not finite", entry, exit_row
    return ret, None, entry, exit_row


def _verdict(direction: str, measured: float, threshold: float) -> str:
    if direction == "neutral":
        return "hit" if abs(measured) <= threshold else "miss"
    signed = measured if direction == "long" else -measured
    if signed >= threshold:
        return "hit"
    if signed <= 0:
        return "miss"
    return "partial"


def _knowledge_cutoff() -> str:
    """The system-fixed settlement knowledge cutoff: microsecond-precision UTC
    now, the 0006 PIT version-key shape (bus.now_iso() is second-precision and
    would tie with same-second version keys at the string-comparison level)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


async def _aligned_benchmark_return(
    benchmark_id: str,
    entry_date: str,
    exit_date: str,
    made_at: str,
    knowledge_as_of: str,
) -> tuple[float | None, str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Benchmark window return aligned to the SECURITY's actual entry/exit
    dates: the entry mark must be dated exactly ``entry_date`` (as known at
    made_at — frozen, no look-ahead) and the exit mark exactly ``exit_date``
    (as known at the settlement cutoff). A benchmark with no usable mark on
    either exact date cannot be compared over the same window and fails
    closed — never a nearest-date proxy. Returns
    (return, why-invalid, entry_mark, exit_mark)."""
    label = f"benchmark {benchmark_id}"
    entry_marks = await market_data.get_marks_pit(
        benchmark_id, made_at, start=entry_date, end=entry_date
    )
    exit_marks = await market_data.get_marks_pit(
        benchmark_id, knowledge_as_of, start=exit_date, end=exit_date
    )
    entry = entry_marks[-1] if entry_marks else None
    exit_row = exit_marks[-1] if exit_marks else None
    if entry is None:
        return (None, f"{label}: no entry value known at made_at on security entry "
                f"date {entry_date} (window misaligned)", entry, exit_row)
    if exit_row is None:
        return (None, f"{label}: no exit value on security exit date {exit_date} "
                "(window misaligned)", entry, exit_row)
    try:
        entry_v, exit_v = float(entry["value"]), float(exit_row["value"])
    except (TypeError, ValueError):
        return None, f"{label}: unusable entry/exit values", entry, exit_row
    if not _usable_price(entry_v):
        return None, f"{label}: unusable entry value at {entry['mark_date']}", entry, exit_row
    if not _usable_price(exit_v):
        return None, f"{label}: unusable exit value at {exit_row['mark_date']}", entry, exit_row
    ret = exit_v / entry_v - 1.0
    if not math.isfinite(ret):
        return None, f"{label}: computed return is not finite", entry, exit_row
    return ret, None, entry, exit_row


def _leg(row: dict[str, Any] | None, date_key: str) -> tuple[str | None, str | None]:
    """(date, as_known_at) version identity of one PIT evidence row."""
    if row is None:
        return None, None
    return row.get(date_key), row.get("as_known_at")


def _conclude(
    fc: dict[str, Any],
    rule: dict[str, Any],
    actual_return: float | None,
    benchmark_return: float | None,
    problems: list[str],
) -> tuple[str, str]:
    """Shared settlement tail: measured -> (verdict, detail), failing closed
    on any recorded problem or a non-finite measure."""
    measured: float | None = None
    if not problems:
        measured = actual_return - benchmark_return if rule["type"] == "price_vs_benchmark" \
            else actual_return
        if not math.isfinite(measured):  # overflow of two huge finite returns
            problems.append("measured return is not finite")
            measured = None
    if problems:
        # fails closed: some required input is missing or unusable
        return "invalid", "; ".join(problems)
    return _verdict(fc["direction"], measured, rule["threshold"]), (
        f"measured={measured:+.6f} vs threshold={rule['threshold']} "
        f"({rule['type']}, direction={fc['direction']})"
    )


async def _evaluate_settlement(
    fc: dict[str, Any],
    rule: dict[str, Any],
    knowledge_as_of: str,
) -> dict[str, Any]:
    """Pure settlement computation against ONE knowledge snapshot.

    Entry legs are frozen at made_at (anti-look-ahead); BOTH exit legs read
    with the same explicit ``knowledge_as_of`` — all four PIT reads answer
    from one consistent point in time. The benchmark is aligned to the
    security's actual entry/exit dates (misalignment fails closed). Returns
    verdict/returns/detail plus the (date, as_known_at) version identity of
    every PIT row used, and writes NOTHING."""
    made_date, expires_date = fc["made_at"][:10], fc["expires_at"][:10]

    actual_return: float | None = None
    benchmark_return: float | None = None
    problems: list[str] = []
    entry_row = exit_row = bench_entry = bench_exit = None

    if not fc["security_id"]:
        problems.append("forecast has no security_id (deleted or never set); cannot price")
    else:
        # entry frozen at made_at knowledge; exit at the settlement cutoff
        entry_bars = await market_data.get_bars_pit(
            fc["security_id"], fc["made_at"], end=made_date
        )
        exit_bars = await market_data.get_bars_pit(
            fc["security_id"], knowledge_as_of, end=expires_date
        )
        actual_return, why, entry_row, exit_row = _window_return(
            entry_bars, exit_bars, "bar_date", _adj_close, f"security {fc['security_id']}"
        )
        if why:
            problems.append(why)

    if rule["type"] == "price_vs_benchmark":
        if not await db.query_one("SELECT id FROM benchmarks WHERE id = ?", (rule["benchmark_id"],)):
            problems.append(f"benchmark {rule['benchmark_id']!r} not found")
        elif actual_return is None:
            # without a priced security window there are no dates to align to;
            # the security problem above already fails the settlement closed
            problems.append(
                f"benchmark {rule['benchmark_id']}: security window unpriced; "
                "no dates to align to"
            )
        else:
            benchmark_return, why, bench_entry, bench_exit = await _aligned_benchmark_return(
                rule["benchmark_id"], entry_row["bar_date"], exit_row["bar_date"],
                fc["made_at"], knowledge_as_of,
            )
            if why:
                problems.append(why)

    verdict, detail = _conclude(fc, rule, actual_return, benchmark_return, problems)
    entry_date, entry_known = _leg(entry_row, "bar_date")
    exit_date, exit_known = _leg(exit_row, "bar_date")
    bench_entry_date, bench_entry_known = _leg(bench_entry, "mark_date")
    bench_exit_date, bench_exit_known = _leg(bench_exit, "mark_date")
    return {
        "verdict": verdict,
        "detail": detail,
        # on 'invalid' these keep whatever leg DID compute (0013 contract:
        # NULL only for what could not be computed)
        "actual_return": actual_return,
        "benchmark_return": benchmark_return,
        "knowledge_as_of": knowledge_as_of,
        "entry_bar_date": entry_date,
        "entry_as_known_at": entry_known,
        "exit_bar_date": exit_date,
        "exit_as_known_at": exit_known,
        "bench_entry_date": bench_entry_date,
        "bench_entry_as_known_at": bench_entry_known,
        "bench_exit_date": bench_exit_date,
        "bench_exit_as_known_at": bench_exit_known,
    }


async def _pinned_bar(security_id: str, bar_date: str, as_known_at: str) -> dict[str, Any] | None:
    """The exact price_bars version row a settlement leg recorded (0006
    version key: security/freq/date/as_known_at; settlement always reads the
    default daily frequency)."""
    return await db.query_one(
        "SELECT * FROM price_bars WHERE security_id = ? AND freq = '1d' "
        "AND bar_date = ? AND as_known_at = ?",
        (security_id, bar_date, as_known_at),
    )


async def _pinned_mark(benchmark_id: str, mark_date: str, as_known_at: str) -> dict[str, Any] | None:
    return await db.query_one(
        "SELECT * FROM benchmark_marks WHERE benchmark_id = ? AND mark_date = ? "
        "AND as_known_at = ?",
        (benchmark_id, mark_date, as_known_at),
    )


async def _evaluate_pinned(
    fc: dict[str, Any],
    rule: dict[str, Any],
    settlement: dict[str, Any],
) -> dict[str, Any]:
    """Replay a settlement from its PERSISTED per-leg version identities.

    Unlike the PIT scan (MAX(as_known_at) <= cutoff), the pinned path fetches
    exactly the version rows the settlement recorded — a revision ingested
    later with a backdated ``as_known_at`` can change what the scan answers
    for the same cutoff, but it can never move a pinned leg. A leg the
    settlement never resolved (NULL identity — fails-closed verdicts) or a
    pinned row no longer present (security/benchmark deleted; version rows
    cascade) reproduces a fails-closed 'invalid'. Writes nothing."""
    problems: list[str] = []

    async def leg(fetch, owner: str, date_val: Any, known: Any, label: str):
        if not date_val or not known:
            problems.append(f"{label}: leg not pinned on the settlement row")
            return None
        row = await fetch(owner, date_val, known)
        if row is None:
            problems.append(
                f"{label}: pinned version ({date_val}, {known}) is missing from the store"
            )
        return row

    entry_row = exit_row = bench_entry = bench_exit = None
    actual_return: float | None = None
    benchmark_return: float | None = None

    if not fc["security_id"]:
        problems.append("forecast has no security_id (deleted or never set); cannot replay")
    else:
        label = f"security {fc['security_id']}"
        entry_row = await leg(_pinned_bar, fc["security_id"],
                              settlement["entry_bar_date"],
                              settlement["entry_as_known_at"], label)
        exit_row = await leg(_pinned_bar, fc["security_id"],
                             settlement["exit_bar_date"],
                             settlement["exit_as_known_at"], label)
        if entry_row is not None and exit_row is not None:
            actual_return, why, _, _ = _window_return(
                [entry_row], [exit_row], "bar_date", _adj_close, label)
            if why:
                problems.append(why)

    if rule["type"] == "price_vs_benchmark":
        label = f"benchmark {rule['benchmark_id']}"
        bench_entry = await leg(_pinned_mark, rule["benchmark_id"],
                                settlement["bench_entry_date"],
                                settlement["bench_entry_as_known_at"], label)
        bench_exit = await leg(_pinned_mark, rule["benchmark_id"],
                               settlement["bench_exit_date"],
                               settlement["bench_exit_as_known_at"], label)
        if bench_entry is not None and bench_exit is not None:
            benchmark_return, why, _, _ = _window_return(
                [bench_entry], [bench_exit], "mark_date",
                lambda m: float(m["value"]), label)
            if why:
                problems.append(why)

    verdict, detail = _conclude(fc, rule, actual_return, benchmark_return, problems)
    return {
        "verdict": verdict,
        "detail": detail,
        "actual_return": actual_return,
        "benchmark_return": benchmark_return,
        "knowledge_as_of": settlement["knowledge_as_of"],
        "entry_bar_date": settlement["entry_bar_date"],
        "entry_as_known_at": settlement["entry_as_known_at"],
        "exit_bar_date": settlement["exit_bar_date"],
        "exit_as_known_at": settlement["exit_as_known_at"],
        "bench_entry_date": settlement["bench_entry_date"],
        "bench_entry_as_known_at": settlement["bench_entry_as_known_at"],
        "bench_exit_date": settlement["bench_exit_date"],
        "bench_exit_as_known_at": settlement["bench_exit_as_known_at"],
    }


async def settle_forecast(
    forecast_id: str,
    *,
    note: str = "",
) -> dict[str, Any] | None:
    """Settle one expired forecast against the PIT store.

    Knowledge times (see the module docstring): the ENTRY legs are always
    read with ``as_of = made_at`` — the basis is frozen at what the
    forecaster could know, so later publications/corrections of entry-date
    data can never rewrite it (no look-ahead). The EXIT legs are read at the
    settlement knowledge cutoff, which is FIXED BY THE SYSTEM (microsecond
    now) — callers cannot supply it — persisted to the settlement row as
    ``knowledge_as_of``, and passed explicitly to both exit reads so the
    whole settlement is one consistent snapshot. Historical replays are the
    read-only ``preview_settlement``. Missing/unusable/misaligned data fails
    closed to verdict='invalid' — never a guess, never a proxy. The status
    flip is a conditional claim on 'open' committed atomically with the
    settlement row.
    """
    fc = await db.query_one("SELECT * FROM forecasts WHERE id = ?", (forecast_id,))
    if fc is None:
        return None
    if fc["status"] != "open":
        raise TransitionConflict(f"forecast {forecast_id} is {fc['status']!r}, not open")
    now = bus.now_iso()
    if now < fc["expires_at"]:
        raise ForecastError(
            f"forecast {forecast_id} has not expired yet (expires_at {fc['expires_at']})"
        )
    rule = parse_settlement_rule(fc["settlement_rule"])
    knowledge_as_of = _knowledge_cutoff()
    ev = await _evaluate_settlement(fc, rule, knowledge_as_of)
    verdict = ev["verdict"]
    full_note = f"{note}; {ev['detail']}" if note else ev["detail"]

    new_status = "invalid" if verdict == "invalid" else "settled"
    sid = new_id()
    try:
        # claim + settlement row commit together: a lost claim rolls back the
        # insert, so double settlement is impossible (UNIQUE(forecast_id) backstops)
        async with db.transaction() as conn:  # NB: use conn directly; events after commit
            cur = await conn.execute(
                "UPDATE forecasts SET status = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                (new_status, now, forecast_id),
            )
            claimed = cur.rowcount
            await cur.close()
            if not claimed:
                raise TransitionConflict(
                    f"forecast {forecast_id} changed concurrently; reload and retry"
                )
            await conn.execute(
                "INSERT INTO forecast_settlements (id, forecast_id, verdict, settled_at, "
                "benchmark_return, actual_return, note, created_at, knowledge_as_of, "
                "entry_bar_date, entry_as_known_at, exit_bar_date, exit_as_known_at, "
                "bench_entry_date, bench_entry_as_known_at, bench_exit_date, "
                "bench_exit_as_known_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, forecast_id, verdict, now, ev["benchmark_return"], ev["actual_return"],
                 full_note, now, ev["knowledge_as_of"],
                 ev["entry_bar_date"], ev["entry_as_known_at"],
                 ev["exit_bar_date"], ev["exit_as_known_at"],
                 ev["bench_entry_date"], ev["bench_entry_as_known_at"],
                 ev["bench_exit_date"], ev["bench_exit_as_known_at"]),
            )
    except sqlite3.IntegrityError as exc:  # settlement row already exists (defensive)
        raise TransitionConflict(f"forecast {forecast_id} is already settled") from exc
    await bus.emit("forecast.settled", "forecast", forecast_id, {
        "verdict": verdict, "actual_return": ev["actual_return"],
        "benchmark_return": ev["benchmark_return"], "rule_type": rule["type"],
        "knowledge_as_of": ev["knowledge_as_of"],
    })
    return await get_forecast(forecast_id)


async def preview_settlement(
    forecast_id: str,
    *,
    as_of: str | None = None,
) -> dict[str, Any] | None:
    """READ-ONLY settlement replay: compute what settlement would say with the
    knowledge available at ``as_of`` (None = now). Writes nothing, changes no
    status, emits no event — works on open, settled and invalid forecasts
    alike. Pre-expiry previews measure the window as known so far
    (``expired`` reports which case this is).

    Two evidence semantics (``evidence_source`` states which one answered):

    ``pinned``  ``as_of`` equals the persisted settlement's
                ``knowledge_as_of``: the legs are fetched by their recorded
                version identities — this REPRODUCES the settlement exactly
                and is immune to revisions ingested later, even ones carrying
                a backdated as_known_at.
    ``pit``     any other ``as_of`` (or a pre-0033 settlement with no pinned
                identities): a PIT scan of the version store as it stands
                NOW — the honest answer to "what does the store currently
                say we knew at T", which a backfilled revision stream is
                allowed to change (that is what backfilled revisions are
                for). Use the pinned path to audit a settlement, the PIT path
                to explore counterfactual cutoffs."""
    fc = await db.query_one("SELECT * FROM forecasts WHERE id = ?", (forecast_id,))
    if fc is None:
        return None
    if as_of is not None:
        # normalize here so a malformed as_of is a readable 400, not a
        # MarketDataError escaping through the forecast API as a 500
        try:
            as_of = market_data._norm_ts(as_of, "as_of")
        except market_data.MarketDataError as exc:
            raise ForecastError(str(exc)) from exc
    rule = parse_settlement_rule(fc["settlement_rule"])
    settlement = await db.query_one(
        "SELECT * FROM forecast_settlements WHERE forecast_id = ?", (forecast_id,))
    pinned = (
        settlement is not None
        and as_of is not None
        and settlement["knowledge_as_of"] is not None
        and settlement["knowledge_as_of"] == as_of
    )
    if pinned:
        ev = await _evaluate_pinned(fc, rule, settlement)
    else:
        ev = await _evaluate_settlement(fc, rule, as_of or _knowledge_cutoff())
    return {
        "preview": True,
        "forecast_id": forecast_id,
        "status": fc["status"],
        "expired": fc["expires_at"] <= bus.now_iso(),
        "evidence_source": "pinned" if pinned else "pit",
        "note": ev.pop("detail"),
        **ev,
    }
