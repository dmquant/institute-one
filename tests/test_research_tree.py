"""BFS research tree / Explore mode (ROADMAP Phase 7).

Test oracles (the REVIEW-C1 rework pattern):

- Tree expansion runs against a fixture-driven explorer (``explorer_output``
  monkeypatches executor.submit for explore prompts ONLY) that returns
  production-shaped line-protocol outputs keyed by node topic — the echoed
  prompt is deliberately NOT a children oracle. The fixture also audits the
  REVIEW-D4 H1 invariant on EVERY explore call: a claimed node's parent must
  already be completed with a durable summary (violations fail the test).
- One real-echo full-chain test locks the echo-immunity contract: a mirrored
  explore prompt has no line-anchored protocol lines, so the node completes
  through the real executor path with ZERO children.
- Claims, caps and budgets are conditional-claim arbitrated in the database:
  concurrency tests drive overlapping ticks/creates through asyncio.gather.
- REVIEW-D4 regressions: H1 (early child claim before the parent conclusion
  is durable), H2 (stop racing a running parent stranding pending rows), M2
  (tree.completed must be the drained final snapshot) each have a dedicated
  reproduction test below.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import research_tree
from app.institute.prompts import work_date
from app.router import executor

TOPIC_RE = re.compile(r"【当前主题】(.*)")


@pytest.fixture
def explorer_output(monkeypatch):
    """Production-shaped explore outputs keyed by (neutralized) node topic.
    Explore prompts get a canned line-protocol result; every other submit
    passes through to the real executor/echo path. ``gate``/``entered`` let
    tests pause mid-flight; ``inflight``/``max_inflight`` observe overlap;
    ``violations`` collects REVIEW-D4 H1 breaches (an explored node whose
    parent is not completed-with-summary at call time)."""
    state = SimpleNamespace(
        scripts={},                       # topic -> output text
        default="CONCLUSION: 该方向已看清，无需继续下钻。",
        task_status="completed",
        calls=[],                         # topics in call order
        prompts=[],                       # full prompts in call order
        delay=0.0,
        gate=None,                        # asyncio.Event: block completion until set
        entered=None,                     # asyncio.Event: set when a call starts
        inflight=0,
        max_inflight=0,
        seq=0,
        violations=[],                    # H1 invariant breaches (must stay empty)
    )
    orig_submit = executor.submit

    async def _submit(hand, prompt, **kwargs):
        if "研究所的探索研究员" not in prompt:   # not EXPLORE_PROMPT
            return await orig_submit(hand, prompt, **kwargs)
        m = TOPIC_RE.search(prompt)
        topic = (m.group(1) if m else "").strip()
        state.calls.append(topic)
        state.prompts.append(prompt)
        # H1 invariant audit: the running node's parent (if any) must already
        # be completed with a durable summary BEFORE the child is explored
        row = await db.query_one(
            "SELECT n.id, n.parent_id, p.status AS parent_status, p.summary AS parent_summary "
            "FROM research_tree_nodes n LEFT JOIN research_tree_nodes p ON p.id = n.parent_id "
            "WHERE n.topic = ? AND n.status = 'running'",
            (topic,),
        )
        if row and row["parent_id"] and (
            row["parent_status"] != "completed" or not row["parent_summary"]
        ):
            state.violations.append(topic)
        state.seq += 1
        state.inflight += 1
        state.max_inflight = max(state.max_inflight, state.inflight)
        try:
            if state.entered is not None:
                state.entered.set()
            if state.delay:
                await asyncio.sleep(state.delay)
            if state.gate is not None:
                await state.gate.wait()
        finally:
            state.inflight -= 1
        if state.task_status != "completed":
            return SimpleNamespace(id=f"fake-{state.seq}", status=state.task_status, output="")
        return SimpleNamespace(
            id=f"fake-{state.seq}", status="completed",
            output=state.scripts.get(topic, state.default),
        )

    monkeypatch.setattr(executor, "submit", _submit)
    return state


async def _set_limits(daily_tree_cap: int = 10, node_concurrency: int = 2) -> None:
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (research_tree.LIMITS_KEY,
         f'{{"daily_tree_cap": {daily_tree_cap}, "node_concurrency": {node_concurrency}}}'),
    )


async def _run_until_terminal(tree_id: str, max_ticks: int = 8) -> str:
    status = ""
    for _ in range(max_ticks):
        await research_tree.tick()
        row = await db.query_one("SELECT status FROM research_trees WHERE id = ?", (tree_id,))
        status = row["status"]
        if status in ("completed", "failed", "stopped"):
            break
    return status


async def _nodes(tree_id: str) -> list[dict]:
    return await db.query(
        "SELECT * FROM research_tree_nodes WHERE tree_id = ? ORDER BY depth, created_at, id",
        (tree_id,),
    )


async def _done_events(tree_id: str) -> list:
    return [e for e in await bus.replay(0, types=["tree.completed"]) if e.ref_id == tree_id]


# ==== creation ================================================================

async def test_create_tree_creates_pending_root():
    await _set_limits()
    tree = await research_tree.create_tree("  量子   计算  ")
    assert tree["status"] == "pending"
    assert tree["root_topic"] == "量子 计算"          # whitespace collapsed
    assert tree["max_depth"] == 2 and tree["max_nodes"] == 12
    assert tree["announced_at"] is None
    assert len(tree["nodes"]) == 1
    root = tree["nodes"][0]
    assert root["parent_id"] is None
    assert root["depth"] == 0
    assert root["status"] == "pending"
    assert root["topic"] == "量子 计算"
    assert root["question"] == ""
    assert root["score"] is None                      # written only after model completion


async def test_create_tree_validation():
    await _set_limits()
    with pytest.raises(ValueError, match="empty"):
        await research_tree.create_tree("   ")
    with pytest.raises(ValueError, match="exceeds"):
        await research_tree.create_tree("题" * (research_tree.MAX_TOPIC_LEN + 1))
    with pytest.raises(ValueError, match="max_depth"):
        await research_tree.create_tree("x", max_depth=research_tree.MAX_DEPTH_LIMIT + 1)
    with pytest.raises(ValueError, match="max_depth"):
        await research_tree.create_tree("x", max_depth=-1)
    with pytest.raises(ValueError, match="max_nodes"):
        await research_tree.create_tree("x", max_nodes=0)
    with pytest.raises(ValueError, match="max_nodes"):
        await research_tree.create_tree("x", max_nodes=research_tree.MAX_NODES_LIMIT + 1)


async def test_daily_tree_cap_concurrent_creates_never_exceed():
    """The admin_state counter row is the arbiter (conditional UPDATE booked
    before the tree lands): concurrent creates can never jointly exceed the
    cap. The counter counts BOOKED attempts (REVIEW-D4 N2 naming)."""
    await _set_limits(daily_tree_cap=2)
    results = await asyncio.gather(*(research_tree.create_tree(f"主题{i}") for i in range(5)))
    created = [r for r in results if not r.get("refused")]
    refused = [r for r in results if r.get("refused") == "daily_cap"]
    assert len(created) == 2 and len(refused) == 3
    assert all(r["booked_today"] >= 2 for r in refused)
    assert await research_tree.trees_booked_today() == 2
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?",
        (research_tree.TREES_BOOKED_KEY_PREFIX + work_date(),),
    )
    assert row["value"] == "2"
    # cap 0 disables creation outright
    await _set_limits(daily_tree_cap=0)
    out = await research_tree.create_tree("再来一棵")
    assert out["refused"] == "daily_cap"


async def test_get_limits_defaults_and_garbage_row():
    # migration seeds the row with the in-code defaults
    limits = await research_tree.get_limits()
    assert limits == {"daily_tree_cap": research_tree.DEFAULT_DAILY_TREE_CAP,
                      "node_concurrency": research_tree.DEFAULT_NODE_CONCURRENCY}
    await db.execute("UPDATE admin_state SET value = 'not json' WHERE key = ?",
                     (research_tree.LIMITS_KEY,))
    assert await research_tree.get_limits() == limits    # corrupt row -> defaults
    await db.execute(
        "UPDATE admin_state SET value = ? WHERE key = ?",
        ('{"daily_tree_cap": 5.9, "node_concurrency": true}', research_tree.LIMITS_KEY),
    )
    partial = await research_tree.get_limits()
    assert partial["daily_tree_cap"] == 5                # floats coerce to int
    assert partial["node_concurrency"] == research_tree.DEFAULT_NODE_CONCURRENCY  # bools rejected
    await db.execute(
        "UPDATE admin_state SET value = ? WHERE key = ?",
        ('{"daily_tree_cap": -4, "node_concurrency": -1}', research_tree.LIMITS_KEY),
    )
    floored = await research_tree.get_limits()
    assert floored["daily_tree_cap"] == 0 and floored["node_concurrency"] == 1


# ==== BFS expansion to completion (line-protocol fixture) =====================

async def test_full_bfs_expansion_layers_then_completion(explorer_output):
    await _set_limits()
    explorer_output.scripts = {
        "量子计算": ("CONCLUSION: 量子计算处于 NISQ 向容错过渡期。\n"
                     "CHILD: 硬件路线 | 哪条硬件路线最可能率先实现容错？\n"
                     "CHILD: 商业化 | 未来三年谁能率先商业化变现？"),
        "硬件路线": ("CONCLUSION: 超导与离子阱领先，光量子追赶。\n"
                     "CHILD: 超导纠错 | 超导路线的纠错开销拐点何时出现？"),
        "商业化": "CONCLUSION: 短期收入以云接入与科研订阅为主。",
        "超导纠错": "CONCLUSION: 表面码开销仍高，逻辑量子比特尚未规模化。",
    }
    tree = await research_tree.create_tree("量子计算", max_depth=2, max_nodes=12)
    assert await _run_until_terminal(tree["id"]) == "completed"

    # BFS order: root first, then the whole of depth 1, then depth 2
    assert explorer_output.calls[0] == "量子计算"
    assert set(explorer_output.calls[1:3]) == {"硬件路线", "商业化"}
    assert explorer_output.calls[3] == "超导纠错"
    assert Counter(explorer_output.calls) == {t: 1 for t in explorer_output.scripts}
    assert explorer_output.violations == []            # H1 invariant held throughout

    nodes = await _nodes(tree["id"])
    assert len(nodes) == 4
    assert all(n["status"] == "completed" for n in nodes)
    by_topic = {n["topic"]: n for n in nodes}
    assert by_topic["硬件路线"]["parent_id"] == by_topic["量子计算"]["id"]
    assert by_topic["超导纠错"]["parent_id"] == by_topic["硬件路线"]["id"]
    assert by_topic["超导纠错"]["depth"] == 2
    assert by_topic["商业化"]["summary"] == "短期收入以云接入与科研订阅为主。"
    assert by_topic["超导纠错"]["question"] == "超导路线的纠错开销拐点何时出现？"
    assert all(n["task_id"] for n in nodes)
    assert all(n["finished_at"] for n in nodes)

    row = await db.query_one(
        "SELECT finished_at, announced_at FROM research_trees WHERE id = ?", (tree["id"],)
    )
    assert row["finished_at"] and row["announced_at"]

    # events: one node_completed per node, exactly one tree.completed snapshot
    node_events = [e for e in await bus.replay(0, types=["tree.node_completed"])
                   if e.ref_id == tree["id"]]
    assert len(node_events) == 4
    assert all(e.payload["status"] == "completed" for e in node_events)
    root_event = next(e for e in node_events if e.payload["topic"] == "量子计算")
    assert root_event.payload["children_added"] == 2
    done_events = await _done_events(tree["id"])
    assert len(done_events) == 1
    assert done_events[0].payload["status"] == "completed"
    assert done_events[0].payload["nodes"] == {"completed": 4}


async def test_node_completion_writes_score_and_bad_score_stays_null(explorer_output):
    await _set_limits()
    explorer_output.scripts = {
        "有评分": "CONCLUSION: 有价值的结论。\nSCORE: 91.5\n",
        "坏评分": "CONCLUSION: 仍然完成。\nSCORE: NaN\n",
    }
    scored = await research_tree.create_tree("有评分", max_depth=0)
    unscored = await research_tree.create_tree("坏评分", max_depth=0)
    assert await _run_until_terminal(scored["id"]) == "completed"
    assert await _run_until_terminal(unscored["id"]) == "completed"

    scored_node = (await _nodes(scored["id"]))[0]
    unscored_node = (await _nodes(unscored["id"]))[0]
    assert scored_node["score"] == 91.5
    assert unscored_node["score"] is None                # parse failure never fails the node
    assert unscored_node["status"] == "completed"
    events = [
        e for e in await bus.replay(0, types=["tree.node_completed"])
        if e.ref_id in (scored["id"], unscored["id"])
    ]
    assert {e.ref_id: e.payload["score"] for e in events} == {
        scored["id"]: 91.5,
        unscored["id"]: None,
    }


async def test_child_prompt_ancestry_carries_parent_conclusions(explorer_output):
    """REVIEW-D4 H1 semantics: by the time a child is explored, its whole
    ancestor chain is completed and the prompt carries the durable
    conclusions (root AND intermediate layers)."""
    await _set_limits()
    explorer_output.scripts = {
        "父链": "CONCLUSION: 根结论甲。\nCHILD: 中层 | 中层问题？",
        "中层": "CONCLUSION: 中层结论乙。\nCHILD: 叶层 | 叶层问题？",
        "叶层": "CONCLUSION: 叶层结论丙。",
    }
    tree = await research_tree.create_tree("父链", max_depth=2, max_nodes=12)
    assert await _run_until_terminal(tree["id"]) == "completed"
    assert explorer_output.violations == []

    leaf_prompt = next(p for p in explorer_output.prompts if "【当前主题】叶层" in p)
    assert "根结论甲。" in leaf_prompt                  # depth-0 conclusion in the chain
    assert "中层结论乙。" in leaf_prompt                # depth-1 conclusion in the chain
    assert "第 0 层" in leaf_prompt and "第 1 层" in leaf_prompt
    assert "（无结论）" not in leaf_prompt              # never an empty ancestor slot


async def test_claim_refuses_child_whose_parent_is_not_completed():
    """REVIEW-D4 H1 defense-in-depth: even if a pending child somehow exists
    under a non-completed parent (operator edit / legacy rows), the claim
    guard refuses it until the parent conclusion is durable."""
    await _set_limits()
    tree = await research_tree.create_tree("守卫")
    root = (await _nodes(tree["id"]))[0]
    await db.execute(
        "UPDATE research_tree_nodes SET status='running' WHERE id = ?", (root["id"],)
    )
    await db.execute(
        "UPDATE research_trees SET status='exploring' WHERE id = ?", (tree["id"],)
    )
    await db.execute(
        "INSERT INTO research_tree_nodes (id, tree_id, parent_id, depth, topic, question, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("early-child-1", tree["id"], root["id"], 1, "过早子节点", "", "pending", bus.now_iso()),
    )
    assert await research_tree._claim_next_node(5) is None      # guard refuses
    # parent completes with a durable summary -> the child becomes claimable
    await db.execute(
        "UPDATE research_tree_nodes SET status='completed', summary='父结论', finished_at=? "
        "WHERE id = ?", (bus.now_iso(), root["id"]),
    )
    claimed = await research_tree._claim_next_node(5)
    assert claimed and claimed["id"] == "early-child-1"


async def test_lost_running_claim_discards_children_batch(explorer_output):
    """REVIEW-D4 H1 transaction: losing the running claim mid-flight discards
    the WHOLE result batch — no children may exist without their parent's
    durable conclusion."""
    await _set_limits()
    explorer_output.scripts = {
        "失权": "CONCLUSION: 不该落库的结论。\nCHILD: 不该出现 | ？",
    }
    tree = await research_tree.create_tree("失权")
    explorer_output.gate = asyncio.Event()
    explorer_output.entered = asyncio.Event()
    tick_task = asyncio.create_task(research_tree.tick())
    await explorer_output.entered.wait()
    root = (await _nodes(tree["id"]))[0]
    # an operator reset yanks the claim while the model runs
    await db.execute(
        "UPDATE research_tree_nodes SET status='pending' WHERE id = ?", (root["id"],)
    )
    explorer_output.gate.set()
    await tick_task
    nodes = await _nodes(tree["id"])
    assert [n["topic"] for n in nodes] == ["失权"]      # batch discarded entirely
    root_after = nodes[0]
    assert root_after["status"] == "pending"            # still re-drivable
    assert root_after["summary"] is None


async def test_max_depth_prunes_grandchildren(explorer_output):
    await _set_limits()
    explorer_output.scripts = {
        "根": "CONCLUSION: 根结论。\nCHILD: 一层 | 一层问题？",
        "一层": "CONCLUSION: 一层结论。\nCHILD: 二层 | 二层问题？",
    }
    tree = await research_tree.create_tree("根", max_depth=1, max_nodes=12)
    assert await _run_until_terminal(tree["id"]) == "completed"

    nodes = await _nodes(tree["id"])
    by_topic = {n["topic"]: n for n in nodes}
    assert by_topic["根"]["status"] == "completed"
    assert by_topic["一层"]["status"] == "completed"
    cut = by_topic["二层"]
    assert cut["status"] == "pruned"                  # depth 2 > max_depth 1
    assert cut["depth"] == 2
    assert cut["question"] == "二层问题？"            # the cut direction stays visible
    assert cut["finished_at"]                          # born terminal
    assert explorer_output.calls.count("二层") == 0    # pruned nodes never explored


async def test_max_nodes_budget_prunes_overflow(explorer_output):
    await _set_limits()
    explorer_output.scripts = {
        "预算": ("CONCLUSION: 根结论。\n"
                 "CHILD: 甲 | 甲问题？\n"
                 "CHILD: 乙 | 乙问题？"),
    }
    tree = await research_tree.create_tree("预算", max_depth=2, max_nodes=2)
    assert await _run_until_terminal(tree["id"]) == "completed"

    nodes = await _nodes(tree["id"])
    explored = [n for n in nodes if n["status"] != "pruned"]
    pruned = [n for n in nodes if n["status"] == "pruned"]
    assert len(explored) == 2                          # root + 甲 (budget includes the root)
    assert [n["topic"] for n in pruned] == ["乙"]      # overflow documented, not explored
    assert explorer_output.calls.count("乙") == 0


async def test_failed_root_lands_failed_tree(explorer_output):
    await _set_limits()
    explorer_output.task_status = "failed"
    tree = await research_tree.create_tree("必败主题")
    assert await _run_until_terminal(tree["id"]) == "failed"
    nodes = await _nodes(tree["id"])
    assert [n["status"] for n in nodes] == ["failed"]
    node_events = [e for e in await bus.replay(0, types=["tree.node_completed"])
                   if e.ref_id == tree["id"]]
    assert [e.payload["status"] for e in node_events] == ["failed"]
    done = await _done_events(tree["id"])
    assert len(done) == 1 and done[0].payload["status"] == "failed"
    assert done[0].payload["nodes"] == {"failed": 1}


async def test_retry_failed_child_reopens_tree_without_parent_surgery(explorer_output):
    """A failed child on an already-completed tree becomes pending again;
    its completed parent stays untouched and the normal drain finishes it."""
    await _set_limits()
    explorer_output.scripts = {
        "重试树": "CONCLUSION: 根结论。\nSCORE: 70\nCHILD: 待重试 | 再研究一次？",
        "待重试": "CONCLUSION: 重试成功。\nSCORE: 88\n",
    }
    tree = await research_tree.create_tree("重试树", max_depth=1)
    await research_tree.tick()                          # completed root + pending child
    nodes = await _nodes(tree["id"])
    root, child = nodes
    failed_at = bus.now_iso()
    await db.execute(
        "UPDATE research_tree_nodes SET status='failed', task_id='old-task', "
        "summary='stale', score=12, finished_at=? WHERE id = ?",
        (failed_at, child["id"]),
    )
    assert await research_tree._settle_tree(tree["id"])
    before = await research_tree.get_tree(tree["id"])
    assert before["status"] == "completed" and before["announced_at"]

    retried = await research_tree.retry_node(tree["id"], child["id"])
    retried_child = next(n for n in retried["nodes"] if n["id"] == child["id"])
    retried_root = next(n for n in retried["nodes"] if n["id"] == root["id"])
    assert retried["status"] == "exploring"
    assert retried["finished_at"] is None and retried["announced_at"] is None
    assert retried_root["status"] == "completed" and retried_root["summary"] == "根结论。"
    assert retried_child["status"] == "pending"
    assert retried_child["task_id"] is None and retried_child["summary"] is None
    assert retried_child["score"] is None and retried_child["finished_at"] is None

    retry_events = [
        e for e in await bus.replay(0, types=["tree.node_retried"])
        if e.ref_id == tree["id"]
    ]
    assert len(retry_events) == 1
    assert retry_events[0].payload == {
        "tree_id": tree["id"],
        "node_id": child["id"],
        "depth": 1,
        "topic": "待重试",
        "status": "pending",
        "previous_tree_status": "completed",
    }
    with pytest.raises(research_tree.TransitionConflict, match="only failed"):
        await research_tree.retry_node(tree["id"], child["id"])

    assert await _run_until_terminal(tree["id"]) == "completed"
    final_nodes = {n["topic"]: n for n in await _nodes(tree["id"])}
    assert final_nodes["待重试"]["score"] == 88
    assert explorer_output.calls.count("重试树") == 1    # completed parent was not rerun
    assert explorer_output.calls.count("待重试") == 1
    snapshots = await _done_events(tree["id"])
    assert [e.payload["status"] for e in snapshots] == ["completed", "completed"]


async def test_retry_node_is_single_claim_and_stopped_tree_stays_final():
    await _set_limits()
    tree = await research_tree.create_tree("并发重试")
    root = (await _nodes(tree["id"]))[0]
    await db.execute(
        "UPDATE research_tree_nodes SET status='failed', finished_at=? WHERE id = ?",
        (bus.now_iso(), root["id"]),
    )
    await db.execute(
        "UPDATE research_trees SET status='failed', finished_at=? WHERE id = ?",
        (bus.now_iso(), tree["id"]),
    )
    outcomes = await asyncio.gather(
        research_tree.retry_node(tree["id"], root["id"]),
        research_tree.retry_node(tree["id"], root["id"]),
        return_exceptions=True,
    )
    assert sum(isinstance(x, dict) for x in outcomes) == 1
    assert sum(isinstance(x, research_tree.TransitionConflict) for x in outcomes) == 1

    stopped = await research_tree.create_tree("停止不可重试")
    stopped_root = (await _nodes(stopped["id"]))[0]
    await db.execute(
        "UPDATE research_tree_nodes SET status='failed', finished_at=? WHERE id = ?",
        (bus.now_iso(), stopped_root["id"]),
    )
    await db.execute(
        "UPDATE research_trees SET status='stopped', finished_at=? WHERE id = ?",
        (bus.now_iso(), stopped["id"]),
    )
    with pytest.raises(research_tree.TransitionConflict, match="stopped"):
        await research_tree.retry_node(stopped["id"], stopped_root["id"])


# ==== echo immunity (real executor path, no fixture) ==========================

async def test_real_echo_full_chain_yields_zero_children():
    """The echoed prompt contains the format spec and all material, yet no
    line starts with a protocol token — the node completes through the REAL
    executor/echo path with zero children and the tree finishes."""
    await _set_limits()
    tree = await research_tree.create_tree("回显免疫主题", max_depth=2, max_nodes=12)
    assert await _run_until_terminal(tree["id"]) == "completed"

    nodes = await _nodes(tree["id"])
    assert len(nodes) == 1                             # no children out of a mirror
    root = nodes[0]
    assert root["status"] == "completed"
    assert root["summary"]                             # collapsed head of the echo output
    tasks = await db.query("SELECT * FROM tasks WHERE source = ?", (research_tree.SOURCE,))
    assert len(tasks) == 1
    assert tasks[0]["status"] == "completed"
    assert root["task_id"] == tasks[0]["id"]           # audit spine linked


# ==== crash recovery ===========================================================

async def test_recover_orphans_requeues_running_and_tick_redrives(explorer_output):
    await _set_limits()
    tree = await research_tree.create_tree("恢复主题")
    root = (await _nodes(tree["id"]))[0]
    # simulate a crash mid-run: the node was claimed by a dead process
    await db.execute(
        "UPDATE research_tree_nodes SET status='running', task_id='ghost' WHERE id = ?",
        (root["id"],),
    )
    await db.execute(
        "UPDATE research_trees SET status='exploring' WHERE id = ?", (tree["id"],)
    )
    # a stuck running node blocks nothing else, but IT can only move via recovery
    assert (await research_tree.tick())["claimed"] == 0

    assert await research_tree.recover_orphans() == 1
    row = await db.query_one(
        "SELECT status, task_id FROM research_tree_nodes WHERE id = ?", (root["id"],)
    )
    assert row["status"] == "pending" and row["task_id"] is None

    assert await _run_until_terminal(tree["id"]) == "completed"
    assert await research_tree.recover_orphans() == 0  # idempotent


async def test_recover_orphans_prunes_running_under_terminal_tree():
    """REVIEW-D4 H2 follow-through: a running node under an already-terminal
    tree must not requeue as pending (it would be unclaimable by design) —
    it is pruned; running nodes under live trees still requeue."""
    await _set_limits()
    dead = await research_tree.create_tree("死树")
    live = await research_tree.create_tree("活树")
    now = bus.now_iso()
    for t in (dead, live):
        await db.execute(
            "UPDATE research_tree_nodes SET status='running', task_id='ghost' WHERE tree_id = ?",
            (t["id"],),
        )
    await db.execute(
        "UPDATE research_trees SET status='stopped', finished_at=? WHERE id = ?",
        (now, dead["id"]),
    )
    await db.execute("UPDATE research_trees SET status='exploring' WHERE id = ?", (live["id"],))

    assert await research_tree.recover_orphans() == 2
    dead_root = (await _nodes(dead["id"]))[0]
    live_root = (await _nodes(live["id"]))[0]
    assert dead_root["status"] == "pruned" and dead_root["finished_at"]
    assert live_root["status"] == "pending" and live_root["task_id"] is None


async def test_tick_finalizes_stalled_exploring_tree():
    """Crash between the last node completing and the tree flipping: the
    tick's sweep flips AND announces it (single snapshot event)."""
    await _set_limits()
    tree = await research_tree.create_tree("卡住的树")
    root = (await _nodes(tree["id"]))[0]
    now = bus.now_iso()
    await db.execute(
        "UPDATE research_tree_nodes SET status='completed', summary='done', finished_at=? "
        "WHERE id = ?", (now, root["id"]),
    )
    await db.execute("UPDATE research_trees SET status='exploring' WHERE id = ?", (tree["id"],))

    out = await research_tree.tick()
    assert out["finalized"] == 1
    row = await db.query_one(
        "SELECT status, finished_at, announced_at FROM research_trees WHERE id = ?", (tree["id"],)
    )
    assert row["status"] == "completed" and row["finished_at"] and row["announced_at"]
    assert len(await _done_events(tree["id"])) == 1


