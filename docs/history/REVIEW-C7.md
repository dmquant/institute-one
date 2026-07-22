# REVIEW-C7 — SPA 大整合第三轮独立审查

## 结论

**FAIL**

前端严格构建通过，`useSSE` 对当前后端实际生成的 SSE 帧解析正确，新页面的主要请求体/响应字段也大体对齐；但“`?since=` 重连后不会丢事件”的核心声明不成立。后端只回放一次、上限 500 条，并在回放完成后才订阅 live queue，存在确定性截断和 replay→subscribe 竞态；live queue 满时还会静默丢事件。由于 `useSSE` 是全站实时刷新通道，此项判为 High / must-fix。

另有三项 Medium：`askStream` 对合法 JSON 错帧及无 `done` EOF 缺少契约校验；paper-book / multi-agent 的“未启用”降级与当前 SPA catch-all 的真实 HTTP 响应不符；Dashboard 的维护状态读取和“恢复运行”失败不渲染错误。

## 审查边界

- C7 独占分区：`frontend/` 全部及 `PATCH-NOTES-C7.md`。
- 对 `app/api/events.py`、`app/bus.py`、`app/api/ask_stream.py`、`app/api/hands.py`、`app/api/paper_book.py`、`app/api/forecasts.py`、`app/api/meta.py`、`app/institute/paper_book.py`、`app/institute/forecasts.py`、`app/institute/scheduler.py` 仅做契约对照，不把其他并行代理的改动归到 C7。
- 通读了 `git diff -- frontend/src/` 的六个 tracked 文件，并逐文件读取六个 untracked 新源码文件。

## 问题分级

### C7-H1（High / must-fix）：SSE 的 replay→live 切换会静默丢事件

- 前端重连携带最后成功解析的 id：`frontend/src/useSSE.ts:39-41,50-54`。
- 后端每次连接只执行一次 `bus.replay(..., 500)`：`app/api/events.py:26-30`。
- live 订阅在回放列表全部 yield 完之后才开始；而 `bus.subscribe()` 是 async generator，真正把 queue 加入 `_subscribers` 要等首次 `anext(sub)`：`app/api/events.py:31-35`、`app/bus.py:81-86`。
- 因此有三条丢失路径：
  1. 断线期间累计超过 500 个匹配事件时，只回放最早 500 个，剩余已落库事件不会再进入新建的 live queue；
  2. replay 查询快照之后、live queue 注册之前发出的事件，既不在回放结果中，也没有订阅者接收；
  3. 已连接但消费受背压时，500 容量的 subscriber queue 满后 `emit()` 直接 `pass`，客户端没有 gap 信号或补偿轮询：`app/bus.py:66-70,81-88`。
- `lastIdRef` 只能去重，不能发现或补回中间缺口；后续较大的 id 到达后游标还会越过已丢事件。
- 结论：少于 500 条且没有撞上握手竞态时，`?since=` 可以恢复；不能据此声称 replay-then-live “safe” 或“不会丢事件”。
- 建议由事件端修复：先注册 bounded live queue，再取数据库 high-water/replay 并按 id 去重；超过一页持续分页追平；queue overflow 时显式终止连接迫使客户端从游标重放，或发送 gap 控制帧。前端可再加周期性 `/api/events?since=` reconciliation，但单靠当前一次 stream 握手无法闭合竞态。

### C7-M1（Medium）：`askStream` 会把不完整/错形流当作正常结束

- 半行缓冲和末尾无换行处理正确：`frontend/src/api.ts:715-742`。
- 非 JSON 行会被跳过，这是可用的容错；但解析后直接断言为 `AskStreamFrame`，没有验证对象、`type`、`text` 或 `task`：`frontend/src/api.ts:719-729`。例如 `null` 会抛 `TypeError`，`{"foo":"bar"}` 会作为空白伪帧传给页面。
- 流若正常 EOF 但没有 `done`，函数返回 `null`；Ask 页只检查“存在且 failed”的 done，随后清除 busy，不显示“响应不完整”：`frontend/src/api.ts:731-742`、`frontend/src/pages/Ask.tsx:74-75,90-97`。零输出时结果卡会直接消失。
- 正常“停止读取”路径是安全的：页面创建 controller、把 signal 传给 fetch、停止按钮和卸载都会 abort，finally 清空引用：`frontend/src/pages/Ask.tsx:31,40,55-56,94-100`。浏览器 abort 会取消 body；但 `askStream` 本身没有 `finally` 取消/释放 reader，回调异常路径仍不够稳健。
- 建议：对每帧做最小 runtime guard；未知类型可转成可见 status/error；EOF 无合法 done 应抛明确错误；reader 用 `try/finally` 收尾。

