# PATCH-NOTES-M8-008 — operator 自改进环完整版（observations → proposals → effects → parameter history）

实现 `roadmap/backlog.json` 卡 **M8-008**（ROADMAP Phase 6 L 项的剩余部分）。在 0023 最小 recipe
复用环（PATCH-NOTES-E7 §2）之上补齐完整自改进链。四条铁律（shadow-only、human-pin、web-UI 人工门、
live confidence floor）全部不变；本轮所有新逻辑是**确定性 SQL，零模型调用**，不新增执行路径。

## 1. 迁移 `migrations/0026_operator_selfimprove.sql`（新建，旧迁移未动）

四张新表（`CREATE TABLE IF NOT EXISTS` + 索引，无 ALTER，无事务语句）：

- **`operator_observations`** —— operator 行为的每日度量快照：`action_recurrence`（每 kind 的
  opened/resolved/dismissed/open_now，即"哪类 action 反复出现"）、`recipe_performance`（每 recipe 的
  hits/hits_approved/adoption_rate，`recipe_id` 列直接挂到 recipe 行）、`router_quality`（建议量、
  recipe 命中份额、unparsed、人工裁决与过 floor 建议的表现）。`uq(kind, subject, work_date)` 唯一索引 =
  同日重跑原位刷新（UPSERT），快照按 SGT work date 落表。
- **`operator_proposals`** —— 系统生成的改进提案（`promote_recipe` / `retire_recipe` /
  `set_parameter`），带 `observation_id`/`recipe_id` 溯源、`params` JSON、`action_id`（每个提案同事务
  开一张 kanban inbox 卡，ref `proposal:<id>`）。`uq(dedupe_ref) WHERE status='proposed'` 部分唯一索引 =
  同一变更同时只有一张活提案（rejected 后可再提，与 actions live-ref 同语义）。
- **`operator_effects`** —— before/after 效果度量：变更落地瞬间冻结 baseline（前窗指标 JSON），
  `outcome` 为 NULL 直到 `measure_effects()` 用 `UPDATE … WHERE outcome IS NULL` 条件认领补齐后窗指标 +
  数值 deltas。`uq(proposal_id) WHERE proposal_id IS NOT NULL` = 每提案恰一行。
- **`parameter_history`** —— 白名单参数变更史（old/new 存 admin_state 原始 JSON 串，作为 byte-CAS 单位），
  `changed_by`（`api` / `proposal:<id>` / `rollback:<id>`）、`rollback_of`、`rolled_back_at`（条件认领目标）。

开放词表（observation/proposal kind、effect subject_kind）刻意不带 CHECK（0023 recipes-status 先例，
代码层枚举）；封闭集（proposal status、applied）保留 CHECK（0018 风格）。

## 2. 域代码 `app/institute/operator.py`

- **observations**：`observe_operator(window_days=7)` —— 只读扫描现有行，UPSERT 三类快照；
  scheduler-facing 风格（never raises，错误进返回值）。`list_observations()` 解码 metrics。
- **proposals**：`generate_proposals()` 从**最新 observations** 推导，三条确定性规则：
  1. `promote_recipe`：某 kind 近窗 opened ≥ 3，且同签名（`_title_keywords`）的模型建议被人工**一致**
     批准 ≥ 2 次、无覆盖该签名的 active recipe、候选 disposition 未曾提炼过 → 提议把最新那条提炼成 recipe。
     签名分歧（人工裁决不一致）fail closed 不提。
  2. `retire_recipe`：最新 recipe_performance 显示 hits ≥ 5 且采纳率 ≤ 20% → 提议退役。
  3. `set_parameter`：过 floor 建议获得人工裁决 ≥ 5 次且批准率 < 30% → 提议把 confidence floor **上调**
     一步（+0.05，cap 0.95）。只允许收紧方向——放松人工门的方向永不自动提。
  `_file_proposal()` 单事务写 proposal 行 + inbox 卡 + 回填 action_id；撞 dedupe 索引 = 收敛跳过。
- **人工决定**：`approve_proposal(id, note)` —— 参数先验证再花认领（畸形提案不烧 claim）；
  `UPDATE … WHERE status='proposed'` 条件认领单赢家；apply 走人类同款原语
  （promote 幂等 / retire 条件认领 / set_parameter byte-CAS）；置 `applied=1`、冻结 effect baseline
  （每提案唯一）、条件解决 inbox 卡。`reject_proposal` 同款认领，什么都不 apply，卡转 dismissed。
- **effect measurement**：`_open_effect()` 在 promote / retire / set_parameter / rollback 落地时冻结前窗
  baseline（recipe 主体：actions_opened / recipe_hits / hits_approved / model_suggestions；parameter 主体：
  router_quality 同形指标）。`measure_effects()` 对到期行（baseline_at + window_days 已过）算同形后窗指标 +
  逐 key 数值 delta（`model_suggestions` 负 delta = recipe 命中省下的模型调用），
  `WHERE outcome IS NULL` 条件认领保证恰测一次。窗口边界取闭区间（秒级时间戳下半开区间会丢同秒行；
  边界 ±1s 双计为已记录怪癖，daily-cap 同款口径）。
