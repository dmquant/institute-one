"""Forecast extraction — regex over research/daily report text (Phase 5, card C3).

A deliberately dumb, deterministic extractor: NO model calls (the whole point
is a zero-quota path from prose to the forecast ledger). It splits a report
into sentences and emits a forecast candidate for every sentence that names
both a direction word and a resolvable security, then records the candidates
through ``forecasts.create_forecast()`` (card M5-001 — all validation, PIT
semantics and events stay in that one place).

Matching rules (the ROADMAP's "regex extractor + ticker stoplist + CJK guard"):

    direction   看多/看空/中性 families + English variants (bullish/bearish/
                overweight/underweight/neutral). The LAST direction hit in a
                sentence wins (“由看多转看空” concludes short); a negation
                particle immediately before a hit (不看空) voids that hit.
    security    canonical ids (600519.SH / 0700.HK / NVDA.US), bare six-digit
                A-share codes, and name/alias substrings (0004 tables).
    stoplist    TICKER_STOPWORDS rejects generic market words (指数/大盘/ETF…)
                as name matches; bare six-digit codes that parse as YYYYMM
                (202605-style report numbers, B-share ids in the 19xx/20xx-
                month range lose bare-code matching but keep canonical-id/
                name matching) or that sit next to units (元/万/%) are
                refused BEFORE the DB lookup. ALL-DIGIT aliases and names are
                excluded from the name rail entirely (REVIEW-C3 H1): the
                importer stamps a kind='ticker' alias equal to every symbol,
                and letting digit strings resolve through the name rail would
                bypass every bare-code guard above — digits belong to the
                bare-code rail and nowhere else.
    CJK guard   bare codes require non-digit context on both sides — 600519
                inside 1600519000 never matches — and refuse a decimal tail;
                CJK names need >= 2 chars, ASCII names >= 3 chars with word
                boundaries (mirrors market_fetchers._name_in_topic).
    negation    a hit is VOIDED (never inverted) by an adjacent particle
                (不看空), an advisory phrase in the 8-char pre-cue window
                (不建议看多), or a question answered in the negative
                (看多？不 — also across the sentence split); when in doubt,
                do not extract (REVIEW-C3 M4).
    homonyms    a name/alias shared by several securities (A/H cross-listing)
                resolves ONLY through stronger evidence: a canonical/bare-code
                hit anchors the listing and suppresses the siblings; a bare
                homonym mention is refused and counted (REVIEW-C3 M1).
    conviction  谨慎/初步/cautious → 0.35 beats 强烈/坚定/strong → 0.9 in the
                same sentence (the conservative cue wins); default 0.6.
    horizon     N个月/N季度/半年/年内/N周/N天内 (+ English variants) → days,
                per-unit sanity caps so “2026年” never becomes a horizon; a
                digit-preceded static cue (2026年内) is a substring of a
                rejected span and is refused too; the SHORTEST surviving cue
                wins (REVIEW-C3 M4 — the tightest deadline governs);
                default 30 days.

Idempotency (REVIEW-C3 M2 state machine, hardened by the 0033 evidence
audit): ``process_source`` claims its ``source_ref`` in
``forecast_extractions`` (INSERT ON CONFLICT DO NOTHING — the database is the
arbiter) with status='pending'. The claim row persists ``text_sha256`` (the
idempotency key is bound to the CONTENT — a replay carrying different text
under the same source_ref is refused with a readable error, never silently
resumed) and a frozen ``made_at`` (stamped once at claim; every candidate of
the source, including crash-resumed ones, gets exactly that knowledge time).
Each candidate claims a row in ``forecast_extraction_items`` (0019) carrying
a PRE-GENERATED deterministic forecast id (sha256 of extraction_id|security_id);
``create_forecast`` is then called WITH that id, so the create is safely
replayable: a resume that finds an item whose forecast row is missing simply
retries the create with the same id (the forecasts PK is the arbiter — the
old "claimed-but-NULL = in doubt, skip" window is gone; NULL items can only
be pre-0033 legacy rows, which still fail closed). A PK hit on an UNRELATED
forecast (48-bit id collision, R2 P1-3) is never counted as a concurrent win:
ownership is verified against the claim row's frozen made_at + the
candidate's security, and a mismatch releases the item claim and fails loud.
The pending → complete seal is a CONDITIONAL CLAIM (R2 P1-2): it fires only
while every claimed item's forecast actually exists, so a candidate in flight
under a concurrent processor can never be entombed in a 'complete' claim;
``forecast_ids``/``n_forecasts`` are aggregated FROM the item table joined to
actually-existing forecasts inside the same transaction — never from a
caller's local list. Replays of a complete source are duplicates; replays of
a pending source RESUME and create exactly the missing rest.

Attribution (REVIEW-C3 M5): the claim row records ``analyst_id`` — the author
of the source artifact, resolved as the analyst of the last non-ops step of
the source workflow run (editors compile, they don't originate calls). Paper
book closes carry it into ``paper_book.closed`` payloads so outcomes flow
back into that analyst's standing memory.

Thesis anchor: forecasts require a thesis (0013 FK). A structured research
item supplies its own thesis_id; everything else lands on the idempotent
singleton fallback thesis (``FALLBACK_THESIS_ID``, kind=thesis, status=watch)
so extraction never blocks on curation.

Hooks: ``register()`` subscribes ``research.completed`` and
``workflow.completed`` (filtered to workflow_id='daily' — the compiled 每日日报;
there is no dedicated daily.completed event). Handlers never raise, and the
app lifespan calls ``register()``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from .. import bus, db
from ..util import new_id
from . import forecasts

log = logging.getLogger("institute.forecast_extract")

MAX_FORECASTS_PER_SOURCE = 5     # one report never floods the ledger
MAX_CLAIM_LEN = 200              # claim = the matched sentence, trimmed
MAX_SENTENCE_LEN = 500           # longer "sentences" are tables/dumps, skip
DEFAULT_HORIZON_DAYS = 30
DEFAULT_CONVICTION = 0.6
STRONG_CONVICTION = 0.9
CAUTIOUS_CONVICTION = 0.35
DEFAULT_RULE = {"type": "absolute_move", "threshold": 0.05}
FALLBACK_THESIS_ID = "auto-forecast-extract"

# ---- ticker stoplist (ROADMAP item) -----------------------------------------
# Generic market vocabulary that must NEVER resolve to a security via the
# name/alias rail, regardless of what the alias table contains (imported
# bundles have carried generic aliases before). Compared casefolded.
TICKER_STOPWORDS = {
    "指数", "大盘", "板块", "行业", "市场", "概念", "主题", "个股", "标的",
    "基金", "国债", "债券", "期货", "期权", "汇率", "黄金", "原油", "商品",
    "美元", "人民币", "港元", "利率", "通胀",
    "a股", "港股", "美股", "债市", "股市", "楼市", "北向", "南向",
    "上证指数", "深证成指", "沪深300", "中证500", "中证1000", "创业板指",
    "科创50", "恒生指数", "恒指", "纳指", "标普", "道指",
    "etf", "ipo", "gdp", "cpi", "ppi", "pmi", "lpr", "ai", "llm", "reits",
}

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n\r]+")
# split KEEPING the delimiter so extract_candidates can see that a sentence
# ended in a question mark whose next fragment opens with a negation
# ("看多？不…" — a question answered in the negative, REVIEW-C3 M4)
_SENTENCE_SPLIT_KEEP_RE = re.compile(r"([。！？!?；;\n\r]+)")

# canonical ids inside prose (B5 dialect, hardened with boundary lookarounds so
# e.g. "tell.us" upper-cased can only hit if such a security actually exists)
_CANONICAL_IN_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:[0-9]{6}\.(?:SH|SZ|BJ)|[0-9]{4,5}\.HK|[A-Z][A-Z.\-]{0,9}\.US)(?![A-Za-z0-9])"
)
# bare A-share code, CJK guard: non-digit on both sides, no decimal tail, and
# not glued to a quantity/currency unit ("600519元" is a number, not a ticker)
_BARE_CODE_RE = re.compile(
    r"(?<![0-9])([0-9]{6})(?![0-9]|\.[0-9]|\s*(?:元|万|亿|%|％|美元|港元|港币))"
)
# six digits that parse as year+month (1900-01 .. 2099-12) are report numbers /
# dates in financial prose far more often than tickers — refuse (stoplist rule)
_DATE_LIKE_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])$")

_NEGATION_RE = re.compile(r"(?:不|并不|并非|非|未|没有|难以|不再|无法|别|勿|莫)\s*$")
# advisory negation ANYWHERE in the pre-cue window (REVIEW-C3 M4: "不建议看多"
# keeps 不 two chars away from the cue, so the adjacent-only rule missed it).
# Multi-char advisory phrases, plus word-initial 别/勿 (a preceding CJK char
# means 级别/类别/切勿-style compounds — those must not void, or are already
# covered by their own alternative).
_ADVISORY_NEG_RE = re.compile(
    r"不再?建议|不推荐|不宜|不应|不要|不会|暂不|切勿|请勿|避免|放弃"
    r"|(?<![\u4e00-\u9fff])[别勿]"
)
# a question mark AFTER the cue followed by a negation opener voids the hit
# ("看多？不" — asked and answered in the negative; conservative: no extract)
_QUESTION_NEG_RE = re.compile(r"[？?]\s*(?:不|否|并非|并不|未必|难说|存疑)")
# a split fragment that OPENS with a negation, used against the fragment
# following a question-mark sentence boundary (same rule, across the split)
_ANSWER_NEG_RE = re.compile(r"^\s*(?:不|否|并非|并不|未必|难说|存疑)")

# ordered: within one sentence the LAST match by position wins
_DIRECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"看多|看涨|做多|买入|增持|超配|强于大市|跑赢"), "long"),
    (re.compile(r"看空|看跌|做空|卖出|减持|低配|弱于大市|跑输"), "short"),
    (re.compile(r"中性|观望"), "neutral"),
    (re.compile(r"\b(?:bullish|overweight)\b", re.I), "long"),
    (re.compile(r"\b(?:bearish|underweight)\b", re.I), "short"),
    (re.compile(r"\bneutral\b", re.I), "neutral"),
]

_STRONG_RE = re.compile(r"强烈|坚定|坚决|重申|高确定|明确|\bstrong(?:ly)?\b|\bhigh[- ]conviction\b", re.I)
_CAUTIOUS_RE = re.compile(r"谨慎|温和|初步|试探|或许|可能|\bcautious(?:ly)?\b|\btentative(?:ly)?\b|\bmild(?:ly)?\b", re.I)

_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM = r"(\d{1,4}|[一两二三四五六七八九十]{1,3})"
# (pattern, days-per-unit, unit sanity cap) — first match by position wins;
# a match whose count exceeds the cap is ignored ("2026年" is a year, not 2026
# years of horizon), scanning continues.
_HORIZON_PATTERNS: list[tuple[re.Pattern[str], int, int]] = [
    (re.compile(_NUM + r"\s*个?\s*月(?:内|以内)?"), 30, 36),
    (re.compile(_NUM + r"\s*个?\s*季度(?:内|以内)?"), 90, 12),
    (re.compile(r"(?:本|当)?季度内|(?:a|one)\s+quarter", re.I), 90, 1),
    (re.compile(r"半年(?:内|以内)?|half\s+a\s+year|\bsix\s+months\b", re.I), 180, 1),
    (re.compile(r"年内|一年内|within\s+the\s+year|\bone\s+year\b", re.I), 365, 1),
    (re.compile(_NUM + r"\s*年(?:内|以内)"), 365, 3),
    (re.compile(_NUM + r"\s*(?:周|星期)(?:内|以内)?"), 7, 104),
    (re.compile(_NUM + r"\s*(?:个交易日|交易日|天)(?:内|以内)?"), 1, 365),
    (re.compile(_NUM + r"\s*日内"), 1, 365),
    (re.compile(r"(\d{1,3})[\s-]*months?", re.I), 30, 36),
    (re.compile(r"(\d{1,3})[\s-]*weeks?", re.I), 7, 104),
    (re.compile(r"(\d{1,3})[\s-]*(?:trading\s+)?days?", re.I), 1, 365),
]


def _cn_num(token: str) -> int | None:
    """'三' -> 3, '十' -> 10, '十二' -> 12, '二十' -> 20, '二十四' -> 24."""
    if token.isdigit():
        return int(token)
    if not token or any(ch not in _CN_DIGITS for ch in token):
        return None
    if "十" not in token:
        return _CN_DIGITS[token[0]] if len(token) == 1 else None
    tens, _, ones = token.partition("十")
    n = (_CN_DIGITS[tens] if tens else 1) * 10
    if ones:
        if _CN_DIGITS[ones] == 10:
            return None
        n += _CN_DIGITS[ones]
    return n


# ---- per-sentence cue extraction ---------------------------------------------

def match_direction(sentence: str) -> str | None:
    """Last non-negated direction hit in the sentence, or None.

    A hit is voided (never inverted) by any of (REVIEW-C3 M4 — when in doubt,
    do not extract):
      - an adjacent leading negation particle (不看空, 别买入);
      - an advisory negation phrase anywhere in the 8-char pre-cue window
        (不建议看多 — the particle is not adjacent but governs the cue);
      - a question mark after the cue answered by a negation (看多？不).
    """
    best_pos, best_dir = -1, None
    for pattern, direction in _DIRECTION_PATTERNS:
        for m in pattern.finditer(sentence):
            # pos/endpos instead of slicing: the 别/勿 lookbehind must still
            # see the char BEFORE the window (级别 sliced at 别 is not 别)
            lo = max(0, m.start() - 8)
            if _NEGATION_RE.search(sentence, lo, m.start()) \
                    or _ADVISORY_NEG_RE.search(sentence, lo, m.start()):
                continue
            if _QUESTION_NEG_RE.search(sentence, m.end()):
                continue  # asked and answered in the negative
            if m.start() > best_pos:
                best_pos, best_dir = m.start(), direction
    return best_dir


def match_conviction(sentence: str) -> float:
    """The conservative cue wins when both appear in one sentence."""
    if _CAUTIOUS_RE.search(sentence):
        return CAUTIOUS_CONVICTION
    if _STRONG_RE.search(sentence):
        return STRONG_CONVICTION
    return DEFAULT_CONVICTION


def match_horizon_days(sentence: str) -> int:
    """SHORTEST plausible horizon cue in the sentence (conservative — the
    tightest deadline governs); DEFAULT_HORIZON_DAYS when none.

    REVIEW-C3 M4 hardening: a static pattern (年内/季度内/半年) directly
    preceded by a digit is a SUBSTRING of a numeric span some other pattern
    already rejected as implausible ("2026年内" — 2026 is a year label, not a
    count), so it is refused rather than re-accepted through the overlap; and
    with multiple surviving cues the minimum wins, so a vague "年内" can never
    out-rank an explicit "未来2周" (365 vs 14 → 14).
    """
    best: int | None = None  # days; the shortest cue wins
    for pattern, per_unit, cap in _HORIZON_PATTERNS:
        for m in pattern.finditer(sentence):
            has_count = bool(m.groups() and m.group(1))
            if not has_count and m.start() > 0:
                prev = sentence[m.start() - 1]
                if prev.isdigit() or prev in _CN_DIGITS:
                    continue  # substring of a rejected numeric span (2026年内)
            n = _cn_num(m.group(1)) if has_count else 1
            if n is None or not 1 <= n <= cap:
                continue  # implausible count ("2026年") — not a horizon cue
            days = n * per_unit
            if best is None or days < best:
                best = days
    return best if best is not None else DEFAULT_HORIZON_DAYS


# ---- security resolution (ids, bare codes, names/aliases) --------------------

def _is_stopword(name: str) -> bool:
    return name.strip().casefold() in TICKER_STOPWORDS


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _name_hits(name: str, sentence: str) -> bool:
    """market_fetchers._name_in_topic semantics: CJK >=2 chars substring;
    ASCII >=3 chars with word boundaries ('MU' inside English words never
    hits). Re-checks name-rail eligibility (stopword / all-digit) so the
    guard holds even for callers that bypass _load_name_table."""
    name = (name or "").strip()
    if not _name_rail_eligible(name):
        return False
    if _has_cjk(name):
        return len(name) >= 2 and name in sentence
    if len(name) < 3:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", sentence, re.I) is not None


def _name_rail_eligible(name: str) -> bool:
    """A candidate string may resolve via the NAME rail only when it is not a
    stopword and not all digits (REVIEW-C3 H1: digit strings — importer-shaped
    kind='ticker' aliases above all — must go through the bare-code rail,
    whose YYYYMM/decimal-tail/unit/boundary guards would otherwise be
    bypassed; str.isdigit also catches full-width digits)."""
    name = (name or "").strip()
    return bool(name) and not name.isdigit() and not _is_stopword(name)


async def _load_name_table() -> list[tuple[str, str]]:
    """(name-or-alias, security_id) pairs for the name rail; stopwords and
    all-digit entries (any alias kind) already dropped — see _name_rail_eligible."""
    pairs: list[tuple[str, str]] = [
        (r["alias"], r["security_id"])
        for r in await db.query("SELECT alias, security_id FROM security_aliases")
    ]
    pairs += [
        (r[k], r["id"])
        for r in await db.query("SELECT id, name_zh, name_en FROM securities")
        for k in ("name_zh", "name_en") if r.get(k)
    ]
    return [(name, sid) for name, sid in pairs if _name_rail_eligible(name)]


async def _find_securities(
    sentence: str,
    name_table: list[tuple[str, str]],
    stats: dict[str, Any] | None = None,
) -> list[str]:
    """Security ids named in one sentence, strongest evidence first
    (canonical id > bare A-share code > name/alias), deduplicated.

    NAME-RAIL ARBITRATION (REVIEW-C3 M1): a name/alias shared by several
    securities (cross-listing homonyms — 中芯国际 on both 688981.SH and
    0981.HK) never resolves on its own. When one of the homonym's securities
    was already found through stronger evidence (canonical id / bare code),
    that evidence disambiguates the mention and the OTHER listings are
    suppressed; with no such anchor the name is refused outright (fail
    closed — better no forecast than a double-listed one), counted into
    ``stats['ambiguous_names']`` and logged.
    """
    found: list[str] = []

    def _add(sid: str | None) -> None:
        if sid and sid not in found:
            found.append(sid)

    upper = sentence.upper()
    masked = list(upper)
    for m in _CANONICAL_IN_TEXT_RE.finditer(upper):
        row = await db.query_one("SELECT id FROM securities WHERE id = ?", (m.group(0),))
        _add(row["id"] if row else None)
        # mask the span so 600519.SH cannot re-hit the bare-code scan below
        masked[m.start():m.end()] = " " * (m.end() - m.start())
    masked_text = "".join(masked)

    for m in _BARE_CODE_RE.finditer(masked_text):
        code = m.group(1)
        if _DATE_LIKE_RE.match(code):
            continue  # stoplist: YYYYMM report numbers beat the DB lookup
        row = await db.query_one(
            "SELECT id FROM securities WHERE symbol = ? AND market = 'CN_A' ORDER BY id LIMIT 1",
            (code,),
        )
        _add(row["id"] if row else None)

    # group the name table by the name STRING (casefolded: the ASCII match is
    # case-insensitive) so homonyms are decided per mention, not per row
    groups: dict[str, list[str]] = {}
    for name, sid in name_table:
        bucket = groups.setdefault(name.strip().casefold(), [])
        if sid not in bucket:
            bucket.append(sid)
    checked: set[str] = set()
    for name, _sid in name_table:
        key = name.strip().casefold()
        if key in checked:
            continue
        checked.add(key)
        if not _name_hits(name, sentence):
            continue
        sids = groups[key]
        if len(sids) == 1:
            _add(sids[0])
            continue
        if any(s in found for s in sids):
            continue  # stronger evidence already picked THE listing; add no siblings
        if stats is not None:
            stats.setdefault("ambiguous_names", []).append(name.strip())
        log.warning(
            "name %r is ambiguous across %d securities (%s); refusing the mention "
            "(add a canonical ticker to disambiguate)", name.strip(), len(sids),
            ", ".join(sorted(sids)),
        )
    return found


# ---- candidate extraction ------------------------------------------------------

async def extract_candidates(
    text: str, stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan a report; one candidate per (first sentence naming) security.

    A candidate = {security_id, direction, conviction, horizon_days, claim}.
    Capped at MAX_FORECASTS_PER_SOURCE (a single report must not flood the
    ledger); the first mention of a security wins — later sentences cannot
    flip an earlier call from the same text. A sentence ending in a question
    mark whose NEXT fragment opens with a negation is skipped ("看多？不" —
    the splitter used to eat the answer, REVIEW-C3 M4). ``stats`` (optional)
    collects refusal bookkeeping such as ambiguous homonym names (M1).
    """
    text = (text or "").strip()
    if not text:
        return []
    name_table = await _load_name_table()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    pieces = _SENTENCE_SPLIT_KEEP_RE.split(text)  # [sent, delim, sent, delim, …]
    for i in range(0, len(pieces), 2):
        sentence = pieces[i].strip()
        if not sentence or len(sentence) > MAX_SENTENCE_LEN:
            continue
        direction = match_direction(sentence)
        if direction is None:
            continue
        delim = pieces[i + 1] if i + 1 < len(pieces) else ""
        nxt = pieces[i + 2] if i + 2 < len(pieces) else ""
        if ("？" in delim or "?" in delim) and _ANSWER_NEG_RE.match(nxt):
            continue  # a question answered in the negative — do not extract
        securities = await _find_securities(sentence, name_table, stats)
        if not securities:
            continue
        conviction = match_conviction(sentence)
        horizon = match_horizon_days(sentence)
        for sid in securities:
            if sid in seen:
                continue
            seen.add(sid)
            out.append({
                "security_id": sid,
                "direction": direction,
                "conviction": conviction,
                "horizon_days": horizon,
                "claim": sentence[:MAX_CLAIM_LEN],
            })
            if len(out) >= MAX_FORECASTS_PER_SOURCE:
                return out
    return out