async def test_sweep_prunes_stranded_pending_under_terminal_tree():
    """REVIEW-D4 H2 backstop: pending rows stranded under a terminal tree
    (pre-fix damage / manual edits) are pruned by the tick sweep; the
    announce arbiter never double-fires."""
    await _set_limits()
    tree = await research_tree.create_tree("遗留")
    await research_tree.stop_tree(tree["id"])           # drained -> announced immediately
    root = (await _nodes(tree["id"]))[0]
    await db.execute(
        "INSERT INTO research_tree_nodes (id, tree_id, parent_id, depth, topic, question, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("stray-pending", tree["id"], root["id"], 1, "搁浅节点", "", "pending", bus.now_iso()),
    )
    await research_tree.tick()
    stray = await db.query_one(
        "SELECT status, finished_at FROM research_tree_nodes WHERE id = 'stray-pending'"
    )
    assert stray["status"] == "pruned" and stray["finished_at"]
    assert len(await _done_events(tree["id"])) == 1     # still exactly one snapshot event


async def test_children_insert_is_idempotent_on_recovered_rerun(explorer_output):
    """A crash-requeued parent re-runs its explore call; the (tree, parent,
    topic) unique index keeps its children single (INSERT OR IGNORE arbiter).
    NB: idempotency is EXACT-topic (BINARY collation) — a nondeterministic
    hand may propose different topics on re-run; the H1 transaction makes
    that window crash-only."""
    await _set_limits()
    explorer_output.scripts = {
        "重放": "CONCLUSION: 重放结论。\nCHILD: 子甲 | 甲？\nCHILD: 子乙 | 乙？",
    }
    tree = await research_tree.create_tree("重放", max_depth=1, max_nodes=12)
    await research_tree.tick()                          # root completes, 2 children pending
    assert len(await _nodes(tree["id"])) == 3

    root = (await _nodes(tree["id"]))[0]
    # simulate the recovered re-run of the SAME parent
    await db.execute("UPDATE research_tree_nodes SET status='running' WHERE id = ?", (root["id"],))
    await research_tree._run_node(await db.query_one(
        "SELECT * FROM research_tree_nodes WHERE id = ?", (root["id"],)
    ))
    nodes = await _nodes(tree["id"])
    assert len(nodes) == 3                              # no duplicate children
    assert Counter(n["topic"] for n in nodes) == {"重放": 1, "子甲": 1, "子乙": 1}


