# REVIEW-A8 — Phase 1a 向量底座独立审查

## 结论

**FAIL**

基础链路本身可运行：本机 `sqlite-vec 0.1.9` 的真实 vec0 查询通过，1024 维、cosine KNN
语法、距离排序、`trust_env=False`、FTS 降级和 API 路由均基本正确。但当前实现有两个阻断
合入的问题：

1. 向量投影没有用 SHA 做“当前版本”校验，既会永久漏建索引，也会在失败、空文件更新或
   并发乱序时持续返回旧内容。
2. `archive._bg_tasks` 没有登记到 A1 已扩展的 shutdown drain；进程停机时可在
   `db.close()` 之后继续运行。

此外，hybrid 合并没有去除同一文件的多个向量 chunk，降级路径会反复探测并刷日志，
`mode` 也无法区分“向量健康但零行”和“向量失败”。

本报告只审 A8 分区及其必要集成点；工作区内其他代理的未提交改动均未纳入结论。

## 验证结果

- `.venv/bin/python -m compileall app -q`：通过。
- `.venv/bin/python -m pytest tests/test_vectors.py tests/test_archive.py -q`：
  **14 passed in 0.66s**。
- `.venv/bin/pip show sqlite-vec`：已安装 **0.1.9**。
- 未跑全量测试，符合审查范围要求。
- 官方 sqlite-vec KNN 惯例核对：
  `WHERE embedding MATCH ? AND k = ?`，cosine 表声明
  `float[1024] distance_metric=cosine`；当前实现一致。

## 四象限降级矩阵

以下默认 `enable_vectors=True`；“sqlite-vec 可用”指不仅能 import，而且能成功加载到
当前 aiosqlite 连接。若开关为 False，四格都直接走 FTS/跳过 embedding。

### 1. Ollama 可用 × sqlite-vec 可用

- Snapshot：对发生变化、非空的 `.md` 分块，逐块调用 Ollama，成功后事务性替换
  `vector_chunks` 与 `vec_search`。
- Search：先查 FTS，再进行 vec0 cosine KNN；只要 `vectors.search()` 返回至少一行，
  返回 `mode="vector+fts"`。
- 日志：正常路径不记录降级日志。
- 结论：主链路可用，但受“陈旧索引”“同 path 多 chunk 重复”和 mode 歧义影响。

### 2. Ollama 不可用 × sqlite-vec 可用

- Snapshot：先成功加载扩展/创建虚表，然后第一个 chunk 的 `embed()` 返回 None，
  `index_file()` 返回 0；快照本身成功。
- Search：query embedding 返回 None，向量结果为 `[]`，最终
  `mode="fts"` 并返回 FTS 行。
- 日志：不是“只记一次”。每个变更的 Markdown 文件首次 embedding 都会 warning；
  每次搜索也会再次连接并 warning。黑洞式网络故障可令每次搜索等待最多 20 秒。
- 结论：功能上降级，但运维行为不够“优雅”；更严重的是旧向量不会失效。

### 3. Ollama 可用 × sqlite-vec 缺失/不可加载

- Snapshot：`ensure_ready()` 在调用 Ollama 前返回 False，跳过 embedding。
- Search：不调用 Ollama，直接返回 FTS，`mode="fts"`。
- 日志：import 缺失会在每次调用记录 debug；import 成功但 load 失败会在每次调用
  warning。没有负缓存、熔断或限频。
- 结论：短路顺序正确，FTS 可用。

### 4. Ollama 不可用 × sqlite-vec 缺失/不可加载

- 行为与第 3 格相同；sqlite-vec 先短路，所以不会尝试 Ollama，也不会产生 Ollama
  失败日志。
- Snapshot 跳过，Search 返回 FTS，`mode="fts"`。
- 结论：降级结果正确，但 sqlite-vec 失败仍会被每次重复探测。

### 开关关闭的默认现实

- `vectors._enabled()` 使用
  `getattr(get_settings(), "enable_vectors", False)`；当前 `config.py` 没有该字段，
  因而功能确定为关闭，没有“补丁未应用时半启用”的状态。
- 即使按 PATCH-NOTES 加入字段，默认值仍是 False；还必须显式设置
  `INSTITUTE_ENABLE_VECTORS=true` 才会启用。
- 小瑕疵：`archive.py:116-123` 仍会为每个变更的 Markdown 创建后台 task，随后
  `vectors.py:150-152` 才因开关关闭返回；所以不是测试注释所说的严格“零向量工作”。

