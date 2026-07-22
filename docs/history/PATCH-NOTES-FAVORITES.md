# PATCH-NOTES-FAVORITES — Phase 7 Favorites & visualizations

## 主控需要挂载的后端 router

`app/main.py` 的 `.api` import tuple 中加入：

```python
favorites as api_favorites,
```

router tuple 中加入：

```python
api_favorites.router,
```

本补丁按边界未修改 `app/main.py`。挂载后提供
`GET/POST /api/favorites` 与 `DELETE /api/favorites/{ref_kind}/{ref_id}`。

## 主控需要挂载的前端页面

`frontend/src/App.tsx` 顶部加入：

```tsx
import Insights from "./pages/Insights";
```

`NAV` 中加入：

```tsx
{ to: "/insights", zh: "洞察", en: "Insights" },
```

`Routes` 中加入：

```tsx
<Route path="/insights" element={<Insights />} />
```

本补丁按边界未修改 `frontend/src/App.tsx`。

## 本次实现

- `migrations/0031_favorites.sql`：异构收藏表，`(ref_kind, ref_id)` 唯一。
- favorites domain/API：幂等 add、幂等 remove、按 kind 过滤；LEFT JOIN
  research、whiteboard、workflow run、thesis、forecast、chain entity 和
  research tree，返回 `title` / `status` 展示字段。
- `Insights.tsx`：收藏清单及三个零依赖图表：
  - 近 30 天 events 按类型堆叠条形图（游标分页读取 `/api/events`）。
  - 最近 500 个终态 tasks 按 hand 的完成成功率横条图。
  - 近 30 天 research 完成趋势 SVG 条形/折线图。
- `Trees.tsx`、`Forecasts.tsx`：列表项 star 收藏/取消收藏示范。

## 验证

- `.venv/bin/python -m pytest tests/test_favorites.py -q` → `2 passed`
- `.venv/bin/python -m pytest tests/test_db_migrate.py -q` → `19 passed`
- `.venv/bin/python -m compileall app -q` → passed
- `cd frontend && npx tsc -noEmit` → passed
