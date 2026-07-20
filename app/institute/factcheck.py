"""Fact-check v2 — claim extraction, tier-1 reuse gate, verification, surfacing.

ROADMAP Phase 3 (proposal §6.2 row 3). The pipeline:

1. **Hooks** (``register()``): ``bus.on`` subscribers for
   ``whiteboard.card_completed`` / ``research.completed`` enqueue one durable
   ``fact_extract_queue`` row per source (INSERT OR IGNORE on the
   (source_kind, source_ref) unique index — replayed events are no-ops).
   Handlers never raise and never call a model: extraction burns quota, so it
   waits for the gated scheduler tick.
2. **Extraction** (``extract_claims``): a cheap-hand task (Filter-A/B style
   prompt) pulls ≤3 checkable claims + categories out of the source text;
   the output is parsed defensively (fenced JSON → raw JSON scan → per-item
   validation → dedup → cap). Each claim becomes a ``fact_cards`` row.
3. **Tier-1 reuse gate** (``check_reuse``): the claim is embedded and compared
   (cosine, in Python — the whiteboard_topic_vectors precedent, no sqlite-vec
   needed) against live verified facts. Per-category threshold + TTL come from
   the ``factcheck_reuse_policy`` admin_state row. A VERIFIED near-neighbor
   marks the card ``reused`` (no re-verification); a DISPUTED near-neighbor
   marks it ``self_contradicted`` (the claim repeats an already-refuted fact —
   surfaced like a dispute, never verified). Vectors degraded == everything is
   fresh (the documented degrade-open posture).
4. **Verification** (``verify_pending``): pending cards are conditional-claimed
   pending→verifying IN THE DATABASE before any model call (two processes can
   never double-verify a card; REVIEW-C1 P1-1), and one slot of the SGT daily
   attempt budget is consumed atomically per model call — successes AND
   failures count, no refunds (the cap is a quota ceiling; refunding failures
   would let a flapping hand burn unbounded quota). The verdict is parsed by
   canonical-line extraction (only bare line-anchored ``VERDICT: <word>``
   lines count; quotes/code fences are context, conflicts collapse
   conservatively UNVERIFIABLE > DISPUTED > VERIFIED; REVIEW-C1 P1-2). The
   verdict row and the card's terminal status commit in one transaction.
5. **Disputed surfacing**: DISPUTED verdicts and self_contradicted cards write
   a durable outbox intent in the same transaction as the dispute. The drain
   atomically materializes one mailbox thread/note/dispatch and marks the row
   delivered; ``factcheck.disputed`` remains the vault exporter signal.

Scheduling is NOT wired here (scheduler.py / main.py are other partitions);
the 30-min gated job and the ``register()`` call live in PATCH-NOTES-C1.md
until integrated. Everything in this module follows the house rules: model
calls only via executor.submit, conditional claims by rowcount, bus.now_iso()
/ work_date() for time, handlers/tick never raise.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import struct
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from . import vectors
from .prompts import date_anchor, work_date

log = logging.getLogger("institute.factcheck")

SOURCE = "factcheck"
SOURCE_KINDS = ("whiteboard_card", "research_report", "daily")
CATEGORIES = ("numerical", "financial", "event", "policy", "other")
VERDICTS = ("VERIFIED", "DISPUTED", "UNVERIFIABLE")

MAX_CLAIMS_PER_SOURCE = 3
MAX_CLAIM_CHARS = 500          # a claim is one sentence; longer == parser junk
EXTRACT_TEXT_CAP = 6000        # source text slice fed to the extraction prompt
EXTRACT_PER_TICK = 2           # extraction tasks one tick may run
VERIFY_PER_TICK = 3            # verification tasks one tick may run (within the daily cap)
STALE_RUNNING_MINUTES = 60     # extraction rows / verifying cards stuck longer are re-opened
DEFAULT_DAILY_CAP = 10         # INSTITUTE_FACTCHECK_DAILY_CAP fallback (config field: PATCH-NOTES-C1)
OUTBOX_MAX_ATTEMPTS = 5        # terminal failed after this many delivery attempts
OUTBOX_PER_DRAIN = 20          # bounded scheduler batch
CLAIM_CHECK_MIN_SIM = 0.75     # claim_check vector recall floor (loose on purpose: writing-time hints)
KEYWORD_MIN_OVERLAP = 0.4      # claim_check keyword fallback: claim-token coverage floor
CLAIM_CHECK_MAX_HITS = 5
CLAIM_CHECK_TEXT_CAP = 20000   # draft slice claim_check will embed/tokenize (MCP has no pydantic guard)
RESEARCH_REPORT_FILE = "06_深度报告.md"   # same artifact exporter._export_research reads

# Daily verification budget: one admin_state counter row per SGT work date
# ('factcheck_attempts:<date>'), consumed by _reserve_attempt()'s conditional
# UPDATE BEFORE each model call. Successes and failures both count, no refunds.
ATTEMPTS_KEY_PREFIX = "factcheck_attempts:"

# ---- reuse policy (config row over in-code defaults; 0011/whiteboard idiom) --
REUSE_POLICY_KEY = "factcheck_reuse_policy"
REUSE_POLICY_DEFAULTS: dict[str, dict[str, float]] = {
    # numbers/finance move fast: tight threshold, short TTL;
    # events/policy are stable once true: looser threshold, long TTL.
    "numerical": {"threshold": 0.92, "ttl_days": 7},
    "financial": {"threshold": 0.92, "ttl_days": 7},
    "event":     {"threshold": 0.88, "ttl_days": 30},
    "policy":    {"threshold": 0.88, "ttl_days": 30},
    "other":     {"threshold": 0.90, "ttl_days": 14},
}

# ---- prompt constants (verbatim-stable once written; CLAUDE.md rule 4) ------

# Filter-A/B style (legacy researchos fact-check prompts): Filter-A keeps only
# checkable factual statements, Filter-B drops the checkable-but-worthless.
CLAIM_EXTRACT_PROMPT = """\
你是研究所的事实核查助理。从下面的研究文本中提取最多 {max_claims} 条「可核查论断」。

【Filter-A：可核查性】只保留同时满足以下条件的句子：
- 是具体的事实性陈述（含明确的主体和可对照的事实），不是观点、预测或建议；
- 原则上可以通过公开来源（新闻、财报、官方公告、市场数据）核实真伪；
- 含具体数字、日期、事件或政策内容者优先。

【Filter-B：价值筛选】在通过 Filter-A 的句子里，丢弃：
- 常识与琐碎事实（如「美联储负责货币政策」）；
- 文本内部自指（如「上一张卡片提到…」）；
- 模糊到无法判真伪的表述（如「市场情绪偏弱」）。

