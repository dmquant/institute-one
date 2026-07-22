# PATCH-NOTES-C3 — Phase 5 前两项（forecast extraction + paper book）分区外事项

> **R-C3 复审修复（2026-07-20 第二轮）**：REVIEW-C3.md 判 FAIL 后，按主代理指派修复了四项 must-fix——H1 ticker 别名绕过守卫、H2 到期后取价、H3 未知收益记零、M3 并发双开仓（详见 §7）。其余审查发现（M1 跨上市歧义、M2 认领崩溃窗口、M4 否定/horizon 反例、M5 attribution、S1 结算 reconciliation、N1/N2）**未在本轮指派范围**，仍然开放，主代理裁决下一轮归属。0017 未上生产，直接原地改（加列/加索引/CHECK 扩枚举）。

C3 交付物（已落盘，独占分区内）：

- `migrations/0017_paper_book.sql` — `forecast_extractions`（source_ref 幂等认领表）+ `paper_positions`（含 open-security 部分唯一索引、close_reason 含 `unpriced`）+ `nav_history`（含 `n_unpriced` 完整性列）+ admin_state 种子 `paper_book`（0015 空洞、0016/0018 属并行卡，`db.migrate()` 按排序应用，有空洞不影响）
- `app/institute/forecast_extract.py`（新）— 正则抽取器（方向/标的/conviction/horizon）、ticker 停用词表 + CJK guard、`process_source()` 幂等、`register()` bus 钩子（research.completed + daily 的 workflow.completed）
- `app/institute/paper_book.py`（新）— `opener_tick()`（5min）、`mark_to_market()`（每日 MTM/NAV/基准/三路平仓 + 结算联动）、`close_position()`（manual）、`render_journal()`、`nav_series()`
- `app/api/paper_book.py`（新）— `GET /api/book/positions`、`GET /api/book/positions/{id}`、`POST /api/book/positions/{id}/close`、`GET /api/book/nav`
- `tests/test_forecast_extract.py`（8）+ `tests/test_paper_book.py`（10，R-C3 第二轮 +2 改 2）；第二轮修复后本卡 18 条全绿，全量见 §7 验证记录。（第一轮历史：中途快照 451/9 全绿；收尾时 `test_workflows` 计数断言与 `test_cli_doctor` plist 断言两处失败均属并行卡自身分区。）

L3 portfolios + Sunday proposer（ROADMAP 标 optional）本卡未做；Phase 5 第 4 条（SPA 页面 + MCP 读工具）不在本卡分区；ROADMAP paper-book 行的 "attribution flows into analyst memory" 留给后续卡（见 §6 遗留）。

## 1. 需要主代理执行的挂载（app/main.py，C3 无权修改）

### 1a. API router

`create_app()` 的 `from .api import (...)` 块加一行（按字母序放在 `meta` 之前）：

```python
        paper_book as api_paper_book,
```

`include_router` 元组里加 `api_paper_book.router`（建议放在 `api_forecasts.router` 之后）。挂载前后 `tests/test_paper_book.py` 都应通过（API 测试用裸 FastAPI app）；建议挂载时顺手加一条 `create_app()` 路由存在性断言（B6 §9 同样建议过）。

### 1b. 抽取器 bus 钩子

`lifespan()` 里 `vault_exporter.register()` 旁边加：

```python
    from .institute import forecast_extract
    forecast_extract.register()
```

事件名已读代码确认：research 是 `research.completed`（research.py `_run_item`）；「daily 完成」**没有**独立事件，是 `workflow.completed` + payload `workflow_id == "daily"`（workflows.py `_finish_run`，与 exporter `_COMPILED` 同一判别）。handler 全部 try/except 兜底永不 raise；文本取 session workspace 的 `06_深度报告.md` / `每日日报.md`，缺文件降级到 payload summary/results。briefing 刻意不抽（晨会简报是转述，不是观点表态；要扩，加一行 workflow_id 判别即可）。

## 2. 需要主代理挂的 scheduler 任务（app/institute/scheduler.py，B1 分区）

job 定义（放在 `_market_refresh_job` 附近）：