# ---- thesis anchor ---------------------------------------------------------------

async def ensure_fallback_thesis() -> str:
    """Idempotent singleton thesis anchoring extracted forecasts that carry no
    thesis of their own (0013 requires one; extraction must not block on
    curation). INSERT OR IGNORE — the id/slug collision is the arbiter."""
    now = bus.now_iso()
    await db.execute(
        "INSERT OR IGNORE INTO theses (id, kind, slug, name_zh, status, current_view, source, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (FALLBACK_THESIS_ID, "thesis", FALLBACK_THESIS_ID, "自动预测抽取（规则引擎）",
         "watch", "unknown", "forecast_extract", now, now),
    )
    return FALLBACK_THESIS_ID


async def _resolve_thesis(thesis_id: str | None) -> str:
    if thesis_id and await db.query_one("SELECT id FROM theses WHERE id = ?", (thesis_id,)):
        return thesis_id
    return await ensure_fallback_thesis()


# ---- analyst attribution (REVIEW-C3 M5) ------------------------------------------

async def _resolve_analyst(run_id: str | None) -> str | None:
    """The author of a workflow run's compiled output, for extraction
    provenance: the analyst of the LAST non-ops step (the opinion author —
    both research and daily end in an ops-editor COMPILE step, and editors
    organize prose, they do not originate calls; ops analysts carry no field
    memory either, memory.SKIP_CATEGORIES). None when anything along the
    lookup is missing — attribution then stays unknown (fails closed).
    """
    if not run_id:
        return None
    run = await db.query_one(
        "SELECT workflow_id FROM workflow_runs WHERE id = ?", (str(run_id),))
    if run is None:
        return None
    wf = await db.query_one("SELECT steps FROM workflows WHERE id = ?", (run["workflow_id"],))
    if wf is None:
        return None
    try:
        steps = json.loads(wf["steps"] or "[]")
    except ValueError:
        return None
    if not isinstance(steps, list):
        return None
    from .analysts import get_analyst  # lazy: catalog module

    for step in reversed(steps):
        if not isinstance(step, dict):
            continue
        aid = str(step.get("analyst_id") or step.get("analyst") or "").strip()
        analyst = get_analyst(aid) if aid else None
        if analyst is not None and analyst.category != "ops":
            return analyst.id
    return None


