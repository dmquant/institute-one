# PATCH-NOTES-C1 — Phase 3 事实核查 v2（claim extraction / reuse gate / verification）

C1 分区内落地的文件：`migrations/0015_fact_check.sql`（新）、`app/institute/factcheck.py`（新）、
`app/api/factcheck.py`（新）、`app/mcp.py`（+3 只读工具：`fact_cards_list` /
`fact_cards_get` / `claim_check`；写工具保持三个不变）、`tests/test_factcheck.py`（新，61 例）。

管线：bus 钩子（card_completed / research.completed）→ durable `fact_extract_queue`
→ 门控 tick 抽取 ≤3 条可核查论断/来源 → tier-1 复用闸（余弦近邻，VERIFIED 邻居 →
`reused`、DISPUTED 邻居 → `self_contradicted`，向量降级 = 全部视为 fresh）→ 核查
任务 → `verified_facts` 判定行与卡片终态同事务落地 → DISPUTED / self_contradicted
开信箱线程 + `factcheck.disputed` 事件。写作时检查
`POST /api/meta/claim_check_before_write` 向量近邻 + 关键词降级。

## R-C1 返工记录（REVIEW-C1 FAIL → 已修）

- **M1 每日 cap**：改为「尝试即记账」——`admin_state` 计数行
  `factcheck_attempts:<SGT date>`，`_reserve_attempt()` 的条件 UPDATE
  （`value < cap`，rowcount 仲裁）在**模型调用前**原子占一格，成功/失败都占、
  不退还（防失败重试风暴烧配额）；同时验证前先做持久条件认领
  `pending→verifying`（0015 加了 `verifying` 状态 + `verify_started_at` 列），
  跨进程封死双验证；崩溃遗留的 `verifying` 由 tick 的 stale sweep（60min）放回
  pending（额度不退）。0015 尚未上生产，schema 调整直接改原文件。
- **M2 verdict 对抗解析**：级联换成**规范行提取**——只认行首裸
  `VERDICT: <词>` 行（词必须独占该行，容忍 markdown 加粗/全角冒号/单个句末
  标点），blockquote（`>`）与 code fence 内是引用材料一律跳过；多行冲突按保守
  序折叠（UNVERIFIABLE > DISPUTED > VERIFIED）；无规范行 → None → 上层落
  UNVERIFIABLE（prompt 明确要求了格式，不给格式不算证据）。组 prompt 时
  `_quote_material()` 把 claim 折叠成单行并隔断 `VERDICT:` 模式（C4
  `_quote_detail` 先例），回显型 hand 不可能从材料里制造规范行。echo 不再是
  verdict oracle：测试改用生产形态输出 fixture，仅保留一个真 echo 用例锁定
  「回显 → 保守落 UNVERIFIABLE」。
- **随手修**（同分区低成本）：claim_check 候选只取存活可行动判定
  （VERIFIED/DISPUTED 且未过期），向量腿异常降级到已算出的关键词命中
  （P2-1）；extraction queue 的 running→done/failed 补 `AND status='running'`
  （P2-5）；抽取/核查 hand 走 `factcheck_extract_hand` / `factcheck_verify_hand`
  防御式读取（P2-2，字段见第 3 节）；`parse_claims` 裸 JSON 扫描改为「最后一个
  顶层块 wins」，模板 `[]` 不再吞掉后置真实答案（P2-3）；claim_check 文本
  20K 截断 + API 层 `k`/`text` 约束。
- **未修（立卡）**：P2-4 surfacing 失败无 durable retry —— mailbox/vault/事件
  投递失败目前只 log；需要 outbox/sweep 机制，建议开 roadmap 卡（rows 是
  truth，digest/callout 可由后续事件或 doctor 重投影兜底）。

以下 4 节需要主代理在 C1 分区之外落实（factcheck.py 对未落实项均防御式降级：
不挂 register 只是钩子不触发，不加 config 字段则用内置默认）；第 4 节含两个
超出现有分区的 ROADMAP 子项立卡。

## 1. app/main.py — 两行挂载（C1 无权修改）

