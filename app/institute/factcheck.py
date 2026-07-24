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
   the ``factcheck_reuse_policy`` admin_state row. The HIGHEST-similarity
   neighbor over the threshold decides — ``reused`` off a VERIFIED winner,
   ``self_contradicted`` off a DISPUTED one — but only after a consistency
   gate (numbers / date patterns / negation polarity must agree between the
   old and new claim); a top-similarity tie between conflicting verdicts, or
   a gate mismatch, sends the claim to normal verification instead. Vectors
   degraded == everything is fresh (the documented degrade-open posture).
4. **Verification** (``verify_pending``): pending cards are conditional-claimed
   pending→verifying IN THE DATABASE before any model call (two processes can
   never double-verify a card; REVIEW-C1 P1-1) under a random per-attempt
   lease token — settle/release carry ``AND lease_id = ?`` so a stale worker's
   late write loses once the sweep re-opens its card — and one slot of the
   SGT daily attempt budget is consumed atomically per model call — successes
   AND failures count, no refunds (the cap is a quota ceiling; refunding
   failures would let a flapping hand burn unbounded quota). The verdict is
   parsed by canonical-line extraction (only bare line-anchored ``VERDICT:
   <word>`` lines count; quotes/fences/indented code/HTML comments are
   context; conflicting canonical lines land UNVERIFIABLE outright), and
   VERIFIED/DISPUTED without non-empty evidence plus at least one source URL
   are downgraded UNVERIFIABLE. The verdict row and the card's terminal
   status commit in one transaction.
5. **Disputed surfacing**: DISPUTED verdicts and self_contradicted cards write
   durable outbox intents in the same transaction as the dispute — a
   'mailbox' intent (analyst notification) and an 'event' intent (the
   ``factcheck.disputed`` bus event the vault exporter consumes, now durable
   instead of a lossy post-commit emit). The drain atomically materializes
   one mailbox thread/note/dispatch per mailbox row, emits events
   at-least-once per event row, and marks rows delivered.

The app lifespan calls ``register()``, and ``scheduler.py`` mounts ``tick()``
as the gated 30-minute ``factcheck-tick`` job. Everything in this module
follows the house rules: model calls only via executor.submit, conditional
claims by rowcount, bus.now_iso() / work_date() for time, bus handlers never
raise (tick/drain top-level failures propagate to their @metered wrappers so
cron health sees them; per-row failures are absorbed inside).
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
from functools import lru_cache
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from ..util import new_id, read_text, session_workspace
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
VERIFY_MAX_ATTEMPTS = 3        # failed verification attempts before a card goes terminal (LOOP-P3)
VECTOR_SCAN_LIMIT = 2000       # bound for reuse-gate / claim_check embedding scans (LOOP-P10e)
STALE_RUNNING_MINUTES = 60     # extraction rows / verifying cards stuck longer are re-opened
DEFAULT_DAILY_CAP = 10         # INSTITUTE_FACTCHECK_DAILY_CAP fallback
OUTBOX_MAX_ATTEMPTS = 5        # terminal failed after this many delivery attempts
OUTBOX_PER_DRAIN = 20          # bounded scheduler batch
OUTBOX_LEASE_STALE_MINUTES = 10  # event-row drainer leases older than this are re-opened
CLAIM_CHECK_MIN_SIM = 0.75     # claim_check vector recall floor (loose on purpose: writing-time hints)
KEYWORD_MIN_OVERLAP = 0.4      # claim_check keyword fallback: claim-token coverage floor
CLAIM_CHECK_MAX_HITS = 5
CLAIM_CHECK_TEXT_CAP = 20000   # draft slice claim_check will embed/tokenize (MCP has no pydantic guard)
RESEARCH_REPORT_FILE = "06_深度报告.md"   # same artifact exporter._export_research reads

# Daily verification budget: one admin_state counter row per SGT work date
# ('factcheck_attempts:<date>'), consumed by _book_verification()'s
# conditional UPDATE — inside the same transaction as the card attempts+1 and
# the durable queued task row, BEFORE each model call (R4). Successes and
# failures both count, no refunds.
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
    # None (unset) falls back to the documented default.
    raw = get_settings().factcheck_daily_cap
    return DEFAULT_DAILY_CAP if raw is None else max(0, raw)


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

# Consistency-gate token extraction (deliberately lightweight, not NLP):
# numbers (commas stripped, %/‰ kept — "37%" and "37" are different claims),
# date-shaped patterns (ISO-ish, 中文年月日, quarters/halves), a small CN/EN
# negation lexicon whose occurrence counts are compared, and the ORDERED
# anchor sequence (numbers + latin words + CJK runs, in appearance order).
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*[%‰]?")
_DATE_RES = (
    re.compile(r"\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?"),
    re.compile(r"\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?"),
    re.compile(r"\d{1,2}月(?:\d{1,2}日)?"),
    re.compile(r"[QqHh][1-4]"),
)
_NEGATION_CN = ("没有", "不再", "未能", "无法", "不", "未", "没", "非", "无")
_NEGATION_EN_RE = re.compile(r"\b(?:not|no|never|without|none|n't)\b", re.IGNORECASE)
# Anchor tokens for the ORDER comparison (R2 P1-2): numbers, latin words,
# single CJK characters — everything except whitespace/punctuation, in
# sequence. CJK anchors are per CHARACTER so spacing inside Chinese text
# (which carries no meaning) cannot flip the comparison, while any actual
# reordering (subject/object swap, number re-attribution) still does.
_ANCHOR_RE = re.compile(r"\d+(?:[.,]\d+)*[%‰]?|[A-Za-z][A-Za-z0-9.\-]*|[\u4e00-\u9fff]")


def _claim_numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUM_RE.finditer(text or "")}


def _claim_dates(text: str) -> set[str]:
    out: set[str] = set()
    for pat in _DATE_RES:
        out.update(m.group(0).casefold() for m in pat.finditer(text or ""))
    return out


def _negation_count(text: str) -> int:
    text = text or ""
    count = len(_NEGATION_EN_RE.findall(text))
    for word in _NEGATION_CN:
        count += text.count(word)
    return count


def _claim_anchor_seq(text: str) -> list[str]:
    """Ordered content-token sequence: 数字/拉丁词/中文连续段 in appearance
    order, case- and thousands-separator-normalized. Whitespace, punctuation
    and (unhandled) symbols are not anchors, so pure re-punctuation of the
    same statement compares equal."""
    return [m.group(0).replace(",", "").casefold() for m in _ANCHOR_RE.finditer(text or "")]


def _consistency_gate(new_claim: str, old_claim: str) -> bool:
    """True == the near-duplicate claims agree on numbers, date patterns,
    negation polarity AND the ordered anchor sequence, so the verdict may be
    reused. Any mismatch answers False and the new claim goes through normal
    verification — cosine similarity alone cannot tell "涨了10%" from
    "涨了40%" or "获批" from "未获批" (audit finding 4), and bag-of-token
    equality cannot tell "A收购B" from "B收购A" or re-attributed numbers
    (R2 P1-2) — the SEQUENCE comparison binds numbers and entities to their
    positions, so any subject/object or attribution swap fails the gate.
    Deliberately conservative: not NLP, just set/count/sequence comparison;
    disagreeing means verify, never means dispute. The sequence check
    tightens reuse to statements identical modulo punctuation/whitespace/
    case — the cost of a false mismatch is one extra verification."""
    if _claim_numbers(new_claim) != _claim_numbers(old_claim):
        return False
    if _claim_dates(new_claim) != _claim_dates(old_claim):
        return False
    if _negation_count(new_claim) != _negation_count(old_claim):
        return False
    if _claim_anchor_seq(new_claim) != _claim_anchor_seq(old_claim):
        return False
    return True


