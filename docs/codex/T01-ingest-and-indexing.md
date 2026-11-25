# T01 · 事件写入、turn 记录与索引作业

依赖：T00。参考：`docs/architecture.md` §5（events、records、record_embeddings、embedding_cache、jobs、sessions）、§6.1、§6.6。

## 目标

事件能幂等写入，同步生成可关键词检索的 turn 记录，后台 worker 生成嵌入。

## 范围

### 1. 文本处理 `src/memfxxk/text/`

- `Tokenizer` 接口：`tokenize(text) -> list[str]`。默认实现 `MixedTokenizer`：
  - 全部小写；Unicode NFKC 归一化。
  - 中文（及其他 CJK）用 jieba `cut_for_search`。
  - 英文/数字按词切分；**代码标识符**同时保留原形和拆分形：`parseHTTPResponse` → `parsehttpresponse, parse, http, response`；`snake_case_name`、`kebab-case`、`a.b.c`、`path/to/file.py` 同理（文件名保留 `file.py` 与 `file`、`py`）。
  - 丢弃纯标点；不做英文词干化（留给评测决定）；停用词表默认为空。
  - jieba 在模块首次使用时懒加载并关闭其日志输出。
- `build_tsvector_literal(tokens) -> str`：构造带位置的 tsvector 字面量（`'tok':1,5 'tok2':2`），正确转义单引号和反斜杠，位置上限 16383，单个 lexeme 超长（>2046 字节）时截断。**不要**用 `to_tsvector`，避免 PG 解析器二次切分。
- `build_tsquery_literal(tokens, op="|") -> str`：同样转义；空输入返回 None。
- `TokenCounter` 接口：`count(text) -> int`；实现 `TiktokenCounter`（o200k_base）与 `SimpleCounter`（CJK 字符按 1、其他按空白分词数 ×1.3 取整，用于测试，确定性）。按配置选择。

### 2. 嵌入 `src/memfxxk/embedding/`

- `EmbeddingProvider` 接口：`model: str`, `dim: int`, `embed(texts: list[str]) -> list[list[float]]`。
- `FakeEmbeddingProvider`：基于 token 哈希的确定性向量（把 tokenizer 结果哈希到 dim 维再 L2 归一化），使“共享词越多越相似”在测试里成立。
- `OpenAICompatibleProvider`：httpx 调 `POST {base_url}/embeddings`，批大小可配（默认 64），超时与重试（429/5xx 指数退避）。输入超长时按 token counter 截断到可配上限（默认 8000 token）。
- `CachedEmbedder`：先查 `embedding_cache(model, sha256(text))`，只对未命中的调用 Provider，再写回缓存。
- CLI：`memfxxk index ensure` —— 按当前配置的 model/dim 创建部分表达式 HNSW 索引（若不存在），索引名包含模型名的安全化形式；`memfxxk index reembed [--model]` —— 为所有 current 记录登记嵌入作业。

### 3. 作业队列 `src/memfxxk/jobs/`

- `enqueue(conn, kind, payload, dedupe_key=None, run_after=None)`，在调用方事务内插入；`dedupe_key` 冲突时若已有作业仍为 `queued` 则忽略，否则重新排队。
- `claim(conn, worker_id, kinds, limit)`：`FOR UPDATE SKIP LOCKED`，置 `running`。
- 完成/失败：失败 `attempts+1`，指数退避（基数 2s，上限 10min），超过 `max_attempts`（默认 5）置 `dead` 并记录 `last_error`。超时未心跳的 `running` 作业（默认 10min）可被回收。
- `Worker`：循环 claim → dispatch 到注册的 handler；`memfxxk worker` CLI 命令；配置 `worker.embedded=true` 时 API 启动后台线程。
- `drain(service, kinds=None, max_rounds=…)`：同步跑完队列，供测试和评测使用。
- Handler `embed_record(payload: {record_id, generation})`：读记录 → 若不存在、generation 不一致或 `status!='active'` 则直接完成（丢弃）→ 用 `CachedEmbedder` 嵌入 → upsert `record_embeddings`。实现批处理：一次 claim 多个 `embed_record` 作业合并成一次 Provider 调用。