【分类】每条论断标注一个类别：
- numerical：具体数字/统计（增速、产能、出货量等）
- financial：财务与市场数据（营收、利润、股价、估值等）
- event：已发生的事件（发布、并购、签约、事故等）
- policy：政策与监管（法规、关税、补贴、审批等）
- other：其余可核查论断

只输出一个 JSON 数组，不要任何其他文字。每个元素形如：
{{"claim": "<论断原文或忠实转述，一句话>", "category": "<numerical|financial|event|policy|other>"}}
没有可核查论断时输出 []。

【待提取文本】
{text}\
"""

CLAIM_VERIFY_PROMPT = """\
你是研究所的事实核查员。请核查下面这条论断的真伪。

【论断】{claim}
【类别】{category}

【核查要求】
1. 优先使用可联网检索到的公开来源（官方公告、财报、权威媒体、市场数据）交叉验证；
2. 数字类论断允许合理的口径误差（约 ±10% 或四舍五入），超出即视为不符；
3. 找不到足以判定真伪的公开证据时，如实给出 UNVERIFIABLE，禁止臆断；
4. 证据必须具体：写明来源名称与关键数字/日期。

严格按以下三行格式输出结论（VERDICT 必须是三个词之一）：
VERDICT: VERIFIED|DISPUTED|UNVERIFIABLE
EVIDENCE: <一段话说明证据与判定理由>
SOURCES: <来源 URL 或名称，多个用空格分隔；没有则写 none>\
"""

DISPUTE_MAIL_SUBJECT = "【事实核查】你的论断存疑：{claim_head}"
DISPUTE_MAIL_BODY = """\
事实核查流程对你此前产出中的一条论断给出了 DISPUTED（与公开证据不符）判定，请复核：

- 论断：{claim}
- 来源：{source_kind} {source_ref}
- 判定：{verdict_label}
- 证据：{evidence}
- 来源链接：{sources}

