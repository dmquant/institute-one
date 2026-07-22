# ROUND2-AUDIT-S2 — 第二轮终审核销（S2）

审计快照：2026-07-20 07:12 CST 后的共享工作树。  
范围：REVIEW-B1～B8 的合入门槛、PATCH-NOTES-B2/B3/B4/B5/B6/B8 的跨分区挂载、全量验证、生产库只读升级预检和 8100 GET 冒烟。未重启服务、未对生产库写入、未发送 POST。

## 结论

- 按 REVIEW 中明确的阻断/`must-fix`/集成门槛拆分为 **25 项：23 FIXED、1 PARTIALLY、1 NOT-FIXED**。
- `PARTIALLY`：B1 的 `ADD COLUMN` 恢复守卫已校验类型、`NOT NULL`、`DEFAULT` 和成对引号，但仍不比较 `CHECK`/`REFERENCES`。
- `NOT-FIXED`：B8 的 SPA 与 Obsidian 插件尚未消费 `/api/ask/stream` 并增量渲染。
- B7 两个指定直修点均已落代码；但测试隔离没有全局完成，最终全量测试为 **2 failed, 385 passed, 9 skipped**，不是预期的 387 passed。
- 六份指定 PATCH-NOTES 的跨分区挂载均已应用。
- 轮级判定：**NOT RELEASE-READY**。生产库的迁移路径本身可安全走全新应用，但发布绿灯须先清掉 B8 前端门槛和红测。

## 1. must-fix / 阻断项核销