# ---- source processing (idempotent) ------------------------------------------------

def _deterministic_forecast_id(extraction_id: str, security_id: str) -> str:
    """Pre-generated forecast id for one candidate slot: a pure function of
    the item's identity, so every claim/retry of the same slot produces the
    SAME id and create_forecast becomes safely replayable (the forecasts
    primary key is the replay arbiter). 12 hex chars, the new_id() shape."""
    return hashlib.sha256(f"{extraction_id}|{security_id}".encode("utf-8")).hexdigest()[:12]


def _owns_forecast(row: dict[str, Any], sid: str, frozen_made_at: str) -> bool:
    """Does the forecasts row sitting under our deterministic id belong to
    THIS extraction slot? A genuine concurrent-replayer creation of the same
    slot carries exactly the claim row's frozen made_at and this candidate's
    security (NULL only after a security delete). Anything else means the
    48-bit id collided with an UNRELATED forecast — counting that as "ours"
    would misattribute somebody else's call (R2 P1-3)."""
    return row["made_at"] == frozen_made_at and row["security_id"] in (sid, None)


def _collision_error(fid: str, extraction_id: str, sid: str,
                     existing: dict[str, Any] | None = None) -> ValueError:
    detail = ""
    if existing is not None:
        detail = (f" (colliding forecast: security {existing['security_id']!r}, "
                  f"made_at {existing['made_at']})")
    return ValueError(
        f"deterministic forecast id collision: {fid} for extraction {extraction_id} "
        f"candidate {sid} already belongs to an unrelated forecast{detail}; "
        "failing loud by design (48-bit ids make true collisions vanishingly rare) — "
        "inspect the colliding rows before replaying this source"
    )