请回复：你是否接受该判定？若不接受，请给出你的反驳证据；若接受，请说明该论断错误对你相关结论的影响。\
"""

SELF_CONTRADICTED_LABEL = "self_contradicted（重复了此前已被驳斥的论断）"
DISPUTED_LABEL = "DISPUTED（与公开证据不符）"

# NB: no in-process "being verified" set — the pending→verifying conditional
# claim in the database is the one arbiter, valid across processes (P1-1).


# ---- small vector helpers (private on purpose — B4: never import another
# module's underscore names; struct/math versions are three lines each) -------

def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vec(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _now_plus_days(days: float) -> str:
    """UTC ISO ``days`` from now, bus.now_iso() format (string order == time
    order). Clock source is bus.now_iso() per hard rule 7."""
    now = datetime.fromisoformat(bus.now_iso())
    return (now + timedelta(days=days)).isoformat(timespec="seconds")


def _daily_cap() -> int:
    # Defensive read: the setting lands via PATCH-NOTES-C1 (config.py is
    # another partition). Missing == the documented default.
    try:
        return max(0, int(getattr(get_settings(), "factcheck_daily_cap", DEFAULT_DAILY_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def _extract_hand() -> str:
    """Cheap-hand routing for extraction (ROADMAP: opencode/cheap hand).
    Defensive read — the config field ships via PATCH-NOTES-C1; missing or
    empty falls back to the default hand (tests: echo)."""
    s = get_settings()
    return str(getattr(s, "factcheck_extract_hand", "") or "") or s.default_hand


def _verify_hand() -> str:
    """Websearch-capable hand for verification (ROADMAP: claude/gemini with
    web access). Same defensive-read contract as _extract_hand."""
    s = get_settings()
    return str(getattr(s, "factcheck_verify_hand", "") or "") or s.default_hand


# ---- reuse policy ------------------------------------------------------------

async def get_reuse_policy() -> dict[str, dict[str, float]]:
    """Per-category {threshold, ttl_days} from admin_state, merged over the
    in-code defaults. A broken/missing row degrades to the defaults."""
    policy = {cat: dict(vals) for cat, vals in REUSE_POLICY_DEFAULTS.items()}
    try:
        row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (REUSE_POLICY_KEY,))
        if row:
            stored = json.loads(row["value"])
            if isinstance(stored, dict):
                for cat in REUSE_POLICY_DEFAULTS:
                    entry = stored.get(cat)
                    if not isinstance(entry, dict):
                        continue
                    for knob in ("threshold", "ttl_days"):
                        if isinstance(entry.get(knob), (int, float)) and not isinstance(entry[knob], bool):
                            policy[cat][knob] = float(entry[knob])
    except Exception:  # noqa: BLE001 - a corrupt config row must not break the gate
        log.warning("could not read %s config; using defaults", REUSE_POLICY_KEY, exc_info=True)
    return policy


def _category(raw: Any) -> str:
    cat = str(raw or "").strip().casefold()
    return cat if cat in CATEGORIES else "other"


# ---- claim extraction: defensive parser --------------------------------------

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", re.DOTALL)


def parse_claims(text: str) -> list[dict[str, str]]:
    """Model output → validated claim dicts. Defensive end to end (the 7-step
    parser spirit): fenced JSON first (LAST block wins — replies quote
    material first and answer last), then a raw scan collecting every
    top-level decodable array/object and keeping the LAST (so a reflected
    prompt's template ``[]`` cannot swallow a real bare-array answer that
    follows it; REVIEW-C1 P2-3); every item is validated, categories are
    normalized (unknown → other), duplicates dropped, capped at
    MAX_CLAIMS_PER_SOURCE. Any failure mode returns what was salvageable."""
    if not (text or "").strip():
        return []
    candidates: list[Any] = []
    for match in reversed(_JSON_FENCE.findall(text)):  # last fenced block wins
        try:
            candidates.append(json.loads(match))
            break
        except ValueError:
            continue
    if not candidates:
        decoder = json.JSONDecoder()
        found: list[Any] = []
        pos = 0
        while True:
            m = re.search(r"[\[{]", text[pos:])
            if m is None:
                break
            start = pos + m.start()
            try:
                data, end = decoder.raw_decode(text[start:])
                found.append(data)
                pos = start + end  # skip the consumed block: no nested re-reads
            except ValueError:
                pos = start + 1
        if found:
            candidates.append(found[-1])  # last top-level block wins
    if not candidates:
        return []
    data = candidates[0]
    if isinstance(data, dict):
        # tolerate {"claims": [...]} wrappers or a single claim object
        data = data.get("claims") if isinstance(data.get("claims"), list) else [data]
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        if not claim:
            continue
        if len(claim) > MAX_CLAIM_CHARS:
            claim = claim[: MAX_CLAIM_CHARS - 1] + "…"
        if claim in seen:
            continue
        seen.add(claim)
        out.append({"claim": claim, "category": _category(item.get("category"))})
        if len(out) >= MAX_CLAIMS_PER_SOURCE:
            break
    return out


def _content_hash(source_kind: str, source_ref: str, claim: str) -> str:
    # \x1f separators keep field boundaries unambiguous (research.py idiom)
    payload = "\x1f".join([source_kind, str(source_ref), claim])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---- tier-1 reuse gate ---------------------------------------------------------

async def _reuse_state(
    vec: list[float] | None, category: str,
) -> tuple[str, str | None, float]:
    """(state, related verified_facts.id, similarity) for an embedded claim.

    state ∈ {fresh, reused, self_contradicted}. Vectors unavailable (vec is
    None) == fresh — the degrade-open contract. Neighbors are live verdict
    rows only (expires_at in the future, current embed model); the candidate
    set is NOT restricted to the same category — categories are model labels
    with jitter, and semantic similarity is category-agnostic — but the
    threshold applied is the NEW claim's category threshold. A DISPUTED
    neighbor over the threshold wins over a VERIFIED one (repeating a refuted
    fact must never be silently reused). Never raises.
    """
    if vec is None:
        return "fresh", None, 0.0
    try:
        policy = await get_reuse_policy()
        threshold = float(policy.get(category, policy["other"])["threshold"])
        rows = await db.query(
            "SELECT fv.embedding, fv.dim, vf.id AS fact_id, vf.verdict "
            "FROM fact_claim_vectors fv "
            "JOIN verified_facts vf ON vf.fact_card_id = fv.fact_card_id "
            "WHERE fv.model = ? AND vf.verdict IN ('VERIFIED','DISPUTED') AND vf.expires_at > ?",
            (vectors.model_name(), bus.now_iso()),
        )
        best: dict[str, tuple[float, str]] = {}
        for r in rows:
            other = _unpack_vec(r["embedding"], r["dim"])
            if len(other) != len(vec):
                continue  # different embedding space (defensive; model already filtered)
            sim = _cosine(vec, other)
            if sim >= threshold and (r["verdict"] not in best or sim > best[r["verdict"]][0]):
                best[r["verdict"]] = (sim, r["fact_id"])
        if "DISPUTED" in best:
            sim, fact_id = best["DISPUTED"]
            return "self_contradicted", fact_id, sim
        if "VERIFIED" in best:
            sim, fact_id = best["VERIFIED"]
            return "reused", fact_id, sim
        return "fresh", None, 0.0
    except Exception:  # noqa: BLE001 - the gate must never block extraction
        log.exception("reuse gate failed; treating claim as fresh")
        return "fresh", None, 0.0


async def check_reuse(claim: str, category: str) -> dict[str, Any]:
    """Public tier-1 gate: embed one claim and classify it against live facts."""
    vec = await vectors.embed(claim)
    state, fact_id, sim = await _reuse_state(vec, _category(category))
    return {"state": state, "related_fact_id": fact_id, "similarity": round(sim, 4)}


async def _store_claim_vector(fact_card_id: str, vec: list[float] | None) -> None:
    """Persist a claim embedding for future gates/claim_check. Never raises —
    a failure only degrades future gates (this claim becomes invisible)."""
    if vec is None:
        return
    try:
        await db.execute(
            "INSERT OR REPLACE INTO fact_claim_vectors (fact_card_id, model, dim, embedding, created_at) "
            "VALUES (?,?,?,?,?)",
            (fact_card_id, vectors.model_name(), len(vec), _pack_vec(vec), bus.now_iso()),
        )
    except Exception:  # noqa: BLE001 - projection only, fact_cards rows are truth
        log.warning("could not store claim vector for card %s", fact_card_id, exc_info=True)


# ---- extraction -----------------------------------------------------------------

async def extract_claims(
    source_kind: str, source_ref: str, text: str, analyst_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """Extract ≤3 checkable claims from one source text into fact_cards rows.

    Returns the list of NEWLY created card rows (possibly empty — a source
    with no checkable claims is a normal outcome), or None when the model
    task itself failed (callers may mark the queue row failed). Idempotent:
    the (source_kind|source_ref|claim) content hash makes re-extraction of
    the same source a no-op per claim. Each new card passes the tier-1 reuse
    gate before it lands: reused/self_contradicted cards are terminal at
    birth and never reach verification.
    """
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source_kind {source_kind!r}")
    text = (text or "").strip()
    if not text:
        return []
    prompt = (
        f"{date_anchor()}\n\n"
        + CLAIM_EXTRACT_PROMPT.format(max_claims=MAX_CLAIMS_PER_SOURCE, text=text[:EXTRACT_TEXT_CAP])
    )
    task = await executor.submit(_extract_hand(), prompt, source=SOURCE)
    if task.status != "completed":
        log.warning("claim extraction task %s ended %s for %s %s",
                    task.id, task.status, source_kind, source_ref)
        return None

    created: list[dict[str, Any]] = []
    for item in parse_claims(task.output or ""):
        claim, category = item["claim"], item["category"]
        vec = await vectors.embed(claim)
        state, related_fact_id, sim = await _reuse_state(vec, category)
        status = {"fresh": "pending", "reused": "reused", "self_contradicted": "self_contradicted"}[state]
        card_id = uuid.uuid4().hex[:12]
        row = {
            "id": card_id, "source_kind": source_kind, "source_ref": str(source_ref),
            "analyst_id": analyst_id or None, "claim": claim, "category": category,
            "status": status, "related_fact_id": related_fact_id, "similarity": round(sim, 4),
        }
        # the INSERT is the arbiter: OR IGNORE on content_hash makes re-runs
        # of the same source a per-claim no-op. A self-contradiction's outbox
        # intent lands in THIS transaction with the terminal card.
        outbox_id: str | None = None
        async with db.transaction() as conn:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO fact_cards "
                "(id, source_kind, source_ref, analyst_id, claim, category, status, related_fact_id, content_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (card_id, source_kind, str(source_ref), analyst_id or None, claim, category,
                 status, related_fact_id, _content_hash(source_kind, str(source_ref), claim), bus.now_iso()),
            )
            inserted = bool(cur.rowcount)
            await cur.close()
            if inserted and state == "self_contradicted":
                outbox_id = await _enqueue_dispute_outbox(
                    conn, row, kind="self_contradicted",
                    verdict_label=SELF_CONTRADICTED_LABEL,
                    evidence=f"与已被驳斥的事实相似度 {sim:.3f}（fact {related_fact_id}）",
                    sources="",
                )
        if not inserted:
            continue  # already extracted from this source
        await _store_claim_vector(card_id, vec)
        created.append(row)
        if state == "self_contradicted":
            await _surface_dispute(
                row, kind="self_contradicted", verdict_label=SELF_CONTRADICTED_LABEL,
                evidence=f"与已被驳斥的事实相似度 {sim:.3f}（fact {related_fact_id}）", sources="",
                outbox_id=outbox_id,
            )
    if created:
        await bus.emit("factcheck.extracted", "factcheck", str(source_ref), {
            "source_kind": source_kind, "source_ref": str(source_ref),
            "cards": len(created),
            "reused": sum(1 for c in created if c["status"] == "reused"),
            "self_contradicted": sum(1 for c in created if c["status"] == "self_contradicted"),
        })
    return created


# ---- verification ----------------------------------------------------------------

# Canonical verdict line (REVIEW-C1 P1-2): line-anchored, tolerates markdown
# bold and ONE trailing sentence mark, and the verdict word must own the rest
# of the line — so the prompt's own format spec ("VERDICT:
# VERIFIED|DISPUTED|UNVERIFIABLE"), prose ("cannot be judged DISPUTED") and
# negations ("NOT VERIFIED") can never parse as a conclusion.
_CANON_VERDICT_LINE = re.compile(
    r"^\s*\**\s*VERDICT\s*[:：]\s*\**\s*(VERIFIED|DISPUTED|UNVERIFIABLE)\b\s*\**\s*[.。！!]?\s*$",
    re.IGNORECASE,
)
_FENCE_LINE = re.compile(r"^\s*(?:```|~~~)")

# Conflict collapse order — most conservative first (UNVERIFIABLE only keeps
# the claim out of the fact store; a wrong VERIFIED would poison reuse and a
# wrong DISPUTED would page the analyst).
_VERDICT_CONSERVATIVE_ORDER = ("UNVERIFIABLE", "DISPUTED", "VERIFIED")

_EVIDENCE_RE = re.compile(r"EVIDENCE\s*[:：]\s*(.+?)(?=\n\s*SOURCES\s*[:：]|\Z)", re.IGNORECASE | re.DOTALL)
_SOURCES_LINE_RE = re.compile(r"SOURCES\s*[:：]\s*(.+)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s)\]>」』]+")

_VERDICT_MATERIAL_GUARD = re.compile(r"(VERDICT)(\s*[:：])", re.IGNORECASE)


def _quote_material(text: str) -> str:
    """Neutralize untrusted material before it enters the verify prompt
    (REVIEW-C1 P1-2; the C4 ``_quote_detail`` precedent). Claims are one
    sentence by contract: collapsing whitespace keeps the material inline
    after the 【论断】 label, so nothing from it can sit at line start; the
    colon spacing is belt and braces for hands that re-wrap lines."""
    flat = " ".join((text or "").split())
    return _VERDICT_MATERIAL_GUARD.sub(r"\1 -\2", flat)


def parse_verdict(text: str) -> str | None:
    """Canonical-line extraction, replacing the global regex cascade.

    Only bare, line-anchored ``VERDICT: <word>`` lines count; lines inside
    blockquotes (``>``) or code fences are quoted material and are skipped.
    Multiple canonical lines that AGREE return that verdict; conflicting
    lines collapse conservatively (UNVERIFIABLE > DISPUTED > VERIFIED — an
    ambiguous answer must never mint a fact or page an analyst on the weaker
    reading). No canonical line — including prose-only mentions, negations
    and the echoed prompt format line — returns None, which the caller lands
    as UNVERIFIABLE (the model was told the exact format; an answer that
    ignores it is not evidence)."""
    found: list[str] = []
    in_fence = False
    for line in (text or "").splitlines():
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(">"):
            continue
        m = _CANON_VERDICT_LINE.match(line)
        if m:
            found.append(m.group(1).upper())
    for verdict in _VERDICT_CONSERVATIVE_ORDER:
        if verdict in found:
            return verdict
    return None


def _parse_evidence(text: str) -> tuple[str, list[str]]:
    """(evidence, source_urls) off the verifier output; both degrade to empty."""
    text = text or ""
    m = _EVIDENCE_RE.search(text)
    evidence = " ".join(m.group(1).split())[:2000] if m else ""
    urls: list[str] = []
    m2 = _SOURCES_LINE_RE.search(text)
    scope = m2.group(1) if m2 else text
    for u in _URL_RE.findall(scope):
        if u not in urls:
            urls.append(u)
    return evidence, urls[:10]


def _attempts_key(date: str | None = None) -> str:
    return ATTEMPTS_KEY_PREFIX + (date or work_date())


async def attempts_today() -> int:
    """Verification attempts already booked against today's SGT budget."""
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (_attempts_key(),)
    )
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


