# PATCH-NOTES-H3 — M8-009 研究树产品面

## P2 per-tree SSE 端点：跳过

现有全局事件面已经覆盖 viewer 的终态数据：`/api/events/stream?types=tree.` 负责低延迟唤醒，`/api/events?since=&types=tree.` 负责耐久补数；`tree.node_completed`、`tree.node_retried`、`tree.completed` 都以 `ref_id=tree_id` 发出。`Trees.tsx` 通过现成 `useSSE` 订阅 `tree.`，再按当前 `treeId` 在客户端过滤唤醒。引擎没有 `pending -> running` 事件，因此详情页另以 5 秒 GET 轮询补齐这一中间态。

新增 `/api/research/tree/{id}/events` 会重复现有 durable cursor/SSE 语义，而现成 `useSSE.ts` 固定消费全局端点；接入专用端点还需修改本卡只读分区。当前方案没有实时性缺口，因此不新增 P2 端点。

## 分区外改动

无。路由与现有 `/trees/:treeId` 入口已存在，不需要修改 `app/main.py`、`frontend/src/App.tsx`、`useSSE.ts` 或 `events.tsx`。
