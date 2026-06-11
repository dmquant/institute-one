"""Analyst dailies: guard, sweep, follow-up application with self-mail drop."""
from __future__ import annotations

from pathlib import Path

from app import bus, db
from app.institute import analyst_daily
from app.institute.analysts import get_analyst, roster

SAMPLE_WITH_SELF_MAIL = """## 观察日报

1. 测试观察一（来源：test）。

## 后续跟进

```json
{
  "whiteboard_topics": [
    {"topic": "利率与权益估值的传导", "question": "10Y 上行 50bp 对成长股估值的弹性?"}
  ],
  "mailbox_followups": [
    {"analyst_id": "macro-analyst", "subject": "自问自答", "body": "写给自己的应被丢弃。"},
    {"analyst_id": "equity-analyst", "subject": "估值弹性", "body": "请测算 10Y+50bp 情景下的估值压缩。"}
  ]
}
```
"""


async def test_run_one_completes_and_guards():
    result = await analyst_daily.run_one("macro-analyst")
    assert result["status"] == "completed"

    record = await analyst_daily._get_record()
    assert record["macro-analyst"] == "completed"

    # second run same day is skipped
    again = await analyst_daily.run_one("macro-analyst")
    assert again.get("skipped")

    events = [e for e in await bus.replay(0, types=["analyst_daily.completed"])
              if e.ref_id == "macro-analyst"]
    assert len(events) == 1
    # the shared per-day session exists
    st = await analyst_daily.status()
    assert st["session_id"]
    assert st["analysts"]["macro-analyst"] == "completed"


async def test_run_all_skips_ops_and_done():
    await analyst_daily.run_one("macro-analyst")
    summary = await analyst_daily.run_all()
    ids = [r["analyst_id"] for r in summary["results"]]
    assert "ops-editor" not in ids          # ops category excluded
    assert "macro-analyst" not in ids       # already done today
    assert summary["completed"] == len(ids)  # echo completes everything


async def test_followups_applied_with_self_mail_dropped():
    analyst = get_analyst("macro-analyst")
    session = await analyst_daily._today_session()
    ws = Path(session["workspace_dir"])
    (ws / "macro-analyst.md").write_text(SAMPLE_WITH_SELF_MAIL, encoding="utf-8")

    n_topics, n_mails = await analyst_daily._apply_followups(analyst, ws, "macro-analyst.md")
    assert n_topics == 1
    assert n_mails == 1  # self-mail dropped, equity-analyst kept

    pool = await db.query("SELECT * FROM topic_pool WHERE source = 'analyst-daily'")
    assert len(pool) == 1 and "利率" in pool[0]["topic"]

    threads = await db.query("SELECT * FROM mailbox_threads")
    assert len(threads) == 1
    assert threads[0]["analyst_id"] == "equity-analyst"
    assert analyst.name in threads[0]["subject"]


def test_rotation_skips_to_default_when_no_clis():
    # tests run with all CLI hands disabled -> rotation falls back to default (echo)
    a = roster()[0]
    assert analyst_daily._pick_hand(a, 0) in ("echo", a.hand or "echo")