async def _reuse_state(
    claim: str, vec: list[float] | None, category: str,
) -> tuple[str, str | None, float]:
    """(state, related verified_facts.id, similarity) for an embedded claim.

    state ∈ {fresh, reused, self_contradicted}. Vectors unavailable (vec is
    None) == fresh — the degrade-open contract. Neighbors are live verdict
    rows only (expires_at in the future, current embed model); the candidate
    set is NOT restricted to the same category — categories are model labels
    with jitter, and semantic similarity is category-agnostic — but the
    threshold applied is the NEW claim's category threshold.

    The HIGHEST-similarity neighbor over the threshold decides (a DISPUTED
    neighbor no longer unconditionally outranks a closer VERIFIED one); if
    conflicting verdicts tie exactly at the top similarity, the gate refuses
    to guess and the claim goes to verification. The winner must also pass
    _consistency_gate against the new claim (numbers / dates / negation
    polarity), otherwise the claim is fresh and gets verified. Never raises.
    """
    if vec is None:
        return "fresh", None, 0.0
    try:
        policy = await get_reuse_policy()
        threshold = float(policy.get(category, policy["other"])["threshold"])
        # newest verdicts first, clamped (LOOP-P10e): the in-Python cosine
        # loop must stay bounded as the fact store grows; a fact old enough
        # to fall outside the window simply stops gating (degrade-open)
        rows = await db.query(
            "SELECT fv.embedding, fv.dim, vf.id AS fact_id, vf.verdict, c.claim AS old_claim "
            "FROM fact_claim_vectors fv "
            "JOIN verified_facts vf ON vf.fact_card_id = fv.fact_card_id "
            "JOIN fact_cards c ON c.id = fv.fact_card_id "
            "WHERE fv.model = ? AND vf.expires_at > ? "
            "AND ((c.status='verified' AND vf.verdict='VERIFIED') "
            "  OR (c.status='disputed' AND vf.verdict='DISPUTED')) "
            "ORDER BY vf.verified_at DESC LIMIT ?",
            (vectors.model_name(), bus.now_iso(), VECTOR_SCAN_LIMIT),
        )
        best_sim = -1.0
        best: list[dict[str, Any]] = []   # every neighbor tied at best_sim
        for r in rows:
            other = _unpack_vec(r["embedding"], r["dim"])
            if len(other) != len(vec):
                continue  # different embedding space (defensive; model already filtered)
            sim = _cosine(vec, other)
            if sim < threshold:
                continue
            if sim > best_sim:
                best_sim = sim
                best = [r]
            elif sim == best_sim:
                best.append(r)
        if not best:
            return "fresh", None, 0.0
        if len({r["verdict"] for r in best}) != 1:
            # conflicting verdicts tied at the top: verify instead of guessing
            return "fresh", None, 0.0
        winner = best[0]
        if not _consistency_gate(claim, winner["old_claim"] or ""):
            return "fresh", None, 0.0
        state = "self_contradicted" if winner["verdict"] == "DISPUTED" else "reused"
        return state, winner["fact_id"], best_sim
    except Exception:  # noqa: BLE001 - the gate must never block extraction
        log.exception("reuse gate failed; treating claim as fresh")
        return "fresh", None, 0.0


async def check_reuse(claim: str, category: str) -> dict[str, Any]:
    """Public tier-1 gate: embed one claim and classify it against live facts."""
    vec = await vectors.embed(claim)
    state, fact_id, sim = await _reuse_state(claim, vec, _category(category))
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
    task = await executor.submit(get_settings().default_hand, prompt, source=SOURCE)
    if task.status != "completed":
        log.warning("claim extraction task %s ended %s for %s %s",
                    task.id, task.status, source_kind, source_ref)
        return None

    created: list[dict[str, Any]] = []
    for item in parse_claims(task.output or ""):
        claim, category = item["claim"], item["category"]
        vec = await vectors.embed(claim)
        state, related_fact_id, sim = await _reuse_state(claim, vec, category)
        status = {"fresh": "pending", "reused": "reused", "self_contradicted": "self_contradicted"}[state]
        card_id = new_id()
        row = {
            "id": card_id, "source_kind": source_kind, "source_ref": str(source_ref),
            "analyst_id": analyst_id or None, "claim": claim, "category": category,
            "status": status, "related_fact_id": related_fact_id, "similarity": round(sim, 4),
        }
        # the INSERT is the arbiter: OR IGNORE on content_hash makes re-runs
        # of the same source a per-claim no-op. A self-contradiction's outbox
        # intents (mailbox + durable event) land in THIS transaction with the
        # terminal card.
        outbox_id: str | None = None
        event_outbox_id: str | None = None
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
                evidence = f"与已被驳斥的事实相似度 {sim:.3f}（fact {related_fact_id}）"
                outbox_id = await _enqueue_dispute_outbox(
                    conn, row, kind="self_contradicted",
                    verdict_label=SELF_CONTRADICTED_LABEL,
                    evidence=evidence, sources="",
                )
                event_outbox_id = await _enqueue_dispute_event(
                    conn, row, kind="self_contradicted",
                    verdict_label=SELF_CONTRADICTED_LABEL,
                    evidence=evidence, sources="", thread_outbox_id=outbox_id,
                )
        if not inserted:
            continue  # already extracted from this source
        await _store_claim_vector(card_id, vec)
        created.append(row)
        if state == "self_contradicted":
            await _surface_dispute(card_id, [outbox_id, event_outbox_id])
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
# CommonMark-ish fences: an opener is a run of >=3 backticks OR tildes at <=3
# spaces of indent (4+ spaces is an indented code block, handled separately);
# the closer must repeat the SAME character at least as many times and carry
# nothing but whitespace (an info-string line inside a fence is content, and a
# ~~~ inside a ``` fence must never close it — kind and length pair up).
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
_INDENTED_CODE = re.compile(r"^(?: {4}|\t)")

# Line-anchored like the canonical verdict line (R2 P1-1): an EVIDENCE/SOURCES
# label buried mid-line (e.g. injected via the claim material, which
# _quote_material folds inline into the 【论断】 line) must not start an
# extraction. Markdown bold around the label is tolerated, same as VERDICT.
_EVIDENCE_RE = re.compile(
    r"^\s*\**\s*EVIDENCE\s*[:：]\s*\**\s*(.+?)(?=\n\s*\**\s*SOURCES\s*[:：]|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_SOURCES_LINE_RE = re.compile(
    r"^\s*\**\s*SOURCES\s*[:：]\s*(.+)", re.IGNORECASE | re.MULTILINE,
)
_URL_RE = re.compile(r"https?://[^\s)\]>」』]+")

_VERDICT_MATERIAL_GUARD = re.compile(r"(VERDICT|EVIDENCE|SOURCES)(\s*[:：])", re.IGNORECASE)


def _quote_material(text: str) -> str:
    """Neutralize untrusted material before it enters the verify prompt
    (REVIEW-C1 P1-2; the C4 ``_quote_detail`` precedent). Claims are one
    sentence by contract: collapsing whitespace keeps the material inline
    after the 【论断】 label, so nothing from it can sit at line start; the
    label guard (VERDICT plus, since R2 P1-1, EVIDENCE/SOURCES — injected
    proof labels must not survive either) is belt and braces for hands that
    re-wrap lines."""
    flat = " ".join((text or "").split())
    return _VERDICT_MATERIAL_GUARD.sub(r"\1 -\2", flat)


def _bare_lines(text: str) -> list[str]:
    """The answer surface of a verifier output: lines OUTSIDE fenced code
    blocks (```` ``` ````/``~~~`` paired by kind AND length — a mismatched
    fence can never fake a close), indented code blocks (4 spaces / tab at
    line start), HTML comment blocks and blockquotes (``>``).

    ONE filter for every parsing stage — verdict, evidence AND sources (R2
    P1-1: quoted material that cannot decide the verdict must not be able to
    satisfy the proof gate either)."""
    out: list[str] = []
    fence: tuple[str, int] | None = None   # (fence char, opener length)
    in_comment = False
    for line in (text or "").splitlines():
        if fence is not None:
            m = _FENCE_CLOSE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if _INDENTED_CODE.match(line):
            continue
        m = _FENCE_OPEN.match(line)
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            continue
        if "<!--" in line:
            if "-->" not in line.split("<!--", 1)[1]:
                in_comment = True
            # a line containing comment markers can never be a bare canonical
            # line (the verdict word must own the line), so skip it either way
            continue
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return out