lifespan 里（`vault_exporter.register()` 之后同风格）：

```python
    from .institute import factcheck as factcheck_mod
    factcheck_mod.register()
```

`create_app()` 的 import 元组加 `factcheck as api_factcheck`，include 元组加一项
`api_factcheck.router`（必须在 SPA fallback 之前，与现有 router 一致即可）：

```python
    from .api import (
        ...
        factcheck as api_factcheck,
        ...
    )
    for r in (
        ..., api_factcheck.router,
    ):
        app.include_router(r)
```

## 2. app/institute/scheduler.py — 30 分钟门控 job（C1 无权修改）

job 定义（与 `_research_tick_job` 同风格；gated=True——tick 会发起新模型调用，
必须尊重维护暂停）：

```python
@metered("factcheck-tick", gated=True)
async def _factcheck_tick_job() -> None:
    from . import factcheck
    await factcheck.tick()
```

`start()` 里挂 interval（与 research-tick 一行同风格）：

```python
    every(_factcheck_tick_job, "factcheck-tick", minutes=settings.factcheck_tick_minutes)
```

`tick()` 自身永不 raise，内部串行做：复活卡死 >60min 的 running 抽取行与
verifying 卡片 → 最多 2 个抽取任务 → 最多 3 个核查任务（且受当日尝试 cap
约束——成功失败都占额度）。

## 3. app/config.py — 新设置（C1 无权修改）

`Settings` 建议加四个字段（放在 research_tick_minutes 附近）：

```python
    # Phase 3 fact-check (factcheck.py reads all four defensively)
    factcheck_tick_minutes: int = 30    # 0/negative disables the job
    factcheck_daily_cap: int = 10       # verification ATTEMPTS per SGT work date
    factcheck_extract_hand: str = ""    # cheap hand for claim extraction ("" = default_hand)
    factcheck_verify_hand: str = ""     # websearch-capable hand for verification ("" = default_hand)
```

- `INSTITUTE_FACTCHECK_DAILY_CAP`：每 SGT 工作日的核查**尝试**上限（成功+失败
  都计入 `factcheck_attempts:<date>` admin_state 计数行；抽取不占）。当前代码
  `getattr(settings, "factcheck_daily_cap", 10)` 防御式读取，未加字段时即为 10。
- `INSTITUTE_FACTCHECK_TICK_MINUTES`：仅第 2 项 scheduler 行引用；若主代理决定
  硬编码 30 分钟（`minutes=30`），此字段可省。
- `INSTITUTE_FACTCHECK_EXTRACT_HAND` / `INSTITUTE_FACTCHECK_VERIFY_HAND`
  （REVIEW-C1 P2-2）：ROADMAP 要求抽取用 opencode/cheap 手、核查用可联网的
  claude/gemini。生产 `.env` 建议 `INSTITUTE_FACTCHECK_VERIFY_HAND=codex`
  （本机现状：codex 可联网），extract 留空走 default。空/缺字段回落
  `default_hand`（测试即 echo）。
- 复用闸阈值/TTL 不进 config：按 0011 惯例是 `factcheck_reuse_policy` admin_state
  JSON 行（0015 已 INSERT OR IGNORE 种子值，删行降级为内置默认）。

## 4. app/vault/exporter.py — Disputed Claims digest + source callout（C1 无权修改）

`register()` 加一行：

```python
    bus.on("factcheck.disputed", _on_factcheck_disputed)
```

### 4a. digest handler（路径按 ROADMAP：`Inbox/Disputed Claims.md`）

精确代码（rows are truth：每次事件从 fact_cards 全量重投影一页滚动 digest，
绝不 append；skip-if-unchanged 让重复事件零成本；handler 永不 raise）：