```python
@metered("paper-opener")
async def _paper_opener_job() -> None:
    from . import paper_book
    await paper_book.opener_tick()


@metered("paper-mtm")
async def _paper_mtm_job() -> None:
    from . import paper_book
    await paper_book.mark_to_market()
```

`start()` 里注册：

```python
    every(_paper_opener_job, "paper-opener", minutes=5)
    cron(_paper_mtm_job, "paper-mtm", "00:00")   # 00:00 SGT，ROADMAP 原文
```

（若想走 config：加 `paper_opener_minutes: int = 5` / `paper_mtm_time: str = "00:00"` 两个 Settings 字段再引用；本卡无 config.py 修改权，两种都兼容。）

**门控裁决：两个 job 都 gated=False（maintenance-exempt），理由**——scheduler.py 现行 gating 语义是「gated=True 仅用于**提交新模型调用**的 job」（janitor / hand-scorecard / market-refresh 均 ungated）。opener 开仓 = 纯 DB 读（PIT 冻结读）+ 一行 INSERT，MTM = PIT 读 + 平仓/NAV 行写 + `settle_forecast`（也是纯本地计算），**零模型 quota**。且这是账本类状态机：维护暂停期间停止 mark/结算会让 NAV 曲线出洞、到期仓位悬置，比继续记账危害更大。若落地时更新 scheduler.py 顶部那段 gating 注释，请把 paper-opener/paper-mtm 加进 ungated 清单。

抽取器不占 scheduler：事件驱动（§1b），无轮询 job。本卡全链路无模型调用（正则抽取），executor 不涉及。

## 3. 需要主代理加的 vault exporter handler（app/vault/exporter.py，分区外）

journal 的渲染函数在 paper_book.py 里（rows are truth，笔记是投影），exporter 只做投影落盘。精确代码：

```python
# ---- paper book journal ------------------------------------------------------

async def _on_paper_book(event: bus.Event) -> None:
    if not get_writer().enabled:
        return
    try:
        from ..institute import paper_book  # lazy: domain module

        wd = str((event.payload or {}).get("work_date") or event.ref_id or work_date())
        body = await paper_book.render_journal(wd)
        if not body.strip():
            return
        rel = f"Book/journal/{wd}.md"
        await get_writer().write_note(
            rel, {"type": "paper-book-journal", "work_date": wd}, body,
            artifact_kind="paper-book-journal", artifact_id=wd,
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("paper book journal export failed for %s", event.ref_id)
```

`register()` 里加：

```python
    bus.on("paper_book.marked", _on_paper_book)
```

说明：`paper_book.marked` 每次 MTM 触发一次（同日重跑幂等——render_journal 重渲整日，writer 的 skip-if-unchanged/hash-ledger 语义自然处理重写与人工编辑冲突）。ROADMAP 原文的 "append markers" 不需要：journal 是**按日整篇重投影**，比 append 更符合 writer 五规则（rebuildable）。开/平仓行按**精确 SGT 日窗**（UTC±8 显式换算，`paper_book._sgt_day_window`）匹配，不是日期前缀近似——因为 MTM 在 00:00 SGT 火，UTC 前缀匹配会系统性丢掉 MTM 自己产生的平仓行。

## 4. 抽取器语义（后续卡接口约定）

