# PATCH-NOTES-LOOP-P8a — sweep_vault_conflicts 文件扫描搬离事件循环（顺带合并双重扫描）

日期：2026-07-20 ｜ 范围：`app/institute/operator.py` + `tests/test_operator.py` ｜ 无 migration

## 问题

`sweep_vault_conflicts()` 每次运行对 vault_index 全表做**两遍**同步文件读+SHA，
且全部发生在事件循环上（原指向 ~367-383）：

1. `writer.doctor()` —— 逐行 `exists()` / `_read_exact` / `_sha_file`（同步 IO）拿计数；
2. `_nonclean_vault_rows()` —— 同样的分类逻辑再扫一遍拿 per-path 状态。

大 vault 下这两遍扫描把整个事件循环冻住（SSE、API、调度 tick 全部停摆），
IO 还白白翻倍。

## 修法（合并 + to_thread，二合一）

- 新增同步纯函数 `_classify_vault_rows(root, rows)`：**一遍**分类同时产出
  doctor() 形状的计数（total/clean/conflict/missing/drifted）和 per-path 非 clean
  状态列表。分类逻辑与 `writer.doctor()` 严格同序镜像（复用 writer 的私有
  helper `_read_exact/_extract_region/_sha_text/_sha_file/_has_ownership`，
  未复制代码）。
- `sweep_vault_conflicts()` 里 DB 查询留在事件循环，文件扫描经
  `await asyncio.to_thread(_classify_vault_rows, writer.root, rows)` 进工作线程；
  不再调用 `writer.doctor()`（重复扫描就此合并——IO 减半）。开卡循环
  （`open_action` per conflict/drift path）本来就是 async DB 写，不变。
- vault 关闭判定从 `doctor() is None` 改为 `writer.root is None`（真 writer 的
  disabled 形状即 `root=None`）；返回形状 `{"doctor": counts, "opened": n}` /
  `{"skipped": "vault_disabled"}` 对 scheduler 与既有测试完全不变。
- 删除被取代的 `_nonclean_vault_rows`；顺手移除因此孤立的 `VaultWriter` 导入。

### 为什么不改 writer.doctor()

PATCH-NOTES-C4 早注明理想解是 `doctor(detail=True)` 返回逐行状态、删掉镜像。
但 `app/vault/writer.py` 不在本任务的文件边界内（只许改 operator.py +
test_operator.py），所以合并落在 operator 侧：一遍扫描同时出计数和路径。
`doctor(detail=True)` 后续卡的备注保留在 `_classify_vault_rows` docstring 里；
镜像同步义务与之前的 `_nonclean_vault_rows` 相同（分类同序，一处注释指路）。

线程安全核对：`_classify_vault_rows` 只读传入的 rows 列表与磁盘文件，无共享可变
状态、不碰 DB；worker 线程中的 `time.sleep`/文件 IO 释放 GIL。

## 回归测试

- `test_sweep_scan_runs_off_the_event_loop`（新）：把 `_read_exact` 拖慢 0.4s，
  并发一个 10ms ticker——修前 ticker 在扫描期间完全饿死（实测 `0 >= 5` 红灯），
  修后线程化扫描下 ticker 正常走表（阈值 5，负载容忍）；同时断言 drifted 计数
  与开卡结果仍正确（合并后的单遍扫描没丢语义）。
- 既有 `test_sweep_vault_conflicts_idempotent` / `test_sweep_vault_drifted` /
  `test_sweep_skips_when_vault_disabled`（Dummy 补 `root=None` 对齐真 writer 的
  disabled 形状）继续锁定行为不变。

## 测试结果

```
.venv/bin/python -m pytest tests/test_operator.py -q
65 passed in 10.52s
```

（新测试先行红灯确认：`assert 0 >= 5` → 1 failed, 3 passed（-k sweep）。）

## R3 闭合（2026-07-20 复核 P2：to_thread 扫描与 Vault 写入无同步）

复核指出 P8a 引入的新竞态：线程扫描读的是"先取的 ledger 快照 + 稍后读的磁盘"，
扫描期间事件循环继续跑 VaultWriter——写入顺序是**先** `os.replace` 磁盘、
**后** async upsert ledger，窗口内扫到"新文件 + 旧 hash"就把本进程的正常写入
误判成 drift 并开卡。

修法（开卡前重验 + 新鲜度宽限，双层）：

1. 抽出单行分类器 `_classify_vault_row()`（批扫描复用同一函数）。开卡循环对
   每个候选**重读最新 ledger 行并重新分类**（单文件小读，走事件循环——与
   writer 自身在循环上做单文件读的既有成本一致）：writer 的 upsert 已落地的
   路径重验为 clean → 跳过不开卡；
