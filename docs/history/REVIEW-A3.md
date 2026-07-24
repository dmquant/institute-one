# R3 独立审查：A3 / Phase 0 Hardening

## 结论

**FAIL**

两项核心实现方向正确：`_mark` 已成为单语句、单分析师 key 的原子 UPSERT，旧 blob 也会继续阻止当天重复执行；roster 的 mtime 缓存、显式 reload 和可变对象隔离均成立。定向编译与 10 个测试全部通过，但 `_get_record(date)` 把外部可控日期直接拼进 GLOB 模式，存在可复现的跨日期误匹配，列为 1 个 must-fix。

## 审查范围

- 通读 `git diff -- app/institute/analyst_daily.py app/institute/analysts.py tests/test_analyst_daily.py tests/test_analysts.py`，并读取 `PATCH-NOTES-A3.md`。
- 核对 `run_one`、`run_all`、`status`、scheduler、REST API、前端、Obsidian 插件及 `/api/admin/state` 透传路径。
- `rg` 全仓搜索 `analyst_daily:`；代码中没有遗漏的旧 key 直读，只有 `CLAUDE.md` 的旧说明，已由 `PATCH-NOTES-A3.md` 明确交给集成者更新。
- 其他在途代理的未提交改动未纳入结论。

## 逐项核验

- **原子写：PASS** — `app/institute/analyst_daily.py:77-82` 的 `_mark` 只有一条 `INSERT ... ON CONFLICT DO UPDATE`，key 精确到分析师，不再存在读—改—写窗口。
- **GLOB 匹配：FAIL** — `app/institute/analyst_daily.py:56-68` 将 `date` 原样当作 GLOB 模式的一部分；分析师 id 本身不参与模式构造，冒号或通配符不会造成注入，但日期中的 `*`、`?`、`[]` 会跨日误匹配，详见 M1。
- **旧/新冲突优先级：PASS** — `app/institute/analyst_daily.py:58-73` 先合并旧 blob、后覆盖 per-analyst 行，确实是 per-analyst 状态胜出。
- **升级防重复：PASS** — `tests/test_analyst_daily.py:101-116` 实测旧 blob 中 `completed` 的分析师会被 `run_one` 跳过，升级当天不会因格式切换再次烧配额。
- **消费方：PASS-WITH-EXPECTED-SHAPE-CHANGE** — `run_one`、`run_all`、`status`、scheduler、SPA 和插件仍消费聚合后的同一状态语义；`/api/admin/state` 是原始表透传，因此返回形状会按设计从一个 blob 变成多个 per-analyst key，仓库内未发现依赖旧形状的调用方。
- **并发聚合读：PASS-WITH-NIT** — per-analyst 扫描本身是一条只读 SELECT，不会覆盖任何 mark；legacy 与新行分两次查询，故并发 `_mark` 时单次结果可能暂时看见写入前或写入后的状态，但不会重现 lost-update，等待并发写完成后的读取完整。
- **坏 JSON / 空 blob：PASS** — `app/institute/analyst_daily.py:59-73` 会忽略无效或非对象 legacy JSON；坏 per-analyst JSON 降级为原始字符串，不会让整个状态读取失败。
- **mtime 读取失败：PASS-WITH-NIT** — `app/institute/analysts.py:43-52` 在文件暂时不存在时直接传播 `FileNotFoundError`，即使已有缓存也不回退；这与旧实现读取缺失文件时的失败语义一致，但有可用性改进空间，见 N1。
- **asyncio 安全：PASS** — `app/institute/analysts.py:43-52` 全部是同步 stat/read/parse/赋值，没有 `await` 让单线程事件循环在缓存更新中途切换任务。
- **缓存清理：PASS** — `app/institute/analysts.py:66-68,73-84` 的 `reload()` 清空 `_cache`，成功 `_save()` 在原子替换后也必经 `reload()`。
- **可变对象污染：PASS** — `roster()` 返回缓存 list 的浅拷贝，元素是 `frozen=True` 且字段均不可变；正常调用方无法就地污染缓存，只有私有 `_load()` 会返回内部 list。
- **硬规则：PASS** — A3 diff 未新增时间戳（既有事件时间仍由 bus 层处理），未改 prompts、migration 或 `tests/conftest.py`。

## Must-fix

### M1. 外部日期未经转义进入 GLOB，历史状态可跨日污染

- `app/institute/analyst_daily.py:56-68` 构造 `prefix + ":*"`；SQL 参数绑定只能防 SQL 注入，不会让 GLOB 元字符失去模式含义。
- `app/api/analysts.py:37-41` 将任意字符串 `date` 直接传给 `status()`。实测 `date="2026-07-??"` 会同时匹配 `analyst_daily:2026-07-19:macro-analyst` 和 `analyst_daily:2026-07-20:macro-analyst`；该模式与真实日期等长，现有切片会把两行都解析为 `macro-analyst`，最终由未定义的返回顺序决定哪个日期的状态胜出。
- 默认 `work_date()` 不含元字符，因此正常 scheduler/run 路径不受影响；但历史状态 API 会返回错误结果，且当前接口没有日期格式校验。
- 应改用字面量前缀谓词（例如比较 `substr(key, 1, ?) = ?`，前缀包含末尾冒号），或把 API 日期收紧为经验证的 ISO 日期；同时增加 `*`、`?`、`[]` 回归测试。

## Nice-to-have

- **N1 暂时缺失时使用热缓存**：`app/institute/analysts.py:45-46` 可考虑仅在已有 `_cache` 时对瞬时 `FileNotFoundError` 返回旧值并记录告警；当前直接失败没有引入相对旧实现的回归。
- **N2 边界测试**：补充空/坏 legacy JSON、坏 per-analyst JSON、含冒号/通配符的手工 roster id，以及并发读取与 `_mark` 交错的测试，可把当前代码审阅确认的降级语义固定下来。

## 验证摘要

- `.venv/bin/python -m compileall app -q`：退出码 0，无输出。
- `.venv/bin/python -m pytest tests/test_analyst_daily.py tests/test_analysts.py -q`：`10 passed in 0.29s`。
- 额外用内存 SQLite 复现 `date="2026-07-??"` 同时匹配两个真实日期，并均切片为同一分析师 id。
- 按要求未运行全量 pytest。