# ==== stop ====================================================================

async def test_stop_tree_prunes_pending_and_running_finishes_naturally(explorer_output):
    await _set_limits(node_concurrency=2)
    explorer_output.scripts = {
        "停树": "CONCLUSION: 根。\nCHILD: 甲 | 甲问题？\nCHILD: 乙 | 乙问题？",
        "甲": "CONCLUSION: 甲结论。\nCHILD: 甲子 | 不该出现？",
        "乙": "CONCLUSION: 乙结论。\nCHILD: 乙子 | 不该出现？",
    }
    tree = await research_tree.create_tree("停树", max_depth=2, max_nodes=12)
    await research_tree.tick()                          # root done; 甲/乙 pending

    explorer_output.gate = asyncio.Event()
    explorer_output.entered = asyncio.Event()
    tick_task = asyncio.create_task(research_tree.tick())   # claims 甲+乙, blocks in-flight
    await explorer_output.entered.wait()

    stopped = await research_tree.stop_tree(tree["id"])
    assert stopped["status"] == "stopped"
    assert stopped["finished_at"]
    # REVIEW-D4 M2: not drained yet (甲/乙 running) -> NO completion event yet
    assert await _done_events(tree["id"]) == []

    explorer_output.gate.set()
    await tick_task

    nodes = await _nodes(tree["id"])
    by_topic = {n["topic"]: n for n in nodes}
    assert by_topic["甲"]["status"] == "completed"      # running nodes finish naturally
    assert by_topic["乙"]["status"] == "completed"
    assert by_topic["甲"]["summary"] == "甲结论。"
    assert "甲子" not in by_topic and "乙子" not in by_topic   # stop means stop: no new rows
    row = await db.query_one("SELECT status FROM research_trees WHERE id = ?", (tree["id"],))
    assert row["status"] == "stopped"                   # natural finishes never flip a stopped tree
    # the ONE snapshot event fired after the LAST finisher, from the final rows
    done = await _done_events(tree["id"])
    assert len(done) == 1 and done[0].payload["status"] == "stopped"
    assert done[0].payload["nodes"] == {"completed": 3}