def parse_verdict(text: str) -> str | None:
    """Canonical-line extraction, replacing the global regex cascade.

    Only bare, line-anchored ``VERDICT: <word>`` lines count; quoted material
    (see _bare_lines) is skipped. Multiple canonical lines that AGREE return
    that verdict; DISAGREEING lines are an ambiguous answer and land
    UNVERIFIABLE outright (the old conservative-order collapse still let
    VERIFIED+DISPUTED escalate to DISPUTED and page an analyst off a
    self-contradicting reply). No canonical line — including prose-only
    mentions, negations and the echoed prompt format line — returns None,
    which the caller lands as UNVERIFIABLE (the model was told the exact
    format; an answer that ignores it is not evidence)."""
    found: list[str] = []
    for line in _bare_lines(text):
        m = _CANON_VERDICT_LINE.match(line)
        if m:
            found.append(m.group(1).upper())
    if not found:
        return None
    if len(set(found)) != 1:
        return "UNVERIFIABLE"
    return found[0]


def _parse_evidence(text: str) -> tuple[str, list[str]]:
    """(evidence, source_urls) off the verifier output; both degrade to empty.

    Extraction runs over the SAME bare-line surface as parse_verdict (R2
    P1-1): EVIDENCE/SOURCES lines — and URLs — inside fences, indented code,
    HTML comments or blockquotes are quoted material; they must not let a
    VERIFIED/DISPUTED pass the actionable-verdict proof gate."""
    text = "\n".join(_bare_lines(text))
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


