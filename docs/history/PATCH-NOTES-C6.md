# PATCH-NOTES-C6 — Phase 8 平台化（institute CLI + launchd 服务 + API contract）

分区交付：`app/cli.py`（`institute` 命令）、`scripts/`（launchd 模板与安装/卸载、start/stop）、
`app/api/contract.py`（`/api/contract` + `/api/artifacts`）、对应测试。
本文件由 C6b 补写，含 REVIEW-C6 修复说明。

## 1. institute CLI（操作员入口）

```bash
.venv/bin/pip install -e .        # 装上 console script（或直接 python -m app.cli）
institute start                   # 后台启动（scripts/start.sh，pidfile 模式）
institute stop                    # 停 pidfile 模式的服务（launchd 模式请用 launchctl）
institute status                  # 进程 + 端口 + /health 探测；健康时退出码 0
institute doctor                  # 离线健康报告；有 FAIL 时退出码 1
```

CLI 永远从仓库根运行（自动 chdir），保证读到与服务器相同的 `.env`。

### doctor 的硬保证与口径

- **严格只读**：所有数据库访问走 `file:...?mode=ro`；vault 漂移扫描直接以只读连接读
  `vault_index` 并复用 writer 的纯哈希/region 分类函数，绝不经过 `app.db.init()`
  （那是写连接 + WAL 切换 + 迁移器）。测试 `test_doctor_subprocess_never_writes`
  对 home + vault 全树做前后字节快照验证零写（SQLite 只读连接会重建 `-shm`/`-wal`
  附属文件，属正常，已排除在断言外；主库文件字节不变）。
- **不烧配额**：hand 检查只跑 `--version` 与 CLI 自带的登录状态命令
  （`claude auth status`、`codex login status`，均只读本地凭据缓存），绝不发 prompt。
  没有可靠状态命令的 CLI（gemini/agy/opencode）报 **auth unknown（WARN）**，不谎报 ok。
  default/research hand 的判定消费探测结果：已登出的 default hand 是 FAIL。
- **事件循环安全**：唯一的 async 桥（ollama health）经 `_run_async`，在已有事件循环内
  调用时自动改走工作线程，不再抛 `asyncio.run() cannot be called...`。
- **异常隔离**：单项检查崩溃只产生该项的 FAIL 行，其余检查与 summary 照常输出。
- **口径（有意的最小范围）**：
  - `cron`：报告每个 job 最后一次状态 + 24h 内失败数；**不**检测"太久没跑"（调度器
    停摆需看 `status`/launchd 日志）。
  - `orphans`：只统计 `tasks`(queued/running) 与 `research_queue`(running)——这两个域
    有 boot 时的孤儿恢复清扫；workflow/whiteboard 等域的 running 残留不计入。

## 2. launchd 常驻服务（KeepAlive，可选）

```bash
./scripts/install-service.sh              # 只渲染 ~/Library/LaunchAgents/com.institute-one.server.plist 并打印激活命令
./scripts/stop.sh                         # 若有 pidfile 模式的服务先停掉，避免端口冲突 crash-loop
./scripts/install-service.sh --activate   # 渲染 + bootstrap + enable（真正激活）
launchctl print gui/$(id -u)/com.institute-one.server    # 状态
launchctl kickstart -k gui/$(id -u)/com.institute-one.server  # 立即（重）启
tail -f ~/.institute-one/logs/launchd.err.log
./scripts/uninstall-service.sh            # bootout + 删 plist
```

要点：
- 端口取 `INSTITUTE_PORT`（默认 8100）；模板注入绝对 venv 路径与安全 PATH。
- launchd 模式**没有 pidfile**：`stop.sh` 会正确拒绝碰它，停服用 `uninstall-service.sh`
  或 `launchctl bootout`。
- 卸载失败安全（REVIEW-C6 M5）：job 已加载但 bootout 与 legacy unload 都失败时，
  **保留 plist 并非零退出**（删掉 plist 只会留下一个内存里还在跑、磁盘上无定义的 job）。
- `--activate` 时 enable 失败会明确报出并给出恢复命令，不再假报 "bootstrapped + enabled"。

## 3. /api/contract 与 /api/artifacts

- `GET /api/contract`：版本、状态枚举、caps（输出截断、note 8 KiB 上限等）、ref 语法。
  状态枚举从各状态机模块的 canonical 常量导入（`executor.ACTIVE/TERMINAL`、
  `workflows.RUN_STATUSES`、`research.QUEUE_STATUSES`、`whiteboard.BOARD_STATUSES`），
  live schema 的 CHECK 约束作为交叉核验（`schema_cross_check` 字段，漂移记 warning）。
- `GET /api/artifacts?ref=task:<id> | note:<path> | fact_card:<id>`。
  note ref 双层防护（REVIEW-C6 H2）：词法（拒 `..`/绝对路径，400）+ 物理
  （resolve 后 realpath 必须仍在 vault 真实根内，symlink 指出去一律 403）。

## 4. 后续给 institute CLI 加子命令的约定

1. `app/cli.py` 加 `cmd_<name>(settings) -> int`（退出码 0=好），在 `main()` 的
   subparser + handlers 两处登记；帮助文案写清楚"读什么、动什么"。
2. doctor 的新检查项：写成独立的 `check_<name>(settings) -> Check`，**只读**
   （数据库一律 `_read_only_conn`），在 `cmd_doctor` 里经 `_guarded(...)` 挂入；
   任何 async 依赖走 `_run_async`，禁止裸 `asyncio.run`。
3. 绝不 import `app.db`/writer 的带副作用入口；纯函数（哈希、region 解析）可复用。
4. 每个新检查配至少一个"损坏态"测试；若检查触文件系统，纳入零写快照测试的目录范围。

## 5. REVIEW-C6 修复摘要（C6b, 2026-07-20）

| 项 | 修复 |
|---|---|
| H1 doctor 写库 | vault 扫描改纯只读（去掉 `db.init()` 桥），新增零写快照测试 |
| H2 note symlink 越界 | resolve 后 `is_relative_to(真实根)` 二次校验，403；4 个 symlink 测试 |
| M1 auth 探测 | `AUTH_PROBES`（claude/codex 登录状态命令）；无命令报 auth unknown；default/research 消费探测结果 |
| M2 asyncio 桥 | `_run_async`（循环内自动走线程）；在事件循环内跑全量 doctor 的测试 |
| M3 坏 cooldown | 逐 entry 校验（类型/有限性），FAIL 不抛异常；`_guarded` 隔离每个检查 |
| M4 状态常量 | 四个状态机模块建 canonical 常量，contract 导入；测试独立解析 0001 SQL 交叉核验 |
| M5 卸载完整性 | 失败保留 plist + 非零退出；fake-launchctl 四场景矩阵测试 |
| L1 口径 | cron/orphans 最小口径在本文档与 docstring 明示 |
| L2 enable 假成功 | bootstrap 成功但 enable 失败时单独报错并给恢复命令 |
