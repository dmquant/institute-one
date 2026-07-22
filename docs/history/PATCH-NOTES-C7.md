# PATCH-NOTES-C7 — React SPA 全面补课

分区：`frontend/` 全部（`src/` + `dist/` 构建产物）。未改任何后端 / 插件 / 测试文件。

## 交付清单

### 新页面（4 个路由 + nav 项）

| 路由 | 文件 | 内容 |
|---|---|---|
| `/ask` | `pages/Ask.tsx` | 流式即问（NDJSON）+ 同步 fallback 开关 |
| `/forecasts` | `pages/Forecasts.tsx` | 预测台账 + 纸面持仓 + NAV/基准双线 SVG 曲线 |
| `/multi-agent` | `pages/MultiAgent.tsx` | 多智能体对比（agents 多选 + mode + 并排结果卡） |
| `/hands` | `pages/Hands.tsx` | 执行手状态 + weights 网格编辑 + scorecard + stats 条形图 |
| `/cron` | `pages/CronHealth.tsx` | 定时任务健康表 |

既有页面增强：`Dashboard`（维护模式横幅 + 前缀分组事件流）、`Settings`（maintenance toggle + gated jobs 清单）。

### 事件清单根治（选定前缀分组方案）

- `useSSE.ts` 重写：不再用 `EventSource`（其命名事件模型强制维护 `KNOWN_EVENT_TYPES` 清单，第二轮新增的 ≥9 类事件全盲，ROUND2-AUDIT-F2）。改为 `fetch` + `ReadableStream` 手工解析 SSE 帧，只消费 `data:` 行——后端每帧 data 都是完整 BusEvent JSON（含 type），因此**任何现在与未来的事件类型都自动可见**，无清单可漏。重连仍带 `?since=` 游标 + 重复 id 丢弃。
- `events.tsx` 新增 `EventFeed` 组件：按 type 首段前缀分组（`task.*`→任务、`forecast.*`→预测……），组内计数 chips 可过滤；已知类型有中文美化标签，未知类型原样显示 + payload 以 `<details>` 展开原始 JSON，不再静默丢弃。

### 流式 ask 实现要点（api.ts `askStream` + Ask 页）

- `fetch` POST `/api/ask/stream` + `ReadableStream` 逐行切分 NDJSON；`stdout` 增量渲染（连续 stdout 合并进最后一个 DOM 块避免元素爆炸）、`stderr` 红色、`status` 帧灰显斜体；`done` 帧后显示 StatusBadge + exit code + 任务链接（`/tasks?id=`）。
- "停止读取"按钮只 abort 客户端读取——按后端语义（ask_stream.py 模块 docstring）任务继续跑完并落库，UI 有提示。
- 复选框关闭流式则走旧的同步 `POST /api/ask`（fallback 保留）。

### 防御式消费（并行在建卡片）

- **paper book（C3）**：动手时发现 `app/api/paper_book.py` 与 `app/institute/paper_book.py` 已在仓库，故字段名按真实源码对齐（`paper_positions` 的 `direction/entry_price/realized_pnl/close_reason`；`nav_history` 的 `work_date/nav/benchmark_nav`）。仍保持防御：所有字段可选 + 渲染回退，`ApiError` 404/501 显示"账本未启用"。
- **multi-agent（C5）**：`POST /api/multi-agent/run` 尚不存在（rg 确认）。请求体按 ROADMAP.md 措辞（`fan_out(agents, prompt)` + `join(all|first_success|majority_vote|best_effort)`）假设为 `{agents: string[], prompt, mode}`；响应假设 `{id?, mode?, status?, results?: [{agent?, hand?, analyst_id?, status?, output?, error?, task_id?}]}`，全部可选链渲染。404/501 显示"接口未启用"。

## 假设清单（若 C5 契约不同需微调）

1. multi-agent 请求体字段名 `agents` / `prompt` / `mode`（依 ROADMAP Phase 7 卡片措辞）；若 C5 用 `analyst_ids` 或嵌套结构，需要改 `api.ts::runMultiAgent` 一处 + MultiAgent 页 results 映射。
2. multi-agent 结果条目以 `agent ?? analyst_id ?? 序号` 作为卡片标题键。
3. `GET /api/book/nav` 返回数组（`nav_series` 源码如此）；若 C3 后续包一层 envelope，`getBookNav` 需跟进。
4. Settings 页的 gated jobs 清单是**硬编码**的（briefing / daily-report / analyst-dailies / whiteboard-kickoff / whiteboard-tick / mailbox-sweep / research-tick / memory-compact，抄自 `scheduler.py` 的 `@metered(..., gated=True)`）。后端没有暴露 gated 清单的端点；若新增 gated job，此提示文案需手工同步（或后端将来在 `/api/cron/health` 里带上 gated 标记）。
5. 事件组中文标签（`events.tsx` 的 `GROUP_LABELS`/`EVENT_LABELS`）覆盖当前 rg 到的全部 emit + 在建卡片（factcheck/chain/book/paper_book/decision/checklist/card）；未覆盖的前缀原样显示英文——功能不受影响，只是没有中文名。

## 验证

- `cd frontend && npm run build`：**干净通过**（tsc 严格模式 + vite，55 modules）。
- `.venv/bin/python -m pytest tests -q`：**435 passed / 9 skipped**（基线 387/9；增量来自并行卡片在建的测试，本分区未改任何后端文件，passed 只增不减 ✓）。
- 未触碰生产 8100（含只读探测也未做，按硬规则从严）。服务器重启后自动拾取新 `frontend/dist`。

## 遗留风险

- 事件流从 `EventSource` 换成 `fetch` 流后，浏览器原生的自动重连/`Last-Event-ID` 不再适用——重连逻辑是手写的（3s 定时 + `since` 游标），行为与原实现一致，但极端网络抖动下的表现依赖该手写路径。
- Hands 页 weights 编辑为 upsert-only（replace=false）：清空某格不会删除已有权重行（后端 PUT 无逐行删除语义；整表替换风险大，未提供）。要清一行可把权重改为 0（等效不参与）。
- Ask 页流式输出对超长输出只保留 done 帧 8KB 截断提示（后端语义）；完整输出在任务详情页。