async def _reserve_attempt() -> bool:
    """Atomically book ONE slot of today's verification budget (P1-1).

    INSERT OR IGNORE seeds the counter row, then a conditional UPDATE
    (value < cap) is the arbiter — the rowcount says whether THIS caller got
    the slot, so concurrent sweeps can never jointly exceed the cap. The slot
    is booked BEFORE the model call and never refunded: failed attempts spend
    quota too, and a refund would let a flapping hand burn calls unbounded.
    """
    cap = _daily_cap()
    if cap <= 0:
        return False
    key = _attempts_key()
    await db.execute(
        "INSERT OR IGNORE INTO admin_state (key, value) VALUES (?, '0')", (key,)
    )
    n = await db.execute(
        "UPDATE admin_state SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = ? AND CAST(value AS INTEGER) < ?",
        (key, cap),
    )
    return bool(n)


async def _claim_card(card_id: str) -> bool:
    """pending→verifying conditional claim, BEFORE the model call (P1-1)."""
    n = await db.execute(
        "UPDATE fact_cards SET status='verifying', verify_started_at=? "
        "WHERE id=? AND status='pending'",
        (bus.now_iso(), card_id),
    )
    return bool(n)


async def _release_card(card_id: str) -> None:
    """verifying→pending (transient failure: retry on a later tick — the
    booked attempt slot is NOT refunded)."""
    await db.execute(
        "UPDATE fact_cards SET status='pending', verify_started_at=NULL "
        "WHERE id=? AND status='verifying'",
        (card_id,),
    )


