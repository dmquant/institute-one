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

Idempotency (REVIEW-C3 M2 state machine): ``process_source`` claims its
``source_ref`` in ``forecast_extractions`` (INSERT ON CONFLICT DO NOTHING —
the database is the arbiter) with status='pending', claims each candidate in
``forecast_extraction_items`` (0019), back-fills each forecast_id as it is
created, and flips the source to 'complete' at the end. Replays of a complete
source are duplicates; replays of a pending source RESUME: already-created
candidates are skipped through their item rows, only the missing rest is
created — a crash between claim and creation no longer bricks the source nor
forces a duplicate-happy full re-extract.

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

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from .. import bus, db
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


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


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
    restart-safe (REVIEW-C3 M2 state machine).

    The claim INSERT on forecast_extractions.source_ref is still the
    source-level idempotency arbiter, but the claim now carries a state:
    it is born 'pending' and flipped to 'complete' only after every candidate
    has been decided. A replay of a 'complete' source is a duplicate (skips);
    a replay of a 'pending' source RESUMES it — each candidate is claimed
    per-security in forecast_extraction_items (the INSERT on the primary key
    is the per-candidate arbiter), its forecast_id back-filled right after
    create_forecast() succeeds, so the resume skips what already exists and
    creates exactly the missing rest. A crash landing in the one-statement
    window between create and back-fill leaves the item claimed-but-NULL:
    resume then SKIPS it (in doubt — never risks a duplicate) and reports it
    in ``detail``; the surgical operator path is to check the forecasts
    table, DELETE that one item row, set the claim back to 'pending' and
    replay. Empty text does NOT claim — the source stays retryable once its
    report exists. made_at defaults inside create_forecast to now: extraction
    time IS knowledge time. ``analyst_id`` records the source artifact's
    author for outcome attribution (M5); None = unknown, nothing flows back.
    """
    source_ref = (source_ref or "").strip()
    if not source_ref:
        raise ValueError("process_source needs a source_ref")
    if not (text or "").strip():
        return {"source_ref": source_ref, "status": "empty", "created": []}

    now = bus.now_iso()
    claimed = await db.execute(
        "INSERT INTO forecast_extractions (id, source_ref, source_kind, status, analyst_id, "
        "created_at, updated_at) VALUES (?,?,?,'pending',?,?,?) "
        "ON CONFLICT(source_ref) DO NOTHING",
        (_new_id(), source_ref, source_kind, analyst_id, now, now),
    )
    row = await db.query_one(
        "SELECT id, status FROM forecast_extractions WHERE source_ref = ?", (source_ref,))
    if not claimed:
        if row is None or row["status"] != "pending":
            return {"source_ref": source_ref, "status": "duplicate", "created": []}
        log.info("resuming unfinished extraction for %s", source_ref)
    extraction_id = row["id"]

    stats: dict[str, Any] = {}
    candidates = await extract_candidates(text, stats=stats)
    created: list[str] = []
    problems: list[str] = []
    if candidates:
        anchor = await _resolve_thesis(thesis_id)
        for cand in candidates:
            sid = cand["security_id"]
            item = await db.query_one(
                "SELECT forecast_id FROM forecast_extraction_items "
                "WHERE extraction_id = ? AND security_id = ?", (extraction_id, sid))
            if item is not None:
                if item["forecast_id"]:
                    created.append(item["forecast_id"])  # done before the crash
                else:
                    problems.append(
                        f"{sid}: in doubt (claimed before a crash; check forecasts, "
                        "DELETE the item row and re-flip to pending to retry)")
                    log.warning("extract %s: candidate %s in doubt, skipped", source_ref, sid)
                continue
            item_now = bus.now_iso()
            item_claimed = await db.execute(
                "INSERT INTO forecast_extraction_items (extraction_id, security_id, "
                "created_at, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(extraction_id, security_id) DO NOTHING",
                (extraction_id, sid, item_now, item_now),
            )
            if not item_claimed:
                continue  # a concurrent processor owns this candidate
            try:
                fc = await forecasts.create_forecast({
                    "thesis_id": anchor,
                    "security_id": sid,
                    "claim": cand["claim"],
                    "direction": cand["direction"],
                    "conviction": cand["conviction"],
                    "horizon_days": cand["horizon_days"],
                    "settlement_rule": dict(DEFAULT_RULE),
                    **({"made_at": made_at} if made_at else {}),
                })
            except forecasts.ForecastError as exc:  # one bad candidate must not stop the rest
                problems.append(f"{sid}: {exc}")
                log.warning("extract %s: candidate %s refused: %s", source_ref, sid, exc)
                # release the claim: a deterministic refusal is not doubt, and a
                # later resume may legitimately re-evaluate it
                await db.execute(
                    "DELETE FROM forecast_extraction_items WHERE extraction_id = ? "
                    "AND security_id = ? AND forecast_id IS NULL", (extraction_id, sid))
                continue
            await db.execute(
                "UPDATE forecast_extraction_items SET forecast_id = ?, updated_at = ? "
                "WHERE extraction_id = ? AND security_id = ?",
                (fc["id"], bus.now_iso(), extraction_id, sid),
            )
            created.append(fc["id"])

    for name in stats.get("ambiguous_names", []):
        problems.append(f"ambiguous name refused: {name}")
    await db.execute(
        "UPDATE forecast_extractions SET n_candidates = ?, n_forecasts = ?, forecast_ids = ?, "
        "detail = ?, status = 'complete', updated_at = ? WHERE source_ref = ?",
        (len(candidates), len(created), json.dumps(created), "; ".join(problems)[:1000],
         bus.now_iso(), source_ref),
    )
    if created:
        await bus.emit("forecast.extracted", "extraction", source_ref, {
            "source_kind": source_kind, "n_candidates": len(candidates),
            "n_forecasts": len(created), "forecast_ids": created,
        })
    return {
        "source_ref": source_ref, "status": "processed",
        "n_candidates": len(candidates), "created": created, "problems": problems,
    }


# ---- bus handlers (never raise) -------------------------------------------------

async def _workspace_text(session_id: Any, filename: str) -> str | None:
    if not session_id:
        return None
    row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (str(session_id),))
    if not row or not row["workspace_dir"]:
        return None
    path = Path(row["workspace_dir"]).expanduser() / filename
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("could not read %s", path)
    return None


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
