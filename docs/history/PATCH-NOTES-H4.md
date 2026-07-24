# PATCH-NOTES-H4 — M8-011 bilingual twins 集成清单

H4 独占分区没有 `app/main.py` 和 SPA，因此以下两项留给主代理集成。

## 1. 在 `app/main.py` 挂载读 API

`create_app()` 的 `.api` 批量导入中增加：

```python
from .api import bilingual as api_bilingual
```

随后在 `for r in (...)` 的 API router 列表中增加：

```python
api_bilingual.router,
```

挂载后提供：

- `GET /api/bilingual/twins/{document_id}?locale=zh|en`
- `GET /api/bilingual/twins/by-path?path=<vault-relative-or-source-path>&locale=zh|en`
- `GET /api/bilingual/coverage`
- `GET /api/bilingual/failures?permanent_only=true|false`
- `GET /api/bilingual/preference`
- `PUT /api/bilingual/preference {"locale":"zh"|"en"}`

不传 `locale` 时使用 `admin_state["bilingual:locale"]`，缺失或损坏时默认 `zh`。

## 2. SPA 留档（本卡按 H4 分区约束未实现）

在 briefing/daily 产品读取处接入 preference GET/PUT，并将一次性的页面 locale
选择作为 twins GET 的 `?locale=` 覆盖。建议设置页仅提供 `zh` / `en` 两态，不把
`bilingual:enabled`（是否烧翻译配额）和 `bilingual:locale`（默认读哪种语言）合并
成一个开关。

## 3. 重试集成说明

无需新增 scheduler/main 生命周期注册：现有 `bilingual.register()` 仍订阅
`workflow.completed`。每次受支持的 briefing/daily 完成周期会先调用
`retry_failed_twins()` 重拾旧的 `failed` / 重启遗留 `translating` 记录，再处理
本次 run；每个周期每个失败 run 最多尝试一次，总上限 3。状态存于
`admin_state["bilingual:twin:<run_id>:en"]`，所以重启后仍有效，且 ready 重放不会
重复提交模型任务或重复发送 ready 事件。

本实现未新增迁移。
