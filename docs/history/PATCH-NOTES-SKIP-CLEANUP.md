# PATCH-NOTES-SKIP-CLEANUP — 测试套件 skip 清理（ROADMAP L91/L179 清账）

来源：ROADMAP.md Phase 8 「Test coverage push」(L179) 与 Phase 0 「P3 · Test gaps」(L91)
的括号说明写着 `remaining: 7 removable skips — 6 over-broad bundle markers + 1 D4
restart probe`。实测核账：那 7 个位置里 6+1 早已在前几轮修掉（S4-P0-02 marker 拆分 +
M8-002 确定性 restart fixture），账目过期；但 2026-07-20 基线 `-rs` 仍有 **3 个真实
skip**，另有 3 处**死代码 skip 守卫**（条件永远不再触发的自愈 skip 路径）。本次处置：
移除 2 个 bundle skip（测试改跑合成 fixture）+ 3 处死守卫，保留 1 个网络 gate 与各
opt-in/环境 gate。

> 说明：本文件的上一版由并行会话在 22:23 写入，主张「保留 2 个 bundle skip、零测试
> 改动」。该主张与本卡任务约束冲突（测试不得读取本机私有数据集；且 `market-thesis-data/`
> 本机当前并不存在，保留即永久 skip），已由本版覆盖。

## 一、基线（处置前，2026-07-20）

```
$ .venv/bin/python -m pytest tests -q -rs --ignore=tests/test_similarity_calibration.py
SKIPPED [1] tests/test_market_fetchers.py:743: real-network smoke; set INSTITUTE_NET_TESTS=1 to run
SKIPPED [1] tests/test_market_thesis_import.py:334: thesis bundle not present (...)
SKIPPED [1] tests/test_market_thesis_import.py:545: thesis bundle not present (...)
1003 passed, 3 skipped in 142.16s
```

## 二、逐位处置

### 移除（5 处）

1. **`tests/test_market_thesis_import.py:334` `test_dry_run_real_bundle_reports_counts_and_writes_nothing`**
   → 移除 `@requires_bundle`，改写为 `test_dry_run_subset_reports_counts_and_writes_nothing`：
   dry_run 跑模块内自包含 `subset` fixture（S4-P0-02 造的合成 bundle：2 lanes / 2
   theses / 10 stocks / 10 edges，含中芯国际/中远海控撞名与 Korea/Japan context 告警），
   断言集与原测试同构（counts、actions、aliases skipped=2、edge_kinds handling、告警、
   零 domain 写入、provenance 行、dry-run 可重复）；原「全 conflicting 方向」告警路径
   用方向翻转副本另跑一次 dry_run 补齐覆盖。
2. **`tests/test_market_thesis_import.py:545` `test_apply_real_bundle_full_counts`**
   → 移除 `@requires_bundle`，改写为 `test_apply_subset_full_counts`：apply 合成 fixture
   后断言聚合契约（每实体计数、thesis_versions、per-market 归一化分布 CN_A/HK/US/
   GLOBAL_CONTEXT、item 行=每条 bundle 记录、alias skipped、全部 thesis 挂上 parent lane）。
   理由：M1-003 契约本就规定商业数据集不进 repo（本机 `market-thesis-data/` 目前不存在，
   `.env` 也没有 `INSTITUTE_THESIS_BUNDLE`），任务纪律又禁止测试读私有数据——原样保留
   等于永久 skip。`requires_bundle` marker 与 `REAL_BUNDLE`/`os`/`Path` 导入一并删除，
   模块 docstring 更新；该文件现在**没有任何 skip 路径**。
3. **`tests/test_mcp.py:202`、`tests/test_mcp.py:224`（2 处死守卫）**
   `if not await _domain_reports_inserted(): pytest.skip("… apply PATCH-NOTES-A2.md")` —
   PATCH-NOTES-A2 早已落地（`whiteboard.add_topic` 返回 `inserted`，实测 True），守卫
   永不触发。→ 删除两处守卫与 `_domain_reports_inserted` 探针；第一处改为硬断言
   `"inserted" in probe`（契约回归即红，不再静默 skip），孤儿化的 `pytest` import 移除。
4. **`tests/test_mcp_roundtrip.py:213`（1 处死守卫）**
   `pytest.skip("parallel-partition tools without their migration yet…")` — 该守卫为
   projects/research-trees 分区「模块先到、迁移未到」的中间态设计；0020/0021 迁移已在
   HEAD 提交，中间态不复存在。→ 删除 `PARALLEL_PARTITION_TOOLS` 容忍分支：注册的读工具
   答 -32000 "no such table" 从「容忍」变回**真失败**。

### 保留（均为任务边界认可的环境/外部资源 gate）

| 位置 | gate | 理由 |
|---|---|---|
| `tests/test_market_fetchers.py:743`（基线第 3 个 skip） | `INSTITUTE_NET_TESTS=1` opt-in | 真网络 smoke（新浪行情），默认套件必须离线可跑 |
| `tests/test_similarity_calibration.py:302/323` | `INSTITUTE_CALIBRATION_REAL=1` + Ollama 可达 | 真模型标定，验证命令显式 ignore；任务明示合理保留 |
| `tests/test_vectors.py:32` `needs_vec` | sqlite-vec 可导入 | 本机已装，实际全跑；degradation 测试无 gate 照跑 |
| `tests/test_cli_doctor.py:601` | `plutil` 存在性 | macOS 本机存在，实际在跑；纯外部工具探测 |

`tests/test_restart_recovery.py`（账目里的 "1 D4 restart probe"）核验为**无需处置**：
M8-002 已在前轮用真实域函数确定性 fixture 重写，文件内已无 `pytest.skip`，5 passed。

## 三、终态验证（2026-07-20 23:0x，均为处置后原样输出）

```
$ .venv/bin/python -m pytest tests/test_market_thesis_import.py tests/test_restart_recovery.py -q -rs
21 passed in 1.82s

$ .venv/bin/python -m pytest tests/test_mcp.py tests/test_mcp_roundtrip.py -q -rs
17 passed in 2.43s
```

（0 skipped；全量数字与 compileall 见任务回报。全量运行期间 operator/forecasts 等
并行分区仍在改动中，其波动与本卡无关——本卡触及的 4 个套件以上述独立运行为准。）

frontend SSE automation（L91 一并挂账项，非本卡改动）：`cd frontend && npm test` →
2 files / 16 tests passed（useSSE 状态机 + askStream NDJSON）。

## 四、改动清单

- `tests/test_market_thesis_import.py`：2 个真实 bundle 集成测试移植到合成 subset
  fixture；`requires_bundle`/`REAL_BUNDLE` 及 `os`、`Path` 导入删除；docstring 更新。
- `tests/test_mcp.py`：A2 死 skip 守卫 ×2 与探针删除（换硬断言）；孤儿 `pytest` import 移除。
- `tests/test_mcp_roundtrip.py`：parallel-partition 死 skip 分支删除；docstring 更新。
- `ROADMAP.md` L91/L179：括号账目改为上述终态（☑ 维持，保留项写明）。
- `PATCH-NOTES-SKIP-CLEANUP.md`（本文件）。
- `app/`、`migrations/`、`frontend/`、`tests/conftest.py`：零改动。