### C7-M2（Medium / integration）：两处“接口未启用”降级与当前应用路由不符

- Forecasts 只把 404/501 解释成“账本未启用”：`frontend/src/pages/Forecasts.tsx:152-170,247-252`。
- MultiAgent 同样只处理 404/501：`frontend/src/pages/MultiAgent.tsx:35-42`。
- 当前 `app/main.py:152-182` 没有挂载 `paper_book.router`，也没有 multi-agent router；SPA GET catch-all 位于 `app/main.py:184-195`。
- ASGI 只读探测的实际结果：
  - `GET /api/book/positions` → `200 text/html`（SPA `index.html`）；
  - `GET /api/book/nav` → `200 text/html`；
  - `POST /api/multi-agent/run` → `405 application/json`。
- 因而当前 paper-book 卡片会在 `res.json()` 抛 HTML 解析错误，MultiAgent 会显示通用 “Method Not Allowed”，都不会出现 PATCH-NOTES 声称的友好“未启用”状态。
- 正解优先是由集成层挂载 C3/C5 router，并让 SPA fallback 永不吞 `/api/*`；若仍要支持混合版本，前端需识别 content-type/405，而不应假设缺端点一定是 404。

### C7-M3（Medium）：Dashboard 维护状态和恢复失败是静默的

- 新增的 `admin = useLoad(getAdminState, ...)` 没有对应 `<ErrorNote error={admin.error}>`：`frontend/src/pages/Dashboard.tsx:31,39-56`。读取失败时页面默认当作“未暂停”，横幅消失。
- “恢复运行”直接 `.then(admin.reload)`，没有 catch、busy 状态或错误渲染：`frontend/src/pages/Dashboard.tsx:45-55`。POST 失败会成为未处理 rejection。
- Settings 页同一操作已有完整 try/catch/loading/error 模式：`frontend/src/pages/Settings.tsx:49-61,102-114`，Dashboard 应复用同等语义。

### C7-N1（Minor）：Forecast 列表永远拿不到代码尝试显示的 settlement verdict

- `Forecast.settlement` 已正确注释为仅详情接口返回：`frontend/src/api.ts:376-392`。
- `list_forecasts()` 只对 forecasts 行执行 `_forecast_out`，不会附加 settlement：`app/institute/forecasts.py:257-276`。
- 页面丢弃 `settleForecast()` 返回的详情对象并立即 reload 列表：`frontend/src/pages/Forecasts.tsx:39-44`；因此 `f.settlement?.verdict` 分支在该页面不可达：`frontend/src/pages/Forecasts.tsx:109-127`。
- 影响是预测可显示 `settled/invalid` 状态，但看不到命中/落空/部分的核心结果。应让列表契约带 settlement summary，或前端保留/补取详情。

### C7-N2（Minor）：事件分组筛选可进入无法复位的空视图

- 选中的 `group` 不会在 rolling window 中该组消失时重置：`frontend/src/events.tsx:87-100`。
- 过滤按钮仅在 `groups.length > 1` 时渲染：`frontend/src/events.tsx:101-119`。若所选组被 60 条窗口淘汰且只剩一个其他组，`shown=[]`，同时“全部”按钮也消失，只能等第二组事件出现或重挂页面。
- 建议在当前 group 不存在时自动清空，或在 `group !== ""` 时始终保留“全部”按钮。

### C7-N3（Minor）：部分新页面的读取/动作错误没有 UI

- Ask 未显示 `analysts.error` / `hands.error`：`frontend/src/pages/Ask.tsx:20-21,118-141`。
- MultiAgent 未显示 `analysts.error`：`frontend/src/pages/MultiAgent.tsx:16,58-73`。
- Hands 的 cooldown clear 使用无 catch 的 promise chain：`frontend/src/pages/Hands.tsx:55-59`。
- 主请求、weights、stats、scorecard、cron、forecast、Settings toggle 的错误态均有渲染；问题不是全局性，但与“防御式消费”口径不完全一致。

