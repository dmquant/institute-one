"""Evidence ledger: cited URLs extracted from institute artifacts.

This is not a crawler. It records traceable URLs that analysts cited, the
artifact/card where each URL appeared, and a short local context. Later prompts
can reuse this ledger before deciding whether a source needs fresh retrieval.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .. import bus, db

log = logging.getLogger("institute.evidence")

URL_RE = re.compile(r"https?://[^\s<>'\"，。；、]+", re.IGNORECASE)
MD_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\((https?://[^)\s]+)\)", re.IGNORECASE)
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}
TRAILING_PUNCT = ".,;:!?)]}）】》、，。；：！？"
MAX_CONTEXT_CHARS = 700
MAX_TITLE_CHARS = 180
MAX_TOPIC_EVIDENCE = 12


@dataclass(frozen=True)
class ExtractedSource:
    url: str
    canonical_url: str
    host: str
    title: str
    context: str


def canonicalize_url(raw_url: str) -> str:
    """Normalize URL enough for dedup without changing semantic query params."""
    url = str(raw_url or "").strip().strip("<>").rstrip(TRAILING_PUNCT)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(query_pairs, doseq=True)
    return urlunparse((scheme, host, path, "", query, ""))


def extract_sources(text: str) -> list[ExtractedSource]:
    """Extract http(s) sources from Markdown/text, preserving nearby context."""
    if not text:
        return []
    titled: dict[str, str] = {}
    for match in MD_LINK_RE.finditer(text):
        title = _clean_title(match.group(1))
        canonical = canonicalize_url(match.group(2))
        if canonical and title:
            titled.setdefault(canonical, title)

    out: list[ExtractedSource] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text):
        raw = match.group(0).rstrip(TRAILING_PUNCT)
        canonical = canonicalize_url(raw)
        if not canonical or canonical in seen:
            continue
        parsed = urlparse(canonical)
        context = _context_window(text, match.start(), match.end())
        out.append(
            ExtractedSource(
                url=raw,
                canonical_url=canonical,
                host=parsed.netloc.lower(),
                title=titled.get(canonical, ""),
                context=context,
            )
        )
        seen.add(canonical)
    return out


async def ingest_text(
    text: str,
    *,
    artifact_kind: str,
    artifact_id: str,
    artifact_path: str = "",
    topic: str = "",
    analyst_id: str = "",
    work_date: str = "",
    link_type: str = "cited_url",
) -> int:
    """Extract and persist cited URLs. Returns number of new link rows."""
    sources = extract_sources(text)
    if not sources:
        return 0

    inserted = 0
    now = bus.now_iso()
    for source in sources:
        await db.execute(
            """
            INSERT INTO evidence_sources
              (canonical_url, url, host, title, first_seen_at, last_seen_at, source_count, metadata)
            VALUES (?,?,?,?,?,?,1,'{}')
            ON CONFLICT(canonical_url) DO UPDATE SET
              last_seen_at=excluded.last_seen_at,
              source_count=evidence_sources.source_count + 1,
              title=CASE
                WHEN evidence_sources.title = '' THEN excluded.title
                ELSE evidence_sources.title
              END
            """,
            (
                source.canonical_url,
                source.url,
                source.host,
                source.title[:MAX_TITLE_CHARS],
                now,
                now,
            ),
        )
        row = await db.query_one(
            "SELECT id FROM evidence_sources WHERE canonical_url = ?", (source.canonical_url,)
        )
        if not row:
            continue
        context = source.context[:MAX_CONTEXT_CHARS]
        context_hash = hashlib.sha256(
            f"{artifact_kind}|{artifact_id}|{source.canonical_url}|{context}".encode("utf-8")
        ).hexdigest()[:24]
        n = await db.execute(
            """
            INSERT OR IGNORE INTO claim_evidence_links
              (source_id, artifact_kind, artifact_id, artifact_path, topic, analyst_id,
               work_date, claim_text, context_text, context_hash, link_type, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["id"],
                artifact_kind,
                artifact_id,
                artifact_path,
                topic,
                analyst_id,
                work_date,
                _claim_from_context(context),
                context,
                context_hash,
                link_type,
                now,
            ),
        )
        inserted += max(n, 0)

    if inserted:
        await bus.emit(
            "evidence.ingested",
            artifact_kind,
            artifact_id,
            {"links": inserted, "sources": len(sources), "topic": topic},
        )
    return inserted


async def evidence_for_topic(topic: str, *, limit: int = MAX_TOPIC_EVIDENCE) -> list[dict[str, Any]]:
    """Return recent evidence rows matching topic terms or exact topic."""
    terms = _topic_terms(topic)
    if not terms:
        return []
    likes = [f"%{term}%" for term in terms]
    where = " OR ".join(["l.topic LIKE ?", "l.context_text LIKE ?", "s.title LIKE ?"] * len(terms))
    params: list[Any] = []
    for like in likes:
        params.extend([like, like, like])
    params.append(min(max(limit, 1), 50))
    return await db.query(
        f"""
        SELECT
          s.canonical_url, s.url, s.host, s.title, s.last_seen_at, s.source_count,
          l.topic, l.artifact_kind, l.artifact_id, l.artifact_path, l.analyst_id,
          l.work_date, l.claim_text, l.context_text
        FROM claim_evidence_links l
        JOIN evidence_sources s ON s.id = l.source_id
        WHERE {where}
        ORDER BY l.created_at DESC
        LIMIT ?
        """,
        params,
    )


async def evidence_context(topic: str, *, limit: int = 8) -> str:
    rows = await evidence_for_topic(topic, limit=limit)
    if not rows:
        return ""
    lines = [
        "## 既有证据账本（先复用，再决定是否重新检索）",
        "以下是本地 evidence ledger 中与本题可能相关的既有出处。它们不是事实本身，必须结合原文时间点和当前信息重新判断是否仍然有效。",
    ]
    seen: set[str] = set()
    for row in rows:
        url = row["canonical_url"]
        if url in seen:
            continue
        seen.add(url)
        label = row["title"] or row["host"] or url
        topic_label = f"；topic={row['topic']}" if row.get("topic") else ""
        artifact = f"{row['artifact_kind']}:{row['artifact_id']}"
        context = (row.get("claim_text") or row.get("context_text") or "").replace("\n", " ")
        if len(context) > 180:
            context = context[:180] + "..."
        lines.append(f"- {label} | {url} | seen={row['last_seen_at']} | from={artifact}{topic_label} | context={context}")
        if len(seen) >= limit:
            break
    return "\n".join(lines)


def _context_window(text: str, start: int, end: int) -> str:
    left = max(0, start - 320)
    right = min(len(text), end + 320)
    context = text[left:right]
    context = re.sub(r"\s+", " ", context).strip()
    return context


def _claim_from_context(context: str) -> str:
    if not context:
        return ""
    parts = re.split(r"(?<=[。！？!?])\s+", context)
    if len(parts) <= 2:
        return context[:300]
    mid = len(parts) // 2
    return " ".join(parts[max(0, mid - 1): min(len(parts), mid + 2)])[:300]


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    if title.lower().startswith("http"):
        return ""
    return title[:MAX_TITLE_CHARS]


def _topic_terms(topic: str) -> list[str]:
    raw = re.split(r"[\s,，。；;:/|｜()（）\[\]【】]+", topic or "")
    terms: list[str] = []
    for term in raw:
        term = term.strip()
        if len(term) < 2:
            continue
        if term not in terms:
            terms.append(term)
    return terms[:8]