## 正确性核验

### 已确认正确

- 维度一致：`vectors.py:34` 的 `EMBED_DIM=1024`、返回值长度检查
  (`vectors.py:109-113`) 与 vec0 声明 (`vectors.py:83-86`) 一致。
- SQL 正确：`vectors.py:203-208` 使用 vec0 的 `MATCH` + `k = ?`；外层
  `ORDER BY v.distance` 默认升序，cosine distance 越小越相似。
- `_pack()` 使用 float32 BLOB，符合 sqlite-vec Python 常见写法。
- 加载顺序正确：`vectors.py:73-86` 先 import、开启 extension loading、load，
  在 `finally` 中关闭 loading，之后才创建 `vec_search`。
- import/load 失败不会进入虚表查询；`_ready_conn` 只在 load + CREATE 全部成功后设置。
  load 失败时连接的 extension-loading 开关会被复原。若 load 成功而 CREATE 失败，
  扩展仍留在该连接中，但普通连接状态可继续使用，不构成污染。
- `trust_env=False` 明确位于 `vectors.py:102`，不会吃到本机全局 SOCKS 代理。
- 新空文件得到零 chunk；无标题超长文本按 1200 字符硬切，现有测试覆盖空输入和
  `2 * CHUNK_MAX_CHARS + 10`。
- 新迁移只增 `vector_chunks` 与 path 索引；虚表没有错误地放进 migration。
- 向量元数据时间戳使用 `bus.now_iso()`（`vectors.py:163`）。

## 分级问题

### [阻断/高] 1. SHA 只存不校验，索引会永久缺失或返回陈旧内容

涉及位置：

- `app/institute/archive.py:94-98`
- `app/institute/archive.py:116-123`
- `app/institute/vectors.py:153-160`
- `app/institute/vectors.py:163-182`
- `app/institute/vectors.py:203-208`

具体有三条可达路径：

1. **首次降级后无法补建。** 快照先写 `archive_files`，向量失败/关闭时返回 0。下次
   文件内容没变，`archive.py:94-98` 在安排 embedding 前就跳过；Ollama 恢复或开关
   打开后仍不会重试。
2. **刷新失败或文件变空后继续服务旧向量。** `index_file()` 在空 chunks 或任一
   embedding 失败时直接返回，明确保留旧行；search 只 join `vector_chunks`，
   从不比较 `vector_chunks.sha256` 与当前 `archive_files.sha256`。
3. **并发更新可能旧任务覆盖新任务。** 后台任务在 embedding 完成后无条件按 path
   替换；没有在事务中确认传入 SHA 仍是 archive 当前 SHA。较旧但较慢的任务可以最后
   提交并覆盖较新的索引。

独立复现结果：

- 初次关闭向量后再开启并对同一未变文件 snapshot：
  `archived=[]`，`vector_rows=[]`。
- 已索引 `gpu gpu`，把文件改成 `cpu cpu` 并让刷新 embedding 失败：
  `vector_chunks` 仍为 `gpu gpu`；Ollama 恢复后搜索 `gpu` 仍命中该文件。
- 已索引非空文件后把它改为空文件：旧 `gpu` chunk 仍存在。

影响：`mode="vector+fts"` 可以把已经不在当前归档中的旧文本排在最前面；这不是单纯
“best effort 少一些结果”，而是返回错误结果。

合入前至少应做到：

- 对当前 SHA 没有完整向量投影的 `.md` 提供重试/回填路径，不能只依赖“文件发生变化”。
- 提交替换前在同一事务中校验 `archive_files.path + sha256` 仍匹配任务版本。
- 空文件或刷新失败时，旧版本必须被删除或在查询层通过 current-SHA join 隐藏。
- 建议把 embedding model/chunker 版本纳入索引版本；仅有源文件 SHA 不足以支持模型升级。

### [阻断/高] 2. embedding 后台任务未接入 shutdown drain

涉及位置：

- `app/institute/archive.py:26-54`
- `app/main.py:55-80`
- `PATCH-NOTES-A8.md:63-69`

`_spawn_vector_index()` 的异常边界对“快照绝不失败”是有效的：

- `create_task()` 本身在 try 内；
- `vectors.index_file()` 捕获普通运行异常；
- `_bg_tasks` 持有强引用；
- done callback 会移除任务并消费异常。

