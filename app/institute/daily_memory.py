"""Observation-level memory and novelty checks for analyst dailies."""
from __future__ import annotations

import json
import logging
import re
import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..hands.registry import get_registry
from ..router import executor
from .analysts import Analyst
from .prompts import work_date

log = logging.getLogger("institute.daily_memory")

RECENT_DAYS = 7
RECENT_LIMIT = 24
MAX_CONTEXT_CHARS = 6000
MAX_AUDIT_TEXT_CHARS = 12000

_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
_NUMBERED_RE = re.compile(r"^\s*(?P<num>\d+)[.)、]\s+(?P<title>.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<title>.+?)\s*$")
_DELTA_RE = re.compile(r"^\s*(?:new_delta|新增(?:事实|量|变化)?|今日新增|增量)\s*[:：]\s*(?P<value>.+?)\s*$", re.I)
_STATUS_RE = re.compile(r"^\s*(?:status|状态)\s*[:：]\s*(?P<value>.+?)\s*$", re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.I | re.S)

_FIELD_PREFIXES = (
    "new_delta", "status", "事实", "判断", "观点", "影响", "来源", "证据",
    "明日关注", "后续跟进", "whiteboard_topics", "mailbox_followups",
)
_SECTION_STOP_WORDS = ("后续跟进", "明日关注", "follow-up", "followups", "json")
_MONITOR_WORDS = ("持续监控", "standing monitor", "无新增", "重复")
_MAIN_WORDS = ("今日新增观察", "新增观察", "主观察")


@dataclass
class Observation:
    ordinal: int
    title: str
    summary: str
    new_delta: str
    status: str
    content_hash: str


@dataclass
class _Draft:
    ordinal: int
    title: str
    status_hint: str
    lines: list[str]


def _plain(text: str, cap: int | None = None) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.strip(" -*_`#")
    if cap is not None and len(text) > cap:
        return text[:cap].rstrip() + "..."
    return text


def _clean_title(title: str) -> str:
    title = _plain(title)
    title = re.sub(r"^(?:观察|Observation)\s*[一二三四五六七八九十0-9]+[：:.\-\s]*", "", title, flags=re.I)
    title = re.sub(r"^(?:主观察|新增观察)\s*[一二三四五六七八九十0-9]*[：:.\-\s]*", "", title)
    return _plain(title, 160) or "未命名观察"


def _status_from_text(value: str, fallback: str = "main") -> str:
    raw = (value or "").strip().lower()
    if any(w in raw for w in ("monitor", "standing", "无新增", "持续", "重复", "repeat")):
        return "monitor" if "repeat" not in raw and "重复" not in raw else "repeat"
    if any(w in raw for w in ("main", "主", "新增")):
        return "main"
    return fallback if fallback in {"main", "monitor", "repeat"} else "main"


def content_hash(title: str, summary: str) -> str:
    payload = f"{_plain(title).lower()}\n{_plain(summary).lower()[:800]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_field_line(text: str) -> bool:
    lowered = text.strip().lower()
    return any(lowered.startswith(prefix.lower() + sep) for prefix in _FIELD_PREFIXES for sep in (":", "："))


def _is_section_heading(title: str) -> bool:
    lowered = title.lower()
    return any(w.lower() in lowered for w in (*_SECTION_STOP_WORDS, *_MAIN_WORDS, *_MONITOR_WORDS))


def _status_for_section(title: str, current: str | None) -> str | None:
    lowered = title.lower()
    if any(w.lower() in lowered for w in _SECTION_STOP_WORDS):
        return None
    if any(w.lower() in lowered for w in _MONITOR_WORDS):
        return "monitor"
    if any(w.lower() in lowered for w in _MAIN_WORDS):
        return "main"
    return current


def _summarize(lines: list[str]) -> tuple[str, str, str | None]:
    delta = ""
    status: str | None = None
    summary_lines: list[str] = []
    in_fence = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m_delta = _DELTA_RE.match(line)
        if m_delta:
            delta = _plain(m_delta.group("value"), 280)
            continue
        m_status = _STATUS_RE.match(line)
        if m_status:
            status = _status_from_text(m_status.group("value"))
            continue
        summary_lines.append(line)
    return _plain("\n".join(summary_lines), 1200), delta, status