async def process_source(
    source_ref: str,
    source_kind: str,
    text: str,
    *,
    thesis_id: str | None = None,
    made_at: str | None = None,
    analyst_id: str | None = None,
) -> dict[str, Any]:
    """Extract + record forecasts from one source text, exactly once —
    restart-safe (REVIEW-C3 M2 state machine, hardened by the 0033 audit).

    The claim INSERT on forecast_extractions.source_ref is still the
    source-level idempotency arbiter, and the claim row now binds the key to
    its CONTENT and its KNOWLEDGE TIME: ``text_sha256`` is persisted at claim
    (a replay carrying different text under the same source_ref raises a
    readable error instead of resuming into a mixed extraction) and
    ``made_at`` is frozen at claim (explicit param or claim-time now) so
    every candidate — first run or crash-resume — records the same knowledge
    time. Candidates are claimed per-security in forecast_extraction_items
    WITH a pre-generated deterministic forecast id; create_forecast() is
    called with that id, so the crash window between claim and create is
    replayable: a resume that finds the item but no forecast row retries the
    create with the same id, and one that finds both counts it AFTER
    verifying ownership (frozen made_at + security) — an id that turns out to
    belong to an unrelated forecast (48-bit collision) releases the claim and
    raises instead of misattributing (R2 P1-3). Items with a NULL forecast_id
    can only be pre-0033 legacy claims — those still fail closed (skipped,
    reported in ``detail``). Validation-refused candidates release their
    claim row (a refusal is not doubt). The finalize seal is conditional
    (R2 P1-2): pending → complete only when every claimed item's forecast
    exists, with ``forecast_ids``/``n_forecasts`` aggregated from the item
    table in the same transaction; while a concurrent processor still owns an
    in-flight candidate this run returns status='pending' and the owner (or
    the next replay) seals. Empty text does NOT claim — the source stays
    retryable once its report exists. ``analyst_id`` records the source
    artifact's author for outcome attribution (M5); None = unknown, nothing
    flows back.
    """
    source_ref = (source_ref or "").strip()
    if not source_ref:
        raise ValueError("process_source needs a source_ref")
    if not (text or "").strip():
        return {"source_ref": source_ref, "status": "empty", "created": []}
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    frozen_made_at = forecasts._norm_ts(made_at, "made_at") if made_at else bus.now_iso()

    now = bus.now_iso()
    claimed = await db.execute(
        "INSERT INTO forecast_extractions (id, source_ref, source_kind, status, analyst_id, "
        "text_sha256, made_at, created_at, updated_at) VALUES (?,?,?,'pending',?,?,?,?,?) "
        "ON CONFLICT(source_ref) DO NOTHING",
        (new_id(), source_ref, source_kind, analyst_id, text_sha256, frozen_made_at, now, now),
    )
    row = await db.query_one(
        "SELECT id, status, text_sha256, made_at FROM forecast_extractions "
        "WHERE source_ref = ?", (source_ref,))
    if not claimed:
        if row is not None and row["text_sha256"] and row["text_sha256"] != text_sha256:
            raise ValueError(
                f"source_ref {source_ref!r} is already claimed for different content "
                f"(text_sha256 {row['text_sha256'][:12]}… != {text_sha256[:12]}…); "
                "one source_ref binds one text — use a new source_ref for new content"
            )
        if row is None or row["status"] != "pending":
            return {"source_ref": source_ref, "status": "duplicate", "created": []}
        log.info("resuming unfinished extraction for %s", source_ref)
        # the FROZEN knowledge time wins over whatever this resume was passed
        if row["made_at"]:
            frozen_made_at = row["made_at"]
    extraction_id = row["id"]

    stats: dict[str, Any] = {}
    candidates = await extract_candidates(text, stats=stats)
    problems: list[str] = []
    if candidates:
        anchor = await _resolve_thesis(thesis_id)
        for cand in candidates:
            sid = cand["security_id"]
            fid = _deterministic_forecast_id(extraction_id, sid)
            item = await db.query_one(
                "SELECT forecast_id FROM forecast_extraction_items "
                "WHERE extraction_id = ? AND security_id = ?", (extraction_id, sid))
            if item is not None:
                if not item["forecast_id"]:
                    # pre-0033 legacy claim (new claims always carry an id):
                    # unknowable whether it was created — keep failing closed
                    problems.append(
                        f"{sid}: in doubt (legacy pre-0033 claim without an id; check "
                        "forecasts, DELETE the item row and re-flip to pending to retry)")
                    log.warning("extract %s: candidate %s in doubt, skipped", source_ref, sid)
                    continue
                fid = item["forecast_id"]
                existing = await db.query_one("SELECT * FROM forecasts WHERE id = ?", (fid,))
                if existing is not None:
                    if not _owns_forecast(existing, sid, frozen_made_at):
                        # poisoned item: it points at an unrelated forecast —
                        # release it so nothing misattributes, then fail loud
                        await db.execute(
                            "DELETE FROM forecast_extraction_items "
                            "WHERE extraction_id = ? AND security_id = ?",
                            (extraction_id, sid))
                        raise _collision_error(fid, extraction_id, sid, existing)
                    continue  # created before a crash — finalize counts it from the items
                # claimed but never created: the deterministic id makes the
                # retry safe (the forecasts PK arbitrates a concurrent winner)
            else:
                item_now = bus.now_iso()
                try:
                    item_claimed = await db.execute(
                        "INSERT INTO forecast_extraction_items (extraction_id, security_id, "
                        "forecast_id, created_at, updated_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(extraction_id, security_id) DO NOTHING",
                        (extraction_id, sid, fid, item_now, item_now),
                    )
                except sqlite3.IntegrityError as exc:
                    # ON CONFLICT covers only (extraction_id, security_id); a hit
                    # on idx_extraction_items_forecast means our deterministic id
                    # is already owned by ANOTHER extraction's item (cross-
                    # extraction 48-bit collision) — fail loud, claim nothing
                    raise _collision_error(fid, extraction_id, sid) from exc
                if not item_claimed:
                    continue  # a concurrent processor owns this candidate
            try:
                await forecasts.create_forecast({
                    "thesis_id": anchor,
                    "security_id": sid,
                    "claim": cand["claim"],
                    "direction": cand["direction"],
                    "conviction": cand["conviction"],
                    "horizon_days": cand["horizon_days"],
                    "settlement_rule": dict(DEFAULT_RULE),
                    "made_at": frozen_made_at,
                }, forecast_id=fid)
            except forecasts.ForecastError as exc:
                existing = await db.query_one("SELECT * FROM forecasts WHERE id = ?", (fid,))
                if existing is not None:
                    if _owns_forecast(existing, sid, frozen_made_at):
                        continue  # a concurrent replayer created it first — that IS success
                    # the create hit the forecasts PK on an UNRELATED row: a
                    # 48-bit collision, not a concurrent win. Release our item
                    # claim (it must never misattribute the foreign forecast)
                    # and fail loud (R2 P1-3).
                    await db.execute(
                        "DELETE FROM forecast_extraction_items "
                        "WHERE extraction_id = ? AND security_id = ?",
                        (extraction_id, sid))
                    raise _collision_error(fid, extraction_id, sid, existing) from exc
                # one bad candidate must not stop the rest; release the claim:
                # a deterministic refusal is not doubt, and a later resume may
                # legitimately re-evaluate it
                problems.append(f"{sid}: {exc}")
                log.warning("extract %s: candidate %s refused: %s", source_ref, sid, exc)
                await db.execute(
                    "DELETE FROM forecast_extraction_items WHERE extraction_id = ? "
                    "AND security_id = ? AND NOT EXISTS "
                    "(SELECT 1 FROM forecasts f WHERE f.id = forecast_extraction_items.forecast_id)",
                    (extraction_id, sid))

    for name in stats.get("ambiguous_names", []):
        problems.append(f"ambiguous name refused: {name}")
    # finalize from the DATABASE: what this source produced = its item rows
    # whose forecasts actually exist (includes work done by concurrent or
    # crashed-then-resumed processors, never a caller-local view). The seal is
    # a CONDITIONAL CLAIM (R2 P1-2): pending → complete fires only while every
    # claimed item's forecast actually exists — a candidate still in flight
    # under a concurrent processor (its item claimed, its forecast not yet
    # created) blocks the seal, so a crash of that processor can never be
    # entombed inside a 'complete' claim that replays as 'duplicate'. The
    # aggregate and the seal share one transaction, so the stored forecast_ids
    # are exactly the state the seal condition certified. Legacy NULL items
    # (pre-0033 in-doubt) do not block — they are reported in detail, as
    # before.
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT i.forecast_id FROM forecast_extraction_items i "
            "JOIN forecasts f ON f.id = i.forecast_id "
            "WHERE i.extraction_id = ? ORDER BY i.created_at, i.security_id",
            (extraction_id,),
        )
        created = [r["forecast_id"] for r in await cur.fetchall()]
        await cur.close()
        cur = await conn.execute(
            "UPDATE forecast_extractions SET n_candidates = ?, n_forecasts = ?, "
            "forecast_ids = ?, detail = ?, text_sha256 = COALESCE(text_sha256, ?), "
            "status = 'complete', updated_at = ? "
            "WHERE source_ref = ? AND status = 'pending' AND NOT EXISTS ("
            "SELECT 1 FROM forecast_extraction_items i WHERE i.extraction_id = ? "
            "AND i.forecast_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM forecasts f WHERE f.id = i.forecast_id))",
            (len(candidates), len(created), json.dumps(created), "; ".join(problems)[:1000],
             text_sha256, bus.now_iso(), source_ref, extraction_id),
        )
        sealed = cur.rowcount
        await cur.close()
    if sealed:
        if created:
            await bus.emit("forecast.extracted", "extraction", source_ref, {
                "source_kind": source_kind, "n_candidates": len(candidates),
                "n_forecasts": len(created), "forecast_ids": created,
            })
        return {
            "source_ref": source_ref, "status": "processed",
            "n_candidates": len(candidates), "created": created, "problems": problems,
        }
    latest = await db.query_one(
        "SELECT status FROM forecast_extractions WHERE source_ref = ?", (source_ref,))
    if latest is not None and latest["status"] == "complete":
        # a concurrent processor sealed (and emitted for) this source first
        return {
            "source_ref": source_ref, "status": "processed",
            "n_candidates": len(candidates), "created": created, "problems": problems,
        }
    # candidates claimed by a concurrent processor are still in flight: the
    # source stays 'pending' — its owner (or the next replay) finishes and
    # seals it; nothing is lost, nothing is entombed
    unfinished = [
        r["security_id"] for r in await db.query(
            "SELECT security_id FROM forecast_extraction_items i "
            "WHERE i.extraction_id = ? AND i.forecast_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM forecasts f WHERE f.id = i.forecast_id) "
            "ORDER BY security_id",
            (extraction_id,),
        )
    ]
    log.info("extract %s: seal deferred, in-flight candidates %s", source_ref, unfinished)
    return {
        "source_ref": source_ref, "status": "pending",
        "n_candidates": len(candidates), "created": created,
        "problems": problems + [f"in flight under a concurrent processor: {s}"
                                for s in unfinished],
    }