| ID | 审查要求 | 状态 | 当前代码证据与核销结论 |
|---|---|---|---|
| B1-H1 | migration 的 COMMIT 失败也必须回滚，且同进程可重试 | FIXED | `app/db.py:27-50` 在 migrate 失败时关闭局部连接且不污染 `_conn`；`app/db.py:248-278` 将 ledger INSERT 与 COMMIT 放在同一 `try`，任意 `BaseException` 尝试 ROLLBACK 后重抛。 |
| B1-M1 | analyst-daily sweep 不得因固定 3h 租约导致长任务双跑 | FIXED | `app/institute/analyst_daily.py:148-194` 使用独立 sweep key、条件 INSERT 与过期 CAS；`:197-234` 周期续租且释放按最新 token CAS；`:368-411` heartbeat 覆盖整个 sweep 并在 finally 停止、释放。未来时间戳也以 `0 <= age < lease` 判 live（`:179-187`）。 |
| B1-M2 | `ADD COLUMN` 恢复守卫必须 fail-closed，不得只按列名补 ledger | PARTIALLY | `app/db.py:112-127` 已收紧成对引号解析，`:157-183` 解析类型/NOT NULL/DEFAULT，`:186-232` 对不一致定义抛 `MigrationRecoveryError`。但 docstring 明示 `CHECK/REFERENCES are not compared`（`:194-198`）；内存探针用“同类型/默认但缺 CHECK”的 `vault_index.mode` 调 `_skip_add_column()` 得到 `skip_result=True`，仍会把约束漂移当恢复成功。 |
| B2-1 | scorecard 判定优先级与 AI/引文假阳性 | FIXED | `app/institute/scorecard.py:115-153` 固定 `DONE+artifacts` 最先豁免；只在剥离引文后的开头检查 refusal；`:73-99` 要求 identity 与 inability 同句/有限距离，不再把身份声明或引文单独判拒答。 |
| B2-2 | 每日结算必须覆盖完整日界 | FIXED | `app/institute/scorecard.py:158-176` 计算前一 SGT work date 与 `[start,end)` UTC 窗口；`:288-317` 无参 `run_once()` 默认前一日。调度见 `scheduler.py:166-169,282`，00:05 SGT 运行。 |
| B2-3 | 权重冷缓存不能静默退化 | FIXED | `app/hands/registry.py:54-58,174-209` 用 `None` 区分“从未加载”和空集合，冷缓存只告警一次；`app/api/hands.py:44-56` 提供回灌；`app/main.py:100-110` 严格在 `db.init()`、`init_registry()` 后启动预热。 |
| B3-H1 | compact 期间/同秒到达的材料不能因时间游标永久漏失 | FIXED | `app/institute/memory.py:117-121,131-201` 三来源均以单调整数 id、`id > cursor ORDER BY id ASC LIMIT` 收集；`:245-248` 明确模型运行期间的新行留给下一轮，不依赖墙钟。 |
| B3-H2 | region 冲突副本不得按日复用覆盖人工文件 | FIXED | `app/vault/writer.py:336-350` 逐个检查当天 sibling 是否存在，100 次后仍用随机后缀；`:412-426` 所有冲突都新建 sibling，不覆盖旧副本。 |
| B3-H3 | region 更新必须保留 ownership 约束 | FIXED | `app/vault/writer.py:193-200` 检查 `managed: institute`；`:372-400` 只有 marker 严格、ownership 存在且 region hash 与 ledger 一致才原位更新，否则走冲突副本。 |
| B3-H4 | region hash/替换必须保留区外字节并感知区内精确改动 | FIXED | `app/vault/writer.py:143-200` 用精确 span、原字符串切片和 `newline=""` 读取；`:358-400` hash 精确 region 文本并通过 `_replace_region()` 保留区外 CRLF/尾空白。 |
| B3-M1 | 多组/乱序/嵌套 marker 必须保守拒绝 | FIXED | `app/vault/writer.py:143-167` 只接受恰好一 begin、一 end 且顺序正确；任何 malformed 使 `_extract_region()` 返回 `None`，随后走 conflict。 |
| B3-M2 | doctor 不得被非 UTF-8/竞态删除中断 | FIXED | `app/vault/writer.py:198-205` 将 OSError/UnicodeDecodeError 降级为 `None`；`:431-455` 根据二次存在性计 missing/drifted，继续扫描。 |
| B3-M3 | 每来源 LIMIT 不能与高水位推进组合丢数据 | FIXED | `app/institute/memory.py:131-201` cursor 只推进到本批实际 fetch 的最大 id，而不是全表 MAX；`:221-234` 分来源保存该值，溢出行后续轮次继续读取。 |
| B3-M4 | whiteboard 注入补丁不得覆盖 BUILD-ON context | FIXED | `app/institute/whiteboard.py:694-698` 保留 `context_blocks=context_blocks or None` 并仅追加 `memory_block=`；其余三点见 `analyst_daily.py:324-328`、`mailbox.py:167-171`、`workflows.py:242-246`。 |
| B4-1 | skip/augment 独立窗口及三态边界 | FIXED | `app/institute/whiteboard.py:214-234` 每个 verdict 只检查自己的阈值与 cutoff，边界为 inclusive；`:295-326` lookback 取两窗口最大值、先 skip 后 augment，否则 pass，覆盖窗口正序/倒序。 |
| B4-2 | similarity cache 必须绑定模型与门配置 | FIXED | `app/institute/whiteboard.py:201-211` fingerprint 含模型、两阈值、两窗口；`:278-289,328-331,374-381` 读写 cache 与候选过滤均要求同 fingerprint，配置/模型变化立即惰性失效。 |
| B5-1 | NaN/Infinity 必须在解析层和 gate 层双重拒绝 | FIXED | `app/institute/market_fetchers.py:195-214` `_f()` 和 `_finite()` 两层 `math.isfinite` 且排除 bool；`:418-480` 对 bar/quote 全数值字段再次白名单校验。 |
| B5-2 | daily ladder 必须区分失败、全拒、至少一条通过 | FIXED | `app/institute/market_fetchers.py:544-584` 明确定义并实现三分法：异常/零解析继续、全拒继续、至少一条通过才采用该源。 |
| B6-1 | forecast entry 必须冻结在 made_at 的 PIT 知识时点 | FIXED | `app/institute/forecasts.py:352-415` entry/exit 两腿分读；security 与 benchmark entry 均以 `fc["made_at"]` 读取，exit 才使用结算 `as_of`。 |
| B6-2 | entry/exit/return 必须为正有限数，否则 invalid | FIXED | `app/institute/forecasts.py:285-346` `_usable_price()` 要求 finite 且 `>0`，entry、exit 和最终 return 都 fail-closed；模块契约见 `:40-47`。 |
| B6-3 | threshold 不得接受 bool/NaN/Inf，canonical JSON 禁止非标准数 | FIXED | `app/institute/forecasts.py:81-84` `allow_nan=False`；`:103-142` 显式拒 bool，并要求 `math.isfinite(threshold) and threshold > 0`；conviction/horizon 同样收紧（`:179-215`）。 |
| B6-4 | structured dedup/seed 并发必须由数据库仲裁 | FIXED | `migrations/0012_research_thesis.sql:50-60` 建 active structured partial UNIQUE；`app/institute/research.py:177-200` INSERT 撞约束后重读赢家，FOREIGN KEY 竞态单独 fail-closed。 |
| B8-MF1 | stream bridge 必须有界，慢消费/断开不得无限积压 | FIXED | `app/api/ask_stream.py:53-98` `_ChunkBridge(asyncio.Queue(maxsize=1000))` 满时丢最旧并计数，close 后停止入队并清空；`:171-190` done 前告知 dropped，finally 关闭 bridge。 |
| B8-MF2 | stream 与 sync ask 必须共享 analyst_id 请求语义 | FIXED | `app/api/ask_stream.py:3-7,100-119` 复用 `tasks.AskBody`，未知 analyst 在流开始前 404，persona 与 hand 优先级镜像同步 ask；`:133-152` 用处理后的 prompt/hand 提交。 |
| B8-MF3 | SPA + plugin 必须消费 NDJSON 并增量渲染 | NOT-FIXED | `frontend/src` 搜索 `ask/stream|digests|NDJSON` 无命中；插件仍只在 `obsidian-plugin/src/api.ts:501-506` POST `/api/ask` 并等待完整结果，未实现 `/api/ask/stream` 消费/增量 UI。后端完成不能代表 ROADMAP 整项完成。 |