async def verify_pending(cap: int | None = None) -> list[dict[str, Any]]:
    """Verify pending fact cards, oldest first, within the daily attempt cap.

    ``cap`` further bounds THIS call (the tick passes VERIFY_PER_TICK). Per
    card, in order: pending→verifying conditional claim (the cross-process
    double-verification guard), atomic attempt-slot booking (successes and
    failures both count), ONE model task, then verdict row + terminal status
    in one transaction conditional on status='verifying'. A failed model task
    releases the card back to pending for a later tick (its slot stays
    spent); a completed task whose output has no parseable verdict lands
    UNVERIFIABLE (retrying an unparseable answer forever would burn quota
    for nothing).
    """
    call_budget = max(0, int(cap)) if cap is not None else None
    results: list[dict[str, Any]] = []
    while call_budget is None or len(results) < call_budget:
        card = await db.query_one(
            "SELECT * FROM fact_cards WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        )
        if card is None:
            break
        if not await _claim_card(card["id"]):
            continue  # lost the race; the next loop picks another card
        if not await _reserve_attempt():
            # today's budget is gone: hand the card back untouched and stop
            await _release_card(card["id"])
            break
        try:
            results.append(await _verify_card(card))
        except Exception as exc:  # noqa: BLE001 - one card must never break the sweep
            log.exception("verification crashed for card %s", card["id"])
            await _release_card(card["id"])
            results.append({"card_id": card["id"], "status": "crashed", "error": str(exc)[:200]})
    return results


async def _verify_card(card: dict[str, Any]) -> dict[str, Any]:
    """Run one verification task for a card THIS caller already claimed
    (status='verifying') and whose attempt slot is already booked."""
    prompt = (
        f"{date_anchor()}\n\n"
        + CLAIM_VERIFY_PROMPT.format(claim=_quote_material(card["claim"]), category=card["category"])
    )
    task = await executor.submit(_verify_hand(), prompt, source=SOURCE)
    if task.status != "completed":
        # transient hand failure: back to pending for a later tick (the
        # booked attempt slot stays spent — failures are quota too)
        log.warning("verification task %s ended %s for card %s", task.id, task.status, card["id"])
        await _release_card(card["id"])
        return {"card_id": card["id"], "status": "task_failed", "task_id": task.id}

    verdict = parse_verdict(task.output or "")
    evidence, urls = _parse_evidence(task.output or "")
    if verdict is None:
        verdict = "UNVERIFIABLE"
        evidence = ("核查输出无法解析出判定：" + " ".join((task.output or "").split()))[:500]

    policy = await get_reuse_policy()
    ttl_days = float(policy.get(card["category"], policy["other"])["ttl_days"])
    now = bus.now_iso()
    fact_id = uuid.uuid4().hex[:12]
    status = verdict.lower()  # VERIFIED→verified / DISPUTED→disputed / UNVERIFIABLE→unverifiable
    outbox_id: str | None = None

    # one transaction: terminal status + verdict row + (for DISPUTED) delivery
    # intent land together, conditional on the claim WE hold.
    # NB: transaction() holds the db write lock — use the yielded conn
    # directly (db.execute/bus.emit in here would deadlock); events after.
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE fact_cards SET status = ?, verify_started_at = NULL "
            "WHERE id = ? AND status = 'verifying'",
            (status, card["id"]),
        )
        if cur.rowcount == 0:
            # claim lost mid-flight (operator reset / stale sweep): discard ours
            log.info("card %s no longer verifying; discarding verification result", card["id"])
            return {"card_id": card["id"], "status": "lost_claim", "task_id": task.id}
        await conn.execute(
            "INSERT INTO verified_facts "
            "(id, fact_card_id, verdict, evidence, source_urls, work_date, verified_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (fact_id, card["id"], verdict, evidence, json.dumps(urls, ensure_ascii=False),
             work_date(), now, _now_plus_days(ttl_days)),
        )
        if verdict == "DISPUTED":
            outbox_id = await _enqueue_dispute_outbox(
                conn, {**card, "related_fact_id": fact_id}, kind="disputed",
                verdict_label=DISPUTED_LABEL, evidence=evidence, sources=" ".join(urls),
            )

    await bus.emit("factcheck.verified", "fact_card", card["id"], {
        "fact_id": fact_id, "verdict": verdict, "category": card["category"],
        "claim": card["claim"][:500], "analyst_id": card["analyst_id"],
        "source_kind": card["source_kind"], "source_ref": card["source_ref"],
    })
    if verdict == "DISPUTED":
        await _surface_dispute(
            {**card, "related_fact_id": fact_id}, kind="disputed",
            verdict_label=DISPUTED_LABEL, evidence=evidence, sources=" ".join(urls),
            outbox_id=outbox_id,
        )
    log.info("card %s verified: %s (%s)", card["id"], verdict, card["category"])
    return {"card_id": card["id"], "status": "completed", "verdict": verdict,
            "fact_id": fact_id, "task_id": task.id}


# ---- disputed surfacing + durable outbox -------------------------------------

def _dispute_payload(
    card: dict[str, Any], *, kind: str, verdict_label: str, evidence: str, sources: str,
) -> dict[str, Any]:
    return {
        "subject": DISPUTE_MAIL_SUBJECT.format(claim_head=card["claim"][:40]),
        "body": DISPUTE_MAIL_BODY.format(
            claim=card["claim"],
            source_kind=card["source_kind"], source_ref=card["source_ref"],
            verdict_label=verdict_label,
            evidence=evidence or "（无）", sources=sources or "（无）",
        ),
        "event": {
            "kind": kind,
            "claim": card["claim"][:500],
            "category": card.get("category"),
            "analyst_id": card.get("analyst_id"),
            "source_kind": card.get("source_kind"),
            "source_ref": card.get("source_ref"),
            "related_fact_id": card.get("related_fact_id"),
            "evidence": (evidence or "")[:1000],
            "source_urls": sources or "",
        },
    }


async def _enqueue_dispute_outbox(
    conn: Any, card: dict[str, Any], *, kind: str, verdict_label: str,
    evidence: str, sources: str,
) -> str | None:
    """Write one analyst-delivery intent using the caller's dispute transaction."""
    recipient_id = str(card.get("analyst_id") or "")
    if not recipient_id:
        return None
    outbox_id = uuid.uuid4().hex[:12]
    dispute_id = f"{kind}:{card['id']}"
    payload = json.dumps(
        _dispute_payload(
            card, kind=kind, verdict_label=verdict_label,
            evidence=evidence, sources=sources,
        ),
        ensure_ascii=False,
    )
    cur = await conn.execute(
        "INSERT OR IGNORE INTO factcheck_dispute_outbox "
        "(id, dispute_id, fact_card_id, recipient_id, payload, status, attempts, created_at) "
        "VALUES (?,?,?,?,?,'pending',0,?)",
        (outbox_id, dispute_id, card["id"], recipient_id, payload, bus.now_iso()),
    )
    inserted = bool(cur.rowcount)
    await cur.close()
    if inserted:
        return outbox_id
    cur = await conn.execute(
        "SELECT id FROM factcheck_dispute_outbox WHERE dispute_id=? AND recipient_id=?",
        (dispute_id, recipient_id),
    )
    existing = await cur.fetchone()
    await cur.close()
    return str(existing["id"]) if existing else None


