# PATCH-NOTES-LOOP-P6 — operator 自改进链：apply 卡死可重放 + 陈旧楼层提案只升不降

日期：2026-07-20 ｜ 范围：`app/institute/operator.py` + `tests/test_operator.py` ｜ 无 migration

## P6a：approved+applied=0 永久卡死 → 幂等重放 apply

### 问题

`approve_proposal()` 先条件宣占 proposed→approved，再执行 apply。若 apply 在宣占后
失败（基础设施抖动、CAS 输给并发人工改动……），提案永远停在
`status='approved' AND applied=0`：重试同一端点撞上 `WHERE status='proposed'`
rowcount=0 → "already decided" → 永久 409，无路可走（原指向 ~1184-1215）。

### 修法

宣占 rowcount=0 时**重读**该行：仅当 `status='approved' AND applied=0`（且行仍存在）
才放行重放 apply；其余一律维持 "already decided" 拒绝（rejected 永不重放，
applied=1 关闭重放窗口——它本身也是 `WHERE applied=0` 的条件宣占）。

apply 原语逐条核对至少一次语义安全（at-least-once safe）：

- `promote_disposition_to_recipe`：0023 部分唯一索引幂等，effect 行按 proposal
  唯一索引收敛；
- `retire_recipe`：条件宣占，重放输掉宣占但 `proposal_id is not None` 仍补齐
  effect 行（同样收敛）；
- `set_parameter`：本次补上 per-proposal 幂等——apply 前先查
  `parameter_history WHERE proposal_id=?`（rollback 行 proposal_id 恒 NULL，
  不会误命中），已落地则复用该行，不追加重复变更；未落地才真正调
  `set_parameter`（byte-CAS 保证并发重放只有一个赢家，输家得到干净的
  "changed concurrently"）。

并发重放 = 一个赢家 + 一个干净拒绝；inbox 卡片解决仍是条件更新（人工已关卡者赢）。

## P6b：陈旧 set_parameter 提案可批降楼层 → approve 时校验 raise-only

### 问题

楼层调参提案在**生成时**是 raise-only（`FLOOR_TUNE_STEP` 只加不减），但提案可以在
收件箱里放几天，期间人工把楼层调高——此时批准陈旧提案会把消费门**降回去**
（原指向 ~1112-1119, 1301-1314）。

### 修法

新增 `_check_floor_raise_only()`：`set_parameter` 类提案在参数校验后、宣占前，
对照 **LIVE** 楼层校验 `new_floor > 当前 floor`，否则 ValueError（→ API 409），
相等也拒（不是 raise）。拒绝发生在宣占前，提案保持 proposed（可 reject、
可等楼层回落后再批），什么都没烧。

语义边界：

- 只守 **提案** 路径；人工直连 `PUT /api/operator/parameters/{key}` 保持可升可降
  （人工门语义不变，测试锁定）；
- 当 `parameter_history` 已有该 proposal_id 的行（即 P6a 的 bookkeeping-only 重放，
  变更本体已落地）则跳过校验——重放不重判、也不重改；
- 已知窄窗：校验在宣占/apply 前发生，与并发人工改楼层存在毫秒级 TOCTOU；
  损害有界（`parameter_history` 记录 `changed_by='proposal:<id>'`，可回滚），
  与库内 "daily-cap idiom" 的容忍度一致。堵死它需要把方向判断塞进
  `set_parameter` 的事务——会改变人工 API 共用原语的语义，不值。

## 回归测试

- `test_approve_apply_failure_is_replayable`（P6a）：apply 首次炸掉 → 断言卡死形态
  （approved+applied=0、无 recipe、卡片仍 open）→ 同一端点重放成功
  （applied=1、recipe active、卡片 done、effect 冻结）→ applied=1 后再批/再拒均 409。
- `test_stale_floor_proposal_cannot_lower_live_floor`（P6b）：楼层 0.9 时批 0.75 提案
  → 409 且提案未烧、楼层不动；相等（0.75 vs 0.75）也 409；楼层降到 0.6 后同一提案
  变回 raise → 批准成功；人工 PUT 直降 0.5 仍放行。

## 测试结果

```
.venv/bin/python -m pytest tests/test_operator.py -q
64 passed in 44.76s
```

（新测试先行红灯确认：`2 failed, 62 deselected`。）

## R3 闭合（2026-07-20 复核 1 P1 + 1 P2）

### [P1] raise-only 校验与写入分离，可覆盖并降低并发人工值 → 判定绑定 byte-CAS 参照值

复核实测探针：proposal 0.75、人工在预检与 apply 之间 PUT 0.9、最终 0.75——
人工刚提的值被降回。根因：`_check_floor_raise_only` 读的是 A 时刻的 live floor，
`set_parameter` 写时又重读 B 时刻的值并沿任意方向 CAS；方向判定与写入参照的
不是同一个值。