但 done callback 只是完成后自清，不是停机 drain。A1 当前的
`main._drain_background()` 枚举了 executor/workflows/whiteboard/mailbox/
analyst_daily/research，遗漏 `archive._bg_tasks`。lifespan 随后立即
`db.close()`；仍在 Ollama HTTP 或逐 chunk embedding 的任务可在数据库关闭后继续，
也没有被有界取消和 await。

PATCH-NOTES 把它描述成“等 P1 Graceful shutdown 再处理”已经不符合当前集成树：
A1 的 drain 已经存在，A8 应直接登记 archive registry。`flush_vector_indexing()` 只供
测试显式调用，不能代替生产停机处理。

### [中] 3. Hybrid 只去掉 FTS 重复，没有去掉 vector 内同 path 多 chunk

涉及位置：

- `app/institute/vectors.py:203-220`
- `app/institute/archive.py:167-169`

KNN 返回 chunk 行。`seen` 只用于过滤 FTS 行，`vec_rows` 自身没有按 path 折叠，因此
一个长文档可在结果中出现多次并耗尽 top-k。

独立复现的 hybrid paths：

`['research/dup-path/long.md', 'research/dup-path/long.md', 'research/dup-path/other.md']`

应利用已经按 distance 升序的顺序，保留每个 path 的第一个（最佳）chunk，再与 FTS
合并；并增加多 chunk 同 path 的回归测试。

### [中] 4. 降级路径没有负缓存，搜索可反复等待 20 秒并刷日志

涉及位置：

- `app/institute/vectors.py:59-91`
- `app/institute/vectors.py:94-116`
- `app/institute/vectors.py:194-207`
- `app/institute/archive.py:161-166`

成功 readiness 有 per-connection 缓存，但失败没有。sqlite import 缺失每次 debug，
load 失败每次 warning；Ollama 不可用则每个搜索请求都重新建立 client、等待最多
`EMBED_TIMEOUT_S=20` 并 warning。默认 localhost connection-refused 通常很快，但超时/
黑洞故障会让所谓 FTS fallback 每次都承担完整 timeout。

建议至少增加短 TTL 健康状态/熔断与日志限频；成功或 TTL 到期后再探测。

### [中] 5. `mode` 反映“有没有向量行”，不可靠地反映真实执行路径

涉及位置：`app/institute/archive.py:164-169`

- sqlite/Ollama 都健康但表为空时，向量查询成功返回零行，mode 仍是 `"fts"`。
- 有陈旧向量行时，mode 会是 `"vector+fts"`，即使当前文件版本从未成功索引。

因此 mode 不能用来区分“健康但无命中”和“向量层降级”，观测字段语义不完整。若 mode
的目的确实是观测降级，应让 vector search 同时返回可用状态，而不是仅靠 rows 的真值。

### [中] 6. 可配置 model 没有索引版本隔离

涉及位置：

- `app/institute/vectors.py:34-37`
- `app/institute/vectors.py:51-52`
- `migrations/0007_vectors.sql:8-17`
- `PATCH-NOTES-A8.md:33-37`

PATCH-NOTES 只提醒“维度变化要 DROP 虚表”，但即使两个模型都是 1024 维，旧文档和新文档
也会逐步混入不同 embedding 空间；查询向量只来自当前模型，距离失去意义。未变文件又会
被 archive hash 短路，无法自动重建。不同维度则会被长度检查降级，但旧索引仍保留。

需要把 model/index schema 版本纳入元数据，并在变化时做完整重建或拒绝混用。

### [低] 7. SHA 去重仅限“同 path + 同内容”

涉及位置：

- `app/institute/archive.py:94-98`
- `app/institute/vectors.py:141,175-177`

同一路径同一 SHA 不会重复 embed；但相同内容位于两个路径时会各自 embed，`sha256`
没有唯一索引或 embedding 复用。独立探针中两个同内容文件产生 2 次 embedding 调用。
ROADMAP 没有明确要求跨文件内容寻址，因此这是成本问题，不单独作为阻断；但
PATCH-NOTES/代码注释不应把它描述成全局 hash 去重。

## FTS5 与 API 兼容性

- `archive.search()` 的 `_sanitize_match`、SQL、snippet 参数和 limit 逻辑在 git diff
  前后没有变化。
- snapshot 的 FTS 内容只从内联 `data.decode(...)` 改成等价局部变量 `text`，内容一致。
- 但 GET `/api/archive/search` 的外部响应不可能“逐字节一致”：按任务要求由数组改为
  `{mode, results}`，且 `archive.py:162-163` 还给每个 FTS row 新增了
  `source="fts"`。PATCH-NOTES 所称“FTS 行内容本身不变”不准确。