- **方向词**：多=看多/看涨/做多/买入/增持/超配/强于大市/跑赢 + bullish/overweight；空=看空/看跌/做空/卖出/减持/低配/弱于大市/跑输 + bearish/underweight；中性=中性/观望 + neutral。句内**最后一个**命中定方向（"由看多转看空"→short）；命中前 8 字符内的否定词（不/并不/非/未/没有/难以/不再/无法）作废该命中（"不看空"≠看多）。英文刻意不收裸 long/short（"long-term" 误报）。
- **标的三轨**：canonical id（600519.SH/0700.HK/NVDA.US，带边界断言）> 裸六位 A 股代码 > 名称/别名（0004 表）。
- **停用词表**（`TICKER_STOPWORDS`，可扩展）：指数/大盘/板块/行业/市场/A股/港股/沪深300/ETF/GDP… 等通用词拒绝按名称解析（精确匹配集合；"沪深 300" 类空格/扩展变体不在集合内，见 §7 遗留）；裸六位码若形如 YYYYMM（`(19|20)\d{2}(0[1-9]|1[0-2])`）拒绝（研报编号/年月远多于 ticker；代价：2000xx–2012xx 深 B 股失去裸码轨，canonical id 与**非数字**名称轨不受影响——测试有正反两例）。**纯数字别名/名称一律排除出名称轨**（R-C3 H1：importer 会给每个证券打 `kind='ticker'`、值=symbol 的别名，若放进名称轨会绕过 YYYYMM/小数尾/量词/边界全部守卫——数字串只许走裸码轨；`_name_rail_eligible()` 在加载层与 `_name_hits()` 双层把关，isdigit 同时覆盖全角数字）。
- **CJK guard**：裸码前后非数字断言（"1600519000" 不命中）、拒小数尾（600519.5）、拒量词后缀（元/万/亿/%/美元/港元）；CJK 名称 ≥2 字符子串匹配、ASCII 名称 ≥3 字符 + 词边界（与 B5 `_name_in_topic` 同口径）。
- **conviction**：谨慎/初步/cautious→0.35 **优先于** 强烈/坚定/strong→0.9（同句保守者胜）；默认 0.6。**horizon**：N个月/N季度/半年/年内/N周/N天内 + 英文变体，按位置取首个**通过单位合理性上限**的线索（"2026年"不是 horizon）；默认 30d。
- **产出**：走 `forecasts.create_forecast()`（B6 文件未动），settlement_rule 固定 `{"type":"absolute_move","threshold":0.05}`，made_at=抽取时刻（**不回填**报告日期——抽取时刻才是知识时刻）。每源封顶 5 条、同源同标的首句胜。
- **thesis 锚**：结构化 research 项用自己的 thesis_id；其余落到幂等单例 `auto-forecast-extract`（kind=thesis，status=**watch**——刻意停在 parked 态，不会被 `seed_from_theses` 扫走，因为无 practical.actionCode）。
- **幂等**：`forecast_extractions.source_ref` UNIQUE，INSERT ON CONFLICT DO NOTHING 即仲裁（重发事件/手工重放跳过）。空文本**不烧认领**（报告晚到可重试）。强制重抽某源 = DELETE 该行再触发。事件：`forecast.extracted`（仅 n>0 时）。

## 5. paper book 语义（后续卡接口约定，R-C3 修复后终稿）