### B7 两个直修点

| 项目 | 状态 | 证据 |
|---|---|---|
| checklist `ORDER BY` tie-breaker | FIXED（实现）；测试建议未补 | `app/institute/roadmap.py:499-503` 已为 `ORDER BY kind, sort_order, id`。未找到“两个相同 sort_order”的专门回归测试。 |
| process 测试 pinned 卡 | FIXED（指定位置）；全局隔离仍 PARTIAL | REVIEW 指定的 process 测试已在 `tests/test_roadmap.py:936-948` 创建 `M7-TMPP3`，不再借活 seed。但同文件 `:570-573` 与 `:769-808` 仍借已变为 done 的 `M3-001` 验证 blocked，正是最终两条失败。 |

## 2. PATCH-NOTES 应用状态

| PATCH | 状态 | 应用证据 / 走样 |
|---|---|---|
| B2 | APPLIED | `scheduler.py:166-169,282` 为 ungated `hand-scorecard` + 00:05；`main.py:100-110` 在 DB/registry 初始化后预热。代码形状与终稿一致。 |
| B3 | APPLIED | `config.py:84` 为 23:30；`scheduler.py:160-163,281` 为 gated `memory-compact`；四个注入点分别在 `analyst_daily.py:324-328`、`whiteboard.py:694-698`、`mailbox.py:167-171`、`workflows.py:242-246`。whiteboard 保留 BUILD-ON context，无旧补丁走样。 |
| B4 | APPLIED+ | `vectors.py:78-81` 提供 `model_name()`；whiteboard 在 fingerprint、写板向量、查板向量三处使用（`:210,256,303`）。补丁原文只点两处，第三处是正确的缓存绑定扩展。 |
| B5 | APPLIED | `config.py:59-65` 五字段齐全；`scheduler.py:172-175,287` 注册 ungated `market-refresh`。B5 自身的 workflow 惰性注入也在 `workflows.py:204-218`。 |
| B6 | APPLIED | `main.py:158,180` 已 import/include forecasts router。无 create_app 级 route smoke；`tests/test_forecasts.py:16-18` 的“尚未挂载”说明已过时。 |
| B8 | APPLIED | `main.py:155-156,175` 已 import/include ask_stream 与 digests。可选的公共 `prepare_ask()` 未抽取，当前仍由 parity 测试防漂移，不算漏补丁。 |

结论：指定的 **6/6 PATCH-NOTES 均已应用**；没有必做挂载漏项。