async def test_stop_mid_flight_leaves_no_stranded_pending(explorer_output):
    """REVIEW-D4 H2 reproduction: stop lands while a parent is mid-explore.
    The parent's completion transaction reads the stopped tree and inserts
    NOTHING — no `stopped + pending` strays, and the snapshot event fires
    once, after the drain, from the final rows."""
    await _set_limits()
    explorer_output.scripts = {
        "停竞态": "CONCLUSION: 根结论。\nCHILD: 竞子 | 会被拦下？",
    }
    tree = await research_tree.create_tree("停竞态")
    explorer_output.gate = asyncio.Event()
    explorer_output.entered = asyncio.Event()
    tick_task = asyncio.create_task(research_tree.tick())
    await explorer_output.entered.wait()                # root claimed & in-flight

    stopped = await research_tree.stop_tree(tree["id"])
    assert stopped["status"] == "stopped"
    assert await _done_events(tree["id"]) == []         # not drained: no event yet

    explorer_output.gate.set()
    await tick_task

    nodes = await _nodes(tree["id"])
    assert [n["topic"] for n in nodes] == ["停竞态"]    # 竞子 never landed in ANY status
    assert nodes[0]["status"] == "completed"            # the natural finish kept its result
    assert nodes[0]["summary"] == "根结论。"
    pending = await db.query(
        "SELECT id FROM research_tree_nodes WHERE tree_id = ? AND status = 'pending'",
        (tree["id"],),
    )
    assert pending == []                                # the H2 stranded-pending shape is gone
    row = await db.query_one(
        "SELECT status, announced_at FROM research_trees WHERE id = ?", (tree["id"],)
    )
    assert row["status"] == "stopped" and row["announced_at"]
    done = await _done_events(tree["id"])
    assert len(done) == 1
    assert done[0].payload["status"] == "stopped"       # payload == durable terminal state
    assert done[0].payload["nodes"] == {"completed": 1}  # final snapshot, not the stop-time view
    # nothing left for later ticks
    assert (await research_tree.tick())["claimed"] == 0


