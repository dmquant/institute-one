"""Post-run claim audit for generated artifacts.

This is a lightweight FEVER-style first layer: extract checkable claims, attach
nearby sources, and flag weak/unsupported claims. It does not claim full
truth-verification; a concrete URL means "source attached", not "verified".
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .. import bus, db
from .evidence import canonicalize_url, extract_sources

MAX_CLAIMS_PER_ARTIFACT = 12
MAX_CLAIM_CHARS = 360
MAX_CONTEXT_CHARS = 900

CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s*|\n+")
NUMERIC_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:%|bp|bps|美元|美金|亿元|亿|万亿|万|元|日元|港元|个月|季度|年|天|D|T|B|M)|"
    r"\$\s?\d+(?:\.\d+)?|\d{4}[-年]\d{1,2}(?:[-月]\d{1,2})?)",
    re.IGNORECASE,
)
SOURCE_LABEL_RE = re.compile(r"(?:来源|source|via|据|根据|according to)[:：]?", re.IGNORECASE)
UNVERIFIED_RE = re.compile(r"未经[^。；，,]{0,16}核实|无法核实|待核实|未证实|传闻")

TRIGGERS = (
    "宣布", "发布", "披露", "显示", "报告", "数据", "同比", "环比", "增长", "下降",
    "上涨", "下跌", "加息", "降息", "维持", "封锁", "签署", "批准", "否认",
    "入列", "制裁", "监管", "法院", "裁定", "IPO", "上市", "融资", "估值",
    "成交", "收入", "利润", "现金流", "PPI", "CPI", "利差", "收益率",
)
POLICY_TERMS = ("政策", "监管", "央行", "PBOC", "Fed", "ECB", "BOJ", "加息", "降息", "制裁", "法案")
FINANCIAL_TERMS = ("收入", "利润", "现金流", "估值", "市值", "PE", "PS", "EV", "ROE", "EPS", "财报")
MARKET_TERMS = ("股价", "指数", "收益率", "利差", "油价", "铜价", "黄金", "汇率", "成交")
EVENT_TERMS = ("宣布", "发布", "签署", "批准", "否认", "上市", "IPO", "入列", "封锁")
LEGAL_TERMS = ("法院", "裁定", "诉讼", "责任", "合规", "监管")


@dataclass(frozen=True)
class ClaimCard:
    claim_text: str
    category: str
    verdict: str
    confidence: float
    rationale: str
    source_urls: list[str]
    context_text: str
    claim_hash: str


@dataclass
class ClaimAuditReport:
    claims: list[ClaimCard] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.claims)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for claim in self.claims:
            out[claim.verdict] = out.get(claim.verdict, 0) + 1
        return out

    def frontmatter(self) -> dict[str, object]:
        counts = self.counts()
        return {
            "claim_audit_total": self.total,
            "claim_audit_unsupported": counts.get("unsupported", 0),
            "claim_audit_weak_source": counts.get("weak_source", 0),
            "claim_audit_declared_unverified": counts.get("declared_unverified", 0),
        }


def audit_text(text: str, *, max_claims: int = MAX_CLAIMS_PER_ARTIFACT) -> ClaimAuditReport:
    candidates = _candidate_sentences(text)
    claims: list[ClaimCard] = []
    seen: set[str] = set()
    for score, sentence, context in candidates:
        claim = _normalize_claim(sentence)
        if not claim or claim in seen:
            continue
        seen.add(claim)
        card = _classify_claim(claim, context, score)
        claims.append(card)
        if len(claims) >= max_claims:
            break
    return ClaimAuditReport(claims=claims)


async def audit_and_store_text(
    text: str,
    *,
    artifact_kind: str,
    artifact_id: str,
    artifact_path: str = "",
    topic: str = "",
    analyst_id: str = "",
    work_date: str = "",
) -> ClaimAuditReport:
    report = audit_text(text)
    await db.execute(
        "DELETE FROM fact_cards WHERE artifact_kind = ? AND artifact_id = ?",
        (artifact_kind, artifact_id),
    )
    if not report.claims:
        return report

    now = bus.now_iso()
    for claim in report.claims:
        await db.execute(
            """
            INSERT INTO fact_cards
              (artifact_kind, artifact_id, artifact_path, topic, analyst_id, work_date,
               claim_text, category, verdict, confidence, rationale, source_urls,
               context_text, claim_hash, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(artifact_kind, artifact_id, claim_hash) DO UPDATE SET
              artifact_path=excluded.artifact_path,
              topic=excluded.topic,
              analyst_id=excluded.analyst_id,
              work_date=excluded.work_date,
              category=excluded.category,
              verdict=excluded.verdict,
              confidence=excluded.confidence,
              rationale=excluded.rationale,
              source_urls=excluded.source_urls,
              context_text=excluded.context_text,
              updated_at=excluded.updated_at
            """,
            (
                artifact_kind,
                artifact_id,
                artifact_path,
                topic,
                analyst_id,
                work_date,
                claim.claim_text,
                claim.category,
                claim.verdict,
                claim.confidence,
                claim.rationale,
                json.dumps(claim.source_urls, ensure_ascii=False),
                claim.context_text,
                claim.claim_hash,
                now,
                now,
            ),
        )
    await bus.emit(
        "claim_audit.completed",
        artifact_kind,
        artifact_id,
        {"total": report.total, "counts": report.counts(), "topic": topic},
    )
    return report


def claim_audit_callout(report: ClaimAuditReport) -> str:
    risky = [
        c for c in report.claims
        if c.verdict in {"unsupported", "weak_source", "declared_unverified"}
    ]
    if not risky:
        return ""
    counts = report.counts()
    lines = [
        "[!warning] Claim audit",
        "",
        f"- 抽取可核查 claim：{report.total} 条；unsupported={counts.get('unsupported', 0)}，"
        f"weak_source={counts.get('weak_source', 0)}，declared_unverified={counts.get('declared_unverified', 0)}。",
        "- `source_attached` 只表示附近有具体来源，不等于事实已被证实。",
    ]
    for claim in risky[:5]:
        lines.append(f"- `{claim.verdict}` {claim.claim_text[:180]} — {claim.rationale}")
    return "> " + "\n> ".join(lines)


def _candidate_sentences(text: str) -> list[tuple[int, str, str]]:
    clean = CODE_BLOCK_RE.sub("\n", text or "")
    chunks: list[tuple[int, str, str]] = []
    for para in re.split(r"\n\s*\n+", clean):
        context = _clean_context(para)
        if not context or context.startswith("|"):
            continue
        for raw in SENTENCE_SPLIT_RE.split(context):
            sentence = _normalize_claim(raw)
            if not _looks_checkable(sentence):
                continue
            score = _claim_score(sentence, sentence)
            chunks.append((score, sentence, sentence[:MAX_CONTEXT_CHARS]))
    chunks.sort(key=lambda item: item[0], reverse=True)
    return chunks


def _looks_checkable(sentence: str) -> bool:
    if len(sentence) < 12 or len(sentence) > MAX_CLAIM_CHARS:
        return False
    if UNVERIFIED_RE.search(sentence):
        return True
    if sentence.startswith(("#", ">", "-", "*")) and len(sentence) < 35:
        return False
    if NUMERIC_RE.search(sentence):
        return True
    if SOURCE_LABEL_RE.search(sentence):
        return True
    return any(token in sentence for token in TRIGGERS)


def _claim_score(sentence: str, context: str) -> int:
    score = 0
    if NUMERIC_RE.search(sentence):
        score += 4
    score += sum(1 for token in TRIGGERS if token in sentence)
    if "http" in context:
        score += 2
    if SOURCE_LABEL_RE.search(context):
        score += 1
    if UNVERIFIED_RE.search(sentence):
        score += 1
    return score


def _classify_claim(claim: str, context: str, score: int) -> ClaimCard:
    sources = _context_sources(context)
    weak = [url for url in sources if _is_weak_url(url)]
    strong = [url for url in sources if url not in weak]
    if UNVERIFIED_RE.search(claim) or UNVERIFIED_RE.search(context):
        verdict = "declared_unverified"
        confidence = 0.35
        rationale = "文本自行标注为未经核实或待核实"
    elif strong:
        verdict = "source_attached"
        confidence = min(0.75, 0.45 + score * 0.03)
        rationale = "claim 附近存在具体 http(s) 来源；仍需人工或二次检索确认原文是否支持"
    elif weak:
        verdict = "weak_source"
        confidence = 0.25
        rationale = "claim 附近只有首页、搜索页或弱来源链接"
    else:
        verdict = "unsupported"
        confidence = 0.1
        rationale = "claim 附近没有可追溯 http(s) 来源"
    return ClaimCard(
        claim_text=claim,
        category=_category(claim),
        verdict=verdict,
        confidence=round(confidence, 2),
        rationale=rationale,
        source_urls=strong or weak,
        context_text=context[:MAX_CONTEXT_CHARS],
        claim_hash=hashlib.sha256(claim.encode("utf-8")).hexdigest()[:24],
    )


def _context_sources(context: str) -> list[str]:
    urls = [s.canonical_url for s in extract_sources(context)]
    seen: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.append(url)
    return seen


def _is_weak_url(url: str) -> bool:
    canonical = canonicalize_url(url)
    if not canonical:
        return True
    parsed = urlparse(canonical)
    path = parsed.path.strip("/")
    if not path:
        return True
    lowered_path = path.lower()
    lowered_query = parsed.query.lower()
    if "search" in lowered_path or lowered_query.startswith(("q=", "query=")):
        return True
    return False


def _category(claim: str) -> str:
    if any(term in claim for term in FINANCIAL_TERMS):
        return "financial"
    if any(term in claim for term in POLICY_TERMS):
        return "policy"
    if any(term in claim for term in LEGAL_TERMS):
        return "legal"
    if any(term in claim for term in MARKET_TERMS):
        return "market"
    if any(term in claim for term in EVENT_TERMS):
        return "event"
    if NUMERIC_RE.search(claim):
        return "numerical"
    return "other"


def _normalize_claim(text: str) -> str:
    text = MD_LINK_RE.sub(r"\1 (\2)", text or "")
    text = re.sub(r"^[#>*\-\s\d.、）)]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_CLAIM_CHARS].strip(" -")


def _clean_context(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text