## 3. 全量验证

所有最终结果均在 07:12 集成窗口结束后重跑。

| 命令 | 结果 | 摘要 |
|---|---|---|
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m compileall -q app` | PASS | exit 0，无输出。 |
| `.venv/bin/python -m pytest tests -q -rs` | FAIL | **2 failed, 385 passed, 9 skipped in 15.15s**。失败均在 `tests/test_roadmap.py`，见下。 |
| `frontend: npm run build -- --outDir /tmp/institute-one-s2-frontend-dist-final --emptyOutDir` | PASS | TypeScript + Vite；49 modules；最终 JS 222.81 kB（gzip 69.03 kB）。 |
| `obsidian-plugin: npm run build` | PASS | TypeScript noEmit + esbuild；最终重复构建前后 `main.js` SHA-256 均为 `fba6d36af32629563f7d98f1425f96c178b7f185d9cb5d8c813523225f379f1c`。 |

### pytest 失败

1. `tests/test_roadmap.py:543-573::test_claim_card_is_a_conditional_claim`
   - 测试给 live seed `M3-001` 写 blocker 后期望错误包含 `blocked`；
   - `roadmap/backlog.json:236-240` 当前已把该卡标为 `done`；
   - 实际先命中 `"cannot claim a card in 'done'; only inbox/ready cards are claimable"`。
2. `tests/test_roadmap.py:758-808::test_export_roundtrip_is_idempotent_and_rebuilds`
   - 相同根因：重建后的 `M3-001` 仍是 done，blocked 断言被状态门先截获。

这不是随机失败；两次全量运行均稳定得到同样的 **2 failed / 385 passed / 9 skipped**。应像 B7 已修的 process 测试一样，在测试内创建固定为 inbox/ready 的临时卡。

### skip 理由核对

- 8 条数据集缺失：`tests/test_market_thesis_import.py:240,272,353,379,396,408,443,457`，均为 `market-thesis-data/bundle.json not present`。
- 1 条真实网络冒烟：`tests/test_market_fetchers.py:743`，需 `INSTITUTE_NET_TESTS=1`。
- **9 条 skip 全部符合预期**。

### 构建产物说明

审计期间首次插件构建把 `main.js` 从旧 hash `b40e9544...` 重生成为 `fba6d36a...`；集成结束后的重复构建 hash 稳定，未发现构建非确定性。`main.js` 是要求执行的 build 所生成，当前相对 HEAD 为 `+573/-76`；主代理应按仓库的生成物策略决定是否随源码纳入。

## 4. 生产升级预检（只读）

数据库：`~/.institute-one/institute.db`，全程用 `sqlite3 -readonly`。

### migration ledger

已应用：

1. `0001_init.sql`
2. `0002_roadmap.sql`
3. `0003_theses.sql`
4. `0004_securities.sql`
5. `0005_research_hardening.sql`
6. `0006_market_data.sql`
7. `0007_vectors.sql`

仓库共有 0001～0014 共 14 个 migration，因此精确待应用集合为：

- `0008_cron_metrics.sql`
- `0009_hand_weights.sql`
- `0010_analyst_memory.sql`
- `0011_whiteboard_similarity.sql`
- `0012_research_thesis.sql`
- `0013_forecasts.sql`
- `0014_shared_data.sql`

### 纪律与部分应用检查

- 对 0008～0014 搜索行首 SQL 关键字 `BEGIN|COMMIT|ATTACH|VACUUM|PRAGMA`：**0 命中**。
- 七个文件均为增量 `CREATE TABLE/INDEX`、`ALTER TABLE ADD COLUMN` 或 `INSERT OR IGNORE`，没有 DROP/重建/数据删除。
- 生产库检查新表/索引和新增列：`new_objects=0`、`new_columns=0`，没有 ledger 未记账的 0008～0014 部分应用痕迹。

### 升级结论

**从数据库迁移角度，下一次重启可安全按“全新应用 0008～0014”执行。** 当前生产库不需要触发 B1 的旧式部分迁移恢复分支，因此该分支遗漏 CHECK/REFERENCES 比对不会影响本次已观测的升级路径。

该结论不等于整轮发布绿灯：全量测试仍红，且 B8 前端门槛未完成。实际重启前仍建议先备份 DB、修红测试，并在重启后核对 ledger 到 0014、cron health 与关键新增路由。

## 5. API GET 冒烟

最终复测：

| URL | curl | HTTP |
|---|---:|---:|
| `http://127.0.0.1:8100/health` | exit 7 | 000 |
| `http://127.0.0.1:8100/api/hands` | exit 7 | 000 |
| `http://127.0.0.1:8100/api/cron/health` | exit 7 | 000 |

