# PATCH-NOTES-A3 — 分区外跟进改动（由集成者执行）

A3 分区改动：`app/institute/analyst_daily.py`、`app/institute/analysts.py`、`tests/test_analyst_daily.py`、`tests/test_analysts.py`。
以下文件不在 A3 授权分区内，但内容已过时，需要同步：

## 1. CLAUDE.md — 两条 Gotcha + 一条 Recipe 过时

- Gotcha「The roster is `lru_cache`d — CRUD reloads it, manual JSON edits don't.」
  → roster 缓存已改为 mtime 检查：手工编辑 `catalog/analysts.json` 在下次读取时自动生效，无需重启。建议改为：
  「The roster cache is mtime-checked — manual edits to `catalog/analysts.json` are picked up on the next read; CRUD still calls `reload()` explicitly.」
- Gotcha「`analyst_daily` guard lives in `admin_state` key `analyst_daily:<date>`」
  → guard 现在是每分析师一行：`analyst_daily:<date>:<analyst_id>`（value 为 JSON 字符串状态）。旧的单 blob key `analyst_daily:<date>` 仍被 `_get_record()` 兼容合并读取（per-analyst 行优先），但不再写入。
- Recipe「New analyst」中「a manual edit needs restart or `analysts.reload()`」→ 同第一条，手工编辑自动生效。

## 2. ROADMAP.md — Phase 0 勾选状态

- 「P2 · `analyst_daily._mark` lost-update race」已完成（方案 a：per-analyst keys）。
- 「P3 · Roster `lru_cache` ignores manual JSON edits」已完成（mtime-checked cache）。

## 3. roadmap/backlog.json — 卡片映射（CLAUDE.md 规则 9）

backlog.json 中未找到对应这两项 Phase 0 issue 的既有卡片（现有卡片属 local-thesis-alpha 里程碑）。若集成者维护 Phase 0 卡片，请补记完成状态；无则忽略。

## 4. 升级说明（无需迁移，运维可选清理）

- 无 schema 迁移：仍用 `admin_state` 表，只是 key 粒度变细。
- 升级当天的旧 blob（`analyst_daily:<日期>`）会被继续兼容读取，跨天后成为死数据，可留可删（`DELETE FROM admin_state WHERE key = 'analyst_daily:<旧日期>'`，不删无副作用）。