async def _record_outbox_failure(row: dict[str, Any], error: str) -> str | None:
    """Count one failed attempt with attempts as the CAS version."""
    attempts = int(row["attempts"])
    next_status = "failed" if attempts + 1 >= OUTBOX_MAX_ATTEMPTS else "pending"
    n = await db.execute(
        "UPDATE factcheck_dispute_outbox "
        "SET attempts=attempts+1, status=?, last_error=? "
        "WHERE id=? AND status='pending' AND attempts=?",
        (next_status, error[:500], row["id"], attempts),
    )
    return next_status if n else None


async def _deliver_dispute_outbox_row(row: dict[str, Any]) -> str | None:
    """Persist exactly one mailbox notification and mark the row delivered.

    The deterministic thread id is the mailbox-side idempotency key. Thread,
    operator note, pending dispatch, attempt increment, and delivered marker
    commit together, so a crash can expose either all of them or none.
    """
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid outbox payload JSON") from exc
    if not isinstance(payload, dict) or not payload.get("subject") or not payload.get("body"):
        raise ValueError("outbox payload requires subject and body")

    from .analysts import get_analyst

    if get_analyst(row["recipient_id"]) is None:
        raise ValueError(f"unknown analyst {row['recipient_id']}")

    thread_id = f"factcheck-{row['id']}"
    now = bus.now_iso()
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE factcheck_dispute_outbox SET attempts=attempts+1 "
            "WHERE id=? AND status='pending' AND attempts=? AND attempts<?",
            (row["id"], int(row["attempts"]), OUTBOX_MAX_ATTEMPTS),
        )
        claimed = bool(cur.rowcount)
        await cur.close()
        if not claimed:
            return None

        cur = await conn.execute(
            "INSERT OR IGNORE INTO mailbox_threads "
            "(id, subject, analyst_id, status, created_at, updated_at) "
            "VALUES (?,?,?,'open',?,?)",
            (thread_id, str(payload["subject"]), row["recipient_id"], now, now),
        )
        created = bool(cur.rowcount)
        await cur.close()
        if created:
            await conn.execute(
                "INSERT INTO mailbox_messages "
                "(thread_id, author, kind, body, status, created_at) "
                "VALUES (?,'operator','note',?,'done',?)",
                (thread_id, str(payload["body"]), now),
            )
            await conn.execute(
                "INSERT INTO mailbox_messages "
                "(thread_id, author, kind, body, status, created_at) "
                "VALUES (?,?,'dispatch','','pending',?)",
                (thread_id, row["recipient_id"], now),
            )
        else:
            cur = await conn.execute(
                "SELECT analyst_id FROM mailbox_threads WHERE id=?", (thread_id,)
            )
            existing = await cur.fetchone()
            await cur.close()
            if existing is None or existing["analyst_id"] != row["recipient_id"]:
                raise RuntimeError(f"mailbox idempotency collision for {thread_id}")

        await conn.execute(
            "UPDATE factcheck_dispute_outbox "
            "SET status='delivered', delivered_at=?, last_error=NULL "
            "WHERE id=? AND status='pending'",
            (now, row["id"]),
        )
    return thread_id


async def drain_dispute_outbox(
    limit: int = OUTBOX_PER_DRAIN, *, outbox_id: str | None = None,
) -> dict[str, Any]:
    """Retry pending analyst notifications without invoking a model.

    This job only writes mailbox's durable pending dispatch. The separately
    gated mailbox sweep starts the analyst model call.
    """
    result: dict[str, Any] = {
        "delivered": 0, "retried": 0, "failed": 0, "thread_ids": {},
    }
    try:
        result["failed"] += await db.execute(
            "UPDATE factcheck_dispute_outbox "
            "SET status='failed', last_error=COALESCE(last_error, 'retry limit reached') "
            "WHERE status='pending' AND attempts>=?",
            (OUTBOX_MAX_ATTEMPTS,),
        )
        params: list[Any] = []
        sql = (
            "SELECT * FROM factcheck_dispute_outbox "
            "WHERE status='pending' AND attempts<?"
        )
        params.append(OUTBOX_MAX_ATTEMPTS)
        if outbox_id is not None:
            sql += " AND id=?"
            params.append(outbox_id)
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(min(max(int(limit), 1), 200))
        for row in await db.query(sql, params):
            try:
                thread_id = await _deliver_dispute_outbox_row(row)
            except Exception as exc:  # noqa: BLE001 - one poison row must not stop the drain
                log.warning("dispute outbox delivery failed for %s: %s", row["id"], exc)
                state = await _record_outbox_failure(row, str(exc))
                if state == "failed":
                    result["failed"] += 1
                elif state == "pending":
                    result["retried"] += 1
                continue
            if thread_id:
                result["delivered"] += 1
                result["thread_ids"][row["id"]] = thread_id
    except Exception:  # noqa: BLE001 - scheduler-driven, must not raise
        log.exception("dispute outbox drain failed")
    return result

async def _surface_dispute(
    card: dict[str, Any], *, kind: str, verdict_label: str, evidence: str, sources: str,
    outbox_id: str | None = None,
) -> None:
    """Best-effort immediate drain, then the existing dispute event. Never raises."""
    thread_id: str | None = None
    if outbox_id:
        try:
            drained = await drain_dispute_outbox(limit=1, outbox_id=outbox_id)
            thread_id = drained["thread_ids"].get(outbox_id)
        except Exception:  # noqa: BLE001 - surfacing must never break verification
            log.exception("immediate dispute outbox drain failed for card %s", card.get("id"))
    payload = _dispute_payload(
        card, kind=kind, verdict_label=verdict_label,
        evidence=evidence, sources=sources,
    )["event"]
    payload["thread_id"] = thread_id
    try:
        await bus.emit(
            "factcheck.disputed", "fact_card", str(card.get("id") or ""), payload,
        )
    except Exception:  # noqa: BLE001
        log.exception("factcheck.disputed emit failed for card %s", card.get("id"))


# ---- hooks (bus.on subscriptions; wiring line in PATCH-NOTES-C1.md) -----------

async def enqueue_extraction(
    source_kind: str, source_ref: str, analyst_id: str | None = None,
) -> bool:
    """Queue one source for claim extraction. INSERT OR IGNORE on the
    (source_kind, source_ref) unique index — the rowcount says whether THIS
    call enqueued (replayed events / double hooks are no-ops)."""
    n = await db.execute(
        "INSERT OR IGNORE INTO fact_extract_queue "
        "(id, source_kind, source_ref, analyst_id, status, created_at) "
        "VALUES (?,?,?,?,'pending',?)",
        (uuid.uuid4().hex[:12], source_kind, str(source_ref), analyst_id or None, bus.now_iso()),
    )
    return bool(n)


