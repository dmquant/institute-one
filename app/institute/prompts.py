"""Prompt building blocks shared by every domain loop.

Conventions carried over from the previous system:
- Every analyst prompt opens with a date anchor (SGT) so models never reason
  from a stale training-date assumption.
- CITATION_MANDATE: claims need sources; uncertainty must be marked.
- Deliverables are FILES in the workspace ("write X.md, reply with one line"),
  because file artifacts are the product.
- Previous-step context is summarized into a bounded block, never dumped whole.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import get_settings
from .analysts import Analyst

CITATION_MANDATE = """\
【引用规范】所有事实性论断必须给出可追溯来源：优先使用具体文章/公告/数据页 URL，并写明发布时间或数据时间点。
禁止把网站首页、媒体名、搜索结果页、file:// 本地 scratch 链接、前序模型输出当作事实证据；这些只能作为线索。
无法用具体来源核实的数字、事件、价格、日程、人物状态、政策动作必须明确标注「未经核实」，不得写成确定事实。
区分事实与观点：观点用「我认为/判断」开头，并说明依赖了哪些已核实事实。禁止编造数据。\
"""

FILE_DELIVERABLE = """\
【交付规范】把完整成果写入工作目录下的文件 {filename}（Markdown，中文为主）。\
写完后只回复一行：DONE: {filename}\
"""


def now_sgt() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


def work_date() -> str:
    """The SGT calendar date — the canonical work date everywhere."""
    return now_sgt().strftime("%Y-%m-%d")


def date_anchor() -> str:
    n = now_sgt()
    return f"【时间锚点】今天是 {n.strftime('%Y-%m-%d %A %H:%M')}（新加坡时间）。所有「最近/目前/今年」均以此为准。"


def persona_block(analyst: Analyst) -> str:
    return (
        f"你是 {analyst.name}（{analyst.name_en}），AI 研究所的{analyst.focus}。\n"
        f"{analyst.persona}"
    )


def previous_steps_block(results: list[tuple[str, str]], budget_chars: int = 8000) -> str:
    """results: [(title, summary)]. Bounded context from earlier steps."""
    if not results:
        return ""
    parts = ["## 前序步骤结论（摘要）"]
    per = max(500, budget_chars // max(len(results), 1))
    for title, summary in results:
        s = (summary or "").strip()
        if len(s) > per:
            s = s[:per] + "…"
        parts.append(f"### {title}\n{s}")
    block = "\n\n".join(parts)
    return block[:budget_chars]


def build_analyst_prompt(
    analyst: Analyst,
    task: str,
    *,
    context_blocks: list[str] | None = None,
    output_file: str | None = None,
) -> str:
    """The standard prompt sandwich: anchor → persona → context → task → mandates."""
    parts = [date_anchor(), persona_block(analyst)]
    for block in context_blocks or []:
        if block and block.strip():
            parts.append(block.strip())
    parts.append(f"## 任务\n{task.strip()}")
    parts.append(CITATION_MANDATE)
    if output_file:
        parts.append(FILE_DELIVERABLE.format(filename=output_file))
    return "\n\n".join(parts)


def substitute_variables(text: str, variables: dict[str, str]) -> str:
    for k, v in variables.items():
        text = text.replace("${" + k + "}", str(v))
    return text


def extract_summary(text: str, cap: int = 800) -> str:
    """Pull the 核心结论 section if present, else the head of the text."""
    marker_hits = [m for m in ("## 核心结论", "# 核心结论", "核心结论") if m in text]
    if marker_hits:
        seg = text.split(marker_hits[0], 1)[1]
        for stop in ("\n## ", "\n# "):
            if stop in seg:
                seg = seg.split(stop, 1)[0]
        return seg.strip()[:cap]
    return text.strip()[:cap]