```python
# ---- fact-check disputed claims ---------------------------------------------

async def _on_factcheck_disputed(event: bus.Event) -> None:
    """factcheck.disputed → regenerate the rolling Disputed Claims digest
    (+ re-export the source dossier so its warning callout appears)."""
    if not get_writer().enabled:
        return
    try:
        rows = await db.query(
            "SELECT c.id, c.claim, c.category, c.status, c.analyst_id, "
            "       c.source_kind, c.source_ref, c.created_at, "
            "       vf.evidence, vf.source_urls "
            "FROM fact_cards c "
            "LEFT JOIN verified_facts vf ON vf.fact_card_id = c.id "
            "WHERE c.status IN ('disputed', 'self_contradicted') "
            "ORDER BY c.created_at DESC LIMIT 50"
        )
        if not rows:
            return
        parts = []
        for r in rows:
            try:
                urls = json.loads(r["source_urls"] or "[]")
            except ValueError:
                urls = []
            label = ("已驳斥（DISPUTED）" if r["status"] == "disputed"
                     else "重复已驳斥论断（self_contradicted）")
            lines = [
                f"## {r['claim']}",
                "",
                f"- 判定：{label}",
                f"- 类别：{r['category']}　分析师：{r['analyst_id'] or '（无）'}",
                f"- 来源：{r['source_kind']} `{r['source_ref']}`（{r['created_at']}）",
            ]
            if r["evidence"]:
                lines.append(f"- 证据：{r['evidence']}")
            if urls:
                lines.append("- 链接：" + " ".join(urls))
            parts.append("\n".join(lines))
        await get_writer().write_note(
            "Inbox/Disputed Claims.md", {"type": "factcheck"}, "\n\n".join(parts),
            artifact_kind="factcheck", artifact_id="factcheck-disputes",
        )
        # source dossier warning callout: re-project the source note so the
        # callout block (4b) lands — rows are truth, the exporter re-reads them
        p = event.payload or {}
        if p.get("source_kind") == "research_report" and p.get("source_ref"):
            try:
                await export_research_queue_item(str(p["source_ref"]))
            except (LookupError, ValueError):
                log.warning("dispute source %s not re-exportable", p.get("source_ref"))
    except Exception:
        log.exception("factcheck disputes export failed for %s", event.ref_id)
```

说明：self_contradicted 卡没有自己的判定行（LEFT JOIN 为 NULL，evidence/链接自然
省略），其 `related_fact_id` 指向被复读的 DISPUTED 事实——需要溯源时看
`GET /api/factcheck/cards/{id}`（API 会带出关联判定行）。frontmatter 不放日期等
动态字段，避免打破 skip-if-unchanged。`write_note` 自动补 `managed: institute`。

### 4b. source dossier `> [!warning]` callout（ROADMAP Phase 3 子项）

callout 必须在**导出时注入**（不是事后 append——笔记是整页 managed 投影，
append 会被下次重投影冲掉）。`_export_research` 组 parts 处加一段（放在
`## 核心结论` 之前）：

```python
    disputes = await db.query(
        "SELECT c.claim, c.status, vf.evidence FROM fact_cards c "
        "LEFT JOIN verified_facts vf ON vf.fact_card_id = c.id "
        "WHERE c.source_kind = 'research_report' AND c.source_ref = ? "
        "AND c.status IN ('disputed','self_contradicted') "
        "ORDER BY c.created_at", (queue_id,),
    )
    if disputes:
        lines = ["> [!warning] 事实核查：本报告中有论断存疑"]
        for d in disputes:
            lines.append(f"> - {d['claim']}" + (f"（{d['evidence']}）" if d["evidence"] else ""))
        parts.insert(0, "\n".join(lines))
```

注意 `_export_research` 目前只收 topic/run_id/session_id/summary，不知道
queue_id —— 需要把 research_queue.id 传进来（`export_research_queue_item` 已有
row["id"]；`_on_research` 的 event.ref_id 即 queue item id）。白板卡 dossier 同思路
（`_on_board` 按 board 下 card id 查 disputed 卡），可与 M7 系列卡一并排期。

### 4c. 其余两个 ROADMAP 子项 — 立卡（不在任何现有分区内）

