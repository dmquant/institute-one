# PATCH-NOTES-B4 — 分区外事项

B4 分区内落地：`migrations/0011_whiteboard_similarity.sql`（新）、`app/institute/whiteboard.py`
（相似度门 + 多样性选题 + 板向量入库 + BUILD-ON 增补块）、`app/api/whiteboard.py`
（阈值/类目权重 GET/PUT + topic/board 可选 category）、`tests/test_whiteboard_similarity.py`（新，14 例）。
`vectors.py` / `prompts.py` 均未修改。以下是需要主代理知悉或代落的分区外事项。

## 1. vectors.py — 建议补一个公共模型名接口（非阻塞）

whiteboard 的板向量入库与门查询需要"当前 embedding 模型名"作为过滤键
（继承 A8 换模型即隐藏旧投影的语义）。vectors.py 只有私有 `_model()`，
B4 无权改该文件，现状是跨模块调用了私有名（两处，均在 `app/institute/whiteboard.py`）：

- `_store_board_vector()`：`vectors._model()` 写入 `whiteboard_topic_vectors.model`
- `_similarity_gate()`：`vectors._model()` 作为 SELECT 的 model 过滤参数

建议 A8 收编时在 `app/institute/vectors.py` 加一行公共别名（语义零变化）：

```python
def model_name() -> str:
    """Public alias: the embedding model tag stamped on stored vectors."""
    return _model()
```

然后把 whiteboard.py 里两处 `vectors._model()` 换成 `vectors.model_name()`。
不做也能跑（Python 私有只是约定），只是命名卫生问题。

## 2. prompts.py — B4 未触碰（说明）

任务预案提到"若组装点在 prompts.py 则写 PATCH-NOTES"。实际读代码后：白板卡片
prompt 的组装点在 `whiteboard._run_card`（`task_text` 是 whiteboard.py 自有字面量，
经 `build_analyst_prompt(context_blocks=...)` 拼装）。BUILD-ON 增补块作为**新常量**
`whiteboard.BUILD_ON_PRIOR_BLOCK` 定义，以 context block 形式插入——B4 对
`prompts.py` 零编辑，无分区外改动需求。注意（R-B4 审查澄清）：共享工作树中
`git diff -- app/institute/prompts.py` 并非空——那是 B3 分区新增的 `memory_block`，
不属于 B4；"零改动"指 B4 未写该文件，且既有 prompt 常量字节未变。

## 3. MCP `topic_pool_add` 绕过问题（Phase 0 P1，非 B4 范围）

`app/mcp.py` 的 `topic_pool_add` 仍是 raw-INSERT（Phase 0 已立案：绕过
`whiteboard.add_topic`，content hash 口径也不同）。修复它的代理请注意：
`add_topic()` 签名本轮已扩展为 `add_topic(topic, question, source, score, category=None)`，
直接透传即可；raw-INSERT 不受 0011 新列影响（显式列名，新列可空）。

## 4. 下游兼容性（无需行动）

- `POST /api/whiteboard/topics` / `POST /api/whiteboard/boards` 请求体新增**可选**
  `category` 字段，旧调用方（SPA / obsidian-plugin / MCP）不带该字段行为不变。
- 新端点 `GET|PUT /api/whiteboard/similarity-config`、`GET|PUT /api/whiteboard/category-weights`
  为纯增量。`GET /api/admin/state` 现在会多出一行 `whiteboard_similarity`（JSON 字符串），
  该端点本就是全量 dump，无消费者假设固定 key 集。
- `whiteboard_boards` 新增 `category`/`prior_board_id` 两列（可空）：`list_boards`/`get_board`
  的 `SELECT *` 响应会多出这两个字段，SPA/插件按 key 取值不受影响。