async def test_stop_prunes_committed_pending_children_atomically(explorer_output):
    """The other H2 interleave: children committed BEFORE the stop are pruned
    in the same transaction as the tree flip; the tree is immediately
    drained, so the snapshot event fires right away."""
    await _set_limits()
    explorer_output.scripts = {
        "先提交": "CONCLUSION: 根。\nCHILD: 甲 | ？\nCHILD: 乙 | ？",
    }
    tree = await research_tree.create_tree("先提交", max_depth=1, max_nodes=12)
    await research_tree.tick()                          # root completed, 甲/乙 pending

    stopped = await research_tree.stop_tree(tree["id"])
    statuses = {n["topic"]: n["status"] for n in stopped["nodes"]}
    assert statuses == {"先提交": "completed", "甲": "pruned", "乙": "pruned"}
    done = await _done_events(tree["id"])
    assert len(done) == 1
    assert done[0].payload["nodes"] == {"completed": 1, "pruned": 2}


async def test_stop_tree_prunes_pending_root_and_is_idempotent():
    await _set_limits()
    tree = await research_tree.create_tree("从未跑过")
    stopped = await research_tree.stop_tree(tree["id"])
    assert stopped["status"] == "stopped"
    assert [n["status"] for n in stopped["nodes"]] == ["pruned"]
    assert len(await _done_events(tree["id"])) == 1     # drained at stop -> immediate snapshot
    again = await research_tree.stop_tree(tree["id"])   # idempotent: no second event
    assert again["status"] == "stopped"
    assert len(await _done_events(tree["id"])) == 1
    assert await research_tree.stop_tree("no-such-tree") is None
    assert (await research_tree.tick())["claimed"] == 0  # nothing claimable afterwards


