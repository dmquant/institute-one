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
from . import prompt_overrides
from .analysts import Analyst

# The four override mount points below (prompt_overrides.SCOPES) render
# through prompt_overrides.render(): an ACTIVE override row replaces the code
# default; with no active override the output is byte-identical to these
# constants (the templates are the former inline strings, unchanged).

CITATION_MANDATE = """\
【引用规范】所有事实性论断必须给出来源（链接、报告名或数据出处）。无法核实的内容必须明确标注「未经核实」。
区分事实与观点：观点用「我认为/判断」开头。数字给出时间点。禁止编造数据。\
"""

FILE_DELIVERABLE = """\
【交付规范】把完整成果写入工作目录下的文件 {filename}（Markdown，中文为主）。\
写完后只回复一行：DONE: {filename}\
"""

DATE_ANCHOR_TEMPLATE = "【时间锚点】今天是 {datetime}（新加坡时间）。所有「最近/目前/今年」均以此为准。"

PERSONA_TEMPLATE = "你是 {name}（{name_en}），AI 研究所的{focus}。\n{persona}"


def now_sgt() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


def work_date() -> str:
    """The SGT calendar date — the canonical work date everywhere."""
    return now_sgt().strftime("%Y-%m-%d")


def date_anchor() -> str:
    n = now_sgt()
    return prompt_overrides.render(
        "prompts.date_anchor", DATE_ANCHOR_TEMPLATE,
        datetime=n.strftime("%Y-%m-%d %A %H:%M"),
    )


def persona_block(analyst: Analyst) -> str:
    return prompt_overrides.render(
        "prompts.persona_block", PERSONA_TEMPLATE,
        name=analyst.name, name_en=analyst.name_en,
        focus=analyst.focus, persona=analyst.persona,
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
    memory_block: str | None = None,
) -> str:
    """The standard prompt sandwich: anchor → persona → memory → context → task → mandates.

    ``memory_block`` is the analyst's standing memory (see
    ``institute.memory.memory_block``); it slots right after the persona so it
    reads as part of who the analyst is, before any per-task context.
    """
    parts = [date_anchor(), persona_block(analyst)]
    if memory_block and memory_block.strip():
        parts.append(memory_block.strip())
    for block in context_blocks or []:
        if block and block.strip():
            parts.append(block.strip())
    parts.append(f"## 任务\n{task.strip()}")
    parts.append(prompt_overrides.render("prompts.citation_mandate", CITATION_MANDATE))
    if output_file:
        parts.append(prompt_overrides.render(
            "prompts.file_deliverable", FILE_DELIVERABLE, filename=output_file,
        ))
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