class _BookingRefused(Exception):
    """Internal control flow for _book_verification: raising inside the
    booking transaction rolls back EVERY leg (daily slot included)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def _book_verification(
    card: dict[str, Any], lease_id: str,
) -> tuple[str | None, str]:
    """Book one verification attempt ATOMICALLY (R4 P1+P3): the daily-slot
    conditional increment, the card's attempts+1 + verify_task_id binding
    (under OUR lease), and a durable born-'queued' tasks row all commit in
    ONE transaction — or none of them do. Returns (task_id, "ok") on
    success, (None, "budget") when today's cap is spent, (None, "lost") when
    the claim was lost mid-way (everything, the daily slot included, rolled
    back — the R3 standalone prebook could burn a slot and crash before the
    card bump, and could count an attempt with no task row behind it).

    After a hard crash the booked attempt is never a phantom: its queued
    tasks row exists and is either driven to a verdict or explicitly settled
    by the boot orphan sweep (executor.recover_orphans) — nothing guesses
    whether the model started. Successes and failures both stay booked, no
    refunds (the cap is a quota ceiling)."""
    cap = _daily_cap()
    if cap <= 0:
        return None, "budget"
    settings = get_settings()
    task_id = new_id()
    workspace = settings.workspaces_dir / "adhoc" / task_id
    prompt = (
        f"{date_anchor()}\n\n"
        + CLAIM_VERIFY_PROMPT.format(claim=_quote_material(card["claim"]), category=card["category"])
    )
    hand = settings.default_hand
    key = _attempts_key()
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO admin_state (key, value) VALUES (?, '0')", (key,)
            )
            cur = await conn.execute(
                "UPDATE admin_state SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                "WHERE key = ? AND CAST(value AS INTEGER) < ?",
                (key, cap),
            )
            if cur.rowcount == 0:
                raise _BookingRefused("budget")
            cur = await conn.execute(
                "UPDATE fact_cards SET attempts=attempts+1, verify_task_id=? "
                "WHERE id=? AND status='verifying' AND lease_id=?",
                (task_id, card["id"], lease_id),
            )
            if cur.rowcount == 0:
                raise _BookingRefused("lost")
            # the executor row shape (executor.book_prepared), born 'queued'
            # inside OUR transaction; the task.queued event is emitted
            # post-commit (bus.emit inside transaction() would deadlock)
            await executor.book_prepared(
                conn=conn, task_id=task_id, hand=hand, prompt=prompt, source=SOURCE,
                workspace=workspace, timeout_s=settings.default_timeout_s,
            )
    except _BookingRefused as refusal:
        return None, refusal.reason
    workspace.mkdir(parents=True, exist_ok=True)
    await bus.emit("task.queued", "task", task_id, {"hand": hand, "source": SOURCE})
    return task_id, "ok"


async def _run_verification_task(task_id: str) -> executor.Task:
    """Drive one pre-booked queued verification task through the executor
    core: submit_prepared() attaches the executor's own claim-and-run driver
    (_execute's conditional queued→running claim, hand mutex + global
    semaphore, fallback, terminal settle) to the row our booking transaction
    created — the ONE execution path, entered one layer in because
    submit()/spawn() insist on creating their own rows and R4 P1 requires
    the row to pre-exist in the booking transaction. The registration keeps
    operator cancel and the shutdown drain working on in-flight
    verifications."""
    return await executor.submit_prepared(task_id)


async def _claim_card(card_id: str) -> str | None:
    """pending→verifying conditional claim, BEFORE the model call (P1-1).

    Returns the fresh lease token written into the row, or None when the
    claim was lost. Every later transition of THIS attempt (settle / release)
    must present the lease — a stale worker whose card was re-opened by the
    sweep (and possibly re-claimed under a new lease) fails the ``AND
    lease_id = ?`` condition and its late write is dropped (finding 5).
    Claiming clears the prior generation id; booking atomically installs the
    new one, making NULL an explicit pre-book/unspent state."""
    lease_id = uuid.uuid4().hex
    n = await db.execute(
        "UPDATE fact_cards SET status='verifying', verify_started_at=?, lease_id=?, "
        "verify_task_id=NULL "
        "WHERE id=? AND status='pending'",
        (bus.now_iso(), lease_id, card_id),
    )
    return lease_id if n else None


async def _release_card(card_id: str, lease_id: str) -> None:
    """verifying→pending WITHOUT counting a card attempt (used when the
    daily budget ran out before any model call — the card itself did nothing
    wrong, and the attempt pre-book deliberately happens AFTER the budget
    check; R3 P1). Conditional on OUR lease: a card someone else re-claimed
    is theirs to release."""
    await db.execute(
        "UPDATE fact_cards SET status='pending', verify_started_at=NULL, lease_id=NULL "
        "WHERE id=? AND status='verifying' AND lease_id=? AND verify_task_id IS NULL",
        (card_id, lease_id),
    )


async def _settle_exhausted_card(
    card_id: str, category: str, *, lease_id: str | None,
    verify_task_id: str | None = None,
) -> bool:
    """Settle a retry-exhausted card terminal in ONE transaction (LOOP-P3):
    status 'unverifiable' + a verified_facts row naming the exhaustion — a
    card must never be terminal without its verdict row ('unverifiable' is
    the terminal used because the 0015 status CHECK is immutable and has no
    'failed' member; semantics match: no verdict could be obtained, and
    UNVERIFIABLE rows never feed the reuse gate or claim_check).

    attempts are NOT bumped here — every attempt is booked atomically by
    _book_verification before its model call (R3 P1/R4), so settling only
    checks the exhaustion. ``lease_id`` set == settling OUR 'verifying' claim
    after its final failed attempt; ``lease_id=None`` == the recovery sweep
    settling a 'pending' card whose attempts are already exhausted (hard
    crash mid-attempt, or a lowered limit)."""
    policy = await get_reuse_policy()
    ttl_days = float(policy.get(category, policy["other"])["ttl_days"])
    now = bus.now_iso()
    evidence = (f"验证任务连续失败 {VERIFY_MAX_ATTEMPTS} 次，超出重试上限，"
                "不再自动重试（操作员可重开）。")
    async with db.transaction() as conn:
        if lease_id is not None:
            cur = await conn.execute(
                "UPDATE fact_cards SET status='unverifiable', verify_started_at=NULL, "
                "lease_id=NULL "
                "WHERE id=? AND status='verifying' AND lease_id=? "
                "AND verify_task_id IS ? AND attempts>=?",
                (card_id, lease_id, verify_task_id, VERIFY_MAX_ATTEMPTS),
            )
        else:
            cur = await conn.execute(
                "UPDATE fact_cards SET status='unverifiable' "
                "WHERE id=? AND status='pending' AND attempts>=?",
                (card_id, VERIFY_MAX_ATTEMPTS),
            )
        settled = bool(cur.rowcount)
        await cur.close()
        if not settled:
            return False
        # UPSERT, not OR IGNORE (R4 P1): an operator-reset card may already
        # own a verdict row (UNIQUE fact_card_id) — and if that row says
        # VERIFIED/DISPUTED, IGNORE would leave it ACTIVE while the card says
        # unverifiable, feeding the dead card's old conclusion to the reuse
        # gate and claim_check forever. The active row flips to UNVERIFIABLE
        # in the same generation as the card status (row id preserved).
        await conn.execute(
            "INSERT INTO verified_facts "
            "(id, fact_card_id, verdict, evidence, source_urls, work_date, verified_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(fact_card_id) DO UPDATE SET "
            "verdict=excluded.verdict, evidence=excluded.evidence, "
            "source_urls=excluded.source_urls, work_date=excluded.work_date, "
            "verified_at=excluded.verified_at, expires_at=excluded.expires_at",
            (new_id(), card_id, "UNVERIFIABLE", evidence, "[]",
             work_date(), now, _now_plus_days(ttl_days)),
        )
    log.warning("card %s exhausted %d verification attempts; settled unverifiable",
                card_id, VERIFY_MAX_ATTEMPTS)
    return True


async def _quarantine_verification_binding(
    card: dict[str, Any], reason: str,
) -> bool:
    """Fail closed when a stale card's durable task binding cannot be trusted.

    A non-null id whose task is missing/mismatched is not retry permission:
    silently re-opening would create an unbounded new generation with no
    auditable relationship to the booked attempt. Preserve ``verify_task_id``
    as provenance and settle an explicit UNVERIFIABLE row under the exact
    stale lease + binding snapshot.
    """
    policy = await get_reuse_policy()
    category = str(card["category"])
    ttl_days = float(policy.get(category, policy["other"])["ttl_days"])
    now = bus.now_iso()
    evidence = f"验证任务绑定异常，已隔离且不自动重试：{reason}"[:2000]
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE fact_cards SET status='unverifiable', verify_started_at=NULL, "
            "lease_id=NULL "
            "WHERE id=? AND status='verifying' AND lease_id IS ? "
            "AND verify_task_id IS ?",
            (card["id"], card.get("lease_id"), card.get("verify_task_id")),
        )
        settled = bool(cur.rowcount)
        await cur.close()
        if not settled:
            return False
        await conn.execute(
            "INSERT INTO verified_facts "
            "(id, fact_card_id, verdict, evidence, source_urls, work_date, verified_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(fact_card_id) DO UPDATE SET "
            "verdict=excluded.verdict, evidence=excluded.evidence, "
            "source_urls=excluded.source_urls, work_date=excluded.work_date, "
            "verified_at=excluded.verified_at, expires_at=excluded.expires_at",
            (new_id(), card["id"], "UNVERIFIABLE", evidence, "[]",
             work_date(), now, _now_plus_days(ttl_days)),
        )
    log.error("quarantined fact card %s: %s", card["id"], reason)
    return True


async def _release_failed_card(
    card: dict[str, Any], lease_id: str, verify_task_id: str,
) -> None:
    """Release a claimed card after a FAILED verification attempt (LOOP-P3).
    The attempt itself was already booked atomically by _book_verification
    BEFORE the model call (R3 P1/R4) — no second bump here, exactly one
    count per attempt whether it ends in a handler or a hard crash. The
    release only lands while retries remain; a card at VERIFY_MAX_ATTEMPTS
    is settled terminal instead of going back into rotation — one poison
    card used to re-enter the picker every tick and burn the whole daily
    cap."""
    n = await db.execute(
        "UPDATE fact_cards SET status='pending', verify_started_at=NULL, lease_id=NULL "
        "WHERE id=? AND status='verifying' AND lease_id=? "
        "AND verify_task_id=? AND attempts < ?",
        (card["id"], lease_id, verify_task_id, VERIFY_MAX_ATTEMPTS),
    )
    if n:
        return
    # zero rows == either our claim was lost (the guarded settle below no-ops
    # too — the new owner does its own accounting) or this attempt exhausted
    # the retry budget: settle terminal under OUR lease
    await _settle_exhausted_card(
        card["id"], card["category"], lease_id=lease_id,
        verify_task_id=verify_task_id,
    )


async def verify_pending(cap: int | None = None) -> list[dict[str, Any]]:
    """Verify pending fact cards within the daily attempt cap, least-retried
    first (attempts ASC, then created_at ASC — a poison card must not shadow
    fresh work; LOOP-P3), skipping cards whose retries are exhausted.

    ``cap`` further bounds THIS call (the tick passes VERIFY_PER_TICK). Per
    card, in order: pending→verifying conditional claim (the cross-process
    double-verification guard), then ONE atomic booking transaction (R4
    P1+P3: daily-slot increment + card attempts+1 + verify_task_id binding +
    a durable born-'queued' tasks row — all or nothing, so no crash window
    can consume a slot without a card attempt, or a card attempt without a
    recoverable task), then the executor drives the booked task, then
    verdict row + terminal status in one transaction conditional on the
    lease. A failed model task/crash releases the card back to pending
    WITHOUT a second bump (the booking already counted it); the failure that
    finds VERIFY_MAX_ATTEMPTS spent settles the card terminal instead — one
    poison card used to re-enter the picker every tick and burn the whole
    daily cap. A completed task whose output has no parseable verdict lands
    UNVERIFIABLE (retrying an unparseable answer forever would burn quota
    for nothing).
    """
    call_budget = max(0, int(cap)) if cap is not None else None
    results: list[dict[str, Any]] = []
    while call_budget is None or len(results) < call_budget:
        card = await db.query_one(
            "SELECT * FROM fact_cards WHERE status = 'pending' AND attempts < ? "
            "ORDER BY attempts ASC, created_at ASC LIMIT 1",
            (VERIFY_MAX_ATTEMPTS,),
        )
        if card is None:
            break
        lease_id = await _claim_card(card["id"])
        if lease_id is None:
            continue  # lost the race; the next loop picks another card
        task_id, reason = await _book_verification(card, lease_id)
        if task_id is None:
            if reason == "budget":
                # today's budget is gone: hand the card back untouched and
                # stop — the refused booking rolled back whole, so neither a
                # daily slot nor a card attempt was consumed
                await _release_card(card["id"], lease_id)
                break
            continue  # "lost": claim gone mid-way; nothing was consumed
        try:
            results.append(await _verify_card(card, lease_id, task_id))
        except Exception as exc:  # noqa: BLE001 - one card must never break the sweep
            log.exception("verification crashed for card %s", card["id"])
            await _release_failed_card(card, lease_id, task_id)
            results.append({"card_id": card["id"], "status": "crashed", "error": str(exc)[:200]})
    return results


async def _settle_completed_verification(
    card: dict[str, Any], lease_id: str, task_id: str, output: str,
) -> dict[str, Any]:
    """Parse and settle an ALREADY completed verification task.

    Shared by the normal driver and task-aware crash recovery (R5 P1-1):
    recovery must reuse durable output without entering the executor. The
    card transition is claimed by the exact triple
    ``card.id + lease_id + verify_task_id`` so an old task can never settle
    a card that a newer lease/generation has reclaimed.
    """
    verdict = parse_verdict(output)
    evidence, urls = _parse_evidence(output)
    if verdict is None:
        verdict = "UNVERIFIABLE"
        evidence = ("核查输出无法解析出判定：" + " ".join(output.split()))[:500]
    elif verdict != "UNVERIFIABLE" and (not evidence or not urls):
        # actionable verdicts need proof: a bare "VERDICT: VERIFIED" without
        # non-empty EVIDENCE and at least one SOURCES URL must neither mint a
        # reusable fact nor page an analyst (findings 2/3)
        log.info("card %s: %s lacked evidence/sources; downgraded UNVERIFIABLE",
                 card["id"], verdict)
        evidence = (f"{verdict} 判定缺少证据或来源链接，降级 UNVERIFIABLE。"
                    + (evidence or ""))[:2000]
        verdict = "UNVERIFIABLE"

    policy = await get_reuse_policy()
    ttl_days = float(policy.get(card["category"], policy["other"])["ttl_days"])
    now = bus.now_iso()
    fact_id = new_id()
    status = verdict.lower()  # VERIFIED→verified / DISPUTED→disputed / UNVERIFIABLE→unverifiable
    outbox_id: str | None = None
    event_outbox_id: str | None = None

    # one transaction: terminal status + verdict row + (for DISPUTED) delivery
    # intents land together, conditional on the claim WE hold (status AND
    # lease — a card the stale sweep re-opened, even if re-claimed since, has
    # a different lease and our late write is dropped; finding 5).
    # NB: transaction() holds the db write lock — use the yielded conn
    # directly (db.execute/bus.emit in here would deadlock); events after.
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE fact_cards SET status = ?, verify_started_at = NULL, lease_id = NULL "
            "WHERE id = ? AND status = 'verifying' AND lease_id = ? "
            "AND verify_task_id = ?",
            (status, card["id"], lease_id, task_id),
        )
        settled = bool(cur.rowcount)
        await cur.close()
        if not settled:
            # claim/generation lost mid-flight: discard THIS task's result
            log.info(
                "card %s no longer held under lease/task %s; discarding result",
                card["id"], task_id,
            )
            return {"card_id": card["id"], "status": "lost_claim", "task_id": task_id}
        # UPSERT on the UNIQUE fact_card_id (R4 P1): an operator-reset card
        # already owns a verdict row — the ACTIVE row must flip to the new
        # generation's verdict (a bare INSERT crashed; OR IGNORE would keep a
        # stale VERIFIED alive under an unverifiable/re-verified card). The
        # conflict path keeps the existing row id, so re-read the live id for
        # the outbox/event provenance.
        await conn.execute(
            "INSERT INTO verified_facts "
            "(id, fact_card_id, verdict, evidence, source_urls, work_date, verified_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(fact_card_id) DO UPDATE SET "
            "verdict=excluded.verdict, evidence=excluded.evidence, "
            "source_urls=excluded.source_urls, work_date=excluded.work_date, "
            "verified_at=excluded.verified_at, expires_at=excluded.expires_at",
            (fact_id, card["id"], verdict, evidence, json.dumps(urls, ensure_ascii=False),
             work_date(), now, _now_plus_days(ttl_days)),
        )
        cur = await conn.execute(
            "SELECT id FROM verified_facts WHERE fact_card_id = ?", (card["id"],)
        )
        live = await cur.fetchone()
        await cur.close()
        if live is not None:
            fact_id = str(live["id"])
        if verdict == "DISPUTED":
            disputed_card = {
                **card, "related_fact_id": fact_id,
                "verify_task_id": task_id,
            }
            outbox_id = await _enqueue_dispute_outbox(
                conn, disputed_card, kind="disputed",
                verdict_label=DISPUTED_LABEL, evidence=evidence, sources=" ".join(urls),
            )
            event_outbox_id = await _enqueue_dispute_event(
                conn, disputed_card, kind="disputed",
                verdict_label=DISPUTED_LABEL, evidence=evidence, sources=" ".join(urls),
                thread_outbox_id=outbox_id,
            )

    await bus.emit("factcheck.verified", "fact_card", card["id"], {
        "fact_id": fact_id, "verdict": verdict, "category": card["category"],
        "claim": card["claim"][:500], "analyst_id": card["analyst_id"],
        "source_kind": card["source_kind"], "source_ref": card["source_ref"],
    })
    if verdict == "DISPUTED":
        await _surface_dispute(card["id"], [outbox_id, event_outbox_id])
    log.info("card %s verified: %s (%s)", card["id"], verdict, card["category"])
    return {"card_id": card["id"], "status": "completed", "verdict": verdict,
            "fact_id": fact_id, "task_id": task_id}


async def _verify_card(
    card: dict[str, Any], lease_id: str, task_id: str,
) -> dict[str, Any]:
    """Drive one atomically booked task, then settle its durable result."""
    task = await _run_verification_task(task_id)
    if task.status != "completed":
        # terminal hand failure: the booked slot + card attempt stay spent;
        # release only THIS exact lease/task generation
        log.warning(
            "verification task %s ended %s for card %s",
            task.id, task.status, card["id"],
        )
        await _release_failed_card(card, lease_id, task_id)
        return {"card_id": card["id"], "status": "task_failed", "task_id": task.id}
    return await _settle_completed_verification(
        card, lease_id, task_id, task.output or "",
    )


# ---- disputed surfacing + durable outbox -------------------------------------

def _dispute_payload(
    card: dict[str, Any], *, kind: str, verdict_label: str, evidence: str, sources: str,
) -> dict[str, Any]:
    if kind == "disputed":
        generation = str(card.get("verify_task_id") or "")
        if not generation:
            raise ValueError("disputed outbox requires verify_task_id generation")
        snapshot_verdict = "DISPUTED"
    else:
        # self-contradicted cards are terminal at extraction and never own a
        # verification task; their immutable card id is their one generation.
        generation = f"extract:{card['id']}"
        snapshot_verdict = "SELF_CONTRADICTED"
    return {
        "verification_generation": generation,
        "verify_task_id": generation if kind == "disputed" else None,
        "snapshot": {
            "verdict": snapshot_verdict,
            "claim": card["claim"][:500],
            "category": card.get("category"),
            "evidence": (evidence or "")[:1000],
            "source_urls": sources or "",
        },
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
            "verdict": snapshot_verdict,
            "verify_task_id": generation if kind == "disputed" else None,
            "evidence": (evidence or "")[:1000],
            "source_urls": sources or "",
        },
    }


def _dispute_generation(card: dict[str, Any], kind: str) -> str:
    if kind == "disputed":
        generation = str(card.get("verify_task_id") or "")
        if not generation:
            raise ValueError("disputed outbox requires verify_task_id generation")
        return generation
    return f"extract:{card['id']}"


async def _enqueue_dispute_outbox(
    conn: Any, card: dict[str, Any], *, kind: str, verdict_label: str,
    evidence: str, sources: str,
) -> str | None:
    """Write one analyst-delivery intent using the caller's dispute transaction."""
    recipient_id = str(card.get("analyst_id") or "")
    if not recipient_id:
        return None
    outbox_id = new_id()
    dispute_id = f"{kind}:{card['id']}:{_dispute_generation(card, kind)}"
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