- **开平仓状态机**：`open →(ret≤-stop)→ closed(stop)`、`→(ret≥target)→ closed(target)`、`→(forecast 到期)→ closed(horizon)`、`→(到期且无可用价)→ closed(unpriced)`、`→(API)→ closed(manual)`；stop/target 优先于 horizon；全部条件认领（`WHERE status='open'` + rowcount）。
- **并发不变量 = 数据库事实**（R-C3 M3）：一个 forecast 终生至多一仓（UNIQUE(forecast_id)）；同一标的至多一个 **open** 仓（0017 部分唯一索引 `idx_paper_positions_open_security`，closed 行离开索引、可再进）；**总 cap 由条件 INSERT 自带**（`INSERT … SELECT … WHERE COUNT(open) < cap`，B6/0012「INSERT 即仲裁」先例）。opener 的查询/句内去重只是省事的预过滤，输家计入 summary 的 `lost_race`，并发 tick 单赢家有 gather 回归测试。
- **entry 口径 = B6 entry 腿**：made_at 日历日当日或之前最后一根、as known at made_at（PIT `as_of=made_at`），复权 close×adj_factor，正有限白名单（复用 `forecasts._usable_price/_adj_close`）；无可用价跳过下轮再试（绝不前视——测试有 made 后修正 10→20 的探针）。
- **mark 口径 = B6 exit 腿 + 窗口钳制**（R-C3 H2）：取 `≤ min(work_date, expires_at 日历日)` 的最后一根、最新知识（`as_of=None`）。到期后再晚运行 MTM/手动平仓，都只能用窗口内的 bar——到期后的暴涨暴跌**改不了平仓价、翻不了 close_reason**，paper 平仓与 B6 settlement 永远同一取价终点（测试：到期后 20 元新 bar 不可见，平仓价仍是窗口内 10.2）。已知边界：停机多日后恢复仍只看窗口内**最后**一根，不逐日回扫首个 stop/target 触发点（审查提的更强形态，留后续卡）。
- **NAV 口径（终稿，R-C3 H3）**：`nav = 1.0 + Σ realized_pnl(closed 且 realized_pnl IS NOT NULL) + Σ unrealized(open 且可定价)`；**未知永不记 0**——不可定价仓位整体排除出 nav，计入 `nav_history.n_unpriced`（= open 不可定价 + closed 'unpriced' 总数，>0 即该行是**部分口径**声明）；到期仍不可定价 → `closed(unpriced)`，close_price=NULL、realized_pnl=**NULL**（SQL SUM 天然跳过 + WHERE 显式排除，永不进 realized 聚合；释放 cap 槽位但未知保持未知；forecast 走 B6 invalid 结算）。journal 对 unpriced 平仓显示"盈亏 未知（不计入 NAV）"、NAV 块附 ⚠ 不完整行。signed return =（mark/entry−1），short 取负，size 名义 1.0；gross_exposure=Σsize(open)；`nav_history` 按 SGT work_date 幂等 upsert。**benchmark_nav**：CSI300 最新已知 mark ÷ 首见可用 mark 时钉死的基线（admin_state `paper_book:benchmark_base`，删除该行可重钉）；无可用 mark → NULL，fails closed 不猜。
- **结算联动（不双结算）**：每次平仓后 `_maybe_settle`——仅当 forecast 仍 open **且已到期**才调 `forecasts.settle_forecast`（B6 拒绝到期前结算，所以 stop/target 提前平仓**不**触发结算，forecast 留待自然到期）；输掉 open→settled 认领（TransitionConflict）静默吞掉——B6 的同事务认领 + UNIQUE(forecast_id) 已保证 exactly-once，测试含预结算探针。
- **manual close**：按窗口钳制取价（同上）；无可用价**拒绝**（400），绝不编价——槽位等数据，或等到期走 unpriced。
- **持仓上限**：admin_state key `paper_book`（`{"max_positions": 20}`，0017 种子，0011 config-row 惯例）；缺行/坏值降级内建 20。
- **事件**：`paper_book.opened` / `paper_book.closed` / `paper_book.marked`（journal 投影钩在 marked 上，§3）。

## 6. 其他分区外事项与遗留

- `roadmap/backlog.json`：Phase 5 前两项状态推进由主代理做（注意 R-C3 M5：ROADMAP paper-book 行含 attribution 回流，**未做**——该行不能标全完成）。
- `pyproject.toml` / `.env` / config.py：无新依赖、无新 env 键。
- **遗留（后续卡）**：① attribution 回流 analyst memory（R-C3 M5 升级为 scope must-fix：daily 报告多作者聚合，现有 provenance 不足以可靠归因，需先定粒度与数据模型；`paper_book.closed` payload 已带 forecast_id/realized_pnl 可作起点）；② 到期 forecasts 的**批量**结算 job（B6 §3 声明属 M5-002+；R-C3 S1 的 close 后崩溃 reconciliation——扫「closed position + expired open forecast」幂等补结算——同属这张卡）；③ 基准 marks 无 fetcher（B5 只读不抓），生产上 benchmark_nav 大概率一直 NULL 直到基准数据卡落地；④ R-C3 其余开放项：M1 跨上市同名双开（中芯国际 A/H；canonical 命中应抑制名称轨）、M2 `forecast_extractions` 认领与逐条 create 不原子（崩溃后 duplicate 封死、DELETE 重抽会复制——需候选级唯一键或状态机）、M4 否定/horizon 反例（"不建议看多"、"2026年内"重叠子串）、N1 私有价格 helper 升公共契约、N2 API 钳制 vs 拒绝；⑤ 停用词是精确集合，"沪深 300" 类变体不拦（R-C3 §三）。
- **已知边界**：journal 的开/平仓行按精确 SGT 日窗匹配（§3）；停机多日恢复的 MTM 只看窗口内最后一根，不逐日回扫首个 stop/target 触发点（§5）。

## 7. R-C3 第二轮修复记录（四项 must-fix）

