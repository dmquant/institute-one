# PATCH-NOTES-E6 — Phase 0 残项（交互问答空闲手 + 单任务取消 + 可选 bearer auth）分区外改动清单

E6 交付物（已落盘，独占分区内）：

- `app/router/executor.py` — 仅两处：`hand_busy(name) -> bool`（读 per-hand mutex：懒创建、无锁=不忙、等锁者不算持有）；`cancel()` 重写为单任务取消协议（顺序敏感：**queued 先条件翻库再唤醒等锁的 submit**——只 cancel asyncio task 会让 CancelledError 打在锁 await 处、行永远卡 queued，这是旧实现对 queued-with-live-task 的实际 bug，本轮顺带修复；running 走 `_running` 的 `atask.cancel()`，复用 A1 停机路径已验证的进程组击杀 + cancelled 落库机制；幽灵 running 行直接翻库兜底）。两条直接翻库路径补发 `task.cancelled` 事件（与 `_finish` 对齐，SSE/前端一致）。
- `app/api/tasks.py` — `prepare_ask(body)` 共享预处理（persona wrap + memory 注入 + 404 + hand 决策 + **交互优先空闲手**：`body.hand`/`body.model` 任一显式给出即视为钉死不重排；否则起点手（analyst.hand 或 default_hand）忙时沿 `DEFAULT_FALLBACK_CHAINS` 取第一个「不忙且 `is_available`」的手，全忙照旧排队）；`POST /api/tasks/{id}/cancel` 端点改为协议化：200 `{"cancelled": true}` / 未知 404 / 终态 409（幂等——重复取消恒 409，检查-取消竞态窗口内到达终态也 409）。
- `app/api/ask_stream.py` — 删除 `_prepare` 镜像，改 `from .tasks import prepare_ask`（B8 提议的共享提取，本轮落地；模块 docstring 同步）。ask_stream 因此自动获得空闲手语义（测试证明：`prepare_ask is tasks.prepare_ask` + 流式集成断言 done frame 落在空闲手上）。**注**：该文件不在 E6 分区列表内，但「ask_stream 复用同一预处理」是任务的明文要求，改动仅为删镜像换 import（净 -25 行），无行为分叉面。
- `app/api/auth.py`（新）— `BearerAuthMiddleware`（**纯 ASGI**，非 BaseHTTPMiddleware：响应零包装，SSE `/api/events/stream` 与 NDJSON ask-stream 语义不受影响）+ `install_auth(app)`（挂载 + 非环回绑定无 token 时一次性 `log.warning`）+ `configured_token()`（`getattr(settings, "token", None)` 优先、`os.environ["INSTITUTE_TOKEN"]` 兜底，每请求读取）。`secrets.compare_digest` 恒时比较；401 带 `WWW-Authenticate: Bearer`。
- `scripts/start.sh` — 一行：`--host 127.0.0.1` → `--host "${INSTITUTE_HOST:-127.0.0.1}"`（ROADMAP 同项点名；C6 已收工，接管此一行）。
- `tests/test_ask_priority.py`（14）+ `tests/test_auth.py`（6）。

## 1. 主代理需要做的事（E6 无权修改的文件）

### 1.1 config.py 新字段（一行）

```python
    # Optional bearer auth (ROADMAP Phase 0). None/empty = auth disabled.
    token: str | None = None            # INSTITUTE_TOKEN
```

放 `host`/`port` 附近即可。**过渡期语义**：字段落地前 `configured_token()` 靠 `os.environ` 兜底——真实环境变量（launchd plist `EnvironmentVariables`、export）立即生效，但 **`.env` 文件里的 `INSTITUTE_TOKEN` 要等此字段落地才生效**（.env 只有 pydantic 解析，不进 os.environ）。字段落地后两条路径合一（pydantic env 优先于 .env，语义不变）。

### 1.2 main.py 挂载 middleware（两行）

`create_app()` 里、`app = FastAPI(...)` 之后任意位置（middleware 注册只需在启动前）：

```python
    from .api.auth import install_auth
    install_auth(app)
```

顺手项（可选）：`app/main.py` 模块 docstring 的 "Bind: 127.0.0.1, no auth (single operator, single machine)" 可补一句 "optional INSTITUTE_TOKEN bearer auth (app/api/auth.py)"。

测试不依赖挂载（test_auth 用自建探针 app + `install_auth`），先合并不炸；挂载后 token 未设=零变化，`test_api_routes` 等全量继续绿。

## 2. auth 行为矩阵（运维须知）

| 场景 | 行为 |
|---|---|
| `INSTITUTE_TOKEN` 未设（默认） | 零变化：middleware 直通，与今日单机无 auth 行为逐字节一致 |
| token 已设，`/api/*` 无/错 header | 401 `{"detail":"unauthorized"}` + `WWW-Authenticate: Bearer`（routing 之前拦截，不泄露路由面；所有方法一视同仁） |
| token 已设，`Authorization: Bearer <token>` | 放行 |
| token 已设，`/health` | 豁免（launchd/监控/`institute status` 探针不需要 token） |
| token 已设，非 `/api/*`（SPA HTML/assets/根路径） | 豁免——**姿态**：HTML 壳不是秘密，它发起的每个数据请求都打 `/api/*` 被强制；评审如认为壳也要保护，收紧 middleware 的 path 判断即可 |
| `INSTITUTE_HOST` ≠ 127.0.0.1/localhost/::1 且无 token | `install_auth` 时打一条 WARNING（配置态告警，非每请求） |

**连带影响（token 设置后的后续卡）**：SPA 的 fetch、Obsidian plugin 的 `requestUrl`、MCP 客户端（`POST /api/mcp`）都需要带 `Authorization` header——三者目前都没有 token 配置面。单机 127.0.0.1 + 不设 token = 现状，无任何影响；想对外绑定时先接受「SPA/plugin/MCP 需手动配 header 或等后续卡」。

## 3. cancel 协议变更（API 契约）

`POST /api/tasks/{id}/cancel` 从「恒 200 `{"cancelled": bool}`」改为：200 `{"cancelled": true}` / 404 未知 / 409 终态（带当前 status 的 detail）。前端 `cancelTask` 只在 200 路径读 body，409/404 走既有 catch → 错误条显示，无需改动（终态时按钮本就隐藏，409 仅竞态可见）。`workflows.cancel_run` 走域层 `executor.cancel()` 不经端点，兼容不变——且受益于 queued 子任务翻库 bug 修复（旧实现下被取消的 queued 子任务行会永远卡 queued 直到重启回收）。

## 4. 测试结果

- 定向：`test_ask_priority`（14）+ `test_auth`（6）+ 回归 `test_executor` + `test_tasks_retry` + `test_executor_shutdown` = **41 passed**。
- 全量：**825 passed / 4 skipped，零失败**（交接基线 764/10；增量含本卡 20 与并行卡测试；skip 数变动来自环境相关跳过）。
- `python -m compileall app` 通过。

## 5. 补遗（F5b LOW-F5B-2，2026-07-20 15:35）

token 模式的连带影响清单补一项：E3 接入的 Step-0 curl 与 digest 端点「永远 200」承诺在 token 设置后失效（研究 prompt 将静默失去 digest 上下文；Step-0 指引句自身是降级安全的，不会炸 prompt）。单机不设 token = 零影响；对外绑定并设 token 前，由 M8-020 系列后续卡处理 digest 端点的 token 豁免或 prompt 侧注入 header 的取舍。
