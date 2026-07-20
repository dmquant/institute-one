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
             at settlement time (PIT ``as_of = None`` = latest, or the
             explicit ``as_of`` for a replay). Settlement is an after-the-fact
             act: measuring the OUTCOME with current knowledge — including
             post-period corrections of exit-leg data — is legitimate and
             intended.

FAILS CLOSED: any missing or unusable input — no security on the row, no
entry price known at made_at, no bar strictly after the entry date, an
entry/exit value that is not a positive finite number, a non-finite computed
return, unknown benchmark, missing/unusable benchmark marks — settles with
verdict='invalid' and forecast status='invalid'. Settlement never guesses
and never substitutes a proxy series. Settling is a conditional claim on
status='open'; the claim and the settlement row commit in one transaction,
and UNIQUE(forecast_id) backstops double-settlement.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import bus, db
from . import market_data

DIRECTIONS = {"long", "short", "neutral"}
RULE_TYPES = {"absolute_move", "price_vs_benchmark"}
STATUSES = ("open", "settled", "invalid")
VERDICTS = ("hit", "miss", "partial", "invalid")


class ForecastError(ValueError):
    """Validation failure (the API maps this to 400)."""


class TransitionConflict(ForecastError):
    """Conditional claim lost — the row changed under us (API maps to 409)."""


# ---- helpers ---------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


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

async def create_forecast(fields: dict[str, Any]) -> dict[str, Any]:
    """Record a forecast (M5-001 acceptance: thesis, claim, horizon, direction,
    and settlement_rule are all required). ``made_at`` defaults to now; an
    explicit value (import/backfill) is normalized to the house UTC shape and
    expires_at is always made_at + horizon_days."""
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
    if data:
        raise ForecastError(f"unknown forecast fields: {', '.join(sorted(data))}")

    expires_at = (
        datetime.fromisoformat(made_at) + timedelta(days=horizon_days)
    ).astimezone(timezone.utc).isoformat(timespec="seconds")

    fid = _new_id()
    now = bus.now_iso()
    try:
        await db.execute(
            "INSERT INTO forecasts (id, thesis_id, security_id, claim, direction, conviction, "
            "horizon_days, settlement_rule, made_at, expires_at, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?)",
            (fid, thesis_id, security_id, claim, direction, conviction, horizon_days,
             _dumps(rule), made_at, expires_at, now, now),
        )
    except sqlite3.IntegrityError as exc:  # racing thesis/security delete slipped past the pre-checks
        raise ForecastError(f"constraint failed: {exc}") from exc
    await bus.emit("forecast.created", "forecast", fid, {
        "thesis_id": thesis_id, "security_id": security_id, "direction": direction,
        "horizon_days": horizon_days, "rule_type": rule["type"],
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
) -> list[dict[str, Any]]:
    if status and status not in STATUSES:
        raise ForecastError(f"unknown status {status!r}; allowed: {', '.join(STATUSES)}")
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if thesis_id:
        clauses.append("thesis_id = ?")
        params.append(thesis_id)
    sql = "SELECT * FROM forecasts"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY made_at DESC, id LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return [_forecast_out(r) for r in await db.query(sql, params)]


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
) -> tuple[float | None, str | None]:
    """Deterministic window return from two separately-frozen PIT snapshots:

    ``entry_rows``  dated <= made_date, **as known at made_at** — the entry is
                    the last row, i.e. the value standing at forecast time.
    ``exit_rows``   dated <= expires_date, as known at settlement time — the
                    exit is the last row.

    Returns (return, None) or (None, why-invalid); the caller fails closed on
    the latter. Every endpoint must pass the positive-finite whitelist and the
    computed return itself must be finite."""
    if not entry_rows:
        return None, f"{label}: no entry value known at made_at"
    entry = entry_rows[-1]
    if not exit_rows:
        return None, f"{label}: no data in window"
    exit_row = exit_rows[-1]
    if exit_row[date_key] <= entry[date_key]:
        return None, f"{label}: no value after entry date {entry[date_key]}"
    try:
        entry_v, exit_v = value_of(entry), value_of(exit_row)
    except (TypeError, ValueError):
        return None, f"{label}: unusable entry/exit values"
    if not _usable_price(entry_v):
        return None, f"{label}: unusable entry value at {entry[date_key]}"
    if not _usable_price(exit_v):
        return None, f"{label}: unusable exit value at {exit_row[date_key]}"
    ret = exit_v / entry_v - 1.0
    if not math.isfinite(ret):
        return None, f"{label}: computed return is not finite"
    return ret, None


