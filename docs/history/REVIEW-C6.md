# REVIEW-C6 — Phase 8 平台化独立审查

## 结论：FAIL

`stop.sh` 的 broad `pkill` 已真正移除，pidfile 精确停止、plist 主模板、默认只渲染安装模式及三项指定验证均通过；但本分区仍有两项 High / must-fix：

1. `doctor` 声称严格只读，却经 vault 检查调用写模式的 `app.db.init()`，会执行 `journal_mode=WAL`、DDL 和迁移器，代码层没有“绝不写库”的保证；
2. note artifact 只做词法 `..` 检查，vault 内 symlink 可以把读取目标引到 vault 根外。

另有四项明确验收缺口：CLI hand 只跑 `--version`、没有验证认证状态；两个 `asyncio.run()` 桥在已有事件循环中直接崩溃；contract 状态枚举并非从代码常量导入；嵌套结构损坏但 JSON 合法的 `rate_limits.json` 会让整个 doctor 抛异常。绿测不能覆盖这些路径，因此不能判 PASS-WITH-NITS。

## 审查范围与边界

- 全文读取：
  - `scripts/com.institute-one.server.plist.template`
  - `scripts/install-service.sh`
  - `scripts/uninstall-service.sh`
  - `scripts/start.sh`
  - `scripts/stop.sh`
  - `app/cli.py`
  - `app/api/contract.py`
  - `pyproject.toml`
  - `tests/test_cli_doctor.py`
  - `tests/test_contract.py`
- 参照读取：`app/db.py`、`app/vault/writer.py`、`app/hands/base.py`、`app/hands/registry.py`、`app/router/executor.py`、三个状态域模块、`migrations/0001_init.sql`、`0015_fact_check.sql`、`tests/conftest.py`、`app/main.py`、`ROADMAP.md`、`implementation-notes.md`。
- 已执行指定 `git diff -- scripts/start.sh scripts/stop.sh pyproject.toml`。其余 C6 新文件均未跟踪，普通 `git diff` 不展示，已逐文件全文审阅。
- 未执行 `start.sh`、`stop.sh`、`install-service.sh --activate`、`uninstall-service.sh` 或任何 `launchctl` 命令；没有触碰真实服务、真实数据库或真实 LaunchAgents。pytest 中的安装脚本测试使用临时 `HOME`。

## 问题分级

### C6-H1（High / must-fix）：doctor 没有兑现“绝不写库”

- `check_db()` 本身正确使用 `file:...?mode=ro`：`app/cli.py:210-267`。
- 但 vault 路径随后执行 `asyncio.run(_vault_counts())`，而 `_vault_counts()` 调用 `app_db.init()`：`app/cli.py:270-303`。
- `app.db.init()` 不是只读入口：它先 `ensure_dirs()`，再以默认读写模式连接，执行 `PRAGMA journal_mode=WAL`，最后进入迁移器：`app/db.py:27-39`。
- 迁移器会执行 `CREATE TABLE IF NOT EXISTS schema_migrations`，遇到未应用文件时还会执行 migration SQL 和 `INSERT INTO schema_migrations`：`app/db.py:240-265`。
- 对 `app/cli.py` 搜索 `INSERT|UPDATE|DELETE` 确实是 0 命中，但这只证明没有直接 DML，不能消除上述传递写路径。即使正常“全部迁移已应用”的常见路径通常不改业务行，写连接、WAL 模式切换和 DDL 仍使硬保证不成立；检查与初始化之间也没有原子只读屏障。
- `check_db()` 若因 integrity error 返回 FAIL，会把 pending 返回为空；`cmd_doctor()` 仍继续进入该写模式初始化，而不是在损坏库上停止 vault 扫描：`app/cli.py:239-250,486-495`。
- 修复要求：vault drift 应直接复用只读 SQLite 连接读取 `vault_index`，文件 hash 扫描保持纯读；doctor 路径不得调用 `db.init()`、`db.migrate()` 或 `db.close()`。

### C6-H2（High / must-fix）：note ref 可经 symlink 读取 vault 根外文件

- 直接 `note:../../x`、反斜杠形式和绝对路径会因 `".." in rel.parts` / `rel.is_absolute()` 返回 400，这部分正确：`app/api/contract.py:126-134`。
- 但随后直接执行 `(vault_dir / rel).is_file()` 和 `read_bytes()`；两者都会跟随 symlink，且没有对 canonical target 做根目录包含检查：`app/api/contract.py:134-140`。
- 因此只要 vault 内存在 `outside -> /某个/vault外文件` 的 symlink，`note:outside` 就会读取该文件。这违反“vault 根之外必须拒绝”，也绕过了当前测试仅覆盖的词法路径穿越。
- 修复要求：分别 resolve vault 根和已存在目标，确认 `resolved_target.is_relative_to(resolved_root)` 后再读；补“文件 symlink”和“中间目录 symlink”越界测试。