# ==== concurrency ==============================================================

async def test_concurrent_ticks_claim_each_node_exactly_once(explorer_output):
    await _set_limits(node_concurrency=3)
    explorer_output.scripts = {
        "并发": ("CONCLUSION: 根。\n"
                 "CHILD: 并甲 | 甲？\nCHILD: 并乙 | 乙？\nCHILD: 并丙 | 丙？"),
    }
    explorer_output.delay = 0.01
    tree = await research_tree.create_tree("并发", max_depth=1, max_nodes=12)
    await research_tree.tick()                          # root done, 3 children pending

    await asyncio.gather(research_tree.tick(), research_tree.tick())
    assert Counter(explorer_output.calls) == {"并发": 1, "并甲": 1, "并乙": 1, "并丙": 1}
    assert explorer_output.violations == []

    node_events = [e for e in await bus.replay(0, types=["tree.node_completed"])
                   if e.ref_id == tree["id"]]
    assert Counter(e.payload["node_id"] for e in node_events).most_common(1)[0][1] == 1

    row = await db.query_one("SELECT status FROM research_trees WHERE id = ?", (tree["id"],))
    assert row["status"] == "completed"


async def test_node_concurrency_cap_bounds_inflight_explores(explorer_output):
    """node_concurrency=1: the claim UPDATE's running-count subquery is the
    arbiter, so even overlapping ticks never explore two nodes of one tree
    at the same time."""
    await _set_limits(node_concurrency=1)
    explorer_output.scripts = {
        "限流": ("CONCLUSION: 根。\n"
                 "CHILD: 限甲 | 甲？\nCHILD: 限乙 | 乙？\nCHILD: 限丙 | 丙？"),
    }
    explorer_output.delay = 0.01
    tree = await research_tree.create_tree("限流", max_depth=1, max_nodes=12)

    for _ in range(6):
        await asyncio.gather(research_tree.tick(), research_tree.tick())
        row = await db.query_one("SELECT status FROM research_trees WHERE id = ?", (tree["id"],))
        if row["status"] == "completed":
            break
    assert row["status"] == "completed"
    assert explorer_output.max_inflight == 1
    assert Counter(explorer_output.calls) == {"限流": 1, "限甲": 1, "限乙": 1, "限丙": 1}


