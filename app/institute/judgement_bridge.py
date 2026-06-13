"""Bridge institute-one outputs into judgement_engine staging.

The bridge is intentionally one-way and candidate-only: it creates a review
queue for judgement_engine adjudication, never canonical knowledge objects.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from .prompts import work_date

RISKY_VERDICTS = ("unsupported", "weak_source", "declared_unverified")
MAX_CLAIMS = 40
MAX_TOPICS = 30


@dataclass(frozen=True)
class BridgeResult:
    path: Path
    claims: int
    topics: int
    verdict_counts: dict[str, int]


async def build_review_queue(
    *,
    date: str | None = None,
    output_dir: Path | None = None,
    max_claims: int = MAX_CLAIMS,
) -> BridgeResult:
    """Write a judgement_engine staging review queue and return its path."""
    wd = date or work_date()
    target_dir = _resolve_output_dir(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{wd}-review-queue.md"

    claims = await _risky_claims(wd, limit=max_claims)
    topics = await _topics(wd)
    sources = await _weak_sources(wd)
    verdict_counts = Counter(str(r["verdict"]) for r in await _all_claims(wd))
    body = render_review_queue(
        date=wd,
        claims=claims,
        topics=topics,
        sources=sources,
        verdict_counts=dict(verdict_counts),
    )
    out_path.write_text(body, encoding="utf-8")
    await bus.emit(
        "judgement_bridge.exported",
        "judgement_bridge",
        wd,
        {"path": str(out_path), "claims": len(claims), "topics": len(topics)},
    )
    return BridgeResult(
        path=out_path,
        claims=len(claims),
        topics=len(topics),
        verdict_counts=dict(verdict_counts),
    )


def render_review_queue(
    *,
    date: str,
    claims: list[dict[str, Any]],
    topics: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    verdict_counts: dict[str, int],
) -> str:
    lines = [
        "---",
        "type: institute-one-review-queue",
        f"date: {date}",
        "source_system: institute-one",
        "status: staging",
        "review_policy: candidate_only_do_not_auto_promote",
        "---",
        "",
        f"# Institute One Review Queue {date}",
        "",
        "## 用法",
        "",
        "这份文件是 judgement_engine 的候选审查队列，不是知识本体。",
        "",
        "建议审查顺序：",
        "",
        "1. 对每条候选先做 semantic retrieval / coverage_probe。",
        "2. 判定为 `update_existing / staging_extend / promote_new / needs_source / archive / reject`。",
        "3. 只有通过 judgement_engine 对象边界和证据规则后，才进入 canonical 层。",
        "",
        "## 今日摘要",
        "",
        f"- 白板/主题候选：{len(topics)}",
        f"- 高风险 claim 候选：{len(claims)}",
        f"- claim verdict 统计：{_fmt_counts(verdict_counts)}",
        "",
        "## 白板与主题候选",
        "",
    ]
    if not topics:
        lines.append("（无）")
    for idx, topic in enumerate(topics[:MAX_TOPICS], start=1):
        lines.extend(_topic_block(idx, topic))

    lines.extend(["", "## 高风险 Claim 候选", ""])
    if not claims:
        lines.append("（无）")
    for idx, claim in enumerate(claims, start=1):
        lines.extend(_claim_block(idx, claim))

    lines.extend(["", "## 弱来源聚合", ""])
    if not sources:
        lines.append("（无）")
    for row in sources:
        lines.append(
            f"- `{row['host'] or 'unknown'}` · {row['n']} claims · {row['canonical_url']}"
        )

    lines.extend([
        "",
        "## Bridge 边界",
        "",
        "- institute-one 负责开放发现；judgement_engine 负责对象命运判定。",
        "- `source_attached` 不是 verified；这里只优先列出风险更高的 claim。",
        "- 本文件可以被删除或重建；不要在这里维护长期判断。",
        "",
    ])
    return "\n".join(lines)


async def _all_claims(date: str) -> list[dict[str, Any]]:
    return await db.query("SELECT verdict FROM fact_cards WHERE work_date = ?", (date,))


async def _risky_claims(date: str, *, limit: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in RISKY_VERDICTS)
    return await db.query(
        f"""
        SELECT
          id, artifact_kind, artifact_id, artifact_path, topic, analyst_id,
          claim_text, category, verdict, confidence, rationale, source_urls,
          context_text, updated_at
        FROM fact_cards
        WHERE work_date = ?
          AND verdict IN ({placeholders})
        ORDER BY
          CASE verdict
            WHEN 'declared_unverified' THEN 0
            WHEN 'unsupported' THEN 1
            WHEN 'weak_source' THEN 2
            ELSE 3
          END,
          updated_at DESC,
          id DESC
        LIMIT ?
        """,
        [date, *RISKY_VERDICTS, min(max(limit, 1), 200)],
    )


async def _topics(date: str) -> list[dict[str, Any]]:
    rows = await db.query(
        """
        SELECT
          b.id AS board_id, b.topic, b.question, b.status, b.created_at, b.updated_at,
          v.path AS artifact_path,
          COUNT(fc.id) AS risky_claims
        FROM whiteboard_boards b
        LEFT JOIN vault_index v
          ON v.artifact_kind = 'whiteboard' AND v.artifact_id = b.id
        LEFT JOIN fact_cards fc
          ON fc.artifact_kind = 'whiteboard'
         AND fc.artifact_id = b.id
         AND fc.verdict IN ('unsupported','weak_source','declared_unverified')
        WHERE b.work_date = ?
        GROUP BY b.id
        ORDER BY risky_claims DESC, b.created_at
        """,
        (date,),
    )
    return rows


async def _weak_sources(date: str) -> list[dict[str, Any]]:
    return await db.query(
        """
        SELECT
          json_extract(fc.source_urls, '$[0]') AS canonical_url,
          COALESCE(es.host, '') AS host,
          COUNT(*) AS n
        FROM fact_cards fc
        LEFT JOIN evidence_sources es
          ON es.canonical_url = json_extract(fc.source_urls, '$[0]')
        WHERE fc.work_date = ?
          AND fc.verdict = 'weak_source'
        GROUP BY canonical_url
        ORDER BY n DESC
        LIMIT 20
        """,
        (date,),
    )


def _topic_block(idx: int, topic: dict[str, Any]) -> list[str]:
    question = str(topic.get("question") or "").strip()
    action = "check_existing"
    if int(topic.get("risky_claims") or 0) > 0:
        action = "possible_blindspot"
    lines = [
        f"### T{idx:02d} · {topic['topic']}",
        "",
        f"- board_id: `{topic['board_id']}`",
        f"- status: `{topic['status']}`",
        f"- artifact: `{topic.get('artifact_path') or ''}`",
        f"- risky_claims: {topic.get('risky_claims') or 0}",
        f"- suggested_action: `{action}`",
    ]
    if question:
        lines.append(f"- question: {question}")
    lines.append("")
    return lines


def _claim_block(idx: int, claim: dict[str, Any]) -> list[str]:
    urls = _loads_json_list(str(claim.get("source_urls") or "[]"))
    suggested = _suggested_action(str(claim.get("verdict") or ""), str(claim.get("category") or ""))
    lines = [
        f"### C{idx:02d} · `{claim['verdict']}` · {claim['category']}",
        "",
        f"> {claim['claim_text']}",
        "",
        f"- fact_card_id: `{claim['id']}`",
        f"- topic: {claim.get('topic') or ''}",
        f"- artifact: `{claim.get('artifact_path') or ''}`",
        f"- analyst_id: `{claim.get('analyst_id') or ''}`",
        f"- confidence: {claim.get('confidence')}",
        f"- rationale: {claim.get('rationale') or ''}",
        f"- suggested_action: `{suggested}`",
    ]
    if urls:
        lines.append("- sources:")
        lines.extend(f"  - {url}" for url in urls)
    else:
        lines.append("- sources: []")
    context = str(claim.get("context_text") or "").strip()
    if context and context != claim.get("claim_text"):
        lines.append(f"- context: {context[:400]}")
    lines.append("")
    return lines


def _suggested_action(verdict: str, category: str) -> str:
    if verdict == "declared_unverified":
        return "needs_source"
    if verdict == "weak_source":
        return "needs_source"
    if verdict == "unsupported" and category in {"financial", "policy", "legal", "market"}:
        return "check_existing_or_possible_conflict"
    if verdict == "unsupported":
        return "check_existing"
    return "review"


def _loads_json_list(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x).strip()]


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "{}"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def _resolve_output_dir(output_dir: Path | None) -> Path:
    if output_dir is not None:
        return output_dir.expanduser()
    raw = getattr(get_settings(), "judgement_bridge_dir", None)
    if raw:
        return Path(raw).expanduser()
    return get_settings().home_dir / "judgement_bridge"
