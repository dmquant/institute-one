"""Research follow-ups: JSON-block parsing and application to mailbox/whiteboard."""
from __future__ import annotations

from pathlib import Path

from app import db
from app.institute import research
from app.institute.research import parse_followups

SAMPLE = """## 后续跟进

报告中对算力供需的分歧最大，建议白板协作辩论；财务口径需要权益分析师单独核对。

```json
{
  "whiteboard_topics": [
    {"topic": "NVDA 算力供需缺口", "question": "2027 年 HBM 供给能否跟上?"},
    {"topic": "AI 资本开支可持续性", "question": "云厂商 capex 同比何时见顶?"}
  ],
  "mailbox_followups": [
    {"analyst_id": "equity-analyst", "subject": "毛利率口径", "body": "请核对数据中心分部毛利率的披露口径变化。"},
    {"analyst_id": "ghost-analyst", "subject": "无效", "body": "这个分析师不存在，应被丢弃。"}
  ]
}
```
"""


def test_parse_followups_extracts_and_caps():
    out = parse_followups(SAMPLE)
    assert [t["topic"] for t in out["whiteboard_topics"]] == [
        "NVDA 算力供需缺口", "AI 资本开支可持续性",
    ]
    assert len(out["mailbox_followups"]) == 2  # validation happens at apply time

    # caps: 5 topics in -> 3 kept; junk tolerated
    many = SAMPLE.replace(
        '"whiteboard_topics": [',
        '"whiteboard_topics": [' + ",".join(
            f'{{"topic": "T{i}", "question": "Q{i}"}}' for i in range(5)
        ) + ",",
    )
    assert len(parse_followups(many)["whiteboard_topics"]) == research.MAX_FOLLOWUP_TOPICS


def test_parse_followups_defensive():
    assert parse_followups("") == {"whiteboard_topics": [], "mailbox_followups": []}
    assert parse_followups("no json here") == {"whiteboard_topics": [], "mailbox_followups": []}
    assert parse_followups("```json\nnot valid json\n```")["whiteboard_topics"] == []


async def test_apply_followups_feeds_pool_and_mailbox():
    # a fake completed-research session whose workspace holds the follow-ups file
    from app.institute import sessions

    session = await sessions.create_session("跟进测试", kind="research")
    ws = Path(session["workspace_dir"])
    (ws / research.FOLLOWUPS_FILE).write_text(SAMPLE, encoding="utf-8")

    await research._apply_followups("item-test", "NVDA", session["id"])

    pool = await db.query("SELECT * FROM topic_pool WHERE source = 'research' ORDER BY id")
    assert [r["topic"] for r in pool] == ["NVDA 算力供需缺口", "AI 资本开支可持续性"]

    threads = await db.query("SELECT * FROM mailbox_threads ORDER BY created_at")
    assert len(threads) == 1  # ghost-analyst dropped
    assert threads[0]["analyst_id"] == "equity-analyst"
    assert "NVDA" in threads[0]["subject"]