- PATCH-NOTES 已正确指出 Obsidian plugin 仍期待数组；主代理必须同步
  `obsidian-plugin/src/api.ts`/消费者后再交付，否则现有搜索 UI 会破坏。

## 测试质量

### Fake embedder 是否真的测到排序/top-k

- **排序是真的测到了。** `test_index_and_topk_ordering` 使用真实 vec0，构造 pure GPU
  与 mixed GPU/CPU 向量，断言 pure 在 mixed 前且 distance 单调。
- **KNN SQL 也确实执行了。** 本机安装 sqlite-vec 后相关用例未 skip，语法若错误会失败。
- **但 top-k 上限没有被独立证明。** 直接调用用的是 `k=5`、库中仅 3 行；API 用例的
  `len <= 2` 还会被最终 `merged[:limit]` 保证，即使底层 k 约束失效也可能通过。
  应增加 `vectors.search(..., k=1)` 的直接断言。

### 四象限覆盖不足

- “Ollama down” 用例没有标 `needs_vec`；在 sqlite-vec 未安装的 CI 中，它会在
  `ensure_ready()` 提前短路，仍然通过，却没有走到 Ollama 失败分支。
- “sqlite missing” 用例没有让一个可用 fake embedder 记录“绝不应被调用”，不能直接
  证明短路顺序。
- 没有检查重复日志、20 秒 timeout/circuit behavior、shutdown drain、首次失败后回填、
  失败刷新后的 stale SHA、空文件更新和同 path 多 chunk 去重。
- `test_snapshot_survives_indexing_crash` 不需要真实 sqlite-vec，却被 `needs_vec` 标记；
  依赖缺失环境会少测一个核心降级保证。

### POST `/api/search` 为什么无需改 main.py 也能通过

- `app.main.create_app()` 原本就 include `api_archive.router`。
- A8 把 archive router 的模块级 prefix 去掉，并给三个旧路由写全路径，同时把
  `/api/search` 放在同一个 router；因此无需新增 main 挂载。
- API 测试使用 `create_app()`，不是另造裸 app。ASGITransport 没跑 lifespan，但
  autouse `tests/conftest.py` fixture 已执行 `db.init()`/migration，所以测试通过是有效的。

## 硬规则核验

- Migration only-add：通过。A8 新增 `0007_vectors.sql`，没有改旧 migration。
- bus/scheduler handler 不 raise：A8 没有新增 bus/scheduler handler；快照任务异常不会
  反向打断 snapshot。
- prompts：A8 分区没有 prompts 改动。
- 时间戳：新增向量元数据使用 `bus.now_iso()`。
- 虚表只在 extension load 成功后创建：通过。
- “绝不让快照失败”的普通异常边界：通过；但 shutdown drain 仍是阻断集成缺口。

## PATCH-NOTES-A8 正确性

1. **pyproject 建议正确。** `[project].dependencies` 加
   `"sqlite-vec>=0.1.9"` 与已验证版本一致；未应用前，干净安装会缺依赖并永久走 FTS。
2. **config 建议基本正确。** `enable_vectors: bool = False` 与
   `embed_model: str = "bge-m3"` 的 env 映射、默认关闭和防御式读取说明正确。应用字段
   本身不会启用，仍需显式 env 开关。
3. **main.py 无需改动的说明正确。** 同一 archive router 已经被 include。
4. **API 消费者提醒正确但表述不完整。** 数组包装变化已指出；“FTS row 不变”遗漏了
   新增 `source` 字段。
5. **测试数量说明与本次定向运行一致。** 11 个 vector + 3 个 archive 全通过；本报告
   未复核其声称的全量 182/8 基线。
6. **shutdown 风险事实正确、处置建议错误/过时。** 当前 A1 drain 已经落地，应现在把
   `archive._bg_tasks` 纳入，而不是继续推迟。
7. **模型切换说明不完整。** 不仅维度变化要重建；同维度换模型也必须完整重建并防止
   不同 embedding 空间混用。

## 重新审查门槛

至少完成以下三项后再给 PASS：

1. 用 current SHA（并建议加 model/chunker 版本）解决漏建、stale、空更新和并发乱序。
2. 把 `archive._bg_tasks` 纳入 `main._drain_background()`，补停机测试。
3. hybrid 结果按 path 去重，补真实 `k=1`、多 chunk、四象限独立分支和降级日志测试。