async def _enqueue_dispute_event(
    conn: Any, card: dict[str, Any], *, kind: str, verdict_label: str,
    evidence: str, sources: str, thread_outbox_id: str | None,
) -> str | None:
    """Write the durable ``factcheck.disputed`` emission intent in the
    caller's dispute transaction (R1 finding 4: the old post-commit
    best-effort emit was simply lost if the process died first).

    recipient_id='' rides the 0025 UNIQUE(dispute_id, recipient_id) key —
    exactly one event intent per dispute, analyst or not. thread_id is the
    DETERMINISTIC mailbox thread id the sibling mailbox intent will
    materialize (factcheck-<outbox id>), or None when the card has no
    analyst — same shape consumers always saw."""
    outbox_id = new_id()
    dispute_id = f"{kind}:{card['id']}:{_dispute_generation(card, kind)}"
    payload = _dispute_payload(
        card, kind=kind, verdict_label=verdict_label,
        evidence=evidence, sources=sources,
    )
    event = payload["event"]
    event["thread_id"] = f"factcheck-{thread_outbox_id}" if thread_outbox_id else None
    cur = await conn.execute(
        "INSERT OR IGNORE INTO factcheck_dispute_outbox "
        "(id, dispute_id, fact_card_id, recipient_id, payload, status, attempts, created_at, intent) "
        "VALUES (?,?,?,'',?,'pending',0,?,'event')",
        (outbox_id, dispute_id, card["id"],
         json.dumps(payload, ensure_ascii=False), bus.now_iso()),
    )
    inserted = bool(cur.rowcount)
    await cur.close()
    if inserted:
        return outbox_id
    cur = await conn.execute(
        "SELECT id FROM factcheck_dispute_outbox WHERE dispute_id=? AND recipient_id=''",
        (dispute_id,),
    )
    existing = await cur.fetchone()
    await cur.close()
    return str(existing["id"]) if existing else None


