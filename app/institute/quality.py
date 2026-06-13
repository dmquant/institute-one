"""Lightweight output quality checks for generated research notes.

These checks are intentionally mechanical. They catch operational failures
(missing output files, stub notes) and surface weak evidence patterns without
pretending to do full fact verification.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

MIN_MARKDOWN_BYTES = 500
URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
SOURCE_LABEL_RE = re.compile(r"(?:来源|source)[:：]\s*([^\n。；;]{2,160})", re.IGNORECASE)
JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


@dataclass
class QualityReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    size_bytes: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "size_bytes": self.size_bytes,
        }


def _add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def check_expected_markdown(path: Path, *, min_bytes: int = MIN_MARKDOWN_BYTES) -> QualityReport:
    report = QualityReport()
    if not path.is_file():
        report.ok = False
        report.errors.append(f"missing expected output file: {path.name}")
        return report

    try:
        data = path.read_bytes()
    except OSError as exc:
        report.ok = False
        report.errors.append(f"could not read output file: {exc}")
        return report

    report.size_bytes = len(data)
    if len(data) < min_bytes:
        report.ok = False
        report.errors.append(f"output file too small: {len(data)} bytes < {min_bytes}")
    return report


def evidence_warnings(text: str, *, require_followups: bool = False) -> list[str]:
    warnings: list[str] = []
    urls = URL_RE.findall(text or "")

    if "file://" in text:
        _add_unique(warnings, "local file:// references are not acceptable evidence")
    if not urls:
        _add_unique(warnings, "no traceable http(s) source URLs found")

    homepage_only: list[str] = []
    search_links = 0
    for url in urls:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            homepage_only.append(parsed.netloc)
        if "search" in path.lower() or parsed.query.lower().startswith(("q=", "query=")):
            search_links += 1
    if homepage_only:
        hosts = ", ".join(sorted(set(homepage_only))[:3])
        _add_unique(warnings, f"homepage-only source links need article/report-level URLs: {hosts}")
    if search_links:
        _add_unique(warnings, "search-result URLs are not acceptable evidence")

    weak_labels = 0
    for match in SOURCE_LABEL_RE.finditer(text or ""):
        snippet = match.group(0)
        if not URL_RE.search(snippet):
            weak_labels += 1
    if weak_labels:
        _add_unique(warnings, f"{weak_labels} source label(s) lack concrete URLs")

    if require_followups:
        blocks = JSON_BLOCK_RE.findall(text or "")
        if not blocks:
            _add_unique(warnings, "missing machine-readable follow-up JSON block")
        elif not any(_loads_followup_block(block) for block in blocks):
            _add_unique(warnings, "follow-up JSON block is not parseable")

    return warnings


def _loads_followup_block(block: str) -> bool:
    try:
        data = json.loads(block)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    return isinstance(data.get("whiteboard_topics"), list) or isinstance(data.get("mailbox_followups"), list)


def quality_callout(warnings: list[str]) -> str:
    if not warnings:
        return ""
    lines = ["[!warning] 质量闸门提示", ""]
    lines.extend(f"- {w}" for w in warnings)
    return "> " + "\n> ".join(lines)
