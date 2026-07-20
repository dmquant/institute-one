"""Operator loop — ROADMAP Phase 6 first slice: action feeds + shadow router.

The institute starts managing itself; THE HUMAN GATE STAYS HUMAN. Iron rules
(ROADMAP Phase 6 / proposal §8.2 invariant), enforced here and locked by
tests/test_operator.py:

1. **Shadow mode first.** Every disposition this module records carries
   ``shadow=1``: it is a logged SUGGESTION. ``route_actions()`` never mutates
   the action row it routed, never touches prompts, schedules, workflows,
   weights, feature switches or vault content, and there is no code path in
   this module that writes ``shadow=0``.
2. **prompt/schedule territory never auto-acts.** Suggestions for kinds whose
   remediation lives in prompt/schedule space (``HUMAN_PINNED_KINDS``), and
   suggestions whose proposed disposition IS a prompt/schedule change
   (``HUMAN_PINNED_DISPOSITIONS``), are flagged ``human_pinned`` — a hard pin
   that must survive the eventual end of shadow mode.
3. **Proposals are approved by a human in the web UI only** — the explicit
   ``POST /api/operator/dispositions/{id}/approve`` endpoint (app/api/
   operator.py). Never via vault frontmatter, never via MCP. Approval itself
   is a bookkeeping act (it resolves the action row); it does not execute any
   system change either.
4. **The 0.7 confidence floor is a consumption gate, not a label**
   (REVIEW-C4 M1). Two layers (F3 P3-1): the ``low_confidence`` flag is a
   PROPOSAL-TIME cache (telemetry/UI only, frozen at routing time), while the
   gate itself lives in the approve endpoint, which re-checks the stored
   confidence against the LIVE floor at consumption time — raising the floor
   retroactively blocks older unflagged proposals; missing confidence never
   passes. The human path for below-floor suggestions is a manual PATCH on
   the action. The floor is live-tunable: admin_state key
   ``operator:confidence_floor`` (``get_confidence_floor()``;
   missing/corrupt row -> 0.7).

Feeds (bus handlers, registered by ``register()`` — mounting: PATCH-NOTES-C4):
- ``factcheck.disputed``  -> disputed_fact   (C1 is in flight; payload shape is
  treated as untrusted — see ``FACTCHECK_DISPUTED_EVENT``)
- ``task.failed``         -> failed_run      (skips the router's own
  classification tasks, or a failing hand would breed actions forever)
- ``workflow.failed``     -> failed_run
- ``scorecard.completed`` -> scorecard_anomaly when the false_complete rate
  crosses ``FALSE_COMPLETE_RATE_THRESHOLD`` over >= ``SCORECARD_MIN_SCANNED``
  judged tasks (B2's event, app/institute/scorecard.py)
plus ``sweep_vault_conflicts()``: a periodic sweep (not a bus feed) that turns
vault ledger conflict/drift into vault_conflict actions.

Idempotency: one live (open/in_progress) action per ``ref`` — feeds
check-then-insert and migrations/0018's partial unique index closes the await
race. Handlers never raise (the bus guards too; belt and braces). The
router's propose-once-per-loop claim is likewise DB-backed: migrations/0022's
partial unique index on ``(action_id, proposed_by)`` arbitrates concurrent
same-loop calls (REVIEW-C4 P2 / F3 NIT-3).

Scheduling (15-min fast tick + hourly deep tick, gated=True) is mounted by the
main agent — see PATCH-NOTES-C4.md; this module deliberately does not import
scheduler.py (the scorecard.py precedent).

Recipes (Phase 6 L item, minimal reuse loop — PATCH-NOTES-E7): a human-
APPROVED disposition can be promoted into a recipe
(``promote_disposition_to_recipe``); ``route_actions`` consults active recipes
before calling a model — a match records the suggestion directly (marked by
``recipe_id``, confidence inherited, ZERO model calls) and it remains
``shadow=1`` behind the same live confidence floor and human approval gate as
every model suggestion. Iron rules 1–4 are unchanged.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from ..vault.writer import (
    VaultWriter,
    _extract_region,
    _has_ownership,
    _read_exact,
    _sha_file,
    _sha_text,
    get_writer,
)

log = logging.getLogger("institute.operator")

# ---- vocabulary -------------------------------------------------------------

ACTION_KINDS = (
    "vault_conflict", "disputed_fact", "scorecard_anomaly",
    "failed_run", "cron_failure", "other",
)
ACTION_STATUSES = ("open", "in_progress", "done", "dismissed")
LIVE_STATUSES = ("open", "in_progress")

# What the router may propose. Anything else a model replies is recorded as
# 'unparsed' (never trusted downstream).
DISPOSITION_VOCAB = (
    "retry", "dismiss", "escalate", "investigate",
    "rebuild_note", "adjust_prompt", "adjust_schedule",
)

# ROADMAP Phase 6 "0.7 confidence floor" — a CONSUMPTION GATE, not a label
# (REVIEW-C4 M1): below-floor suggestions are stored as shadow telemetry
# (flagged low_confidence) but the approve endpoint refuses to consume them.
# The live value is the admin_state row; this is only the fallback default.
CONFIDENCE_FLOOR_DEFAULT = 0.7
CONFIDENCE_FLOOR_KEY = "operator:confidence_floor"

# Iron rule 2: prompt/schedule-change territory. scorecard_anomaly remediation
# is prompt space (the hand "completed" garbage — you fix the prompt, not the
# row); cron_failure remediation is schedule space. Both are pinned at the
# KIND level: even a high-confidence suggestion stays human_pinned.
HUMAN_PINNED_KINDS = frozenset({"scorecard_anomaly", "cron_failure"})
# ...and pinned at the DISPOSITION level regardless of kind.
HUMAN_PINNED_DISPOSITIONS = frozenset({"adjust_prompt", "adjust_schedule"})

# scorecard anomaly thresholds (module constants, not Settings — B1 §1 idiom)
FALSE_COMPLETE_RATE_THRESHOLD = 0.2
SCORECARD_MIN_SCANNED = 5      # below this a bad rate is noise, not an anomaly

DEFAULT_PRIORITY = {
    "scorecard_anomaly": 3, "cron_failure": 3,
    "vault_conflict": 2, "disputed_fact": 2,
    "failed_run": 1, "other": 1,
}

ROUTER_SOURCE = "operator-router"  # tasks.source for classification calls
ROUTER_TIMEOUT_S = 300

# C1 (fact-check) is being built in parallel; this is the agreed event name.
# If C1's PATCH-NOTES lands a different one, this constant is the one-line fix.
# Defensive by construction: if the event never fires, the handler never runs.
FACTCHECK_DISPUTED_EVENT = "factcheck.disputed"


# ---- actions ----------------------------------------------------------------

# Injection surface (REVIEW-C4 M3 / F3 P2-1): titles and refs come from
# untrusted event payloads and end up inside the router prompt, where the
# parser's protocol regexes are line-anchored. Folding to ONE printable line
# makes such text inert (it cannot contain a line break at all).
_CONTROL_OR_SEP = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _fold_line(text: str, cap: int) -> str:
    """One printable line: control/separator chars become spaces, whitespace
    runs collapse, length capped."""
    return " ".join(_CONTROL_OR_SEP.sub(" ", text).split())[:cap]


async def open_action(
    kind: str, ref: str, title: str, detail: str = "", priority: int | None = None,
) -> dict[str, Any]:
    """Open a kanban action; idempotent per ref: a live (open/in_progress)
    action with the same non-empty ref is returned instead of duplicated.

    Titles are folded to one line on the way in; refs are ``kind:<id>``
    one-liners, so a newline/control character in a ref is an injection
    attempt (F3 P2-1), not a legitimate id — refused outright (feeds swallow
    the ValueError through their own belts).

    Returns ``{"id": ..., "created": bool}``.
    """
    if kind not in ACTION_KINDS:
        raise ValueError(f"unknown action kind {kind!r}")
    if ref and _CONTROL_OR_SEP.search(ref):
        raise ValueError("control characters in action ref")
    if ref:
        live = await db.query_one(
            "SELECT id FROM operator_actions WHERE ref = ? AND status IN (?, ?)",
            (ref, *LIVE_STATUSES),
        )
        if live is not None:
            return {"id": live["id"], "created": False}
    now = bus.now_iso()
    try:
        action_id = await db.insert(
            "INSERT INTO operator_actions (kind, ref, title, detail, status, priority, created_at, updated_at) "
            "VALUES (?,?,?,?,'open',?,?,?)",
            (kind, ref, _fold_line(title, 200), detail[:2000],
             priority if priority is not None else DEFAULT_PRIORITY.get(kind, 1),
             now, now),
        )
    except sqlite3.IntegrityError:
        # lost the check-then-insert race to a concurrent feed — the partial
        # unique index (0018) guarantees a live row for this ref now exists
        live = await db.query_one(
            "SELECT id FROM operator_actions WHERE ref = ? AND status IN (?, ?)",
            (ref, *LIVE_STATUSES),
        )
        if live is None:  # pathological (e.g. CHECK violation) — surface it
            raise
        return {"id": live["id"], "created": False}
    return {"id": action_id, "created": True}


async def resolve_action(action_id: int, resolution: str) -> bool:
    """Human disposition: mark done. Conditional claim — only a live action
    can be resolved, so double-disposal loses cleanly (rowcount 0)."""
    now = bus.now_iso()
    n = await db.execute(
        "UPDATE operator_actions SET status='done', resolution=?, resolved_at=?, updated_at=? "
        "WHERE id=? AND status IN (?, ?)",
        (resolution, now, now, action_id, *LIVE_STATUSES),
    )
    return n > 0


async def dismiss_action(action_id: int, reason: str = "") -> bool:
    """Human disposition: dismiss (won't fix / noise). Conditional claim."""
    now = bus.now_iso()
    n = await db.execute(
        "UPDATE operator_actions SET status='dismissed', resolution=?, resolved_at=?, updated_at=? "
        "WHERE id=? AND status IN (?, ?)",
        (reason or "dismissed", now, now, action_id, *LIVE_STATUSES),
    )
    return n > 0


# ---- feeds (bus handlers — must never raise) --------------------------------

def _payload_str(payload: dict[str, Any], *keys: str, cap: int = 120) -> str:
    """First non-empty string under *keys, folded to one line (F3 P2-1: a
    newline inside an untrusted claim must not survive into a title)."""
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return _fold_line(v, cap)
    return ""


async def _on_factcheck_disputed(event: bus.Event) -> None:
    """C1's disputed-claim event -> disputed_fact action. Payload untrusted."""
    try:
        payload = event.payload if isinstance(event.payload, dict) else {}
        fact_ref = str(event.ref_id or payload.get("fact_id") or payload.get("claim_id") or "").strip()
        claim = _payload_str(payload, "claim", "statement", "summary", "text")
        analyst = _payload_str(payload, "analyst_id", "analyst", cap=40)
        title = f"Disputed fact: {claim}" if claim else f"Disputed fact {fact_ref or '(unknown ref)'}"
        detail_parts = [p for p in (
            f"claim: {claim}" if claim else "",
            f"analyst: {analyst}" if analyst else "",
            f"event: {event.type} #{event.id}",
        ) if p]
        await open_action(
            "disputed_fact",
            f"fact:{fact_ref}" if fact_ref else "",
            title, "\n".join(detail_parts),
        )
    except Exception:  # noqa: BLE001 - feeds must never break the emitter
        log.exception("factcheck.disputed feed failed for event %s", event.id)


async def _on_task_failed(event: bus.Event) -> None:
    """task.failed -> failed_run action (one live action per task id)."""
    try:
        task_id = str(event.ref_id or "").strip()
        if not task_id:
            return
        row = await db.query_one(
            "SELECT hand, requested_hand, source, error FROM tasks WHERE id = ?", (task_id,)
        )
        source = (row or {}).get("source") or ""
        if source == ROUTER_SOURCE:
            # never file actions about our own classification calls: a failing
            # hand would otherwise breed one action per routing attempt
            return
        hand = (row or {}).get("hand") or (row or {}).get("requested_hand") or "?"
        await open_action(
            "failed_run", f"task:{task_id}",
            f"Task failed: {source or 'unknown'}/{hand} ({task_id})",
            ((row or {}).get("error") or "")[:1000],
        )
    except Exception:  # noqa: BLE001
        log.exception("task.failed feed failed for event %s", event.id)


async def _on_workflow_failed(event: bus.Event) -> None:
    """workflow.failed -> failed_run action (one live action per run id)."""
    try:
        run_id = str(event.ref_id or "").strip()
        if not run_id:
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        workflow_id = _payload_str(payload, "workflow_id", cap=60) or "unknown"
        await open_action(
            "failed_run", f"workflow:{run_id}",
            f"Workflow failed: {workflow_id} (run {run_id})",
            f"workflow_id: {workflow_id}\nsession_id: {payload.get('session_id') or ''}",
        )
    except Exception:  # noqa: BLE001
        log.exception("workflow.failed feed failed for event %s", event.id)


async def _on_scorecard_completed(event: bus.Event) -> None:
    """scorecard.completed -> scorecard_anomaly when the false_complete rate
    crosses the threshold over a non-noise sample (B2 payload shape:
    {"date", "scanned", "verdicts": {"ok","stub","false_complete"}, ...})."""
    try:
        payload = event.payload if isinstance(event.payload, dict) else {}
        verdicts = payload.get("verdicts")
        if not isinstance(verdicts, dict):
            return
        try:
            scanned = int(payload.get("scanned") or 0)
            false_complete = int(verdicts.get("false_complete") or 0)
        except (TypeError, ValueError):
            return
        if scanned < SCORECARD_MIN_SCANNED:
            return
        rate = false_complete / scanned
        if rate <= FALSE_COMPLETE_RATE_THRESHOLD:
            return
        date = str(payload.get("date") or event.ref_id or "").strip() or "unknown"
        await open_action(
            "scorecard_anomaly", f"scorecard:{date}",
            f"Scorecard anomaly {date}: false_complete rate {rate:.0%}",
            f"scanned: {scanned}\nverdicts: {verdicts}\n"
            f"threshold: {FALSE_COMPLETE_RATE_THRESHOLD:.0%} over >= {SCORECARD_MIN_SCANNED} tasks",
        )
    except Exception:  # noqa: BLE001
        log.exception("scorecard.completed feed failed for event %s", event.id)


_registered = False


def register() -> None:
    """Hook the feeds onto the bus. Idempotent (safe to call more than once);
    mounted once from the app lifespan — see PATCH-NOTES-C4.md."""
    global _registered
    if _registered:
        return
    bus.on(FACTCHECK_DISPUTED_EVENT, _on_factcheck_disputed)
    bus.on("task.failed", _on_task_failed)
    bus.on("workflow.failed", _on_workflow_failed)
    bus.on("scorecard.completed", _on_scorecard_completed)
    _registered = True
    log.info("operator feeds registered")


# ---- vault-conflict sweep ----------------------------------------------------

async def _nonclean_vault_rows(writer: VaultWriter) -> list[tuple[str, str]]:
    """Per-path states doctor() only counts: [(path, conflict|drifted|missing)].

    Mirrors writer.doctor()'s classification exactly (same check order) using
    the writer's own helpers, because doctor() returns counts without refs and
    actions need refs. Follow-up card: a doctor(detail=True) would remove this
    mirror (noted in PATCH-NOTES-C4.md).
    """
    assert writer.root is not None
    out: list[tuple[str, str]] = []
    for r in await db.query("SELECT path, sha256, state, mode FROM vault_index"):
        path = writer.root / r["path"]
        if not path.exists():
            out.append((r["path"], "missing"))
        elif r["state"] == "conflict":
            out.append((r["path"], "conflict"))
        elif r["mode"] == "region":
            text = _read_exact(path)
            if text is None:
                out.append((r["path"], "missing" if not path.exists() else "drifted"))
                continue
            region = _extract_region(text)
            if region is None or _sha_text(region) != r["sha256"] or not _has_ownership(text):
                out.append((r["path"], "drifted"))
        elif _sha_file(path) != r["sha256"]:
            out.append((r["path"], "drifted"))
    return out


async def sweep_vault_conflicts() -> dict[str, Any]:
    """Turn vault ledger conflict/drift into vault_conflict actions.

    Calls writer.doctor() for the authoritative counts, then opens one action
    per conflicted/drifted path (ref ``vault:<path>`` — idempotent: a live
    action for the same path is never duplicated). Missing notes are counted
    but NOT actioned: rows are truth and the note is rebuildable, deletion is
    a human prerogative, not a conflict. Never raises (sweep-facing)."""
    try:
        writer = get_writer()
        counts = await writer.doctor()
        if counts is None:
            return {"skipped": "vault_disabled"}
        opened = 0
        if counts["conflict"] or counts["drifted"]:
            for rel, state in await _nonclean_vault_rows(writer):
                if state not in ("conflict", "drifted"):
                    continue
                res = await open_action(
                    "vault_conflict", f"vault:{rel}",
                    f"Vault {state}: {rel}",
                    f"state: {state}\npath: {rel}\n"
                    "resolve in Obsidian (merge/delete the sibling or restore the region), "
                    "then re-run the sweep",
                )
                opened += 1 if res["created"] else 0
        return {"doctor": counts, "opened": opened}
    except Exception as exc:  # noqa: BLE001 - scheduler-facing, never raise
        log.exception("vault conflict sweep failed")
        return {"error": str(exc)[:500]}


# ---- recipes (Phase 6 minimal reuse loop) ------------------------------------
# A recipe = human-approved knowledge made reusable: kind + title keywords →
# disposition. route_actions consults recipes BEFORE the model; a hit records
# the suggestion directly (recipe_id set, confidence inherited, zero model
# calls) — still shadow=1, still behind the live floor + human approval gate.

RECIPE_STATUSES = ("active", "retired")  # code-enforced (0023 adds no CHECK)
RECIPE_MAX_KEYWORDS = 6

# word-ish tokens only: single ASCII letters and bare numbers (instance ids
# like "t-123", dates, counts) never become pattern keywords.
_KEYWORD_TOKEN = re.compile(r"[a-z][a-z0-9]+|[\u4e00-\u9fff]{2,}")


def _title_keywords(title: str, cap: int = RECIPE_MAX_KEYWORDS) -> list[str]:
    """Deduped pattern keywords from an action title, in order, capped."""
    out: list[str] = []
    for tok in _KEYWORD_TOKEN.findall(_fold_line(title, 200).casefold()):
        if tok not in out:
            out.append(tok)
        if len(out) >= cap:
            break
    return out


async def promote_disposition_to_recipe(disposition_id: int) -> dict[str, Any]:
    """Distill a HUMAN-APPROVED disposition into a recipe row.

    The human gate extends to recipe knowledge: only dispositions carrying the
    ``approved`` flag (set exclusively by the web-UI approve endpoint) are
    promotable, and only vocabulary dispositions ('unparsed' never becomes
    reusable knowledge). Pattern = the source action's kind + title keywords;
    confidence is inherited, so recipe-proposed suggestions face the same live
    consumption floor as model ones. Idempotent per source disposition (0023's
    partial unique index): re-promoting returns the existing recipe.

    Returns the recipe row plus ``{"created": bool}``; raises ValueError on
    unknown/unapproved/unpromotable dispositions.
    """
    d = await db.query_one(
        "SELECT * FROM action_dispositions WHERE id = ?", (disposition_id,)
    )
    if d is None:
        raise ValueError(f"unknown disposition {disposition_id}")
    if "approved" not in (d["flags"] or "").split(","):
        raise ValueError(
            f"disposition {disposition_id} is not human-approved; only approved "
            "suggestions can become recipes"
        )
    if d["disposition"] not in DISPOSITION_VOCAB:
        raise ValueError(f"disposition {d['disposition']!r} is not promotable")
    action = await db.query_one(
        "SELECT kind, title FROM operator_actions WHERE id = ?", (d["action_id"],)
    )
    if action is None:  # FK'd; belt and braces
        raise ValueError(f"action {d['action_id']} for disposition {disposition_id} is gone")
    keywords = _title_keywords(action["title"] or "")
    if not keywords:
        # fail closed: with ALL-keywords-match semantics an empty keyword set
        # would match every action of the kind — too broad to be knowledge
        raise ValueError("no usable keywords in the action title; recipe would over-match")
    joined = " ".join(keywords)
    try:
        recipe_id = await db.insert(
            "INSERT INTO recipes "
            "(pattern, disposition, kind, keywords, confidence, source_disposition_id, status, created_at) "
            "VALUES (?,?,?,?,?,?,'active',?)",
            (f"{action['kind']}: {joined}", d["disposition"], action["kind"],
             joined, d["confidence"], disposition_id, bus.now_iso()),
        )
    except sqlite3.IntegrityError:
        existing = await db.query_one(
            "SELECT * FROM recipes WHERE source_disposition_id = ?", (disposition_id,)
        )
        if existing is None:  # pathological — surface it
            raise
        return {**existing, "created": False}
    row = await db.query_one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    log.info("promoted disposition %s to recipe %s (%s: %s → %s)",
             disposition_id, recipe_id, action["kind"], joined, d["disposition"])
    return {**(row or {}), "created": True}


async def list_recipes(status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return await db.query(
            "SELECT * FROM recipes WHERE status = ? ORDER BY id DESC", (status,)
        )
    return await db.query("SELECT * FROM recipes ORDER BY id DESC")


async def retire_recipe(recipe_id: int) -> bool:
    """Conditional claim: only an active recipe retires; repeats lose (False)."""
    n = await db.execute(
        "UPDATE recipes SET status='retired', retired_at=? WHERE id=? AND status='active'",
        (bus.now_iso(), recipe_id),
    )
    return n > 0


async def _match_recipe(action: dict[str, Any]) -> dict[str, Any] | None:
    """Best active recipe for an action: same kind AND every keyword substring-
    matches the folded casefold title. Ties break to highest confidence
    (missing sorts lowest), then newest."""
    rows = await db.query(
        "SELECT * FROM recipes WHERE status = 'active' AND kind = ?", (action["kind"],)
    )
    if not rows:
        return None
    title = _fold_line(action["title"] or "", 200).casefold()
    best: tuple[tuple[float, int], dict[str, Any]] | None = None
    for r in rows:
        keywords = (r["keywords"] or "").split()
        if not keywords:
            continue  # same over-match guard as promotion (legacy/manual rows)
        if all(k in title for k in keywords):
            key = (r["confidence"] if r["confidence"] is not None else -1.0, r["id"])
            if best is None or key > best[0]:
                best = (key, r)
    return best[1] if best else None


# ---- action router (SHADOW MODE) ---------------------------------------------

# The classification prompt. The reply format uses <angle-bracket> placeholders
# on purpose: the echo hand reflects the whole prompt back, and the parser's
# ^DISPOSITION: ([a-z_]+)$ line regex must never match the template itself.
ROUTER_PROMPT = """\
你是研究所的运维分诊路由器（operator triage router）。当前处于 SHADOW MODE：
你的建议只会被记录下来供人工复核，绝不会被自动执行。

请针对下面这一条 operator action 给出处置建议：

【Action】
- kind: {kind}
- ref: {ref}
- priority: {priority}
- title: {title}
- detail:
{detail}

【可选处置】retry（重试原任务）| dismiss（噪音，关掉）| escalate（升级给人工，紧急）|
investigate（需要深入调查）| rebuild_note（重建 vault 笔记）|
adjust_prompt（需要改提示词——只会记录，人工批准前绝不执行）|
adjust_schedule（需要改调度——只会记录，人工批准前绝不执行）

只回复两行，除此之外不要输出任何内容：
DISPOSITION: <从可选处置中选一个词>
CONFIDENCE: <0 到 1 之间的数字>
"""

_DISPOSITION_LINE = re.compile(r"^DISPOSITION:\s*([a-z_]+)\s*$", re.IGNORECASE | re.MULTILINE)
_CONFIDENCE_LINE = re.compile(
    r"^CONFIDENCE:\s*(1(?:\.0+)?|0(?:\.\d+)?|\.\d+)\s*$", re.IGNORECASE | re.MULTILINE
)


_PROTOCOL_LINE = re.compile(r"^(\s*)(DISPOSITION|CONFIDENCE)(\s*:)", re.MULTILINE | re.IGNORECASE)


def _quote_detail(detail: str) -> str:
    """Neutralize protocol lines inside untrusted action detail (REVIEW-C4 M3).

    Hands that reflect the prompt (echo; some CLIs quote it back) would make a
    'DISPOSITION: …' line inside detail the parser's last match. Prefixing
    every line with '> ' breaks the line-anchored protocol regex for reflected
    content while keeping the detail readable for the model.
    """
    quoted = "\n".join(f"> {line}" for line in detail.splitlines())
    # belt and braces: a protocol line that somehow stays line-anchored gets
    # its colon spaced out so the parser regex cannot match it
    return _PROTOCOL_LINE.sub(r"\1> \2 \3", quoted)


def build_router_prompt(action: dict[str, Any]) -> str:
    """Every untrusted field is neutralized before interpolation (REVIEW-C4
    M3 + F3 P2-1): detail is quoted line by line, title/ref are folded to one
    line here EVEN THOUGH open_action already folds/refuses them — rows that
    predate that hygiene (or arrive by other writers) must not steer the
    parser either. kind is vocabulary-checked, priority is an int."""
    return ROUTER_PROMPT.format(
        kind=action["kind"], ref=_fold_line(action["ref"] or "", 200) or "(none)",
        priority=action["priority"], title=_fold_line(action["title"] or "", 200),
        detail=_quote_detail((action["detail"] or "(no detail)")[:1500]),
    )


def parse_disposition(output: str) -> tuple[str, float | None]:
    """(disposition, confidence) from a router reply; last match wins (real
    replies end with the two lines). Out-of-vocabulary or missing disposition
    degrades to 'unparsed'; out-of-range confidence fails the regex -> None.
    Pure and synchronous so tests can probe it directly."""
    text = output or ""
    d_matches = _DISPOSITION_LINE.findall(text)
    disposition = d_matches[-1].lower() if d_matches else ""
    if disposition not in DISPOSITION_VOCAB:
        disposition = "unparsed"
    c_matches = _CONFIDENCE_LINE.findall(text)
    confidence: float | None = None
    if c_matches:
        try:
            confidence = min(max(float(c_matches[-1]), 0.0), 1.0)
        except ValueError:  # regex-guarded; belt and braces
            confidence = None
    return disposition, confidence


async def get_confidence_floor() -> float:
    """Live floor from admin_state (operator:confidence_floor); 0.7 fallback."""
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (CONFIDENCE_FLOOR_KEY,)
    )
    if row is None:
        return CONFIDENCE_FLOOR_DEFAULT
    try:
        v = float(json.loads(row["value"]))
        return v if 0.0 <= v <= 1.0 else CONFIDENCE_FLOOR_DEFAULT
    except (ValueError, TypeError):
        return CONFIDENCE_FLOOR_DEFAULT


def disposition_flags(
    kind: str, disposition: str, confidence: float | None,
    floor: float = CONFIDENCE_FLOOR_DEFAULT,
) -> str:
    """Comma-joined marker set for a suggestion (see migrations/0018)."""
    flags: list[str] = []
    if confidence is None or confidence < floor:
        flags.append("low_confidence")
    if kind in HUMAN_PINNED_KINDS or disposition in HUMAN_PINNED_DISPOSITIONS:
        flags.append("human_pinned")
    return ",".join(flags)


async def route_actions(
    cap: int = 5, *, proposed_by: str = "fast_loop", hand: str | None = None,
) -> dict[str, Any]:
    """Classify up to ``cap`` open actions and record shadow dispositions.

    SHADOW MODE IRON RULE (rule 1 in the module docstring): this function's
    ONLY writes are ``action_dispositions`` rows with ``shadow=1``. It never
    updates ``operator_actions`` (the rows it routes stay byte-identical), and
    it executes no system change of any kind. The single side effect besides
    the disposition rows is the model call itself (one ``tasks`` row per
    action, through executor.submit — the one execution path).

    Recipe short-circuit (Phase 6 minimal loop): each action is first checked
    against active recipes — a match records the suggestion directly
    (``recipe_id`` set, disposition + confidence inherited from the approved
    source, NO model call, no tasks row) and counts as this loop's proposal.
    Only recipe misses go to the model.

    Each loop (fast_loop/deep_loop) proposes at most once per action: already-
    proposed actions are skipped, so a 15-min tick does not re-burn quota on a
    stagnant kanban. Never raises (scheduler-facing; errors in the summary).
    """
    if proposed_by not in ("fast_loop", "deep_loop"):
        raise ValueError("route_actions proposes as fast_loop or deep_loop only")
    try:
        hand_name = hand or get_settings().default_hand
        cap = max(1, min(int(cap), 50))
        rows = await db.query(
            "SELECT id, kind, ref, title, detail, priority FROM operator_actions a "
            "WHERE a.status = 'open' AND NOT EXISTS ("
            "  SELECT 1 FROM action_dispositions d "
            "  WHERE d.action_id = a.id AND d.proposed_by = ?) "
            "ORDER BY a.priority DESC, a.created_at ASC, a.id ASC LIMIT ?",
            (proposed_by, cap),
        )
        proposed: list[dict[str, Any]] = []
        errors = 0
        recipe_hits = 0
        floor = await get_confidence_floor()
        for a in rows:
            try:
                recipe = await _match_recipe(a)
                recipe_id = None
                if recipe is not None:
                    recipe_id = recipe["id"]
                    disposition, confidence = recipe["disposition"], recipe["confidence"]
                else:
                    task = await executor.submit(
                        hand_name, build_router_prompt(a),
                        source=ROUTER_SOURCE, timeout_s=ROUTER_TIMEOUT_S,
                    )
                    if task.status != "completed":
                        errors += 1
                        log.warning("router task %s for action %s ended %s", task.id, a["id"], task.status)
                        continue
                    disposition, confidence = parse_disposition(task.output)
                flags = disposition_flags(a["kind"], disposition, confidence, floor=floor)
                try:
                    disp_id = await db.insert(
                        "INSERT INTO action_dispositions "
                        "(action_id, proposed_by, disposition, confidence, shadow, flags, recipe_id, created_at) "
                        "VALUES (?,?,?,?,1,?,?,?)",  # shadow=1 ALWAYS — iron rule 1
                        (a["id"], proposed_by, disposition, confidence, flags, recipe_id, bus.now_iso()),
                    )
                except sqlite3.IntegrityError:
                    # lost the propose-once race to a concurrent same-loop
                    # call inside our model-call window — 0022's partial
                    # unique index arbitrates; the winner's row stands and
                    # this is convergence, not an error (the feeds' idiom)
                    log.info("action %s already proposed by %s; dropping duplicate", a["id"], proposed_by)
                    continue
                if recipe_id is not None:
                    recipe_hits += 1
                    log.info("action %s routed by recipe %s (%s, zero model calls)",
                             a["id"], recipe_id, disposition)
                proposed.append({
                    "id": disp_id, "action_id": a["id"], "disposition": disposition,
                    "confidence": confidence, "flags": flags, "recipe_id": recipe_id,
                })
            except Exception:  # noqa: BLE001 - one bad action must not sink the tick
                errors += 1
                log.exception("routing action %s failed", a["id"])
        return {
            "proposed_by": proposed_by, "hand": hand_name, "shadow": True,
            "routed": len(rows), "proposed": proposed, "errors": errors,
            "recipe_hits": recipe_hits,
        }
    except Exception as exc:  # noqa: BLE001 - scheduler-facing, never raise
        log.exception("route_actions failed")
        return {"proposed_by": proposed_by, "error": str(exc)[:500]}
