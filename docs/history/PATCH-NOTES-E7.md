# PATCH-NOTES-E7 — 相似度门合成校准 + recipes 最小自改进环（两个 ◔ 验收缺口）

第五轮 E7 分区交付（基线 764 passed / 10 skipped）。对应 ROUND4-AUDIT-S4 §4.2 的两行：
Phase 1a「similarity gate 缺 50+ pair sanity」与 Phase 6「recipes 只有 schema 占位」。

## 1. 相似度门合成校准（Phase 1a 验收缺口 → 可复现基建）

本机无 Ollama，真实 bge-m3 校准做不了。交付的是**合成等价物 + 真实校准的现成台架**：

- `tests/test_similarity_calibration.py`（新）——
  - **60 对已知关系的中文金融 topic 对**（手写语料，三档各 20：同义改写 / 主题相关 / 无关），满足 ROADMAP 1a「~50 known pairs」的验收单位；
  - **确定性代理 embedder**：字符 n-gram（中文 uni+bigram、ASCII 整词）经 `zlib.crc32` 哈希入 4096 维词袋 + 余弦。它是 bge-m3 的**结构代理**——保留三档之间的分离与排序，不复现真实模型的余弦绝对刻度；
  - 用**真实门代码** `whiteboard._classify_prior` 跑 相似度×板龄 阈值矩阵（近期板 / 窗口间板龄 / 双窗外陈旧板三个切面），断言三档分类分布（同义→skip 率高、无关→pass 率高、相关→不 skip 且多 augment），并把分布表打进测试日志（`-rP`/`-s` 可见）；
  - 断言线由**实测分布**回定（非拍脑袋）：代理刻度下 skip=0.52 落在 related.max(0.508) 与 paraphrase.min(0.529) 之间，augment=0.16 落在 unrelated.max(0.135) 与 related.p25(0.190) 之间——与生产 0.85/0.65 在真实刻度上的**结构位置**同构。

  实测分布表（合成，代理 embedder）：

  | tier | n | min | p25 | med | p75 | max | skip | augment | pass |
  |---|---|---|---|---|---|---|---|---|---|
  | paraphrase | 20 | 0.529 | 0.688 | 0.736 | 0.808 | 0.849 | **20** | 0 | 0 |
  | related | 20 | 0.072 | 0.190 | 0.215 | 0.298 | 0.508 | 0 | **15** | 5 |
  | unrelated | 20 | 0.000 | 0.000 | 0.043 | 0.053 | 0.135 | 0 | 0 | **20** |

- **真实校准路径（Ollama 就绪后跑同一测试换真 embedder）**：
  `INSTITUTE_ENABLE_VECTORS=1 INSTITUTE_CALIBRATION_REAL=1 pytest tests/test_similarity_calibration.py -q -rP`
  → `test_real_bge_m3_calibration`（默认 skip）用同一 60 对语料 + 真实 `vectors.embed`，在**生产阈值 0.85/0.65** 下打印分布表和建议切点（related.max↔paraphrase.p25、unrelated.max↔related.p25 的中点），人工经 `PUT /api/whiteboard/similarity-config` 调 admin_state 行。Ollama 不可达时该测试 skip 不 fail。

- `app/institute/whiteboard.py`（**仅注释**，SIMILARITY_DEFAULTS 上方）：写明 0.85/0.65 出处是 proposal §6.3 而非实测校准、合成 sanity 的位置、真实校准的命令与生效路径。

## 2. recipes 最小自改进环（Phase 6 ◔→可用）

**本次只做 recipe 复用最小环**：人工批准的处置 → 提炼为 recipe → 同型 action 复发时零模型调用直接建议 → 仍走同一人工批准门。ROADMAP Phase 6 L 项的完整链 **observations / proposals / parameter history / effect measurement 全部留后续卡**，本轮不做。