1. **H1 ticker 别名绕过守卫**：纯数字别名/名称（含 importer 的 `kind='ticker'`）一律排除出名称轨（`_name_rail_eligible`，加载层 + `_name_hits` 双层），数字串只许走裸码轨→YYYYMM/小数尾/量词/边界守卫全部生效。回归：fixture 全面改成生产形态（每个证券带 ticker 别名），审查者三个探针句（`根据200601的研究`/`成交额600519万元`/`指标600519.5`）+ 数字串边界 + 字母 ticker 别名仍可用，逐条断言。
2. **H2 到期后取价**：`_mark_window(wd, expires_at) = min(wd, expires_at[:10])` 钳住 MTM 与 manual close 的 exit 腿取价窗——与 B6 settlement 同终点，晚跑只增窗口内知识（修正），不增新 bar。回归：到期后 20 元暴涨 bar 对平仓不可见（reason 仍 horizon、价仍 10.2、settlement 同终点 0.02）、manual close 同钳制。
3. **H3 未知记零**：新 close_reason `unpriced`（close_price/realized_pnl 均 NULL）；NAV 聚合显式排除 NULL realized、不可定价 open 仓不再"entry-flat 记 0"而是整体排除；`nav_history.n_unpriced` 完整性列 >0 = 部分口径；journal 显示"盈亏 未知"与 ⚠ 不完整行。回归：先形成 +8% 浮盈再删标的——nav 变为按已定价子集的 1.0 且 n_unpriced=1（不再是假装平盘的 0 收益断言），到期 unpriced 平仓 NULL/NULL 永不进 realized 聚合。
4. **M3 并发双开仓**：0017 加部分唯一索引（open+security 非 NULL 唯一）+ 总 cap 写进条件 INSERT 的 WHERE（`_insert_position` 返回 opened/cap/lost_race）——数据库仲裁，预读只是过滤。回归：同标的两次插入单赢家、同 forecast 重插拒绝、cap 处条件 INSERT 拒绝、双 `opener_tick()` gather 总赢家恰一。

验证记录（2026-07-20 第二轮）：`compileall` PASS；`test_forecast_extract.py + test_paper_book.py` 18 passed；全量 `pytest tests -q` **509 passed / 9 skipped / 0 failed**（要求 ≥507 零失败，达标；上轮并行卡的 2 处失败已由其代理自行修复）。

## 8. 第四轮修复记录（D2：ROUND3-AUDIT-S3 的 C3 四项 NOT-FIXED）

**Schema 纪律**：0017 已应用到生产库，本轮一切 schema 改动走新迁移 `migrations/0019_paper_book_hardening.sql`（预分配号；0020-0022 属并行卡）——0017 未动。0019 内容：`forecast_extractions` 加 `status`（pending/complete，旧行默认 complete 不被 resume）与 `analyst_id`（来源工件作者，NULL=无归因）两列；新表 `forecast_extraction_items`（PRIMARY KEY(extraction_id, security_id) 的候选级幂等认领 + forecast_id 上的 partial UNIQUE 反查索引）。B1 纪律自检（无 BEGIN/COMMIT/ATTACH/VACUUM/PRAGMA、IF NOT EXISTS、空库两遍幂等）通过。