# ==== defensive parsing ========================================================

def test_parse_explore_canonical_lines_and_tolerances():
    out = research_tree.parse_explore(
        "CONCLUSION: 第一条结论。\n"
        "  **SCORE： 87.5/100**\n"                      # bold + full-width colon
        "CHILD: 主题甲 | 问题甲？\n"
        "CHILD： 主题乙 ｜ 问题乙？\n"                   # full-width colon + pipe
        "  **CHILD: 主题丙 | 问题丙？**\n"              # bold-wrapped line, leading spaces
        "CONCLUSION: 后来的结论不覆盖。\n"
    )
    assert out["conclusion"] == "第一条结论。"           # first canonical line wins
    assert out["score"] == 87.5
    assert out["children"] == [
        {"topic": "主题甲", "question": "问题甲？"},
        {"topic": "主题乙", "question": "问题乙？"},
        {"topic": "主题丙", "question": "问题丙？"},
    ]


def test_parse_explore_skips_quoted_material_and_mimics():
    out = research_tree.parse_explore(
        "```\nCHILD: 栅栏里 | 引用材料不算\nCONCLUSION: 栅栏里的结论\nSCORE: 99\n```\n"
        "> CHILD: 引用块 | 也不算\n> SCORE: 98\n"
        "CONCLUSION: <一段结论>\n"                       # placeholder conclusion mimic (REVIEW-D4 N1)
        "CHILD: <子主题> | <子问题>\n"                   # format-spec placeholder mimic
        "CHILD: 真主题 | <子问题>\n"                     # half-mimic dropped too
        "前缀 CHILD: 不在行首 | 无效\n"
        "CHILD: | 只有问题没有主题\n"
        "CONCLUSION: 真正的结论。\n"
        "CHILD: 有效主题 | 有效问题？"
    )
    assert out["conclusion"] == "真正的结论。"           # the mimic never became the summary
    assert out["score"] is None                         # quoted scores are not answers
    assert out["children"] == [{"topic": "有效主题", "question": "有效问题？"}]


def test_parse_explore_dedup_cap_and_degraded_lines():
    out = research_tree.parse_explore(
        "CHILD: 重复 | 第一次\n"
        "CHILD: 重复 | 第二次被去重\n"
        "CHILD: 没有分隔符只有主题\n"                    # missing | degrades to topic-only
        "CHILD: 超员甲 | 甲\n"
        "CHILD: 超员乙 | 乙\n"
    )
    assert len(out["children"]) == research_tree.MAX_CHILDREN_PER_NODE
    assert out["children"][0] == {"topic": "重复", "question": "第一次"}
    assert out["children"][1] == {"topic": "没有分隔符只有主题", "question": ""}
    assert out["conclusion"] == ""
    assert out["score"] is None
    empty = {"conclusion": "", "score": None, "children": []}
    assert research_tree.parse_explore("") == empty
    assert research_tree.parse_explore("毫无协议痕迹的散文。") == empty


def test_parse_explore_score_is_defensive_and_first_valid_wins():
    out = research_tree.parse_explore(
        "SCORE: 不是数字\n"
        "SCORE: 101\n"
        "SCORE: 82.25％\n"
        "SCORE: 90\n"
    )
    assert out["score"] == 82.25
    for payload in ("nan", "inf", "-1", "100.01", "八十", "80 points", ""):
        assert research_tree.parse_explore(f"SCORE: {payload}")["score"] is None


def test_parse_explore_immune_to_reflected_prompt():
    """The whole assembled prompt mirrored back (the echo hand shape) must
    parse to nothing: format-spec tokens sit mid-line, material is
    neutralized."""
    node = {"topic": "量子 计算", "question": "何时商用？", "parent_id": None, "depth": 0}
    prompt = research_tree._build_prompt(node, research_tree.NO_ANCESTRY_LABEL)
    assert "SCORE: <0-100>" in prompt
    for reflected in (prompt, f"[echo] {prompt}"):
        assert research_tree.parse_explore(reflected) == {
            "conclusion": "", "score": None, "children": [],
        }


def test_quote_material_neutralizes_protocol_tokens():
    hostile = "第一行\nCHILD: 恶意 | 注入？\nCONCLUSION: 假结论\nSCORE: 100\nchild： 小写全角"
    flat = research_tree._quote_material(hostile)
    assert "\n" not in flat                             # inline after its label, never at line start
    assert research_tree.parse_explore(flat) == {
        "conclusion": "", "score": None, "children": [],
    }
    # belt and braces: the token pattern itself is broken
    assert all(token not in flat for token in ("CHILD:", "CONCLUSION:", "SCORE:", "child："))