def _verdict(direction: str, measured: float, threshold: float) -> str:
    if direction == "neutral":
        return "hit" if abs(measured) <= threshold else "miss"
    signed = measured if direction == "long" else -measured
    if signed >= threshold:
        return "hit"
    if signed <= 0:
        return "miss"
    return "partial"


async def settle_forecast(
    forecast_id: str,
    *,
    as_of: str | None = None,
    note: str = "",
) -> dict[str, Any] | None:
    """Settle one expired forecast against the PIT store.

    Knowledge times (see the module docstring): the ENTRY leg is always read
    with ``as_of = made_at`` — the basis is frozen at what the forecaster
    could know, so later publications/corrections of entry-date data can
    never rewrite it (no look-ahead). The EXIT leg is read with the caller's
    ``as_of`` (None = latest known): settlement is an after-the-fact act and
    measures the outcome with current knowledge; an explicit ``as_of``
    replays settlement as of that time. Missing/unusable data fails closed to
    verdict='invalid' — never a guess, never a proxy. The status flip is a
    conditional claim on 'open' committed atomically with the settlement row.
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
    if as_of is not None:
        # normalize here so a malformed as_of is a readable 400, not a
        # MarketDataError escaping through the forecast API as a 500
        try:
            as_of = market_data._norm_ts(as_of, "as_of")
        except market_data.MarketDataError as exc:
            raise ForecastError(str(exc)) from exc
    rule = parse_settlement_rule(fc["settlement_rule"])
    made_date, expires_date = fc["made_at"][:10], fc["expires_at"][:10]

    actual_return: float | None = None
    benchmark_return: float | None = None
    problems: list[str] = []

    if not fc["security_id"]:
        problems.append("forecast has no security_id (deleted or never set); cannot price")
    else:
        # entry frozen at made_at knowledge; exit at settlement knowledge
        entry_bars = await market_data.get_bars_pit(
            fc["security_id"], fc["made_at"], end=made_date
        )
        exit_bars = await market_data.get_bars_pit(fc["security_id"], as_of, end=expires_date)
        actual_return, why = _window_return(
            entry_bars, exit_bars, "bar_date", _adj_close, f"security {fc['security_id']}"
        )
        if why:
            problems.append(why)

    if rule["type"] == "price_vs_benchmark":
        if not await db.query_one("SELECT id FROM benchmarks WHERE id = ?", (rule["benchmark_id"],)):
            problems.append(f"benchmark {rule['benchmark_id']!r} not found")
        else:
            entry_marks = await market_data.get_marks_pit(
                rule["benchmark_id"], fc["made_at"], end=made_date
            )
            exit_marks = await market_data.get_marks_pit(
                rule["benchmark_id"], as_of, end=expires_date
            )
            benchmark_return, why = _window_return(
                entry_marks, exit_marks, "mark_date", lambda m: float(m["value"]),
                f"benchmark {rule['benchmark_id']}",
            )
            if why:
                problems.append(why)

    measured: float | None = None
    if not problems:
        measured = actual_return - benchmark_return if rule["type"] == "price_vs_benchmark" \
            else actual_return
        if not math.isfinite(measured):  # overflow of two huge finite returns
            problems.append("measured return is not finite")
            measured = None

    if problems:
        verdict = "invalid"  # fails closed: some required input is missing or unusable
        detail = "; ".join(problems)
    else:
        verdict = _verdict(fc["direction"], measured, rule["threshold"])
        detail = (
            f"measured={measured:+.6f} vs threshold={rule['threshold']} "
            f"({rule['type']}, direction={fc['direction']})"
        )
    full_note = f"{note}; {detail}" if note else detail

    new_status = "invalid" if verdict == "invalid" else "settled"
    sid = _new_id()
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
                "benchmark_return, actual_return, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (sid, forecast_id, verdict, now, benchmark_return, actual_return, full_note, now),
            )
    except sqlite3.IntegrityError as exc:  # settlement row already exists (defensive)
        raise TransitionConflict(f"forecast {forecast_id} is already settled") from exc
    await bus.emit("forecast.settled", "forecast", forecast_id, {
        "verdict": verdict, "actual_return": actual_return,
        "benchmark_return": benchmark_return, "rule_type": rule["type"],
    })
    return await get_forecast(forecast_id)
