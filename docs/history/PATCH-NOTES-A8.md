# PATCH-NOTES-A8 — Phase 1a 向量底座（sqlite-vec + bge-m3）

A8 分区内落地的文件：`app/institute/vectors.py`（新）、`migrations/0007_vectors.sql`（新）、
`app/institute/archive.py`（快照钩子 + `search_hybrid`）、`app/api/archive.py`（升级 GET
search + 新增 `POST /api/search`）、`tests/test_vectors.py`（新）。

**R8 审查返工（已完成）**：按 REVIEW-A8.md 修复了两个阻断项与点名次级项——
(1) rebuild-by-source：`vector_chunks` 增加 `model` 列；`index_file` 幂等化
（(path, sha, model) 已有投影即 no-op、空文件清除旧投影、替换事务内复核
`archive_files` 当前 sha 防并发乱序覆盖）；unchanged 文件在快照时仍安排幂等
回填 job（修"首次降级后永久漏索引"）；`vectors.search` JOIN 当前 sha + model
过滤，陈旧向量在查询层隐藏。(2) `archive._bg_tasks` 已纳入 `main._drain_background`
的注册表集合（**特批改动 main.py 一行 + import**，与现有六组同风格）。次级项：
KNN oversample + 每 path 折叠最优 chunk（hybrid 无重复行）；sqlite-vec 失败
进程级负缓存（只警告一次）、Ollama 失败 60s TTL 负缓存（首次 WARNING 后转
DEBUG，避免黑洞故障下每次搜索等满 20s 超时）。

以下是需要主代理在 A8 分区之外落实的事项。

## 1. pyproject.toml — 依赖（A8 无权修改）

`[project].dependencies` 追加一行：

```toml
    "sqlite-vec>=0.1.9",
```

已用 `.venv/bin/pip install sqlite-vec`（装到 0.1.9）验证扩展在 aiosqlite 连接上
可加载、vec0 虚表 + cosine KNN 可用。运行时 `import sqlite_vec` 失败会优雅降级
（等同 Ollama 缺失），所以就算暂不进 pyproject 也不会崩——只是永远走 FTS。

## 2. app/config.py — 新设置（A8 无权修改）

`Settings` 建议加两个字段（放在 ollama_model/ollama_host 附近）：

```python
    # Phase 1a embeddings (vectors.py degrades gracefully when off/unavailable)
    enable_vectors: bool = False
    embed_model: str = "bge-m3"
```

- `INSTITUTE_ENABLE_VECTORS`：总开关。当前代码用 `getattr(settings, "enable_vectors", False)`
  防御式读取，config 未加字段时视为 False（纯 FTS，与现状完全一致），加字段后即可
  用 env/.env 打开。
- `INSTITUTE_EMBED_MODEL`：embedding 模型名，默认 bge-m3（1024 维）。同样是
  `getattr(settings, "embed_model", "") or "bge-m3"` 防御式读取。
- 复用现有 `ollama_host` 指 Ollama 端点，未新增 host 设置。
- 注意：`vectors.EMBED_DIM = 1024` 与 bge-m3 绑定。换模型若维度不同，vec_search
  虚表需要重建（DROP 后由 vectors.ensure_ready 重建）——列宽在建表时固定。

## 3. app/main.py — 一处特批改动（R8 返工时落地）

路由挂载不用动（`app/api/archive.py` 的 router 去掉模块级 prefix，改为每路由全路径，
`POST /api/search` 与 `/api/archive/*` 同 router 承载）。R8 返工时经特批在
`_drain_background._registered()` 增加了 `| set(archive._bg_tasks)` 一行（含 import
行加 `archive`），使停机 drain 覆盖在途 embedding 任务，与 A1 现有六组注册表同风格。

## 4. API 响应形状变化 — 下游消费者需要适配（A8 无权修改）

`GET /api/archive/search` 响应从 `[...rows]` 变为 `{"mode": "vector+fts"|"fts", "results": [...rows]}`
（任务要求带 mode 观测字段）。FTS 行内容本身不变（path/ref_kind/ref_id/snippet），
向量行额外带 `session_id/chunk_index/distance/source`。已知消费者：

- `obsidian-plugin/src/api.ts` `archiveSearch()`（+ `src/main.ts` `searchArchive()`）
  仍期望数组，需要改为读 `.results`（并顺手可显示 mode）。改后需 `npm run build`
  并连同 main.js 提交（CLAUDE.md gotcha）。
- `frontend/src` 目前没有引用该端点（已 grep 确认），无需改动。
- `app/mcp.py` 的 `archive_search` 工具用自己的 SQL，不受影响；若想让 MCP 也吃到
  向量检索，可改调 `archive.search_hybrid`（非本次范围）。

## 5. 测试基线

test_vectors.py 现有 18 个用例（R8 返工补了回填、stale 隐藏/恢复、空文件清除、
并发乱序、drain 纳管、k=1、多 chunk 折叠、负缓存/单次告警、短路 spy），全部通过；
现有 test_archive.py 3 个不受影响。sqlite-vec 缺失的环境 skip 其中 11 个真实虚表
用例，降级/drain/负缓存用例照常跑。全量套件（含其他并行代理的测试）在 R8 返工后
为 191 passed / 8 skipped（返工前基线 184 / 8），只增未减。

## 6. 遗留风险（详见最终报告）

- vec_search 虚表只在 `enable_vectors=true` 且 sqlite-vec 可加载时创建；一旦创建过，
  之后带着该表的 DB 被"无扩展进程"打开也安全（migrate/普通查询不触表；已验证）。
- 换 embedding 模型（`INSTITUTE_EMBED_MODEL`）后：查询按当前 model 过滤，旧模型的
  投影自动隐藏；下一次各来源快照时按 (path, sha, model) 幂等回填新投影。旧模型的
  死行会留在 `vector_chunks`/`vec_search` 占空间，需要时可做一次性清理（非本次范围）。
- `mode` 字段语义是"本次响应是否并入了向量行"，不区分"向量层健康但零命中"
  （REVIEW-A8 [中]5，未点名返工；观测上可接受，需要时可再加 status 字段）。
- 跨路径同内容仍各自 embed（REVIEW-A8 [低]7，成本问题，未点名返工）。