def _parse_outbox_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid outbox payload JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("outbox payload must be an object")
    return payload


async def _dispute_generation_is_current(
    row: dict[str, Any], payload: dict[str, Any],
) -> bool:
    """Whether a DISPUTED intent still describes the card's active generation.

    Other intent kinds retain their existing semantics. Legacy DISPUTED
    payloads without immutable task provenance fail closed: they cannot prove
    which mutable verified_facts value they describe.
    """
    event = payload.get("event")
    is_disputed = (
        str(row.get("dispute_id") or "").startswith("disputed:")
        or (isinstance(event, dict) and event.get("kind") == "disputed")
    )
    if not is_disputed:
        return True
    generation = payload.get("verify_task_id")
    snapshot = payload.get("snapshot")
    if (
        not isinstance(generation, str)
        or not generation
        or not isinstance(snapshot, dict)
        or snapshot.get("verdict") != "DISPUTED"
    ):
        return False
    live = await db.query_one(
        "SELECT c.status, c.verify_task_id, vf.verdict "
        "FROM fact_cards c "
        "LEFT JOIN verified_facts vf ON vf.fact_card_id=c.id "
        "WHERE c.id=?",
        (row["fact_card_id"],),
    )
    return bool(
        live
        and live["status"] == "disputed"
        and live["verdict"] == "DISPUTED"
        and live["verify_task_id"] == generation
    )


async def _mark_outbox_superseded(row: dict[str, Any]) -> bool:
    """Terminalize, but retain, a stale-generation audit row."""
    n = await db.execute(
        "UPDATE factcheck_dispute_outbox "
        "SET status='failed', last_error='superseded-generation', "
        "lease_id=NULL, leased_at=NULL "
        "WHERE id=? AND status='pending' AND lease_id IS NULL AND attempts=?",
        (row["id"], int(row["attempts"])),
    )
    return bool(n)


async def _record_outbox_failure(row: dict[str, Any], error: str) -> str | None:
    """Count one failed attempt with attempts as the CAS version.

    A CAS miss on the snapshot's attempts re-reads the live row ONCE and
    retries at the fresh value (LOOP-P10c: a concurrent drain's own failure
    record used to make this a silent no-op — the failure went uncounted).
    A row that is no longer pending (delivered/failed elsewhere) or still
    leased records nothing."""
    attempts = int(row["attempts"])
    for _ in range(2):
        next_status = "failed" if attempts + 1 >= OUTBOX_MAX_ATTEMPTS else "pending"
        n = await db.execute(
            "UPDATE factcheck_dispute_outbox "
            "SET attempts=attempts+1, status=?, last_error=? "
            "WHERE id=? AND status='pending' AND lease_id IS NULL AND attempts=?",
            (next_status, error[:500], row["id"], attempts),
        )
        if n:
            return next_status
        fresh = await db.query_one(
            "SELECT status, attempts, lease_id FROM factcheck_dispute_outbox WHERE id=?",
            (row["id"],),
        )
        if fresh is None or fresh["status"] != "pending" or fresh["lease_id"] is not None:
            return None  # settled or actively leased elsewhere: not ours to record
        attempts = int(fresh["attempts"])
    return None


async def _deliver_dispute_outbox_row(row: dict[str, Any]) -> str | None:
    """Persist exactly one mailbox notification and mark the row delivered.

    The deterministic thread id is the mailbox-side idempotency key. Thread,
    operator note, pending dispatch, attempt increment, and delivered marker
    commit together, so a crash can expose either all of them or none.
    """
    payload = _parse_outbox_payload(row)
    if not payload.get("subject") or not payload.get("body"):
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


async def _emit_dispute_event_row(row: dict[str, Any]) -> str:
    """Emit one durable ``factcheck.disputed`` event and mark the row
    delivered. Returns ``emitted`` / ``skipped`` (lost the claim) /
    ``retry`` / ``failed`` (emit failure, before / at the attempts limit).

    The claim is a drainer LEASE (``SET lease_id WHERE lease_id IS NULL`` —
    the fact_cards pattern), not a bare attempts CAS: bus.emit cannot run
    inside a transaction (it writes the events table itself — the
    _verify_card deadlock note applies), and a value-CAS alone lets a second
    drainer re-SELECT the row after our claim and win a CAS at the
    incremented value before our emit lands (R2 P1-3 double emit). One
    attempt is booked at claim time; every later write on this attempt
    carries ``AND lease_id = ?``. Post-claim failures are fully accounted
    HERE (attempts already booked; the generic _record_outbox_failure must
    not double-count them — LOOP-P10c) and never raise:

    - emit FAILED → the event did not go out: release the lease with
      last_error and the row pending (or failed when this was the last
      attempt);
    - emit OK but the delivered marker failed → the row stays PENDING and
      retryable, never failed (R2 P1-3: at-least-once is the declared
      posture and consumers project idempotently) — lease released
      best-effort, else the stale-lease sweep re-opens it."""
    payload = _parse_outbox_payload(row)
    event = payload.get("event")
    if not isinstance(event, dict):
        raise ValueError("event outbox payload requires an event object")
    lease_id = uuid.uuid4().hex
    n = await db.execute(
        "UPDATE factcheck_dispute_outbox "
        "SET lease_id=?, leased_at=?, attempts=attempts+1 "
        "WHERE id=? AND status='pending' AND lease_id IS NULL AND attempts<?",
        (lease_id, bus.now_iso(), row["id"], OUTBOX_MAX_ATTEMPTS),
    )
    if not n:
        return "skipped"  # another drainer holds/handled it, or attempts exhausted
    try:
        await bus.emit("factcheck.disputed", "fact_card", str(row["fact_card_id"]), event)
    except Exception as exc:  # noqa: BLE001 - accounted here, never re-raised (P10c)
        # the event did NOT go out: release under OUR lease, pending for a
        # later retry — or terminal failed when this attempt was the last
        next_status = "failed" if int(row["attempts"]) + 1 >= OUTBOX_MAX_ATTEMPTS else "pending"
        log.warning("dispute event emit failed for outbox row %s: %s", row["id"], exc)
        try:
            await db.execute(
                "UPDATE factcheck_dispute_outbox "
                "SET lease_id=NULL, leased_at=NULL, last_error=?, status=? "
                "WHERE id=? AND lease_id=?",
                (str(exc)[:500], next_status, row["id"], lease_id),
            )
        except Exception:  # noqa: BLE001 - the stale-lease sweep recovers
            log.warning("lease release failed for outbox row %s after emit failure", row["id"])
        return "failed" if next_status == "failed" else "retry"
    try:
        marked = await db.execute(
            "UPDATE factcheck_dispute_outbox "
            "SET status='delivered', delivered_at=?, last_error=NULL, lease_id=NULL, leased_at=NULL "
            "WHERE id=? AND lease_id=?",
            (bus.now_iso(), row["id"], lease_id),
        )
        if not marked:
            # our lease was swept mid-window (stale sweep): someone else may
            # re-emit — documented at-least-once
            log.info("event outbox row %s lost its lease after emit; may re-deliver", row["id"])
    except Exception:  # noqa: BLE001 - the event DID go out: never escalate to failed
        log.exception(
            "delivered marker failed for event outbox row %s (event already emitted; "
            "row stays pending for an at-least-once retry)", row["id"])
        try:
            await db.execute(
                "UPDATE factcheck_dispute_outbox "
                "SET lease_id=NULL, leased_at=NULL, last_error=? "
                "WHERE id=? AND lease_id=?",
                ("delivered marker failed after emit", row["id"], lease_id),
            )
        except Exception:  # noqa: BLE001
            log.warning("lease release failed for %s; stale-lease sweep will recover", row["id"])
    return "emitted"