- **Step-0 disputed-claims block**：数据端 `app/institute/digests.py` 的
  `analyst_disputes_md(analyst_id)` 是 Phase 3 占位（"Phase 3 replaces the
  body, not the route"），替换 body 为：查
  `fact_cards WHERE analyst_id=? AND status IN ('disputed','self_contradicted')
  ORDER BY created_at DESC LIMIT 20`，按 digests.py 现有 8KB clamp/占位风格渲染
  markdown。prompt 端（把 `curl /api/institute/analyst-disputes/<id>.md` 编进
  Step-0 上下文块）按 ROADMAP Phase 2 注记是**独立的 prompt-change 卡**
  （CLAUDE.md 规则 4：prompts are the product），勿随手改 prompts.py。
- **Obsidian plugin claim-check command**：`obsidian-plugin/` 新增命令
  「Check selection against fact store」——取编辑器选区 →
  `requestUrl POST /api/meta/claim_check_before_write {"text": selection}` →
  Notice/modal 列命中（verdict + claim + similarity）。CORS 走 `requestUrl`
  （CLAUDE.md 惯例）；改后 `npm run build` 并连 main.js 一起提交。建议开
  roadmap 卡挂在 Phase 3 收尾。

## 5. 测试基线

`tests/test_factcheck.py` 61 例全绿。R-C1 返工后的 oracle 结构：抽取仍真跑
echo/executor 全链路；核查判定改用生产形态输出 fixture（仅对
CLAIM_VERIFY_PROMPT 拦截 executor.submit，抽取照走真路径），另保留一个真 echo
用例锁定「回显 → 保守落 UNVERIFIABLE」。覆盖：防御式 claims 解析 + P2-3 对抗
（模板 `[]` 后置裸答案、引用 fence 后置答案 fence、生产形态裸数组）、规范行判定
提取 24 组参数化（blockquote/fence 引用、否定句、格式说明行、词不独占行、
冲突多行保守折叠、加粗/全角/大小写）、`_quote_material` 防注入、复用闸三态 +
DISPUTED 优先 + 过期忽略 + degrade-open、抽取幂等 + 出生即 terminal、钩子防重放 +
tick 全链路、**每日尝试 cap（并发不可突破、失败也烧额度且到顶即停、崩溃恢复
sweep、cap=0/额度耗尽零调用）**、verifying 认领并发丢弃（槽位不退）、claim_check
（关键词降级、向量模式、UNVERIFIABLE/过期排除、坏向量 BLOB 降级关键词）、
cards API 过滤/404、MCP 三工具往返。

全量套件（排除并行在途的 chain/multi_agent 分区）：580 passed / 9 skipped，
零失败。

注意：`/api/factcheck/*` 路由在第 1 项落地前不对外可用（测试自行 include router），
MCP 三只读工具已随 `app/mcp.py` 生效。

## 6. 遗留风险 / 边界

- `daily` source_kind 在 0015 里预留了，但无自动钩子（`_source_text` 对 daily 返回
  None）；有文本在手的调用方直接用 `extract_claims()`。
- 复用闸候选集不限同类别（类别是模型标签有抖动），阈值取新论断类别的阈值；
  TTL 在判定时刻冻结进 `expires_at`，改 policy 只影响新判定。
- echo/回显型手驱动的核查恒落 UNVERIFIABLE（保守路径：镜像输出里没有规范
  VERDICT 行）——每次仍消耗一格当日额度；生产环境务必配
  `factcheck_verify_hand` 为可联网真实手。
- 判定输出若不守三行格式（如 `VERDICT: DISPUTED，附加说明` 词不独占行）会落
  UNVERIFIABLE ——设计内保守行为，额度已花；观测到高频时再考虑放宽规范行。
- 向量模型切换后旧 claim 向量按 model 过滤隐藏（A8 语义）：旧事实停止 gating，
  属文档化的 degrade-open；死行清理非本次范围。
- surfacing（mailbox/digest/事件）失败无 durable retry（REVIEW-C1 P2-4）——
  建议 roadmap 卡：factcheck outbox/sweep 或复用 operator 的 action feed 兜底。
- 每日尝试计数行 `factcheck_attempts:<date>` 按日累积在 admin_state（一天一行，
  几字节）；如需清理可在 janitor 里顺手删 30 天前的行（非本次范围）。