async def _on_card_completed(event: bus.Event) -> None:
    """whiteboard.card_completed → queue the card for extraction. Never raises."""
    try:
        p = event.payload or {}
        card_id = str(event.ref_id or "")
        if not card_id:
            return
        await enqueue_extraction(
            "whiteboard_card", card_id, analyst_id=str(p.get("analyst_id") or "") or None,
        )
    except Exception:  # noqa: BLE001 - bus handlers must never raise
        log.exception("factcheck card_completed hook failed for %s", event.ref_id)


async def _on_research_completed(event: bus.Event) -> None:
    """research.completed → queue the report for extraction. Never raises."""
    try:
        item_id = str(event.ref_id or "")
        if not item_id:
            return
        await enqueue_extraction("research_report", item_id)
    except Exception:  # noqa: BLE001 - bus handlers must never raise
        log.exception("factcheck research_completed hook failed for %s", event.ref_id)


def register() -> None:
    """Hook the fact-check queue into the bus. Called once from the app
    lifespan (the one-line mount is in PATCH-NOTES-C1.md — zero edits to the
    whiteboard/research partitions)."""
    bus.on("whiteboard.card_completed", _on_card_completed)
    bus.on("research.completed", _on_research_completed)
    log.info("factcheck hooks registered (card_completed, research.completed)")


# ---- source text resolution ------------------------------------------------------

def _read_text(path: Path) -> str | None:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("could not read %s", path)
    return None


async def _session_workspace(session_id: Any) -> Path | None:
    if not session_id:
        return None
    row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (str(session_id),))
    if row and row["workspace_dir"]:
        ws = Path(row["workspace_dir"]).expanduser()
        if ws.is_dir():
            return ws
    return None


async def _source_text(row: dict[str, Any]) -> str | None:
    """The text to extract claims from, for one queue row. None == no source
    (unknown ref / nothing readable) — the row is marked failed, not retried."""
    kind, ref = row["source_kind"], row["source_ref"]
    if kind == "whiteboard_card":
        card = await db.query_one("SELECT * FROM whiteboard_cards WHERE id = ?", (ref,))
        if card is None:
            return None
        board = await db.query_one(
            "SELECT session_id FROM whiteboard_boards WHERE id = ?", (card["board_id"],)
        )
        ws = await _session_workspace(board["session_id"]) if board else None
        if ws and card["output_file"]:
            text = _read_text(ws / str(card["output_file"]))
            if text and text.strip():
                return text
        return (card["summary"] or "").strip() or None
    if kind == "research_report":
        item = await db.query_one("SELECT run_id FROM research_queue WHERE id = ?", (ref,))
        if item is None:
            return None
        run = None
        if item["run_id"]:
            run = await db.query_one(
                "SELECT session_id FROM workflow_runs WHERE id = ?", (item["run_id"],)
            )
        ws = await _session_workspace(run["session_id"]) if run else None
        if ws:
            text = _read_text(ws / RESEARCH_REPORT_FILE)
            if text and text.strip():
                return text
        if item["run_id"]:
            log_row = await db.query_one(
                "SELECT summary FROM research_log WHERE run_id = ? ORDER BY completed_at DESC LIMIT 1",
                (item["run_id"],),
            )
            if log_row and (log_row["summary"] or "").strip():
                return log_row["summary"]
        return None
    # 'daily' rows have no automatic text source yet (no hook enqueues them);
    # callers with text in hand use extract_claims() directly.
    return None


# ---- the tick (scheduler entrypoint; mount in PATCH-NOTES-C1.md) ---------------

async def _recover_stale_running() -> None:
    cutoff = (
        datetime.fromisoformat(bus.now_iso()) - timedelta(minutes=STALE_RUNNING_MINUTES)
    ).isoformat(timespec="seconds")
    n = await db.execute(
        "UPDATE fact_extract_queue SET status='pending', started_at=NULL "
        "WHERE status='running' AND started_at < ?",
        (cutoff,),
    )
    if n:
        log.warning("re-opened %d stale running extraction rows", n)
    # a crash mid-verification leaves 'verifying' cards behind; hand them
    # back after the same staleness window (their attempt slots stay spent)
    n = await db.execute(
        "UPDATE fact_cards SET status='pending', verify_started_at=NULL "
        "WHERE status='verifying' AND verify_started_at < ?",
        (cutoff,),
    )
    if n:
        log.warning("re-opened %d stale verifying fact cards", n)


