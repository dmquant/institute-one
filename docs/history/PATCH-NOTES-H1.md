# PATCH-NOTES-H1

H1 新增了 `rate-limit-revival`（`gated=True`）调度任务。现有三处“全量 job 集合”测试位于 H1 独占分区外，必须由主代理同步更新，否则定向验收会有 4 个陈旧断言失败、全量还会多 2 个：

1. `tests/test_cron_metrics.py`
   - `EXPECTED_GATES` 增加 `"rate-limit-revival": True`。
   - 两处文案/断言中的 20 改为 21；`len(reg) == 20` 改为 `== 21`。
2. `tests/test_maintenance.py`
   - `test_job_gating_registry_matches_semantics` 的 `expected` 增加 `"rate-limit-revival": True`。
   - `len(found) == 20` 改为 `== 21`，同步旁边 job 数量文案。
3. `tests/test_mcp_roundtrip.py`
   - `test_empty_db_shapes_of_key_aggregates` 中 `len(cron["jobs"]) == 20` 改为 `== 21`，同步注释。

该 job 会调用 `executor.spawn`，因此必须 gated；不能为保住旧断言而从 `job_registry()` 隐藏，否则会破坏 cron health 的完整定义面。