### C6-M1（Medium / must-fix）：hand “auth check” 实际只检查安装/配置

- CLI hand 只执行 `[binary, "--version"]`：`app/cli.py:173-193`。退出 0 只能证明二进制可运行，不能证明 Claude/Codex/Gemini/Agy/OpenCode 已登录。
- API hand 只检查 key 字符串存在且明确“不调用”：`app/cli.py:153-159`；这可作为无配额配置检查，但不能命名为认证成功。
- 最后的 default/research 判定又调用静态 `hand.available()`，没有消费前面的探测结果：`app/cli.py:195-204`。已登出但 `--version` 正常的默认 hand 会得到整体 PASS。
- `tests/conftest.py:21-27` 禁用了所有真实 hand；`tests/test_cli_doctor.py:90-111` 只覆盖 echo 和 disabled 分支，未覆盖任何真实无配额认证探针。
- 应为每种 CLI 定义明确的无 prompt 登录状态命令；无可靠命令时应报告 `auth unknown`/WARN，而不是 `ok`。

### C6-M2（Medium / must-fix）：asyncio 桥不是事件循环安全的

- Ollama health 路径调用 `asyncio.run(hand.health_check())`：`app/cli.py:160-167`。
- Vault 路径调用 `asyncio.run(_vault_counts())`：`app/cli.py:270-294`。
- 从已有事件循环调用任一路径都会抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`。只读合成探针已复现 vault 路径。
- 测试文件自己承认 full vault 路径不能在 pytest 的事件循环中运行，因而只在 subprocess 覆盖：`tests/test_cli_doctor.py:3-8,147-158,270-298`；这规避了问题，不是证明安全。
- 建议将 doctor 编排改成 async，一次性在真正 CLI 入口运行事件循环；纯同步 DB/文件检查无需 async bridge。

### C6-M3（Medium / must-fix）：合法 JSON 的坏 cooldown 结构会中断整个 doctor

- 顶层只验证为 dict，随后直接比较 `cd.get("until", 0) > time.time()`：`app/cli.py:384-410`。
- `{"claude":{"until":"tomorrow"}}` 会抛 `TypeError`；`until=Infinity` 等值还可能在 `datetime.fromtimestamp()` 抛出。合成只读探针实际得到：`TypeError: '>' not supported between instances of 'str' and 'float'`。
- 非 dict 的单项被静默忽略并仍计入“recorded”，而 registry 加载同类文件会捕获异常并整体“starting clean”，doctor 的诊断语义与运行时也不一致。
- `cmd_doctor()` 以列表表达式顺序调用检查，没有逐项异常隔离：`app/cli.py:486-495`；因此 rate-limit 坏项会让磁盘检查和汇总都消失。
- 当前测试只覆盖语法坏、顶层 list 和正常 cooldown：`tests/test_cli_doctor.py:224-253`。应校验每个 entry、有限数时间戳和字段类型，并把损坏报告为 FAIL 而不是抛异常。

### C6-M4（Medium / explicit acceptance）：contract 状态枚举没有从代码常量导入

- 唯一导入的状态常量是 `executor.TERMINAL`；active task 状态在 contract 内新建 `TASK_ACTIVE = ("queued", "running")`，其他三组完整硬编码在 `_FALLBACK_ENUMS`：`app/api/contract.py:39-52`。
- 正常响应也不是从代码常量构造，而是正则解析 live `sqlite_master`：`app/api/contract.py:54-83`。这能反映当前 DB CHECK，当前值也正确，但不满足“状态枚举从代码常量 import 而非硬编码”的指定验收。
- 仓库检索确认 workflow/research/whiteboard 当前没有可导入的 canonical status 常量；需要先在各状态机模块建立单一常量，再由 contract 导入，并让 schema/测试交叉核验。
- 测试从被测模块本身导入 `TASK_ACTIVE`，其余预期再次写死字符串：`tests/test_contract.py:15-18,35-52`，无法发现代码状态机与 contract 同时漂移。

### C6-M5（Medium）：uninstall 失败时仍删除 plist，反向操作不完整

- 现代 `bootout` 与 legacy `unload -w` 两条反向路径都存在：`scripts/uninstall-service.sh:10-17`。
- 但预检 `launchctl print` 失败时直接判“not loaded”；更严重的是 bootout 与 unload 都失败时只打印 warning，随后仍删除 plist并以 0 退出：`scripts/uninstall-service.sh:10-24`。
- 这会留下仍在内存运行、却已失去磁盘 plist 的 job，不能视为“反向完整”。卸载失败时应保留 plist并非零退出；只有确认 job 未加载或成功 bootout/unload 后才删除。
- 当前测试没有 fake-launchctl 的成功/失败矩阵，只检查脚本存在、可执行和 `bash -n`：`tests/test_cli_doctor.py:301-320`。

### C6-L1（Low）：cron/orphan “健康”口径偏弱

- cron 空表直接 PASS，任意久以前的最后一次成功也仍 PASS；实现没有按任务调度周期检测缺报/陈旧：`app/cli.py:306-346`。它更接近“历史失败摘要”，不能发现 scheduler 已停止产出。
- orphan 仅统计 `tasks` queued/running 与 `research_queue` running：`app/cli.py:349-381`；未纳入同样有持久 running 状态的 workflow/card/dispatch 等域。若这是有意的最小口径，应在输出和文档中明确。

### C6-L2（Low）：launchctl enable 失败被吞掉并误报成功

- bootstrap 成功后，`launchctl enable ... || true` 忽略失败，下一行仍输出 “bootstrapped + enabled”：`scripts/install-service.sh:53-57`。
- 对曾被 disable 或 launchctl 状态异常的 job，这会产生假成功。至少应区分 “bootstrapped, enable failed” 并非零退出或给出可执行恢复命令。

## stop.sh 核验

- **PASS — 精确 PID：** 唯一 TERM/SIGKILL 目标均为 pidfile 解析出的 `$PID`：`scripts/stop.sh:22-62`。
- **PASS — broad pkill 已移除：** `pkill` 仅出现在说明旧缺陷的注释 `:5`；所有可执行行均无 `pkill`，不存在兜底。
- **PASS — 有界等待与升级：** TERM 后 20 × 0.5s 轮询，约 10 秒后只对同一 PID 发 `kill -9`，再检查存活并决定是否删除 pidfile：`:44-62`。
- **PASS — 陈旧检测：** 覆盖无 pidfile、空/非数字 PID、PID 不存在、PID 重用且命令不是目标 uvicorn：`:16-42`。
- **PASS-WITH-NIT — start 对齐：** `start.sh:13-23` 也加入 alive + 命令双检查；它没有像 stop 一样先校验 PID 格式，但失败分支只删除陈旧 pidfile、不发送停止信号，未形成 broad-kill 风险。

## plist / service 脚本核验

- **PASS — 核心键：** `KeepAlive.SuccessfulExit=false`、`RunAtLoad=true`、`WorkingDirectory`、独立 stdout/stderr 日志、`ProcessType=Background` 全部存在：`scripts/com.institute-one.server.plist.template:28-63`。
- **PASS — 启动解释器路径：** ProgramArguments 使用绝对 `{{VENV_DIR}}/bin/uvicorn`；launchd 不依赖用户 PATH 找 uvicorn。PATH 注入包含 Homebrew/local/system 路径，hand 子进程再由 `get_cli_env()` 捕获 login-shell PATH：模板 `:14-23,53-60`，`app/hands/base.py:105-136`。
- **PASS — 渲染一致：** 模板五个占位符 `REPO_DIR/VENV_DIR/LOG_DIR/PORT/PATH` 与安装脚本五次替换一一对应：`scripts/install-service.sh:38-46`。
- **PASS — 激活语法：** `launchctl bootstrap gui/$UID` 为主路径，`load -w` 为兼容兜底：`:53-64`。默认模式只渲染、lint、打印命令，不调用 launchctl；只有显式 `--activate` 才激活。
- **FAIL — 完整卸载：** 见 C6-M5；命令具备，但失败后清理顺序不安全。
- **NIT — enable 假成功：** 见 C6-L2。
- 本审查没有实际安装/激活/卸载服务。

## doctor 逐项核验

- **Hands：FAIL。** 不烧 prompt/配额这一点成立；但 `--version` 不是 auth probe，真实 CLI 分支无测试。
- **DB integrity + migration 差集：PASS。** read-only URI、`PRAGMA integrity_check`、pending 与 ghost migration 都有实现；真实 ledger 删行/插入 ghost 的测试有效。缺少实际损坏 SQLite 文件的 integrity-failure 测试。
- **Vault drift：FAIL。** writer 的 clean/conflict/missing/drifted 计数逻辑可复用，但桥接方式违反只读且事件循环不安全；测试只覆盖空 ledger PASS 与 gating，没有制造 missing/drift/conflict。
- **Cron：PASS-WITH-NIT。** 能报告每 job 最后状态和 24h 失败；不检测缺报/陈旧，见 C6-L1。
- **Orphans：PASS-WITH-NIT。** tasks/research 的真实残留行测试有效，但计数域较窄。
- **rate_limits：FAIL。** 语法错误和顶层形状会 FAIL；嵌套坏类型会崩溃，见 C6-M3。
- **Disk：PASS。** 1 GiB FAIL、5 GiB WARN 阈值和三档测试正确。
- **Offline：PARTIAL。** server probe、DB/cron/orphan/rate/disk 都不依赖服务器；vault 本意离线可跑，但错误地使用完整 DB 初始化；hand HTTP 分支仍会主动访问本地 Ollama，这是不烧配额的健康探测，但其 async bridge 不安全。

## contract / artifacts 逐项核验

- **Contract 形状与 caps：PASS。** version、caps、truncation marker、ref grammar 当前返回正确。
- **状态枚举来源：FAIL。** 当前值正确但来源不符合指定的代码常量要求，见 C6-M4。
- **task ref：PASS。** 参数化查询、404、JSON 列反序列化有测试。
- **note ref：FAIL。** 直接 `../../`/绝对路径会 400，8 KiB byte cap、ledger、404 都正确；symlink 越界未拦截，见 C6-H2。
- **fact_card ref：PASS。** 表缺失先查 `sqlite_master` 并返回 501；表存在时返回 row、缺 id 返回 404，两种真实 schema 状态均有测试。
- **Nit：** `app/api/contract.py:16-17` 和 `tests/test_contract.py:3-5` 仍写“尚未挂载”，但主代理已在 `app/main.py:152-184` 挂载；`contract.py:109` 仍称 fact_card “501 until Phase 3 lands”，当前 0015 已落地。均为陈旧说明，不影响路由运行。

## 测试质量与 plutil

- 有真实性的损坏场景：schema_migrations 真删行/真插 ghost、cron_metrics 真失败、tasks/research_queue 真 running、真实坏 JSON 文件、真实 fact tables drop、临时 HOME 下真实 plist 渲染。
- 关键缺口：
  - 所有真实 CLI hands 被禁用，未测认证；
  - 未测 SQLite 真损坏、vault missing/drift/conflict；
  - 未测已有事件循环；
  - 未测 cooldown 嵌套坏类型；
  - 未测 vault symlink 越界；
  - stop 只做源文本断言，没有 TERM→等待→KILL/stale PID 的行为测试；
  - uninstall/`--activate` 没有 fake launchctl 矩阵；
  - contract 状态测试与被测硬编码同源。
- 当前 macOS 有 `plutil`，定向测试结果是 `40 passed`、没有 skip，因此 template lint 与渲染后 plist lint 两条都实际执行并通过。无 `plutil` 平台上模板测试显式 `pytest.skip`，渲染测试条件跳过 lint；处理合理。

## 硬规则

- **C6 无 migration：PASS。** 本分区没有新增/修改 migration；工作树中的 0005–0018 来自其他分区，不能归因 C6。
- **不碰生产：PASS。** 本审查只做源码读取、语法/测试及纯合成异常探针；服务安装、停止、数据库操作均未指向真实环境。
- **pyproject aggregate diff：字面不满足“只加 `[project.scripts]`”。**
  - 除 `pyproject.toml:20-21` 的 console script 外，当前相对 HEAD diff 还新增 `sqlite-vec>=0.1.9`（`:14`）。
  - `ROADMAP.md:100-101` 明确把 sqlite-vec 依赖归给 Phase 1a，`implementation-notes.md:107-119` 也把该底座分给 A8；共享工作树无法把它归因给 C6。
  - 因此 C6 自身的 pyproject 目标可视为只增加 script，但主代理若按“当前 aggregate diff 必须仅这一处”的字面门禁验收，结果应标为不通过并显式豁免/拆分该外部分区改动。

## 验证记录

```text
.venv/bin/python -m compileall app -q
→ exit 0

.venv/bin/python -m pytest tests/test_cli_doctor.py tests/test_contract.py -q
→ 40 passed in 1.80s

bash -n scripts/*.sh
→ exit 0

rg '\b(INSERT|UPDATE|DELETE)\b' app/cli.py
→ 0 matches（但传递调用 db.init()/migrate()，见 C6-H1）

rg 'pkill' scripts/stop.sh
→ 仅注释 1 处；可执行行 0 处
```
