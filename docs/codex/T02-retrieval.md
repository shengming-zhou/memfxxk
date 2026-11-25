# T02 · 混合检索与读取

依赖：T01。可与 T03 并行（T03 写 `memory_marks` / `memory_relations`，本任务只读这两张表；表结构 T00 已建好，测试里直接插行）。参考：`docs/architecture.md` §6.2、§6.3。

## 目标

`search` 和 `get` 可用：关键词 + 向量 + 重要项通道，RRF 融合，一跳关联展开，相邻轮次回读，每条结果带可解释的打分明细。

## 范围

### 1. 检索组件 `src/memfxxk/retrieval/`

- `LexicalIndex` 接口与 `PgTsLexical` 实现：查询分词 → `build_tsquery_literal(op="|")` → `WHERE lexemes @@ q` + `ts_rank_cd(lexemes, q, 32)` 排序，取 top `N_lexical`（默认 50）。查询分词为空时返回空。
- `VectorIndex` 接口与 `PgVectorIndex` 实现：查询文本经 `CachedEmbedder` 嵌入；`ORDER BY embedding::vector(dim) <=> :q` 取 top `N_vector`（默认 50），只查当前配置模型；返回相似度 `1 - distance`。查询时 `SET LOCAL hnsw.ef_search`（默认 100，可配）。
- 公共过滤（两路都要应用，**在 SQL 内**而不是召回后过滤，否则作用域外的候选会挤占名额）：`visible_filter`、`is_current`、`status='active'`（`include_inactive=true` 时放开）、`types`、`time_from/time_to`（按 `event_time`）、`session_id`。
  - 注意 HNSW 加 WHERE 过滤会导致召回不足：开启 pgvector 的 iterative scan（`SET LOCAL hnsw.iterative_scan = relaxed_order`，pgvector ≥0.8），并在测试里覆盖“目标在过滤后才进入 top N”的情况。
- important 通道：在 `memory_marks.mark='important'` 且标记对 ctx 生效（标记作用域按同一可见性规则）的记忆中，取向量相似度 top `M_important`（默认 10），保留 相似度 ≥ `important_min_sim`（默认 0.35）**或** 同时出现在 lexical 结果中的候选。
- `fuse()`：RRF，`score = w_lex/(k+rank_lex) + w_vec/(k+rank_vec)`，`k=60`，`w_lex=w_vec=1.0`（均可配）；important 通道的候选若不在任一路中，按其向量相似度在 vector 名次中的位置插值计算 rank（实现时写清楚规则并在 docstring 说明），再乘 `important_boost`（默认 1.5）。只有一路的候选照常计分。
- `Reranker` 接口，默认 `NoopReranker`。
- 关联展开：对最终 top k，查 `memory_relations` 两个方向一跳，过滤掉对 ctx 不可见、非 current、非 active 的端点，作为 `related: [{memory_id, purpose, direction, type, snippet, event_time}]` 附在结果上，每条最多 `max_related`（默认 3）。不参与排名，也不占 k。
- 结果最后一步再核验 `is_current AND status`（防止召回到融合之间发生更正/停用）。

### 2. `BoundService.search(SearchRequest) -> SearchResponse`

`SearchRequest`：`query, k=10, types?, time_from?, time_to?, session_id?, include_inactive=false, expand_relations=true, channels?（默认 ["lexical","vector","important"]，评测要能只开某一路）`。

`SearchHit`：`memory_id, version, type, title?, snippet（最多 500 字符，优先包含命中词附近）, content_truncated: bool, event_time, recorded_at, scope, session_id, source_pos, marks: [important|pinned], related[], score{lexical_rank?, lexical_score?, vector_rank?, vector_sim?, important: bool, rrf, boost, final}`。

`SearchResponse`：`hits, query_tokens, timings_ms{embed, lexical, vector, important, fuse, expand, total}`。

### 3. `BoundService.get(...)`

- `get_memory(memory_id, version=None, window=0)`：返回 `content` 全文、版本列表（`[{version, recorded_at, created_by}]`）、marks（对 ctx 生效的）、relations（可见端点）、`source_event_ids`。
- `window>0` 且类型为 `turn`：按 `source_pos.session_id` 和 seq 找同一会话前后各 `window` 个 turn 记录（按 `event_time`/seq 排序），一并返回为 `neighbors: {before: [...], after: [...]}`。
- 不可见或不存在 → `NotFound`（API 404）。

API：`POST /v1/search`，`GET /v1/memories/{id}?version=&window=`。

## 不做

写入标记/关联（T03）、上下文组装（T04）、分词策略调优（T06 之后再说）。

## 验收标准

用 fake embedder + 固定的中英混合小语料（放 `tests/fixtures/`，约 30 轮对话，覆盖 Python 版本迁移、CI 失败、发布约定、无关闲聊）：

- 纯中文查询、纯英文查询、中英混合查询、代码标识符查询（如 `parseHTTPResponse`、`test_upload.py`）各至少 1 个用例能在 top 3 命中预期记录。
- `channels=["lexical"]` / `["vector"]` 单路可用，结果的 score 明细只含对应字段。
- 作用域：同一语料在 project p1 和 p2 各写一份，p1 的 ctx 搜索结果全部来自 p1；user 级 note 在 p1、p2 都能搜到；作用域外候选不挤占名额（构造“p2 有 100 条高度相关、p1 只有 1 条”的情况，p1 搜索仍返回那 1 条）。
- 过滤后召回：对向量通道，构造只有在 `types`/`session_id` 过滤后才能进入 top N 的目标，断言能召回。
- important：一条与查询相关但排名靠后的记录被标为 important 后排名上升；一条与查询无关的 important 记录不会出现在结果里；标记作用域为 p1 时，在 p2 不生效。
- 关联：A 命中时带回关联的 B；B 对 ctx 不可见时不带回；B 被停用时不带回。
- 停用记录默认不出现，`include_inactive=true` 时出现。
- `get_memory(window=2)` 返回正确的前后各 2 轮；跨会话边界不越界。
- 不可见的 memory_id → 404，与不存在的 ID 响应体一致。
- 性能冒烟：插入 2 万条 turn 记录（fake 向量），search 的 `timings_ms.total`（不含 embed）p95 < 150ms（在测试里标 `@pytest.mark.slow`，默认不跑，CI 外手动跑）。