def extract_observations(text: str, *, max_items: int = 12) -> list[Observation]:
    """Best-effort parser for the fixed daily structure, with old-format fallback."""
    observations: list[Observation] = []
    current: _Draft | None = None
    current_section: str | None = None
    ordinal = 0

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        summary, delta, explicit_status = _summarize(current.lines)
        status = explicit_status or current.status_hint or "main"
        if not summary and not delta:
            current = None
            return
        title = _clean_title(current.title)
        observations.append(Observation(
            ordinal=current.ordinal,
            title=title,
            summary=summary,
            new_delta=delta,
            status=status,
            content_hash=content_hash(title, delta or summary),
        ))
        current = None

    def start(title: str, status_hint: str) -> None:
        nonlocal current, ordinal
        flush()
        ordinal += 1
        current = _Draft(ordinal=ordinal, title=_clean_title(title), status_hint=status_hint, lines=[])

    for raw in text.splitlines():
        line = raw.rstrip()
        heading = _HEADING_RE.match(line)
        if heading:
            title = heading.group("title")
            level = len(heading.group("marks"))
            next_section = _status_for_section(title, current_section)
            if next_section != current_section or any(w.lower() in title.lower() for w in (*_SECTION_STOP_WORDS, *_MAIN_WORDS, *_MONITOR_WORDS)):
                flush()
                current_section = next_section
                continue
            if level >= 3 or re.match(r"^(?:观察|Observation|主观察|新增观察)\b", title, re.I):
                start(title, current_section or "main")
                continue

        numbered = _NUMBERED_RE.match(line)
        bullet = _BULLET_RE.match(line)
        candidate = numbered.group("title") if numbered else (bullet.group("title") if bullet and current_section else "")
        if candidate and not _is_field_line(candidate) and not _is_section_heading(candidate):
            if current_section or numbered:
                start(candidate, current_section or "main")
                continue

        if current is not None:
            current.lines.append(line)

    flush()
    return observations[:max_items]


def novelty_warnings(text: str, observations: list[Observation] | None = None) -> list[str]:
    observations = observations if observations is not None else extract_observations(text)
    warnings: list[str] = []
    if "今日新增观察" not in text:
        warnings.append("novelty gate: missing section `## 今日新增观察`")
    if "new_delta:" not in text and "新增量" not in text and "今日新增" not in text:
        warnings.append("novelty gate: no explicit `new_delta:` markers found")
    main_missing = [o.title for o in observations if o.status == "main" and not o.new_delta]
    for title in main_missing[:5]:
        warnings.append(f"novelty gate: main observation missing new_delta: {title}")
    if observations and not any(o.status == "main" and o.new_delta for o in observations):
        warnings.append("novelty gate: no main observations with explicit new_delta")
    if not observations:
        warnings.append("novelty gate: no parseable observation blocks found")
    return warnings


def _lower_bound(target_date: str, days: int) -> str:
    try:
        d = date.fromisoformat(target_date)
    except ValueError:
        d = date.fromisoformat(work_date())
    return (d - timedelta(days=days)).isoformat()


async def recent_observations(
    analyst_id: str,
    *,
    days: int = RECENT_DAYS,
    limit: int = RECENT_LIMIT,
    before_date: str | None = None,
) -> list[dict[str, Any]]:
    target = before_date or work_date()
    rows = await db.query(
        """SELECT analyst_id, work_date, ordinal, title, summary, new_delta, status
           FROM analyst_daily_observations
           WHERE analyst_id = ? AND work_date < ? AND work_date >= ?
           ORDER BY work_date DESC, ordinal ASC
           LIMIT ?""",
        (analyst_id, target, _lower_bound(target, days), limit),
    )
    return rows


async def render_recent_context(analyst_id: str, *, days: int = RECENT_DAYS) -> str:
    rows = await recent_observations(analyst_id, days=days)
    if not rows:
        return ""
    lines = [
        "## 近期观察记忆与 novelty gate",
        f"以下是你过去 {days} 天已经写过的观察。今天的主观察必须相对这些内容有可说明的新事实、新数据、新政策动作、新价格/量能反应、新来源、或明确概率变化。",
        "",
    ]
    for row in rows:
        delta = _plain(row.get("new_delta") or "未标注", 220)
        summary = _plain(row.get("summary") or "", 260)
        lines.append(
            f"- {row['work_date']} [{row['status']}] {row['title']} | new_delta: {delta}"
            + (f" | 摘要: {summary}" if summary else "")
        )
    lines.extend([
        "",
        "写作约束：",
        "- 放进 `## 今日新增观察` 的每一条都必须写 `new_delta: ...`，说明相对上列记忆到底新增了什么。",
        "- 同一主题若没有新增事实、执行动作、价格反应、来源更新或概率变化，放入 `## 持续监控（无新增）`，不要占用主观察位。",
        "- 机械复述旧主题会被降级为 standing monitor。",
    ])
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


