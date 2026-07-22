# PATCH-NOTES-B2 — Hand weights + scorecard（ROADMAP Phase 2 第三项）分区外改动清单

**本稿已按 REVIEW-B2 三个阻断项修订**（M1 启发式误判引文 / M2 结算窗口 / M3 缓存预热），并采纳 gated 裁决。

B2 交付物（已落盘，独占分区内）：

- `migrations/0009_hand_weights.sql` — `hand_weights` / `hand_stats`（含 `duration_samples`）/ `hand_scorecard` 三表 + `tasks(status, finished_at)` 增量索引（REVIEW-B2 #6；0008 编号留给并行卡 B1；`db.migrate()` 按文件名排序，序号空洞无影响）
- `app/hands/registry.py` — 新增 `WEIGHT_SCOPES`、`set_weights_cache()` / `weights_loaded()` / `weights_snapshot()` / `weight_for()` / `pick_weighted_hand()`。**`resolve()`/`resolve_chain()` 语义零改动**（`tests/test_hand_weights.py::test_resolve_semantics_unchanged_by_weights` 有断言）；加权选择是 opt-in 新入口，本轮不接线任何调用方。缓存 None 哨兵=从未加载：pick 仍按中性 1.0 工作但记一次 WARNING（REVIEW-B2 M3，冷启动可见不静默）；inf/nan 权重钳 0、极大有限权重按最大值归一后采样（REVIEW-B2 #4）
- `app/institute/scorecard.py`（新）— `judge_output()` 纯函数启发式（DONE+artifacts 豁免最先、引文剥离、refusal 仅探测开头 300 字符、身份声明须与"无法"同句，REVIEW-B2 M1）+ `run_once(date=None)`（**无参=结算前一 SGT 日**，REVIEW-B2 M2；写 `hand_scorecard` + 小时窗口 `hand_stats`；重跑安全、内部兜底不 raise、不 import scheduler）
- `app/api/hands.py` — `GET/PUT /api/hands/weights`（PUT 单事务含 replace，REVIEW-B2 #5；weight 拒绝非有限值；GET 顺带回灌 registry 缓存=懒加载自愈）、`GET /api/hands/scorecard?date=`（真实日历校验；缺省=前一日）、`GET /api/hands/stats?hours=`（跨窗口均值按 `duration_samples` 加权，REVIEW-B2 #7；now 经 `bus.now_iso()` 取得）
- `tests/test_hand_weights.py`（新）— 23 个测试（含 REVIEW-B2 的引文反例回归、前一日结算、冷缓存警告、非有限权重）

## 需要主代理执行的挂载 1：scheduler.py 注册 scorecard 每日任务（B2 无权修改 scheduler.py）

**终稿（已按 R-B2 裁决改定：ungated + 00:05 SGT 结算前一日）**。job 定义与现有 job 同风格，放 `_janitor` 旁边（同属不烧配额的观测/维护类）：

```python
@metered("hand-scorecard")
async def _scorecard_job() -> None:
    from . import scorecard
    await scorecard.run_once()   # 无参 = 结算前一 SGT 日（日终集合已封闭）
```

`start()` 里注册：

```python
    cron(_scorecard_job, "hand-scorecard", "00:05")
```

裁决与理由（均已采纳，不再留选项）：

- **ungated**：A4 轮确立的门控判据是"是否提交新模型调用"，scorecard 不调 `executor.submit/spawn`，只读终态 tasks 写投影，与 janitor 同类；maintenance 期间在途任务照常 drain，质检恰恰应该继续记录。若 `tests/test_maintenance.py::test_job_gating_registry_matches_semantics` 维护了 job 清单，挂载时把 hand-scorecard 与 janitor 一样断言 `gated is False`（B2 无权动该文件）。
- **00:05 结算前一日**：23:45 扫"当天"会永久漏掉 23:45–24:00 完成的任务（也罩不住 23:00 daily-report 的长尾）。`run_once()` 无参语义已改为前一日并有测试锁定；补扫历史日期用 `run_once("YYYY-MM-DD")`。
- **时间硬编码 vs settings**：config.py 是 B2 禁区，示例硬编码 `"00:05"`。主代理若想走配置：`config.py` 加 `scorecard_time: str = "00:05"`，注册处改 `settings.scorecard_time`（空串=禁用，`cron()` 已处理）。

## 需要主代理执行的挂载 2：main.py 启动预热权重缓存（**必做**，R-B2 升级为正确性要求）

registry 是同步模块（resolve 跑在 executor 锁内），不做 async DB 读；权重缓存由 async 调用方推送。**不预热的后果**：进程重启后 DB 里已保存的权重不进入执行面（全按 1.0 采样），形成"控制面显示已配置、执行面静默忽略"的分裂，直到有人 PUT 或 GET weights。

B2 已在分区内加了两层缓解（见上：冷缓存首次使用记 WARNING；`GET /api/hands/weights` 顺带回灌缓存自愈），但**启用加权选择前预热仍是必做项**，不是可选优化。在 `app/main.py` lifespan 里 `init_registry(...)` 之后加：

```python
    from .api.hands import refresh_weights_cache
    await refresh_weights_cache()   # 把 hand_weights 表载入 registry 进程缓存
```

（顺序要求：`db.init()` → `init_registry()` → `refresh_weights_cache()` → 任何可能采样权重的后台工作。按现有 lifespan 顺序放 registry 初始化之后即满足。预热后冷缓存 WARNING 不会出现。建议主代理挂载后补一个"DB 有权重 + 重启 lifespan 后立即生效"的集成测试，B2 分区内已有 GET 自愈路径的等价测试。）

## 后续接线卡约定（本轮明确不做）

- 调用方（whiteboard 轮换 / research round-robin / daily / mailbox）改用加权选择时的形态：先自行过滤出可用池（如 `[h for h in pool if registry.is_available(h)]`），再 `registry.pick_weighted_hand(scope, live_pool)` 取一个，然后照旧把选中 hand 交给 `executor.submit(..., fallback_chain=...)`。**不要**改 resolve/resolve_chain 本身。
- `hand_weights.scope` 枚举（whiteboard/research/daily/mailbox/default）三处同步维护：migration CHECK、`registry.WEIGHT_SCOPES`、`app/api/hands.py` 的 `Literal`。加 scope 需要新 migration（ALTER 不了 CHECK，得建新表迁数据，或接受宽松校验只靠 API 层）。
- `hand_stats` 是 scorecard 重算式聚合（整窗覆盖写），别的写入方不要往里增量写，避免双写打架；跨窗口平均时长必须按 `duration_samples` 加权（stats API 已示范），不要用 `tasks_total`。
- `run_once` 重跑是"覆盖式 upsert"而非严格幂等：任务后来离开 completed/日期窗口时旧 verdict 行不删除，每次运行新发一条 `scorecard.completed` 事件（docstring 已声明；未来做回填/纠错卡时再定删除旧投影或版本化事件的策略）。
- ROADMAP 该卡的 "triage pane"（SPA 面板）不在本轮范围（Phase 6 triage page 一并做更顺）。

## 其他分区外事项

- `roadmap/backlog.json`：本卡对应状态迁移由主代理推进，B2 未动。
- `.env` / `config.py`：本轮零新增设置（见上面 scorecard_time 的可选项）。
- 生产 8100 未动过；migration 会在下次重启 `db.init()` 时自动应用（纯 CREATE TABLE，无数据回填，秒级）。