# ---- bus handlers (never raise) -------------------------------------------------

def _read_file_text(path: Path) -> str | None:
    """Sync workspace read — run under asyncio.to_thread (see _workspace_text)."""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("could not read %s", path)
    return None


async def _workspace_text(session_id: Any, filename: str) -> str | None:
    if not session_id:
        return None
    row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (str(session_id),))
    if not row or not row["workspace_dir"]:
        return None
    path = Path(row["workspace_dir"]).expanduser() / filename
    # the compiled report reaches the 200KB output cap and the bus awaits this
    # handler inline — keep the read off the emitter's event loop
    return await asyncio.to_thread(_read_file_text, path)


async def _on_research_completed(event: bus.Event) -> None:
    """research.completed → extract from the compiled deep report (falls back
    to the payload summary); the structured item's thesis_id anchors. The
    author for attribution is resolved from the run's workflow steps (payload
    run_id, falling back to the queue item's recorded run)."""
    try:
        p = event.payload or {}
        item = await db.query_one(
            "SELECT thesis_id, run_id FROM research_queue WHERE id = ?", (str(event.ref_id),)
        )
        text = await _workspace_text(p.get("session_id"), "06_深度报告.md") \
            or str(p.get("summary") or "")
        await process_source(
            f"research:{event.ref_id}", "research", text,
            thesis_id=(item or {}).get("thesis_id"),
            analyst_id=await _resolve_analyst(p.get("run_id") or (item or {}).get("run_id")),
        )
    except Exception:  # noqa: BLE001 - bus handlers must never raise
        log.exception("forecast extraction failed for research %s", event.ref_id)


async def _on_workflow_completed(event: bus.Event) -> None:
    """workflow.completed filtered to the compiled daily (每日日报): the payload
    always carries workflow_id (workflows._finish_run), so no run re-read."""
    try:
        p = event.payload or {}
        if str(p.get("workflow_id") or "") != "daily":
            return
        run_id = str(p.get("run_id") or event.ref_id or "")
        text = await _workspace_text(p.get("session_id"), "每日日报.md")
        if not text:
            results = p.get("results") or []
            text = "\n".join(
                str(r.get("summary") or "") for r in results if isinstance(r, dict)
            )
        await process_source(
            f"workflow:{run_id}", "daily", text,
            analyst_id=await _resolve_analyst(run_id),
        )
    except Exception:  # noqa: BLE001 - bus handlers must never raise
        log.exception("forecast extraction failed for workflow %s", event.ref_id)


def register() -> None:
    """Hook the extractor into the bus. Called once from the app lifespan."""
    bus.on("research.completed", _on_research_completed)
    bus.on("workflow.completed", _on_workflow_completed)
    log.info("forecast extractor registered (research.completed + daily workflow.completed)")