async def store_daily_observations(
    analyst_id: str,
    daily_date: str,
    text: str,
    *,
    source_task_id: str = "",
) -> list[Observation]:
    observations = extract_observations(text)
    now = bus.now_iso()
    for obs in observations:
        await db.execute(
            """INSERT INTO analyst_daily_observations
                 (analyst_id, work_date, ordinal, title, summary, new_delta, status,
                  source_task_id, content_hash, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(analyst_id, work_date, ordinal) DO UPDATE SET
                 title = excluded.title,
                 summary = excluded.summary,
                 new_delta = excluded.new_delta,
                 status = excluded.status,
                 source_task_id = excluded.source_task_id,
                 content_hash = excluded.content_hash,
                 updated_at = excluded.updated_at""",
            (
                analyst_id,
                daily_date,
                obs.ordinal,
                obs.title,
                obs.summary,
                obs.new_delta,
                obs.status,
                source_task_id,
                obs.content_hash,
                now,
                now,
            ),
        )
    return observations


def _json_payload(text: str) -> dict[str, Any] | None:
    for m in _FENCE_RE.finditer(text or ""):
        try:
            obj = json.loads(m.group("body"))
            return obj if isinstance(obj, dict) else None
        except ValueError:
            pass
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None


async def cheap_novelty_audit(
    analyst: Analyst,
    text: str,
    recent_context: str,
    *,
    workspace: Path,
) -> list[str]:
    """Ask the configured cheap/local hand to flag repeated observations."""
    if not recent_context:
        return []
    settings = get_settings()
    registry = get_registry()
    if not settings.cheap_hand or not registry.is_available(settings.cheap_hand):
        return []

    prompt = (
        "你是 analyst-daily novelty gate，只做去重和增量审稿，不写投资判断。\n"
        "比较【近期观察记忆】和【今日日报草稿】。\n"
        "任务：找出放在主观察里但没有新事实/新数据/新政策动作/新价格反应/新来源/概率变化的重复主题。\n"
        "只返回 JSON，不要解释，格式：\n"
        '{"warnings":["简短警告"],"repeat_titles":["重复主观察标题"],"monitor_titles":["应降级为持续监控的标题"]}\n\n'
        "【近期观察记忆】\n"
        f"{recent_context[:MAX_CONTEXT_CHARS]}\n\n"
        "【今日日报草稿】\n"
        f"{text[:MAX_AUDIT_TEXT_CHARS]}"
    )
    try:
        task = await executor.submit(
            settings.cheap_hand,
            prompt,
            source="analyst-novelty",
            model=settings.cheap_model,
            workspace=workspace,
            timeout_s=300,
            fallback=False,
        )
    except Exception:  # noqa: BLE001
        log.exception("cheap novelty audit crashed for %s", analyst.id)
        return []

    if task.status != "completed" or not task.output:
        await bus.emit("analyst_daily.novelty_audit_skipped", "analyst", analyst.id, {
            "date": work_date(), "status": task.status, "task_id": task.id, "error": task.error,
        })
        return []

    payload = _json_payload(task.output)
    if not payload:
        await bus.emit("analyst_daily.novelty_audit_unparseable", "analyst", analyst.id, {
            "date": work_date(), "task_id": task.id,
        })
        return []

    warnings = [str(w).strip() for w in payload.get("warnings") or [] if str(w).strip()]
    repeat_titles = [str(t).strip() for t in payload.get("repeat_titles") or [] if str(t).strip()]
    monitor_titles = [str(t).strip() for t in payload.get("monitor_titles") or [] if str(t).strip()]
    out = [f"novelty gate: {w}" for w in warnings[:6]]
    for title in repeat_titles[:6]:
        out.append(f"novelty gate: repeated main observation should be demoted: {title}")
    for title in monitor_titles[:6]:
        out.append(f"novelty gate: should move to standing monitor: {title}")
    if out:
        await bus.emit("analyst_daily.novelty_audited", "analyst", analyst.id, {
            "date": work_date(), "task_id": task.id, "warnings": out,
        })
    return out