1. **M1 跨市场同名消歧**：`_find_securities` 名称轨按名称字符串（casefold）分组仲裁——同名映射多 security（A/H 双上市）时，若其一已被 canonical/裸码强证据命中则该证据钉死上市地、**不再补兄弟上市**；无强证据锚则整体拒绝（宁缺勿错）+ 计入 `stats['ambiguous_names']` + log，`process_source` 把拒绝记进 claim 行 detail（`ambiguous name refused: …`）。单义名称、显式写出两个 canonical id（两个都开）语义不变。回归：中芯国际 A/H 裸名拒绝、`（688981.SH）` 锚定抑制 H 股、跨 kind 同 alias 拒绝、端到端 detail 计数。
2. **M2 crash-consistent 抽取状态机**：claim 行生而 `pending`、全部候选决定后置 `complete`；每个候选在 `forecast_extraction_items` 做 INSERT-即仲裁的认领，`create_forecast()` 成功后立即回填 forecast_id。complete 的重放=duplicate；pending 的重放=**resume**——已建候选经 item 行跳过（id 计入 created），只补缺失部分。create 提交与回填之间的单语句窗口若中枪：item 留 NULL=in-doubt，resume **保守跳过**（绝不冒重复风险）并写入 detail；人工通道从「DELETE claim 整行重抽（会复制已成功部分）」收窄为「核对 forecasts → DELETE 单个 item 行 → 置回 pending → 重放」。ForecastError 拒绝的候选释放认领（拒绝≠存疑）。回归：候选边界崩溃注入→resume 恰好补齐 2 条不重复、事件只 emit 一次；create 后回填前崩溃→resume 不复制该候选且 detail 记 in doubt、其余照建。
3. **M4 否定与 horizon 保守解析**：方向否定三层——紧邻否定集扩 `别/勿/莫`；**建议式否定窗口扫描**（不建议/不推荐/不宜/不应/不要/不会/暂不/切勿/请勿/避免/放弃 + 词首 别/勿，CJK lookbehind 防 级别/类别 误杀）在 cue 前 8 字符窗口内任意位置生效；**问号后否定**（`看多？不`）句内由 `_QUESTION_NEG_RE` 作废、跨拆分片段由 extract_candidates 的保留分隔符拆分（`？`+下片段以否定开头→整句不抽取）。horizon：静态 cue（年内/季度内/半年）前一字符为数字（含中文数字）即为被 cap 拒绝 span 的子串→拒绝（`2026年内` 不再 365）；多候选**取最短**（`2026年内，未来2周`→14，最紧 deadline 治理）。审计三反例 + `暂不看多`/`别追高买入`/`十年内` 等全部进测试；`不看空反而看多`→long、`年内目标价上调`→365 等原有语义回归保持。
4. **M5 attribution 回流 analyst memory**：归因规则=来源 workflow run 的**最后一个非 ops 步骤**的 analyst（research/daily 的编译步骤都是 ops-editor——编辑组织成稿不产生观点；research 经 payload.run_id/queue 行、daily 经 source_ref 的 run_id 解析，查无→NULL fails closed）。`process_source` 新参数 `analyst_id` 写入 claim 行；`paper_book.closed` payload 经 items→extraction 反查带上 `analyst_id`；`memory.py`（B3 已收工，授权最小扩展）加第四材料来源 `_outcome_items`——按 `paper_book.closed` 事件 id 游标（`cursors` 新键 `outcome_event`，旧版本行缺键=从头消费）收集该分析师已平仓结果（标的/方向/平仓原因/盈亏%/原始 claim，MAX 10 条、单条 300 字符 cap），材料段「纸面账本结果（你此前观点的实盘化验证）」。回归：抽取→开仓→target 平仓→closed 事件带 analyst_id→compact 材料含 outcome 行→游标推进（二次 compact 无新材料）→他人 memory 不可见；手工 forecast 平仓 analyst_id=NULL 不回流。

新增测试 9 条（test_forecast_extract 7：M4 方向反例矩阵、M4 horizon 最短/重叠抑制、M4 端到端问号否定、M1 同名仲裁、M2 边界崩溃 resume、M2 in-doubt 不重复、M5 两 workflow 归因；test_paper_book 2：M5 回流全链、M5 无归因 fails closed），全部走真实迁移链与真实 `create_forecast`，无 mock。

验证记录（2026-07-20 第四轮 D2）：`compileall app tests` PASS；分区 `test_forecast_extract.py + test_paper_book.py` **27 passed**（18 旧 + 9 新）；`test_memory.py` 8 passed 无回归；全量 `pytest tests -q -rs` 最终 **757 passed / 10 skipped / 0 failed**（审计基线 ≥634 零失败达标；总数被并行卡推高，10 个 skip 均为环境型——真网络 smoke、bundle.json 缺失、D4 未落地的探针——与本卡无关。中途一次全量曾见并行 chain 分区 `POST /api/chain/reproject` 未登记路由分类表的 1 处失败，复跑时该并行卡已自行修复；`reproject` 在 C3 分区全部文件中零出现）。
