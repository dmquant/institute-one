"""Analyst dailies: guard, sweep, follow-up application with self-mail drop."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app import bus, db
from app.institute import analyst_daily
from app.institute.analysts import get_analyst, roster

SAMPLE_WITH_SELF_MAIL = """## 今日新增观察

### 观察一：测试观察一
new_delta: 测试新增事实。
status: main
事实: 测试观察一（来源：https://example.com/test）。
判断: 我认为这是测试用判断。
影响: 测试影响。

## 持续监控（无新增）

### 观察二：旧主题
status: monitor
事实: 没有新增事实，仅维持观察。

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


async def test_daily_prompt_includes_recent_observation_memory(monkeypatch):
    await db.execute(
        """INSERT INTO analyst_daily_observations
             (analyst_id, work_date, ordinal, title, summary, new_delta, status,
              source_task_id, content_hash, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "macro-analyst",
            "2026-06-12",
            1,
            "对外投资新规",
            "政策日报已经报道过对外投资规定。",
            "首次出现规则文本。",
            "main",
            "task-old",
            "hash-old",
            "2026-06-12T00:00:00+00:00",
            "2026-06-12T00:00:00+00:00",
        ),
    )
    captured: dict[str, str] = {}

    async def fake_submit(hand, prompt, *, source, model, session_id, workspace):
        captured["prompt"] = prompt
        body = (
            "## 今日新增观察\n\n"
            "### 观察一：央行流动性变化\n"
            "new_delta: 今天出现新的公开市场操作数据。\n"
            "status: main\n"
            "事实: 来源 https://example.com/liquidity/1 ，发布时间 2026-06-13。\n"
            "判断: 我认为边际影响偏中性。\n"
            "影响: 利率曲线短端承压。\n"
            + "补充正文。" * 120
            + "\n\n## 后续跟进\n\n```json\n"
            '{"whiteboard_topics": [], "mailbox_followups": []}'
            "\n```\n"
        )
        (Path(workspace) / "macro-analyst.md").write_text(body, encoding="utf-8")
        return SimpleNamespace(
            id="task-memory",
            status="completed",
            hand=hand,
            requested_hand=hand,
            error=None,
            output="",
        )

    monkeypatch.setattr(analyst_daily.executor, "submit", fake_submit)

    result = await analyst_daily.run_one("macro-analyst", force=True)

    assert result["status"] == "completed"
    assert "近期观察记忆" in captured["prompt"]
    assert "对外投资新规" in captured["prompt"]
    assert "new_delta" in captured["prompt"]


async def test_run_one_stores_daily_observations(monkeypatch):
    async def fake_submit(hand, prompt, *, source, model, session_id, workspace):
        body = (
            "## 今日新增观察\n\n"
            "### 观察一：美元指数突破区间\n"
            "new_delta: 今天美元指数突破上周高点并伴随收益率上行。\n"
            "status: main\n"
            "事实: 来源 https://example.com/dxy/1 ，发布时间 2026-06-13。\n"
            "判断: 我认为外汇波动风险上升。\n"
            "影响: 新兴市场资产折现压力上升。\n"
            + "补充正文。" * 120
            + "\n\n## 持续监控（无新增）\n\n"
            "### 观察二：上周政策主题\n"
            "status: monitor\n"
            "事实: 无新增事实，仅观察后续执行。\n"
            + "\n\n## 后续跟进\n\n```json\n"
            '{"whiteboard_topics": [], "mailbox_followups": []}'
            "\n```\n"
        )
        (Path(workspace) / "macro-analyst.md").write_text(body, encoding="utf-8")
        return SimpleNamespace(
            id="task-store",
            status="completed",
            hand=hand,
            requested_hand=hand,
            error=None,
            output="",
        )

    monkeypatch.setattr(analyst_daily.executor, "submit", fake_submit)

    result = await analyst_daily.run_one("macro-analyst", force=True)

    assert result["observations"]["total"] == 2
    rows = await db.query(
        "SELECT title, new_delta, status FROM analyst_daily_observations "
        "WHERE analyst_id = ? ORDER BY ordinal",
        ("macro-analyst",),
    )
    assert rows[0]["title"] == "美元指数突破区间"
    assert "突破上周高点" in rows[0]["new_delta"]
    assert rows[0]["status"] == "main"
    assert rows[1]["status"] == "monitor"


def test_rotation_skips_to_default_when_no_clis():
    # tests run with all CLI hands disabled -> rotation falls back to default (echo)
    a = roster()[0]
    assert analyst_daily._pick_hand(a, 0) in ("echo", a.hand or "echo")


async def test_missing_output_retries_next_hand(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(analyst_daily, "_candidate_hands", lambda _first: ["broken", "echo"])

    async def fake_submit(hand, prompt, *, source, model, session_id, workspace):
        calls.append(hand)
        if hand == "echo":
            body = (
                "## 今日新增观察\n\n"
                "### 观察一：测试观察\n"
                "new_delta: 今天出现新的测试事实。\n"
                "status: main\n"
                "事实：测试来源 https://example.com/report/1 。\n"
                "判断：这是足够长的测试观点。\n"
                "影响：用于验证重试路径。\n\n"
                + "补充正文。" * 120
                + "\n\n## 后续跟进\n\n```json\n"
                '{"whiteboard_topics": [], "mailbox_followups": []}'
                "\n```\n"
            )
            (Path(workspace) / "macro-analyst.md").write_text(body, encoding="utf-8")
        return SimpleNamespace(
            id=f"task-{hand}",
            status="completed",
            hand=hand,
            requested_hand=hand,
            error=None,
            output="",
        )

    monkeypatch.setattr(analyst_daily.executor, "submit", fake_submit)

    result = await analyst_daily.run_one("macro-analyst", force=True)

    assert result["status"] == "completed"
    assert calls == ["broken", "echo"]
    assert result["attempts"][0]["status"] == "failed"
    assert "missing expected output file" in result["attempts"][0]["error"]
    assert result["attempts"][1]["status"] == "completed"