async def _drain_extractions(cap: int) -> int:
    """Run up to ``cap`` queued extractions. Conditional claim per row; a
    model-task failure marks the row failed (operator can reset it — blind
    retries would burn quota on a source that keeps failing)."""
    done = 0
    for _ in range(max(0, cap)):
        row = await db.query_one(
            "SELECT * FROM fact_extract_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        )
        if row is None:
            break
        claimed = await db.execute(
            "UPDATE fact_extract_queue SET status='running', started_at=? WHERE id=? AND status='pending'",
            (bus.now_iso(), row["id"]),
        )
        if not claimed:
            continue  # lost the race; try the next row
        # every running→terminal transition below names its source state
        # (AND status='running') per the conditional-claim hard rule: a stale
        # sweep / operator reset that re-opened the row must win over us
        try:
            text = await _source_text(row)
            if text is None:
                await db.execute(
                    "UPDATE fact_extract_queue SET status='failed', error=?, finished_at=? "
                    "WHERE id=? AND status='running'",
                    ("source text unavailable", bus.now_iso(), row["id"]),
                )
                continue
            cards = await extract_claims(
                row["source_kind"], row["source_ref"], text, analyst_id=row["analyst_id"],
            )
            if cards is None:
                await db.execute(
                    "UPDATE fact_extract_queue SET status='failed', error=?, finished_at=? "
                    "WHERE id=? AND status='running'",
                    ("extraction task failed", bus.now_iso(), row["id"]),
                )
            else:
                n = await db.execute(
                    "UPDATE fact_extract_queue SET status='done', finished_at=? "
                    "WHERE id=? AND status='running'",
                    (bus.now_iso(), row["id"]),
                )
                if n:
                    done += 1
        except Exception as exc:  # noqa: BLE001 - one row must never break the drain
            log.exception("extraction failed for queue row %s", row["id"])
            await db.execute(
                "UPDATE fact_extract_queue SET status='failed', error=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (str(exc)[:500], bus.now_iso(), row["id"]),
            )
    return done


async def tick() -> dict[str, Any]:
    """Scheduler job body (30-min interval, gated=True — it starts model
    work; mount in PATCH-NOTES-C1.md). Never raises."""
    out: dict[str, Any] = {"extracted": 0, "verified": 0}
    try:
        await _recover_stale_running()
        out["extracted"] = await _drain_extractions(EXTRACT_PER_TICK)
        results = await verify_pending(VERIFY_PER_TICK)
        out["verified"] = sum(1 for r in results if r.get("status") == "completed")
    except Exception:  # noqa: BLE001 - scheduler-driven, must not raise
        log.exception("factcheck tick failed")
    return out


# ---- claim-check-before-write ----------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*|\d[\d.,%]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokens(text: str) -> set[str]:
    """Keyword tokens: latin words, numbers, CJK bigrams (CJK has no spaces)."""
    toks = {m.group(0).casefold() for m in _TOKEN_RE.finditer(text or "")}
    for run in _CJK_RE.findall(text or ""):
        if len(run) == 1:
            toks.add(run)
        for i in range(len(run) - 1):
            toks.add(run[i : i + 2])
    return toks


async def _verdict_rows(limit: int = 2000) -> list[dict[str, Any]]:
    """Live actionable verdicts (the claim_check candidate set): VERIFIED /
    DISPUTED only (UNVERIFIABLE is a non-answer, not a writing-time hint) and
    unexpired (a lapsed short-TTL financial fact must not hit forever).
    reused/self_contradicted cards have no verdict row and are excluded — the
    fact they point at is already in the set."""
    return await db.query(
        "SELECT c.id, c.claim, c.category, vf.verdict, vf.expires_at "
        "FROM fact_cards c JOIN verified_facts vf ON vf.fact_card_id = c.id "
        "WHERE vf.verdict IN ('VERIFIED','DISPUTED') AND vf.expires_at > ? "
        "ORDER BY vf.verified_at DESC LIMIT ?",
        (bus.now_iso(), limit),
    )


async def _keyword_hits(text: str, k: int) -> list[dict[str, Any]]:
    qtok = _tokens(text)
    if not qtok:
        return []
    hits: list[dict[str, Any]] = []
    for r in await _verdict_rows():
        ctok = _tokens(r["claim"])
        if not ctok:
            continue
        overlap = len(qtok & ctok) / len(ctok)  # claim-token coverage by the draft
        if overlap >= KEYWORD_MIN_OVERLAP:
            hits.append({
                "fact_card_id": r["id"], "claim": r["claim"], "category": r["category"],
                "verdict": r["verdict"], "similarity": round(overlap, 4), "source": "keyword",
            })
    hits.sort(key=lambda h: h["similarity"], reverse=True)
    return hits[:k]


async def claim_check(text: str, k: int = CLAIM_CHECK_MAX_HITS) -> dict[str, Any]:
    """Writing-time check: does this draft touch verified/disputed facts?

    Candidates are LIVE actionable verdicts only (VERIFIED/DISPUTED,
    unexpired — see _verdict_rows). Vector near-neighbors over the claim
    embeddings when the vector layer is live, merged with keyword-overlap
    hits (vector rows lead, same-card dupes dropped). Degradation ladder:
    vectors unavailable OR the vector leg failing == keyword-only (mode
    "keyword"); only both legs failing returns mode "error". Never raises."""
    text = (text or "").strip()[:CLAIM_CHECK_TEXT_CAP]
    k = min(max(int(k), 1), 20)
    if not text:
        return {"mode": "none", "hits": []}
    try:
        kw = await _keyword_hits(text, k)
    except Exception:  # noqa: BLE001 - the writing-time check must never raise
        log.exception("claim_check keyword leg failed")
        kw = None
    try:
        vec = await vectors.embed(text)
        if vec is None:
            return {"mode": "keyword", "hits": kw or []} if kw is not None \
                else {"mode": "error", "hits": []}
        vec_hits: list[dict[str, Any]] = []
        rows = await db.query(
            "SELECT c.id, c.claim, c.category, vf.verdict, fv.embedding, fv.dim "
            "FROM fact_claim_vectors fv "
            "JOIN fact_cards c ON c.id = fv.fact_card_id "
            "JOIN verified_facts vf ON vf.fact_card_id = c.id "
            "WHERE fv.model = ? AND vf.verdict IN ('VERIFIED','DISPUTED') AND vf.expires_at > ?",
            (vectors.model_name(), bus.now_iso()),
        )
        for r in rows:
            other = _unpack_vec(r["embedding"], r["dim"])
            if len(other) != len(vec):
                continue
            sim = _cosine(vec, other)
            if sim >= CLAIM_CHECK_MIN_SIM:
                vec_hits.append({
                    "fact_card_id": r["id"], "claim": r["claim"], "category": r["category"],
                    "verdict": r["verdict"], "similarity": round(sim, 4), "source": "vector",
                })
        vec_hits.sort(key=lambda h: h["similarity"], reverse=True)
        seen = {h["fact_card_id"] for h in vec_hits}
        merged = vec_hits + [h for h in (kw or []) if h["fact_card_id"] not in seen]
        return {"mode": "vector+keyword", "hits": merged[:k]}
    except Exception:  # noqa: BLE001 - a broken vector row must not eat keyword hits
        log.exception("claim_check vector leg failed; degrading to keyword hits")
        if kw is not None:
            return {"mode": "keyword", "hits": kw}
        return {"mode": "error", "hits": []}


# ---- queries (API/MCP read surface) ------------------------------------------------

async def list_cards(
    status: str | None = None, category: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM fact_cards"
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if category:
        where.append("category = ?")
        params.append(category)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(min(max(limit, 1), 200))
    return await db.query(sql, params)


async def get_card(card_id: str) -> dict[str, Any] | None:
    card = await db.query_one("SELECT * FROM fact_cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    fact = await db.query_one(
        "SELECT * FROM verified_facts WHERE fact_card_id = ?", (card_id,)
    )
    if fact is None and card.get("related_fact_id"):
        # reused/self_contradicted: show the verdict the gate matched against
        fact = await db.query_one(
            "SELECT * FROM verified_facts WHERE id = ?", (card["related_fact_id"],)
        )
    if fact:
        try:
            fact["source_urls"] = json.loads(fact["source_urls"] or "[]")
        except ValueError:
            pass
    card["fact"] = fact
    return card


async def outbox_overview(limit: int = 50) -> dict[str, Any]:
    """Pending/failed counts plus newest delivery rows for operators."""
    counts = await db.query_one(
        "SELECT "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) AS delivered "
        "FROM factcheck_dispute_outbox"
    ) or {}
    rows = await db.query(
        "SELECT * FROM factcheck_dispute_outbox "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (min(max(int(limit), 1), 200),),
    )
    for row in rows:
        try:
            row["payload"] = json.loads(row["payload"])
        except (TypeError, ValueError):
            pass
    return {
        "pending": int(counts.get("pending") or 0),
        "failed": int(counts.get("failed") or 0),
        "delivered": int(counts.get("delivered") or 0),
        "recent": rows,
    }