async def test_hostile_ancestor_summary_cannot_inject_children(explorer_output):
    """An ancestor summary carrying protocol lines rides into the child's
    prompt neutralized: mirrored back, it still parses to zero children."""
    await _set_limits()
    explorer_output.scripts = {
        "注入树": "CONCLUSION: 根结论 CHILD: 伪装 | 假问题\nCHILD: 真子 | 真问题？",
    }
    tree = await research_tree.create_tree("注入树", max_depth=1, max_nodes=12)
    await research_tree.tick()                          # root completes; 真子 pending

    child = await db.query_one(
        "SELECT * FROM research_tree_nodes WHERE tree_id = ? AND topic = '真子'", (tree["id"],)
    )
    prompt = research_tree._build_prompt(child, await research_tree._ancestry_block(child))
    assert "注入树" in prompt                            # ancestry chain is present
    assert research_tree.parse_explore(f"[echo] {prompt}") == {
        "conclusion": "", "score": None, "children": [],
    }


async def test_child_mirroring_parent_topic_is_dropped(explorer_output):
    await _set_limits()
    explorer_output.scripts = {
        "镜像": "CONCLUSION: 结论。\nCHILD: 镜像 | 又是自己？\nCHILD: 别名 | 正常？",
    }
    tree = await research_tree.create_tree("镜像", max_depth=2, max_nodes=12)
    await research_tree.tick()
    topics = [n["topic"] for n in await _nodes(tree["id"])]
    assert Counter(topics) == {"镜像": 1, "别名": 1}     # the self-copy never lands


# ==== API ======================================================================

async def test_api_round_trip():
    from app.api import research_tree as api_research_tree

    app = FastAPI()
    app.include_router(api_research_tree.router)
    await _set_limits(daily_tree_cap=2)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/research/tree", json={"root_topic": "API 主题", "max_depth": 1})
        assert r.status_code == 200
        tree = r.json()
        assert tree["status"] == "pending" and tree["max_depth"] == 1
        assert len(tree["nodes"]) == 1 and tree["nodes"][0]["parent_id"] is None

        r = await client.get(f"/api/research/tree/{tree['id']}")
        assert r.status_code == 200
        assert r.json()["root_topic"] == "API 主题"

        r = await client.get("/api/research/trees")
        assert r.status_code == 200
        listed = r.json()
        assert [t["id"] for t in listed] == [tree["id"]]
        assert listed[0]["nodes_total"] == 1 and listed[0]["nodes_completed"] == 0

        r = await client.post(f"/api/research/tree/{tree['id']}/stop")
        assert r.status_code == 200
        assert r.json()["status"] == "stopped"
        assert (await client.get("/api/research/trees?status=stopped")).json()[0]["id"] == tree["id"]

        # validation surface: empty topic 400, bounds/typos 422, unknown ids 404
        assert (await client.post("/api/research/tree", json={"root_topic": "  "})).status_code == 400
        assert (await client.post(
            "/api/research/tree",
            json={"root_topic": "x", "max_depth": research_tree.MAX_DEPTH_LIMIT + 1},
        )).status_code == 422
        assert (await client.post(
            "/api/research/tree", json={"root_topic": "x", "bogus": 1},
        )).status_code == 422
        assert (await client.get("/api/research/tree/no-such")).status_code == 404
        assert (await client.post("/api/research/tree/no-such/stop")).status_code == 404

        # second create books the last daily slot; the third refuses with 200
        assert (await client.post(
            "/api/research/tree", json={"root_topic": "第二棵"},
        )).json()["status"] == "pending"
        refused = await client.post("/api/research/tree", json={"root_topic": "第三棵"})
        assert refused.status_code == 200
        assert refused.json()["refused"] == "daily_cap"


async def test_retry_api_status_and_tree_ownership_contract():
    from app.api import research_tree as api_research_tree

    app = FastAPI()
    app.include_router(api_research_tree.router)
    await _set_limits(daily_tree_cap=3)
    first = await research_tree.create_tree("API 重试一")
    second = await research_tree.create_tree("API 重试二")
    stopped = await research_tree.create_tree("API 停止")
    first_node = first["nodes"][0]
    second_node = second["nodes"][0]
    stopped_node = stopped["nodes"][0]
    now = bus.now_iso()
    await db.execute(
        "UPDATE research_tree_nodes SET status='failed', task_id='old', "
        "summary='stale', score=33, finished_at=? WHERE id = ?",
        (now, first_node["id"]),
    )
    await db.execute(
        "UPDATE research_trees SET status='failed', finished_at=?, announced_at=? WHERE id = ?",
        (now, now, first["id"]),
    )
    await db.execute(
        "UPDATE research_tree_nodes SET status='failed', finished_at=? WHERE id = ?",
        (now, stopped_node["id"]),
    )
    await db.execute(
        "UPDATE research_trees SET status='stopped', finished_at=? WHERE id = ?",
        (now, stopped["id"]),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        base = "/api/research/tree"
        assert (await client.post(f"{base}/no-such/node/{first_node['id']}/retry")).status_code == 404
        assert (await client.post(f"{base}/{first['id']}/node/no-such/retry")).status_code == 404
        # Existing node under the wrong tree is deliberately indistinguishable from missing.
        assert (
            await client.post(f"{base}/{first['id']}/node/{second_node['id']}/retry")
        ).status_code == 404
        for status in ("pending", "running", "completed", "pruned"):
            await db.execute(
                "UPDATE research_tree_nodes SET status = ? WHERE id = ?",
                (status, second_node["id"]),
            )
            assert (
                await client.post(f"{base}/{second['id']}/node/{second_node['id']}/retry")
            ).status_code == 409                          # every non-failed state conflicts
        assert (
            await client.post(f"{base}/{stopped['id']}/node/{stopped_node['id']}/retry")
        ).status_code == 409                              # stop means stop

        response = await client.post(
            f"{base}/{first['id']}/node/{first_node['id']}/retry"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "exploring"
        assert body["finished_at"] is None and body["announced_at"] is None
        node = body["nodes"][0]
        assert node["status"] == "pending"
        assert node["task_id"] is None and node["summary"] is None
        assert node["score"] is None and node["finished_at"] is None
        assert (
            await client.post(f"{base}/{first['id']}/node/{first_node['id']}/retry")
        ).status_code == 409                              # duplicate retry is idempotent conflict