- **parameter history**：`set_parameter(key, value)` —— 白名单校验（目前仅
  `operator:confidence_floor`，prompt/schedule 领地不可作为"参数"触达，铁律 2 不被绕过）；admin_state
  写入是对读到值的 **byte-CAS**（feature-switches 同款），并发变更输 ValueError→409，历史行同事务追加。
  `rollback_parameter(history_id)` —— 单事务双条件认领：`rolled_back_at IS NULL`（一条变更只回滚一次）+
  admin_state 当前值仍等于该变更 new_value 的 byte-CAS（被更新变更叠过 → 409"先回滚更新的那条"）；
  回滚本身是新历史行（`changed_by='rollback:<id>'`，`rollback_of` 溯源），历史只增不改。
- `route_actions` 增加 `a.ref NOT LIKE 'proposal:%'`：提案 inbox 卡由提案端点裁决，路由器不烧配额去
  分类 operator 自己的文书（与 task.failed feed 的 ROUTER_SOURCE 自保护同族）。

## 3. API `app/api/operator.py`（12 个新端点）

- `GET /api/operator/observations`（kind/subject 过滤）、`POST /api/operator/observe`
- `GET /api/operator/proposals`（status 过滤）、`POST /api/operator/proposals/generate`
- `POST /api/operator/proposals/{id}/approve`（**唯一 apply 路径**，§8.2：web UI only，非 vault
  frontmatter、非 MCP——`app/mcp.py` 未加任何 operator 写工具）、`POST /api/operator/proposals/{id}/reject`
  （未知 404 / 已决定 409）
- `GET /api/operator/effects`（subject_kind 过滤）、`POST /api/operator/effects/measure`
- `GET /api/operator/parameters`、`PUT /api/operator/parameters/{key}`（未知 key 404 / 并发 409 /
  非法值 422）、`GET /api/operator/parameter-history`、`POST /api/operator/parameter-history/{id}/rollback`
  （未知 404 / 已回滚或被叠 409）

## 4. 测试 `tests/test_operator.py`（追加 10 个，原 48 个未动）

observe 快照与同日 UPSERT 不重复；promote 提案全链（观察→提案→inbox 卡→人工批准→recipe 激活→effect
baseline→同型 action 零模型命中→双批准/批后拒绝 409）；阈值 fail closed（低于复发线 / 人工分歧 /
已有覆盖 recipe 均不提）；retire 提案（低采纳→批准→退役+effect）；floor 提案（过 floor 建议被连续
dismiss→提 0.75→批准→live floor 变更+history 归因 proposal）；reject 条件认领与可再提；提案卡绝不进
路由（tasks 行数不变）；parameters API（GET/PUT/校验/白名单/history/effect）；rollback 条件认领
（被叠 409 且认领整体回滚、最新可回滚、恰一次、首设回滚=键删除回落默认 0.7）；effect 度量
（前窗冻结→未到期 pending→到期补 outcome+deltas→恰测一次）；直接 promote/retire 也冻结 baseline
（幂等重提/输掉的 retire 认领不加行）。

## 5. 验证

- `.venv/bin/python -m pytest tests/test_operator.py -q` → **58 passed**（48 旧 + 10 新，零失败）。
- `.venv/bin/python -m pytest tests/test_db_migrate.py -q` → 19 passed（0026 过迁移卫生 + 全文件重放）。
- `.venv/bin/python -m compileall app -q` → exit 0。
- 未跑全量套件（其他 agent 并行改动中，按分区纪律只跑定向）。

## 6. 验收对照（卡 acceptance）

| 验收 | 状态 |
|---|---|
| observations and proposals are durable rows linked to recipes | ✅ 两表均有 `recipe_id`（另有 observation_id/proposal_id 全链溯源） |
| proposals apply only through explicit web-UI human approval | ✅ 唯一 apply 路径是 approve 端点；generate 零 apply；MCP/vault 无入口；测试锁死 |
| parameter changes record before/after effect measurements | ✅ 每次 set/rollback 冻结 baseline，measure 补 outcome+deltas，`GET /effects` 可查 |

## 7. 边界与遗留

- 分区纪律：只动 `migrations/0026_operator_selfimprove.sql`（新）、`app/institute/operator.py`、
  `app/api/operator.py`、`tests/test_operator.py`、本文件。未动 backlog/CHANGELOG/main.py/scheduler.py/
  前端/MCP；未 git commit/push。
- **scheduler 未挂载**：observe/generate/measure 三个 sweep 目前只有 POST 端点（函数已按 metered 风格
  never-raise 写好）。挂定时任务要改 `scheduler.py`（本卡文件边界外）——留后续卡。
- **approve 的 apply 失败窗口**：认领（status→approved）先于 apply；apply 若失败（如参数并发 CAS 输掉），
  提案停留在 approved+applied=0，可经 `GET /proposals?status=approved` 查出，但无自动重试路径。
  概率极低（需与人工参数写并发），按可查询残留而非 saga 处理。
- **floor 自调只会收紧**（raise-only）：降 floor（放松人工门）刻意不自动提议，人工仍可直接
  `PUT /parameters/…`。
- effect 窗口闭区间的 ±1s 边界双计已在代码注释声明（秒级时间戳限制）。
- 前端无 proposals/parameters 面板（M8-008 expected_files 不含 frontend）；API 已齐，UI 留后续卡。