async def drain_dispute_outbox(
    limit: int = OUTBOX_PER_DRAIN, *, outbox_id: str | None = None,
) -> dict[str, Any]:
    """Retry pending delivery intents without invoking a model: 'mailbox'
    rows materialize the analyst thread (the separately gated mailbox sweep
    starts the model call), 'event' rows emit the durable
    ``factcheck.disputed`` bus event.

    Per-item failures are caught, counted and retried up to
    OUTBOX_MAX_ATTEMPTS (a poison row must not stop the batch). TOP-LEVEL
    failures (the retry-limit sweep / the batch SELECT) now PROPAGATE to the
    caller — the scheduler wraps this in @metered("factcheck-outbox"), which
    records the firing as failed in cron health instead of the old silent
    self-swallow (finding 7); in-process callers (_surface_dispute) guard
    themselves."""
    result: dict[str, Any] = {
        "delivered": 0, "events": 0, "retried": 0, "failed": 0, "thread_ids": {},
    }
    # re-open event rows whose drainer died inside the claim→emit→delivered
    # window (lease stale); their booked attempts stay spent
    stale_cutoff = (
        datetime.fromisoformat(bus.now_iso())
        - timedelta(minutes=OUTBOX_LEASE_STALE_MINUTES)
    ).isoformat(timespec="seconds")
    n = await db.execute(
        "UPDATE factcheck_dispute_outbox SET lease_id=NULL, leased_at=NULL "
        "WHERE status='pending' AND lease_id IS NOT NULL AND leased_at < ?",
        (stale_cutoff,),
    )
    if n:
        log.warning("re-opened %d stale-leased dispute outbox rows", n)
    result["failed"] += await db.execute(
        "UPDATE factcheck_dispute_outbox "
        "SET status='failed', last_error=COALESCE(last_error, 'retry limit reached') "
        "WHERE status='pending' AND lease_id IS NULL AND attempts>=?",
        (OUTBOX_MAX_ATTEMPTS,),
    )
    params: list[Any] = []
    sql = (
        "SELECT * FROM factcheck_dispute_outbox "
        "WHERE status='pending' AND attempts<? AND lease_id IS NULL"
    )
    params.append(OUTBOX_MAX_ATTEMPTS)
    if outbox_id is not None:
        sql += " AND id=?"
        params.append(outbox_id)
    sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
    params.append(min(max(int(limit), 1), 200))
    for row in await db.query(sql, params):
        try:
            payload = _parse_outbox_payload(row)
            if not await _dispute_generation_is_current(row, payload):
                if await _mark_outbox_superseded(row):
                    result["failed"] += 1
                continue
            if row["intent"] == "event":
                outcome = await _emit_dispute_event_row(row)
                if outcome == "emitted":
                    result["events"] += 1
                elif outcome == "retry":
                    result["retried"] += 1
                elif outcome == "failed":
                    result["failed"] += 1
                continue
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
    return result


async def _surface_dispute(card_id: str, outbox_ids: list[str | None]) -> None:
    """Best-effort immediate drain of the intents this dispute just enqueued
    (mailbox thread first, then the durable event, so the emitted payload's
    thread pointer refers to a thread that already exists on the happy path).
    Never raises — rows left pending are re-driven by the factcheck-outbox
    scheduler drain."""
    for outbox_id in outbox_ids:
        if not outbox_id:
            continue
        try:
            await drain_dispute_outbox(limit=1, outbox_id=outbox_id)
        except Exception:  # noqa: BLE001 - surfacing must never break verification
            log.exception("immediate dispute outbox drain failed for card %s", card_id)