8100 没有监听者，三项均 `connection refused`。因此本次不能验证旧进程的 `/api/cron/health` 404，也不能验证其他两个 GET；没有为冒烟而重启服务，符合只读约束。

## 6. 遗留与第三轮待办

### 仍影响合入/发布

1. **B8-MF3**：SPA 与插件实现 `/api/ask/stream` NDJSON 消费、增量渲染、dropped 状态和 done 收口。
2. **B7 测试隔离**：把 `tests/test_roadmap.py:570-573,769-808` 的 live `M3-001` 换成测试自建 open 卡，恢复 387 passed / 9 skipped；同时补相同 `sort_order` 的 tie-break 回归。
3. **B1 恢复守卫**：历史恢复应按 migration/table/column 白名单，或从 `sqlite_master`/`table_xinfo` 验证完整声明；至少对 CHECK/REFERENCES 不可证明等价时 fail-closed，并固化本报告探针。

### REVIEW / PATCH-NOTES 中仍明确留后

- B1：补 cron metric INSERT 自身失败的降级测试、正式 cancellation 回归、比 `sqlite_master` 更深的数据/PRAGMA migration 等价测试；APScheduler 私有内部漂移时“取消但不等待”仍是已知降级。
- B2：权重选择器仍是 opt-in，whiteboard/research/daily/mailbox 尚未接线；boot lifespan 预热缺集成测试；triage pane 和严格回填/纠错投影语义留后。
- B3：即时 ask/session/MCP 三个 ad-hoc 路径的 memory 注入仍是可选项；并发 compact 虽只落一版，仍可能双烧模型调用。
- B4：`mcp.topic_pool_add` 仍 raw INSERT 绕过 `whiteboard.add_topic()`；严格 prompt byte-equality 测试可增强。
- B5：research workflow JSON 仍无 `${DATA_BUNDLE}`，能力尚未进入真实研究 prompt；并发手工 refresh + scheduler 仍可能写重复 PIT 版本；benchmark marks fetcher 留后。
- B6：thesis seeding 与到期 forecast 批量结算尚无 scheduler；forecast vault exporter 留后；create_app router smoke 未补，测试文件头已有过时说明。
- B8：公共 `prepare_ask()` 未抽取；未知 hand 仍返回流内 `rate_limited` 而非 422；缺真实 ASGI disconnect 测试；占位 digest clamp 与坏 JSON 降级仍可增强。

### 本轮新发现

- `app/institute/research.py:230` 仍用 `cap = int(cap)`，领域调用会静默接受 `True`、字符串整数和小数截断；Pydantic 的 `SeedBody(cap=True)` 与 `EnqueueBody(priority=True)` 也都转成 1。现有测试只锁定 0、负数和上限，第三轮应使用 strict integer 并在领域层拒绝 bool/分数/非有限值。
- `tests/test_forecasts.py:16-18` 仍声称 router 未挂载，与 `app/main.py` 当前状态相反，应随 create_app smoke 一并更新。
- 8100 当前未运行；升级后 API smoke 尚待实际重启验证。

### 第三轮建议顺序

1. P0：完成 B8 两端前端消费；修 B7 两条红测；补强 B1 migration 恢复守卫。
2. P1：为 research cap/priority 加 strict/domain 双层整数边界；把 `${DATA_BUNDLE}` 放入研究 prompt；接入至少一个真实 hand-weight 消费者。
3. P1：补 create_app/lifespan/路由 smoke、真实 ASGI disconnect、cron metric 写失败、per-security refresh 仲裁测试。
4. P2：抽公共 `prepare_ask()`，清理过时文档，补可选 ad-hoc memory 注入和 digest 防御性边界。