修法：`set_parameter` 增加 `raise_only: bool = False`。proposal apply 路径传
`raise_only=True`：方向判定（`new > _floor_from_raw(old_raw)`，相等拒）针对的
就是 CAS `WHERE value = old_raw` 引用的**同一个** `old_raw`——"判在 X 上"与
"写在 X 上"合一，人工 PUT 插进任何缝隙都会让 CAS rowcount=0 → ValueError
回滚，绝不可能落下更低的值。`_check_floor_raise_only` 保留为宣占前的快速 409
（省得烧 claim），docstring 明确它**不是仲裁者**。人工 API 维持
`raise_only=False` 可升可降。`_floor_from_raw` 抽出为楼层唯一解析器，
`get_confidence_floor` 复用（两处对"当前楼层"的理解永不分叉）。

已知残余（有意）：竞态发生在宣占之后时，refuse 留下 approved+applied=0 的
惰性僵尸提案（无法 reject，但 applied=0 无任何效果，重放在方向重新有效时
自愈）——测试锁定该形态。

回归测试：`test_floor_raise_only_holds_against_concurrent_human_put`
（钩住预检后注入人工 PUT 0.9，断言 409、最终楼层 0.9、proposal 零写入、
僵尸形态、楼层回落后重放成功；修前实测 200 且楼层被降回 0.75）。

### [P2] 并发 replay 追加两条同 proposal 参数历史 → 事务内复用 + 0037 唯一索引

复核交错：两个 replay 都过 approved+applied=0 窗口、都看到 history 为空；
第一个写 0.7→0.75 后，第二个重读 0.75，`SET 0.75 WHERE value=0.75` 的 CAS
照样 rowcount=1 → 追加一条 0.75→0.75 废行。

修法三层：

1. per-proposal 复用移进 `set_parameter` 的**写事务内**（`db.transaction()`
   持全进程写锁，该读与对手写serialize）：已有该 proposal_id 的 history 行
   直接返回复用，且置于 raise-only 判定**之前**（bookkeeping 重放不重判）；
2. `migrations/0037_parameter_history_proposal_unique.sql`：先按 0022 惯例
   DELETE 保留每个 proposal_id 的最早行（真实变更；后来的是 no-op 回声——
   避免 live 库已有重复行时建索引卡死 boot），再建
   `uq_parameter_history_proposal` 部分唯一索引
   （`WHERE proposal_id IS NOT NULL`，rollback 行 proposal_id 恒 NULL 天然豁免）。
   进程内写锁已使重复不可达，索引是跨写者的 DB 终审；`set_parameter` 捕
   IntegrityError → 重读赢家行收敛（feeds 惯例）；
3. `approve_proposal` 的 `applied=1` UPDATE 检查 rowcount，0 = 对手 replay
   已完成记账，log 收敛。

回归测试：`test_concurrent_proposal_replays_leave_one_history_row`
（门控第二个 admin_state 读直到第一个完整提交——复核给出的精确交错；断言
两次调用收敛到同一 history 行、该 proposal 恰一行、无 old==new 废行；修前
实测 2 行）。红灯验证在"临时移走 0037 + 移除事务内复用块"的修前形态下做出。

## R4 闭合（2026-07-21 复核 P1：0037 的 DELETE 会误删真实审计行）

R4 复核指出：0037 建索引前的 DELETE 对每个 proposal_id 无条件保留 MIN(id)
删其余，**假设所有后续重复都是 no-op**。但旧 replay 与人工改值可交错——同一
proposal 的第二次应用可能真实地把值从 X 改到 Y（如 0.7→0.75 之后人工设 0.8，
旧 bug 的 replay 又 CAS 0.8→0.75）：即便源自 bug，也是真实发生、必须留在
审计链上的状态转移；删掉它之后剩余历史的末态是错的。

修正（迁移是审计日志的旁观者，绝不猜测）：DELETE 收窄为只删**可证明的
no-op 回声**——`old_value = new_value`（回声的构造特征；SQL 的 NULL 永不
相等，含 NULL 的行一律保留）且存在同 proposal 更早的行。真实转移的重复行
原样保留，随后的 `CREATE UNIQUE INDEX` 对它们**响亮失败**：迁移文件整体
回滚、boot 报出失败迁移，由人工核对真实重复后处置——绝不静默改写历史。

新 DELETE 语句：

```sql
DELETE FROM parameter_history
WHERE proposal_id IS NOT NULL
  AND old_value = new_value
  AND EXISTS (
    SELECT 1 FROM parameter_history earlier
    WHERE earlier.proposal_id = parameter_history.proposal_id
      AND earlier.id < parameter_history.id
  );
```

live 影响：复核已核实 live 库 `parameter_history` 为空表——两条语句在 live
均为 no-op，首次生产应用干净落地（0037 尚未应用于任何库，本次是应用前修正）。