### C7-N4（Nit）：验收搜索并非空

`KNOWN_EVENT_TYPES` 常量已经删除，功能目标达成；但指定命令仍命中注释：

```text
frontend/src/events.tsx:5:// (the old hand-kept KNOWN_EVENT_TYPES list went blind on every new emit,
```

因此 `rg -n "KNOWN_EVENT_TYPES" frontend/src` 的字面验收结果不是空。

### C7-N5（Nit）：两个展示/文档小误差

- 未来到期时间使用只适合过去时间的 `ago()`；该 helper 把负差值 clamp 为 0，未到期预测会长期显示“0秒前”：`frontend/src/pages/Forecasts.tsx:129-131`、`frontend/src/ui.tsx:13-21`。
- `PATCH-NOTES-C7.md:7` 写“新页面（4 个路由）”，随后实际列出并注册了 5 个路由。

## useSSE 逐点核验

| 检查点 | 结论 | 证据 / 说明 |
|---|---|---|
| UTF-8 与分块半行 | PASS | `TextDecoder.decode(value, {stream:true})` + `buf` 保留最后半行，跨 chunk 的中文码点和行都能续接（`useSSE.ts:63-75`） |
| 多行 `data:` | PASS | 每行去掉字段名和至多一个前导空格，帧结束时以 `\n` 合并（`useSSE.ts:78-92`） |
| CRLF | PASS | 先按 LF 切行，再剥末尾 CR（`useSSE.ts:72-75`） |
| 注释/heartbeat | PASS | `:` 开头整行忽略；后端 heartbeat 正是 `: heartbeat\n\n`（`useSSE.ts:88`、`app/api/events.py:35-38`） |
| 空行边界 | PASS | 空行触发一次 JSON 解析并清空 `dataLines`（`useSSE.ts:76-86`） |
| EOF 中间帧 | PASS（规范语义） | 没有终止空行的 pending event 不派发；随后进入重连（`useSSE.ts:96-101`） |
| 完整通用 SSE 规范 | PARTIAL / 当前服务兼容 | 规范也允许单独 CR 作为行终止符及无冒号的 `data` 字段；当前解析器不支持这两种形式。但本后端固定输出 LF、`data:`，故不影响当前互通 |
| `event:` / `id:` | PASS（当前契约） | 有意忽略，因为后端 `data` JSON 自带 id/type；未知 type 仍进入 `EventFeed` 并原样显示 payload（`useSSE.ts:94`、`events.tsx:121-137`） |
| 重连策略 | PASS-WITH-RISK | 固定 3000ms、无限重试，无指数退避/抖动（`useSSE.ts:97-102`）；旧 EventSource 实现也在 onerror 主动 close 后固定 3s 重连，因此不是新增回归 |
| 卸载关闭 | PASS | cleanup 清 timer 并 abort 当前 fetch（`useSSE.ts:105-110`） |
| 重复 id 内存 | PASS | 没有无限 Set；只保留一个单调 `lastIdRef`，O(1) 内存（`useSSE.ts:29,39-41`） |
| `types` 前缀 | PASS | replay 使用 `LIKE '<prefix>%'`，live 使用 `startswith(prefix)`，当前硬编码字面前缀一致（`app/bus.py:91-99`、`app/api/events.py:39-42`） |
| 断线补偿 | FAIL | 见 C7-H1：500 上限、握手竞态和 queue overflow 使游标不能保证无损 |

## askStream 逐点核验

| 检查点 | 结论 |
|---|---|
| NDJSON 半行缓冲 | PASS：`buf` 只消费完整 `\n` 行，EOF 再 feed 尾行 |
| CRLF | PASS：`trim()` 会移除行尾 CR，不影响 JSON |
| 非 JSON 行 | PASS-WITH-NIT：跳过且不中断，但没有可观测告警 |
| 后端失败终帧 | PASS：`done.task.status/error/exit_code/output` 与 `app/api/ask_stream.py:122-130,181-188` 对齐 |
| 合法 JSON 错形帧 | FAIL：只有 TS cast，无 runtime guard，见 C7-M1 |
| 无 done EOF | FAIL：返回 null，页面不报“不完整”，见 C7-M1 |
| 停止读取 | PASS：按钮和卸载均 abort；按后端契约只断客户端，任务继续落库 |