### 4. 写入 `src/memfxxk/ingest/` 与 `BoundService.ingest`

请求模型 `IngestEvent`：`kind, role?, content?, payload?, session_id?, turn_id?, event_time?（缺省 now）, source?, idempotency_key?, scope_drop?`。`ingest(events: list[IngestEvent]) -> list[IngestResult{event_id, session_seq, duplicate: bool, memory_ids_touched}]`。

同一事务内：
1. 作用域：`resolve_write_scope(ctx, scope_drop)`。
2. 幂等：`idempotency_key` 已存在 → 返回既有事件，`duplicate=true`，不重复处理。
3. 会话：`session_id` 不存在则以写入作用域创建 `sessions` 行；已存在但作用域对 ctx 不可见 → 404。`SELECT … FOR UPDATE` 分配连续 `session_seq`。
4. 插入事件，`actor` 来自 ctx。
5. 对每个涉及的 `(session_id, turn_id)`（无 turn_id 的事件以 `event_id` 作为 turn_id）重建 turn 记录：
   - `memory_id = derived_id("turn", org_id, session_id, turn_id)`；`type='turn'`，`origin='auto_index'`，`version=1`，`is_current=true`。
   - 内容按 `session_seq` 拼接该轮全部 message/tool 事件：`[user] …`、`[assistant] …`、`[tool_call name] {args 截断}`、`[tool_result name] {输出截断}`（每段默认 2000 字符，保留头 1500 尾 500，中间标注省略字数）。
   - `event_time` = 该轮最早事件时间；`source_event_ids`、`source_pos = {session_id, seq_from, seq_to}`。
   - 计算 `lexemes`；已存在则就地更新并 `generation+1`。
   - 登记 `embed_record` 作业，`dedupe_key = f"embed:{record_id}"`。
6. 不属于会话的事件（如 `document_import`，无 session_id）只入事件表，不建 turn 记录（文档 chunk 是 M2 的事）。

API：`POST /v1/events`（body `{events:[…]}`），`GET /v1/events/{id}`，`GET /v1/sessions/{id}/events?from_seq&to_seq&limit`。不可见 → 404。被 redacted 的事件 content 返回 null。

## 不做

检索（T02）、用户管理（T03）、会话 state 读写接口（T04）。

## 验收标准

- tokenizer 单测：中文句子、中英混排（“把 parseHTTPResponse 改成 async 版本”）、文件路径、版本号（`python 3.12`）、含单引号/反斜杠的文本都能生成合法 tsvector 字面量（真实 PG `::tsvector` 转换不报错），且查询 `@@` 能命中。
- 幂等：同一 `idempotency_key` 写两次 → 一条事件，第二次 `duplicate=true`。
- 并发：两个线程同时向同一会话各写 50 个事件 → `session_seq` 连续无重复。
- 一轮的 user/tool/assistant 事件分两次 ingest（共享 turn_id）→ 只有一条 turn 记录，内容包含两次的事件，`generation` 递增，嵌入作业被重新登记且只保留一个 queued。
- 长工具输出被截断，完整内容可从事件读回。
- `drain()` 后每条 turn 记录都有嵌入；第二次对相同内容嵌入命中缓存（fake provider 计数器验证没有被调用）。
- 作业写回竞态：登记作业后修改记录（generation+1）或删除记录，再运行旧作业 → 不写入陈旧嵌入、不报错。
- 失败重试：Provider 抛错 → attempts 增加、`run_after` 后移，超过上限进 `dead`。
- 作用域：user A 写入的会话，user B 的 ctx 读 `/v1/sessions/{id}/events` 返回 404；B 往 A 的 session_id 写事件 → 404。
- `memfxxk index ensure` 幂等；建好索引后用 `EXPLAIN` 证明按模型过滤的近邻查询能用上 HNSW（测试里先 `SET LOCAL enable_seqscan = off`，再断言计划中出现索引名即可；查询表达式必须与索引表达式 `embedding::vector(dim)` 完全一致，T02 的向量查询复用同一个 SQL 构造函数）。