# ---- hooks (bus.on subscriptions; registered by the app lifespan) ------------

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
        (new_id(), source_kind, str(source_ref), analyst_id or None, bus.now_iso()),
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
    lifespan."""
    bus.on("whiteboard.card_completed", _on_card_completed)
    bus.on("research.completed", _on_research_completed)
    log.info("factcheck hooks registered (card_completed, research.completed)")


# ---- source text resolution ------------------------------------------------------

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
        ws = await session_workspace(board["session_id"]) if board else None
        if ws and card["output_file"]:
            text = read_text(ws / str(card["output_file"]))
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
        ws = await session_workspace(run["session_id"]) if run else None
        if ws:
            text = read_text(ws / RESEARCH_REPORT_FILE)
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


# ---- the tick (scheduler entrypoint) ------------------------------------------

async def _recover_stale_running() -> None:
    cutoff = (
        datetime.fromisoformat(bus.now_iso()) - timedelta(minutes=STALE_RUNNING_MINUTES)
    ).isoformat(timespec="seconds")
    # Clearing lease_id invalidates the dead worker's lease — its late
    # done/failed write carries "AND lease_id = ?" and silently loses (P7).
    n = await db.execute(
        "UPDATE fact_extract_queue SET status='pending', started_at=NULL, lease_id=NULL "
        "WHERE status='running' AND started_at < ?",
        (cutoff,),
    )
    if n:
        log.warning("re-opened %d stale running extraction rows", n)
    # R5 P1-1: the bound durable task is the authority for every recoverable
    # verification generation (terminal tasks converge immediately; active
    # tasks only after card staleness). Never blindly re-open a completed
    # task or let a corrupt binding create a fresh generation. Every card
    # transition carries the id+lease+verify_task_id snapshot.
    recovery_cards = await db.query(
        "SELECT * FROM fact_cards "
        "WHERE status='verifying' AND (verify_started_at < ? "
        "OR verify_task_id IN ("
        "  SELECT id FROM tasks WHERE status IN "
        "  ('completed','failed','rate_limited','cancelled','expired','overcommitted')"
        ")) "
        "ORDER BY verify_started_at, id",
        (cutoff,),
    )
    terminal_failures = {
        "failed", "rate_limited", "cancelled", "expired", "overcommitted",
    }
    for card in recovery_cards:
        task_id = card.get("verify_task_id")
        lease_id = card.get("lease_id")
        if not task_id:
            # pending→verifying claim and atomic booking are separate by
            # necessity. A crash in that tiny pre-book window has no task,
            # attempt or daily slot to recover. _claim_card clears any prior
            # generation id, so NULL is an explicit unbooked state rather
            # than a broken binding; release the exact stale claim.
            released = await db.execute(
                "UPDATE fact_cards SET status='pending', verify_started_at=NULL, lease_id=NULL "
                "WHERE id=? AND status='verifying' AND lease_id IS ? "
                "AND verify_task_id IS NULL",
                (card["id"], lease_id),
            )
            if released:
                log.warning("re-opened stale unbooked fact card %s", card["id"])
            continue
        if not lease_id:
            await _quarantine_verification_binding(
                card, f"bound task {task_id} has no card lease",
            )
            continue
        task = await db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if task is None:
            await _quarantine_verification_binding(
                card, f"bound task {task_id} is missing",
            )
            continue
        expected_claim = _quote_material(str(card["claim"]))
        task_prompt = str(task["prompt"] or "")
        if (
            task["source"] != SOURCE
            or "你是研究所的事实核查员" not in task_prompt
            or expected_claim not in task_prompt
        ):
            await _quarantine_verification_binding(
                card, f"bound task {task_id} does not match this verification",
            )
            continue

        status = str(task["status"])
        if status == "completed":
            await _settle_completed_verification(
                card, lease_id, task_id, str(task["output"] or ""),
            )
            continue
        if status in terminal_failures:
            await _release_failed_card(card, lease_id, task_id)
            continue
        if status in {"queued", "running"}:
            owner = executor._running.get(task_id)  # noqa: SLF001 - executor's local owner registry
            if owner is not None and not owner.done():
                # A slow but live owner still has the exact lease/task; stale
                # wall-clock age alone is not permission to steal its card.
                continue
            # Boot normally made this terminal via recover_orphans(). If the
            # factcheck sweep runs independently, explicitly terminalize an
            # ownerless task, then converge the card through the same failure
            # path. The conditional task claim avoids overwriting a result
            # that completed between our read and write.
            failed = await db.execute(
                "UPDATE tasks SET status='failed', error='orphaned factcheck verification', "
                "finished_at=? WHERE id=? AND status IN ('queued','running')",
                (bus.now_iso(), task_id),
            )
            if failed:
                await _release_failed_card(card, lease_id, task_id)
                continue
            task = await db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
            if task and task["status"] == "completed":
                await _settle_completed_verification(
                    card, lease_id, task_id, str(task["output"] or ""),
                )
            elif task and task["status"] in terminal_failures:
                await _release_failed_card(card, lease_id, task_id)
            else:
                await _quarantine_verification_binding(
                    card, f"bound task {task_id} changed to invalid status",
                )
            continue
        await _quarantine_verification_binding(
            card, f"bound task {task_id} has invalid status {status!r}",
        )

    # LOOP-P3/R3 exhaustion backstop for legacy/manual pending rows.
    rows = await db.query(
        "SELECT id, category FROM fact_cards WHERE status='pending' AND attempts >= ?",
        (VERIFY_MAX_ATTEMPTS,),
    )
    for row in rows:
        await _settle_exhausted_card(row["id"], row["category"], lease_id=None)


async def _fail_extract_row(row_id: str, lease_id: str, error: str) -> None:
    await db.execute(
        "UPDATE fact_extract_queue SET status='failed', error=?, finished_at=?, lease_id=NULL "
        "WHERE id=? AND status='running' AND lease_id=?",
        (error[:500], bus.now_iso(), row_id, lease_id),
    )


async def _drain_extractions(cap: int) -> int:
    """Run up to ``cap`` queued extractions. Conditional claim per row under a
    worker lease (LOOP-P7, the fact_cards pattern); a model-task failure marks
    the row failed (operator can reset it — blind retries would burn quota on
    a source that keeps failing)."""
    done = 0
    for _ in range(max(0, cap)):
        row = await db.query_one(
            "SELECT * FROM fact_extract_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        )
        if row is None:
            break
        lease_id = uuid.uuid4().hex
        claimed = await db.execute(
            "UPDATE fact_extract_queue SET status='running', started_at=?, lease_id=? "
            "WHERE id=? AND status='pending'",
            (bus.now_iso(), lease_id, row["id"]),
        )
        if not claimed:
            continue  # lost the race; try the next row
        # every running→terminal transition below names its source state AND
        # our lease (conditional-claim hard rule + P7): a stale sweep /
        # operator reset that re-opened the row — even one re-claimed by a
        # new worker, which makes status 'running' again — must win over us
        try:
            text = await _source_text(row)
            if text is None:
                await _fail_extract_row(row["id"], lease_id, "source text unavailable")
                continue
            cards = await extract_claims(
                row["source_kind"], row["source_ref"], text, analyst_id=row["analyst_id"],
            )
            if cards is None:
                await _fail_extract_row(row["id"], lease_id, "extraction task failed")
            else:
                n = await db.execute(
                    "UPDATE fact_extract_queue SET status='done', finished_at=?, lease_id=NULL "
                    "WHERE id=? AND status='running' AND lease_id=?",
                    (bus.now_iso(), row["id"], lease_id),
                )
                if n:
                    done += 1
        except Exception as exc:  # noqa: BLE001 - one row must never break the drain
            log.exception("extraction failed for queue row %s", row["id"])
            await _fail_extract_row(row["id"], lease_id, str(exc))
    return done


async def tick() -> dict[str, Any]:
    """Scheduler job body (30-min interval, gated=True — it starts model
    work). Top-level failures PROPAGATE (LOOP-P10d): the
    @metered("factcheck-tick") wrapper records them as failed firings in cron
    health — the old self-swallow left a systemically broken tick looking
    permanently ok=1. Per-card / per-row failures are still absorbed inside
    verify_pending/_drain_extractions."""
    out: dict[str, Any] = {"extracted": 0, "verified": 0}
    await _recover_stale_running()
    out["extracted"] = await _drain_extractions(EXTRACT_PER_TICK)
    results = await verify_pending(VERIFY_PER_TICK)
    out["verified"] = sum(1 for r in results if r.get("status") == "completed")
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


@lru_cache(maxsize=4096)
def _claim_tokens(claim: str) -> frozenset[str]:
    """Cached tokenization of a verdict claim for the claim_check keyword leg.

    Verdict claims are stable text, so their token sets are memoizable —
    keying on the claim string means an edited claim simply re-tokenizes under
    a new key. Without this, claim_check re-tokenizes the whole live verdict
    set (<= _verdict_rows' LIMIT) on every writing-time call."""
    return frozenset(_tokens(claim))


async def _verdict_rows(limit: int = 2000) -> list[dict[str, Any]]:
    """Live actionable verdicts (the claim_check candidate set): VERIFIED /
    DISPUTED only (UNVERIFIABLE is a non-answer, not a writing-time hint) and
    unexpired (a lapsed short-TTL financial fact must not hit forever).
    reused/self_contradicted cards have no verdict row and are excluded — the
    fact they point at is already in the set."""
    return await db.query(
        "SELECT c.id, c.claim, c.category, vf.verdict, vf.expires_at "
        "FROM fact_cards c JOIN verified_facts vf ON vf.fact_card_id = c.id "
        "WHERE vf.expires_at > ? "
        "AND ((c.status='verified' AND vf.verdict='VERIFIED') "
        "  OR (c.status='disputed' AND vf.verdict='DISPUTED')) "
        "ORDER BY vf.verified_at DESC LIMIT ?",
        (bus.now_iso(), limit),
    )


async def _keyword_hits(text: str, k: int) -> list[dict[str, Any]]:
    qtok = _tokens(text)
    if not qtok:
        return []
    hits: list[dict[str, Any]] = []
    for r in await _verdict_rows():
        ctok = _claim_tokens(r["claim"])
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
        # newest verdicts first, clamped (LOOP-P10e; same posture as the
        # keyword leg's _verdict_rows LIMIT)
        rows = await db.query(
            "SELECT c.id, c.claim, c.category, vf.verdict, fv.embedding, fv.dim "
            "FROM fact_claim_vectors fv "
            "JOIN fact_cards c ON c.id = fv.fact_card_id "
            "JOIN verified_facts vf ON vf.fact_card_id = c.id "
            "WHERE fv.model = ? AND vf.expires_at > ? "
            "AND ((c.status='verified' AND vf.verdict='VERIFIED') "
            "  OR (c.status='disputed' AND vf.verdict='DISPUTED')) "
            "ORDER BY vf.verified_at DESC LIMIT ?",
            (vectors.model_name(), bus.now_iso(), VECTOR_SCAN_LIMIT),
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