2. `SWEEP_FRESH_GRACE_S = 120`：重验后仍为 drift 但文件 mtime 在宽限期内
   （bus.now_iso() 换算 epoch 对比，无裸 datetime.now()）→ 本轮推迟——正在
   写入中的文件（replace 已落、upsert 未落）必然 mtime≈now，推迟一轮后
   ledger 追平即 clean；真人工编辑只是晚一轮开卡（sweep 周期性运行）。
   `stat` 失败（文件刚消失）同样推迟。
3. 防御自身：`SWEEP_MAX_ATTEMPTS = 100` 限定每轮重验尝试数（每次尝试 =
   一次循环上的小文件读），海量新鲜漂移不会把 P8a 刚搬下循环的 IO 又
   无界地搬回来；余量由 P10 游标带到后续轮次。

### 为什么不是复核首选的"writer 级 async 锁"

锁需要 writer 参与（"磁盘写 + ledger upsert"临界区在 `app/vault/writer.py`
内），而 writer.py 是本任务禁区；operator 侧的锁管不住 writer 的写入，等于
没锁。故采用复核明示的备选"开卡前用最新 ledger 重验"，并叠加宽限期把单次
重验的 TOCTOU 收窄到"upsert 落后 os.replace 超过 120s"（事件循环已僵死的
病理场景）；即便发生，代价是一张幂等、可人工 dismiss 的多余卡，无级联。
`doctor(detail=True)` + writer 级锁留给拥有 writer.py 边界的后续卡。

回归测试：

- `test_sweep_reverifies_against_fresh_ledger_before_carding`：伪造"快照说
  drift、现实已一致"的扫描结果 → 不开卡（修前实测开卡）。
- `test_sweep_grace_defers_inflight_writer_updates`：mid-write 形态（磁盘已
  替换、ledger 未 upsert、mtime=now）→ 本轮不开卡；upsert 落地 → 永不开卡；
  真人工编辑 → 新鲜时推迟、`_age_file` 老化过宽限后开卡（修前实测 mid-write
  即开卡）。
- 既有 drift 类测试补 `_age_file()` 对齐宽限语义（drifted/off-loop/flood/
  liveness 四处），conflict 类（行状态判定，无磁盘竞态）不受宽限影响。

## R4 闭合（2026-07-21 复核 P2 未来 mtime + P3 残余 TOCTOU 记档）

### [P2] 宽限期对未来 mtime 无下界 → 有界新鲜度窗口

R4 指出 `(now - mtime) < 120` 没有下界：时钟漂移/恢复备份/同步工具产生的
**未来** mtime 使 age 为负、永远 < 120——真实人工编辑会被逐轮 defer，直到
本机时间追上（可能数月）。

修正：显式计算 age，新鲜度是有界窗口
`-SWEEP_MAX_CLOCK_SKEW_S(300s) <= age < SWEEP_FRESH_GRACE_S(120s)`。
小幅未来偏移（合理 NTP 抖动）仍算 fresh 照常推迟；超出允许偏移的未来 mtime
记 log.warning（clock anomaly）并按**非 fresh**处理立即开卡，绝不无限延期。

回归测试：`test_sweep_future_mtime_is_not_forever_fresh`（mtime=now+60s →
推迟；mtime=now+1年 → 立即开卡；修前红灯：+1年 被当作 fresh 永久推迟）。

### [P3] "fresh ledger 重读 + 120s grace"仍非 writer upsert 的同步屏障（记档）

已知残余，本轮记档 + 边界说明（复核允许纯记档）：fresh query、磁盘
hash/mtime、open_action 之间没有共享 generation/锁/条件写。writer 先
`os.replace` 后 await ledger upsert——若两步之间停顿 **超过 120s**（事件循环
长暂停/进程假死），sweep 会看到旧 ledger + 新磁盘且宽限已过 → 开一张假
drift 卡；writer 随后 upsert 为 clean，但卡不会自动撤回（幂等、可人工
dismiss，无级联）。grace 只降低概率、不消除窗口。

operator 侧无进一步可行收窄：schema 无 generation 计数（本轮迁移编号 0037
已用于 P6a 后盾，且"只改 0037 不新建迁移"），`written_at` 与文件 mtime 的
比较无法区分"in-flight 写入"与"真人工编辑"（两者都是 mtime > written_at）。
根治 = writer 级锁或 ledger generation（`app/vault/writer.py`，本卡禁区）；
与既有的 `doctor(detail=True)` 建议合并为同一张后续卡：**writer.py 提供
detail 化 doctor + 写入临界区原语，operator 删镜像与宽限启发式**。代码内
`SWEEP_FRESH_GRACE_S` 常量注释同步标注此残余。

### [P2 附] poison path 见 PATCH-NOTES-LOOP-P10.md 的 R4 闭合（同一开卡循环）。
