# PATCH-NOTES-PLUGIN-ALIGN — Obsidian 私人线能力对齐

本轮只改 `obsidian-plugin/src/api.ts`、`obsidian-plugin/src/dashboard.ts`，并新增本说明。
未改后端、`main.ts`、`roadmap.ts`、`modals.ts`，未重建 `main.js`。

## 数据源与口径

### 1. Operator 收件箱

- 路由：`GET /api/operator/actions?status=open&limit=1000`
- 后端的待处理状态实际为 `open`；`pending` 不在 `ActionStatus` 枚举中，会返回 422。
- 接口的 `count` 是本页行数而非独立总数，因此以允许的最大页长 1000 拉取，展示待裁决数和优先级排序后的前 5 条标题；达到上限时显示 `1000+`。
- 点击标题会提示裁决只在 Web 操作台进行，并打开 SPA `/operator`；插件不调用任何裁决写端点。

### 2. 预测账本速览

- 列表：`GET /api/forecasts?limit=5`（后端按 `made_at DESC` 返回最近记录）。
- 详情：对已结算/无效记录调用 `GET /api/forecasts/{id}`。列表响应不含 settlement，详情响应才有 verdict。
- 后端没有 forecast stats 端点，因此插件聚合这 5 条中的可评估 verdict：`hit / (hit + miss + partial)`；`invalid` 不进入分母，并单独提示部分命中数。
- 区块展示最近 5 条的方向、论断、日期和结算/待结算状态。

### 3. 研究树监控

- 活跃列表：`GET /api/research/trees?status=pending&limit=200` 与
  `GET /api/research/trees?status=exploring&limit=200`
- 无活跃树时的回落列表：`GET /api/research/trees?limit=1`
- 详情：`GET /api/research/tree/{tree_id}`
- `pending` 与 `exploring` 计为活跃树；分状态查询避免大量历史终态树挤掉仍活跃的旧树。
  优先监控最新活跃树，没有活跃树时回落到最新一棵。
- 详情的节点按 `finished_at ?? created_at` 选出最近发生状态变化的节点，展示主题与
  `pending/running/completed/failed/pruned` 中文状态；同时展示树的完成节点进度。

## 插件改动

- `obsidian-plugin/src/api.ts`
  - 新增 Operator action、forecast/settlement、research tree/node 的 typed payload。
  - 新增 `operatorActions()`、`forecasts()`、`forecast()`、`researchTrees()`、
    `researchTree()` helper；全部复用现有 `request()`，因此沿用 bearer token 与超时处理。
- `obsidian-plugin/src/dashboard.ts`
  - 新增三个原生 `<details>` 折叠区块，刷新时保留用户的展开状态。
  - 三块都挂进 dashboard 现有 `refresh()`，轮询节奏继续使用 `pollIntervalS`。
  - 404/405/501 按既有可选能力模式隐藏区块；其他错误只在对应区块显示，不影响其余面板。

## 验证

```text
cd obsidian-plugin && npx tsc -noEmit -skipLibCheck
```

结果：通过（exit 0）。按边界未运行 `npm run build`。