## 页面/API 对齐表

| 页面 | API | 对齐结论 |
|---|---|---|
| Ask | `POST /api/ask/stream`、`POST /api/ask` | body 的 `prompt/analyst_id/hand/model/timeout_s` 与共享 `AskBody` 对齐；done/chunk 字段对齐。解析韧性见 C7-M1 |
| Forecasts | `GET /api/forecasts`、`POST /{id}/settle` | `claim/direction/security_id/settlement_rule/expires_at/status` 均与真实返回对齐；settlement verdict 的列表消费缺口见 C7-N1 |
| Forecasts / positions | `GET /api/book/positions` | `direction/entry_price/realized_pnl/close_reason/opened_at` 与 `paper_positions` 返回对齐；当前 router 未挂载，降级失效见 C7-M2 |
| Forecasts / NAV | `GET /api/book/nav` | 返回确为数组；`work_date/nav/benchmark_nav/gross_exposure/n_open/realized_pnl_cum` 对齐；当前 router 未挂载 |
| Hands / weights | `PUT /api/hands/weights` | `{entries, replace:false}` 正确；scope 集合与 Literal 完全一致；前端拒绝非有限数和负数，满足 `weight >= 0` |
| Hands / scorecard | `GET /api/hands/scorecard` | `date/counts/by_hand/entries` 对齐 |
| Hands / stats | `GET /api/hands/stats` | `hours/since/by_hand/windows` 及聚合字段对齐 |
| CronHealth | `GET /api/cron/health` | 正确消费 `{window_days, jobs: Record<name, job>}`；job 的 fires/ok/failed/skipped/rate/duration/last_error 全部对齐 |
| Settings | `POST /api/admin/maintenance` | `JSON.stringify({paused: boolean})` 发出 JSON 布尔字面量，满足后端 `StrictBool`；不是字符串 |
| Dashboard | `GET /api/admin/state` | `maintenance` JSON string 的解析与 scheduler 存储形状一致；错误态缺口见 C7-M3 |
| MultiAgent | `POST /api/multi-agent/run` | 当前无后端契约可核验；请求/响应仍是 C7 假设，实际未挂载时返回 405，见 C7-M2 |

## TypeScript 与 UI 质量

- `rg ': any' frontend/src`：0；`as any` / `<any>` / `any[]`：0。防御式开放字段使用 `unknown`，没有 any 泛滥。
- `tsconfig.json` 开启 `strict`、`noUnusedLocals`、`noUnusedParameters`；构建通过。
- `Forecasts`、`MultiAgent`、`CronHealth`、`Hands` 对并行接口大量使用可选链、默认空数组和字段类型守卫，整体防御性良好。
- 主要错误态缺口已列为 C7-M2、C7-M3、C7-N3；其余 fetch 主路径均有 `ErrorNote`。
- IDE diagnostics：C7 文件无错误。

## 硬规则与构建产物

- `git status` 当前确有大量 `app/` 修改和新增文件，但用户已明确这些属于其他并行代理；共享工作树无法仅凭 status 证明作者归属。C7 声明分区内未发现它主动需要修改后端才能编译的痕迹。
- C7 的源码变更只出现在 `frontend/src/`，另有 `PATCH-NOTES-C7.md`；本审查除新建本报告外未修改源码。
- `frontend/dist/` 被 `.gitignore:6` 整目录忽略，不能用 git diff 审计产物是否提交；本轮构建实际生成：
  - `dist/index.html`
  - `dist/assets/index-D89bey8d.css`
  - `dist/assets/index-BmIPOQQK.js`
- `git diff --check -- frontend/src/`：PASS，无空白错误。

## 验证记录

```text
cd frontend && npm run build
→ tsc && vite build
→ 55 modules transformed
→ exit 0

rg -n "KNOWN_EVENT_TYPES" frontend/src
→ frontend/src/events.tsx:5（仅注释；不为空）

rg ': any' frontend/src
→ 0 matches
```

`npm` 仅提示环境中的旧 `devdir` 配置将在未来 major 版本停止支持，与 C7 代码无关。