- `migrations/0023_recipes_minimal_loop.sql` —— 给 0018 的占位 `recipes` 表 ADD COLUMN：`kind / keywords / confidence / source_disposition_id / status / retired_at`；给 `action_dispositions` 加 `recipe_id`（非 NULL = recipe 命中产生，非模型）。新列**刻意不带 CHECK/REFERENCES**（S4-P0-01：`_skip_add_column` 崩溃恢复守卫只比较 type/NOT NULL/DEFAULT，无约束声明保持恢复路径可证明；枚举/引用完整性在代码层）。`uq_recipes_source_disposition` 部分唯一索引 = promote 幂等兜底（0018/0022 同款收敛语义）。
- `app/institute/operator.py`：
  - `promote_disposition_to_recipe(disposition_id)` —— 只接受 flags 含 `approved` 的 disposition（该 flag 只有 web UI approve 端点写 → **人工门延伸到 recipe 知识**）；`unparsed` 不可提炼；pattern = kind + title 关键词（`_title_keywords`：ASCII 词 ≥2 字符、中文串 ≥2 字、去重、cap 6；实例 id/纯数字/单字母永不成词）；**关键词提不出时 fail closed 拒绝**（ALL-match 语义下空关键词会全匹配同 kind，过宽不算知识）；confidence 继承；幂等（重复 promote 返回已有行）。
  - `route_actions` 组 prompt 前先 `_match_recipe`（同 kind 且全部关键词命中折行 casefold 后的 title；多命中取 confidence 最高、再取最新）——命中则**直接落 disposition 行：`recipe_id` 标记、disposition/confidence 继承、零 executor.submit、零 tasks 行、仍 shadow=1**；flags 照常按 live floor + human-pin 规则算；仍占 propose-once-per-loop 槽位（0022 索引语义不变）。返回摘要新增 `recipe_hits`。未命中才走模型，四条铁律全部不变。
  - `list_recipes(status)` / `retire_recipe(id)`（条件认领：仅 active 可 retire）。
- `app/api/operator.py`：`GET /api/operator/recipes`（可按 status 过滤）、`POST /api/operator/dispositions/{id}/promote-recipe`（人工提炼入口，幂等；未批准/不可提炼 409，未知 404）、`POST /api/operator/recipes/{id}/retire`（重复 409、未知 404）。
- proposed_by 沿用 `fast_loop`/`deep_loop`（0018 CHECK 生产不可改）；recipe 建议与模型建议在同一 (action, loop) 槽内互斥，不会双写。

## 3. 测试

- `tests/test_similarity_calibration.py`（新，7 个）：语料形状（50+ 对、无重复）、代理 embedder 确定性、三档分布分离、近期板矩阵（分布表输出）、陈旧板全 pass、窗口间板龄 skip 退化为 augment、真实校准台架（默认 skip）。
- `tests/test_operator.py` 追加 8 个：关键词提取边界；promote 需人工批准（未批准 ValueError/409）；promote 幂等与行形状；**recipe 命中零模型调用**（tasks 行数不增、recipe_id/继承 confidence/shadow=1、未命中 action 仍走模型、recipe 建议仍需人工 approve 才收敛）；kind/关键词不满足不命中；retired 不再命中；API 全回路（GET 过滤/promote/retire/404/409/422）；unparsed 与空关键词 fail closed。

## 4. 验证

- `.venv/bin/python -m compileall app -q` exit 0。
- 定向：`tests/test_operator.py tests/test_similarity_calibration.py tests/test_db_migrate.py` → 67 passed / 1 skipped（skip = 真实校准台架，预期）。
- 全量（跑于 2026-07-20，与第五轮其他并行分区共库）：`.venv/bin/python -m pytest tests -q -rs` → **804 passed / 4 skipped，零失败**。相对基线 764/10 的增量含并行分区落盘；E7 自身贡献 +15 passed（7 校准 + 8 recipe）+1 skip（INSTITUTE_CALIBRATION_REAL 台架）。剩余 4 个 skip：net smoke ×1、real bundle ×2、真实校准台架 ×1。

## 5. 边界与遗留

- 分区纪律：未动 main.py / scheduler.py / 前端（E4 并行做 operator 前端）/ git / launchd；8100 生产进程未触碰。
- 合成校准回答的是「门代码在已知三档语料上的行为是否合理」，**不回答** bge-m3 刻度下 0.85/0.65 是否最优——那正是留给真实台架的问题（命令见上）。
- recipe 匹配是保守 AND 语义：宁可 miss 走模型，不误命中；关键词来自 title 而非 detail（detail 是不可信 payload，且折行/引号化管道只为 prompt 设计）。
- retire 是当前唯一的 recipe 治理手段；命中率/效果度量（用了 recipe 的 action 后续是否真被 resolve）属于 effect measurement 后续卡。
- `action_dispositions.recipe_id` 无 FK（0023 恢复守卫约束，见上）；recipe 行只增不删，retire 保留审计痕迹。