回归测试：`test_migration_0037_never_deletes_real_audit_transitions`
（内存库跑 0037 之前的完整迁移链构造真实表形；三组数据集：A 回声被剪、
真实首行保留、索引落地；B 复核反例——两条真实转移全部存活、索引
IntegrityError 响亮失败；C 含 NULL 的行永不视作相等、保留并失败。修前
红灯：数据集 B 的真实第二行被删、索引静默成功）。

## R5 闭合（2026-07-21 队列复核 P2：parameter history 与 effect 非原子）

### 问题

R5 报告 236–260 行指出：`set_parameter()` 先在事务中提交 admin_state +
parameter_history，事务外才调用 best-effort `_open_effect()`。若在两者之间
硬崩，参数与历史永久存在但 effect 丢失；随后三条收敛路径都不会自愈：

- 事务内看到 prior history 直接 return；
- `approve_proposal()` 更早看到 history，甚至不再调用 set_parameter；
- 0037 IntegrityError loser 重读 winner 后直接 return。

于是 proposal 仍能 `applied=1`，但其“每次 change freezes effect baseline”
测量审计永久缺口。更糟的是，若重放时才无标记地抓“当前”baseline，会把
当前口径冒充原始应用时刻。

### 修法：三件事实同事务提交

`set_parameter()` 的正常新写协议改为：

1. 写前调用 `_capture_parameter_effect()`，冻结**应用前** router metrics；
2. 该 capture 的 `baseline_at` 同时作为 parameter_history 的 `created_at`
   （一个逻辑应用时钟；baseline 中的 floor 因在写前读取，仍是 old floor）；
3. 在同一个 `db.transaction()` 中依次 CAS admin_state、INSERT
   parameter_history、通过 `_insert_parameter_effect()` INSERT
   operator_effects；effect 插入不是 best-effort，任何异常向外传播并让整个
   事务回滚——不存在“参数已改、effect 后补”的正常路径。

`rollback_parameter()` 同样切到该协议：rollback history + admin_state 回滚 +
应用前 effect baseline 同事务提交，删除事务后的 `_open_effect()` 窗口。
recipe promote/retire 仍走原 `_open_effect()`，不在本 finding 范围。

### 所有重放分支对齐双不变量

- fast prior：返回前 `_ensure_parameter_effect(prior)`；
- write-lock 内 prior：同事务查询 effect；存在才 return，缺失则退出后走明确
  legacy repair；
- 0037 IntegrityError winner：校验 key/new_value 一致后
  `_ensure_parameter_effect(winner)`，两项齐全才收敛返回；
- `approve_proposal()` 不再自己查 history 后跳过 set_parameter，而是始终让
  set_parameter 仲裁“history + effect”；只有它成功返回后才置 applied=1。

另补 legacy applied=1 修复入口：普通 double-approve 仍 409；但若状态已
approved+applied=1 且能证明 history 存在、effect 缺失，允许一次修复调用，
effect 到位后下一次立即恢复 409。

### legacy 缺 effect 的诚实降级语义

旧历史的原始 baseline 已不可恢复。`_ensure_parameter_effect()` 只能在当前
时刻捕获 baseline，因此：

- `baseline_at`/`created_at` 使用真实补录时刻，不伪造为 history.created_at；
- baseline JSON 持久写入：
  `"_baseline_capture": {"mode":"late_backfill",
  "application_at": <history.created_at>, "captured_at": <baseline_at>}`；
- backfill 失败向外传播，approve 不会借此返回/置 applied；
- 并发 backfill 在事务内重查 effect，0026 的 proposal 唯一索引作最终后盾。

### TDD 证据

四个测试在旧代码上同时红灯（真实输出 `4 failed, 74 deselected`），修后全绿：

- `test_parameter_effect_commits_before_legacy_post_commit_crash`：把旧的事务后
  `_open_effect` seam 替换为硬崩；新协议不再调用该 seam，断言一条 history +
  一条 effect 同时存在、baseline floor=0.7（写入前）、effect baseline_at /
  created_at 与 history created_at 相同；加入后续 telemetry 再 replay，effect
  整行 byte-for-byte 不变（不以当前 baseline 冒充原始口径）。
- `test_parameter_effect_insert_failure_rolls_back_change`：在事务内
  `INSERT operator_effects` 注入 OperationalError，断言 admin_state、
  parameter_history、operator_effects 三者全空。
- `test_legacy_missing_parameter_effect_backfills_with_marker`：构造旧
  applied=1 + history + 无 effect，显式 replay 后恰一条带 late_backfill 标记
  的 effect，baseline_at != 原 application_at；第二次 replay 回到 409。
- `test_concurrent_proposal_replays_leave_one_history_row`：R3 并发交错断言扩展
  为恰一 history **且恰一 effect**，二者应用时钟一致；不再 monkeypatch 掉
  `_open_effect`。
