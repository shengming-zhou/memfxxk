# memfxxk 开发手册 {#memfxxk}

开发任务的依赖见 [路线图](https://github.com/shengming-zhou/memfxxk/blob/main/docs/roadmap.md)，接口、作用域与数据契约见 [架构](https://github.com/shengming-zhou/memfxxk/blob/main/docs/architecture.md)。运行服务和获取 key 的步骤见 [用户手册](https://shengming-zhou.github.io/memfxxk/user-manual/)。

<div id="_1" class="legacy-anchor" aria-hidden="true"></div>

## 本地开发与测试 {#local-development}

```sh
docker compose up -d --wait db
uv sync --locked
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Compose 在同一 PostgreSQL 17 实例中创建 `memfxxk` 和 `memfxxk_test`，两库都启用 pgvector。测试库可由 `MEMFXXK_TEST_DATABASE_URL` 指定。集成测试在独立临时 schema 里执行迁移，测试后清理业务表。只跑单元测试时用 `uv run pytest -m "not integration"`。

<div id="_2" class="legacy-anchor" aria-hidden="true"></div>

## 服务边界 {#service-boundaries}

- HTTP 层解析 Bearer key 和作用域头，业务方法统一走 `MemoryService.for_request(principal, ctx)` 返回的 `BoundService`。
- `org_id` 来自 key。调用方可用 `X-Memfxxk-User`、`X-Memfxxk-Agent` 和 `X-Memfxxk-Project` 指定作用域，`X-Memfxxk-Actor` 接受 `{"type":"user","id":"u1"}` 这类 JSON 对象。
- 使用 SQLAlchemy Core 和 Alembic。作用域查询使用 `visible_filter`，不可见和不存在统一返回 404。

T00 已通过 18 项测试，包括真实数据库的迁移升降级、作用域矩阵和鉴权。Starlette TestClient 当前会报告一条关于 httpx 的弃用提示。

<div id="_3" class="legacy-anchor" aria-hidden="true"></div>

## 事件写入和索引 {#ingest-and-indexing}

- `BoundService.ingest` 在同一事务内检查作用域和重试键、分配会话序号、插入原文、重建 turn 并登记嵌入作业。跨会话批量写入按固定顺序取得事务锁，降低交叉批次死锁的风险。
- 会话内事件的作用域可以比会话更窄。一个 turn 要求所有源事件作用域一致，确保派生索引沿用原文的可见范围。可见 turn 的作用域冲突返回 409。调用者权限之外的会话或事件返回 404。
- `memfxxk.text` 负责分词和 PostgreSQL 字面量，`memfxxk.embedding` 提供嵌入 Provider、缓存和索引表达式。混合检索复用 `embedding_expression` 和 `embedding_model_filter`。
- Worker 用 `FOR UPDATE SKIP LOCKED` 领取作业。写回前锁定记录并校验 generation 和作业租约。更新 turn 时，旧向量与原文变更在同一事务内失效。生产路径和测试共用 `jobs.drain` 所调用的作业处理逻辑。

T01 验收通过 45 项测试，覆盖并发序号、重复事件、跨作用域隔离、中文和代码标识符分词、缓存、作业重试和过期写回，以及真实 PostgreSQL 的 HNSW 查询计划。下一项是 [T02 混合检索](https://github.com/shengming-zhou/memfxxk/blob/main/docs/codex/T02-retrieval.md)，可按路线图与 T03 并行。

## M1 实现与验收 {#m1-implementation}

- T02–T03 将关键词、向量和重要标记合并排序。用户可手动保存、标记、关联、修订或停用记忆。
- T04–T05 将文档、会话和上下文通过同步及异步 Python SDK、CLI 和 MCP 暴露给宿主。HTTP 和评测适配器复用同一工具定义与输出限制。
- T06–T08 提供 LongMemEval 检索与问答评测、A01–A13 回归和开发 Agent 演示。运行 `uv run pytest -m slow` 检查较大测试语料的检索延时。

M1 关口还需要全量 LongMemEval_S 的检索、真实模型问答和约 25 万条 turn 上的 p95，以及用真实开发 Agent 接入 MCP 一周的记录。M2 原型与这些验收并行推进。

## M2 的接入与验证 {#m2-harness-and-verification}

- 升级数据库用 `uv run alembic upgrade head`。服务按 `max_tokens` 限制完整记忆文本，同时保留各区限额。参考 Harness 在 HTTP 请求前另算整轮输入和回复余量，在每次工具返回后重算。
- MCP 默认提供模型侧记忆工具。给宿主进程设置 `MEMFXXK_MCP_ENABLE_HOST=true` 后，可直接调用 `events_append`、`session_state`、`session_events`、`context_build` 和两步压缩工具。仅把宿主工具接给 Harness。管理侧可配置 `MEMFXXK_MCP_ENABLE_MANAGE=true`，并使用 `memory-use-v1` 提示词。
- 不配置模型时，压缩按重要度和最近性保留原文片段。可通过 `MEMFXXK_M2__MODEL__BASE_URL`、`MEMFXXK_M2__MODEL__MODEL` 和 `MEMFXXK_M2__MODEL__API_KEY` 接模型做摘要。开启 `MEMFXXK_M2__EXTRACT_FACTS=true` 后，worker 从已结束轮次提议事实并核对原文、作业租约和记录代次。事实经过审核才生效。草案保留时间默认 24 小时。
- 检索返回分阶段延时，context 返回使用量和被舍弃的引用，worker 日志记录事实提取用量。回放、删除、作用域和图对照可用以下命令复验。图方法与实验边界见 [图检索试验](/decisions/0001-m2-graph-pilot)。

```bash
uv run pytest -s tests/integration/test_m2_memory.py tests/integration/test_m2_benchmark.py tests/integration/test_cli_mcp.py
```

512 token 的 LongMemEval S 回放使用四道知识更新题和四道时间推理题，每题原文约 9.8–10.3 万 token，按简单计数器估算。纯截尾和本地滚动摘要都保留了 0/8 题的完整标注消息。用关键词和 hash 向量按问题检索后，前八条候选覆盖 4/8 题。组装后，完整条目和查询相关片段都保留了 3/8 题的至少一条标注消息。回放关闭了模型答题，统计的是证据保留。 [LongMemEval](https://github.com/xiaowu0162/LongMemEval)

```bash
uv run pytest -s -m slow tests/integration/test_m2_long_replay.py
```

## 重跑基线 {#run-evaluation}

使用独立 PostgreSQL 数据库。在 `.env.example` 查看嵌入、生成和评分的配置。检索跑分记录数据 SHA、模型、各类问题的指标和延时。QA 还记录各组 token、工具调用与模型延时。

仓库保留了官方 Oracle 检索、固定 50 题的确定性向量证据横评、六题本地 BGE 语义横评，以及两题假模型的 QA 烟测。 [M1 评测产物](https://github.com/shengming-zhou/memfxxk/tree/main/eval/results/m1-smoke)

### 固定题集的证据横评 {#evidence-comparison}

在独立评测库完成迁移后，按固定清单导入 LongMemEval_S 的 50 道题。命令校验数据 SHA-256，再按清单顺序处理。最近 turn、memfxxk 的关键词/向量/混合检索和可选的 Mem0 OSS 使用同一时间截止点。

```sh
uv run --locked memfxxk eval longmemeval fetch --dataset s
uv run --locked memfxxk eval longmemeval ingest --dataset s --suite eval/suites/longmemeval-s-smoke.json
uv run --locked memfxxk index ensure
uv run --locked --with mem0ai==2.2.1 memfxxk eval longmemeval evidence --suite eval/suites/longmemeval-s-smoke.json --mem0
```

证据评测记录 session 和 turn 的 Recall@5/10/20，查询 p50/p95/p99，及无结果和无法映射的数量。弃答题进入延时统计。与原文不可映射的返回在来源指标中标为缺失。Mem0 以原始 turn 和 `infer=False` 运行，实验覆盖原文检索。该阶段无需配置生成或评分端点。

真实语义向量可由本地 CPU 服务生成。用 `uv run --locked --with fastembed==0.7.4 python eval/serve_embeddings.py` 启动服务，模型权重缓存后可加 `--offline`。在运行导入和评测的另一个终端设置：

```dotenv
MEMFXXK_EMBEDDING__PROVIDER=openai_compatible
MEMFXXK_EMBEDDING__BASE_URL=http://127.0.0.1:8011/v1
MEMFXXK_EMBEDDING__MODEL=BAAI/bge-small-en-v1.5
MEMFXXK_EMBEDDING__DIM=384
```

以上设置同时用于 memfxxk 和 Mem0，Mem0 在准备语料时复用相同原文的文档向量。若评测库已用其他模型完成导入，先运行 `memfxxk index reembed` 登记当前模型的任务，再用相同的 `ingest --suite` 处理队列。向量覆盖不足时评测会报错。快速语义烟测可以使用 `eval/suites/longmemeval-s-embedding-smoke.json`，包含 50 题清单的前六题，每个题型一题。

### 模型接口烟测 {#model-api-smoke}

`uv run pytest tests/unit/test_t07_llm.py` 使用本地 HTTP 模拟验证 OpenAI 兼容请求和工具续轮。真实运行时用 `MEMFXXK_EVAL__GEN__BASE_URL` 配置 API 前缀（例如以 `/v1` 结尾），配合 `MEMFXXK_EVAL__GEN__MODEL` 和 `MEMFXXK_EVAL__GEN__API_KEY`。评分模型使用相同名称结构的 `MEMFXXK_EVAL__JUDGE__*` 配置。

### 用固定证据比较答题 {#replay-evidence-qa}

保留 `evidence` 生成的完整本地目录，再把同一份题集和该目录交给 QA。程序按保存的排序读取截止时间内的原文 turn，用相同格式、Top-K 和 token 预算提供给答题模型。保留原始证据报告用于检索延时，QA 报告记录从装配已有证据到答题的延时。自主工具调用组 G4 单独计时。

```sh
uv run --locked memfxxk eval longmemeval qa \
  --suite eval/suites/longmemeval-s-smoke.json \
  --evidence-run eval/results/ACTUAL-EVIDENCE-RUN \
  --groups G1,recent-turns,memfxxk-hybrid,mem0-raw \
  --k 8 --evidence-budget 1500 --max-answer-tokens 384 --run-name qa-smoke
uv run --locked memfxxk eval longmemeval judge eval/results/qa-smoke
```

### 检索与问答命令 {#retrieval-and-qa-commands}

```sh
uv run memfxxk eval longmemeval fetch --dataset oracle
uv run memfxxk eval longmemeval ingest --dataset oracle
uv run memfxxk index ensure
uv run memfxxk eval longmemeval retrieval --dataset oracle --run-name oracle
uv run memfxxk eval longmemeval qa --dataset oracle --groups G1,G2,G3,G4 --limit 50 --run-name oracle-qa
uv run memfxxk eval longmemeval judge eval/results/oracle-qa
uv run memfxxk eval report eval/results/oracle-qa
```

官方评分通过 `judge --official` 运行，需用 `MEMFXXK_EVAL__OFFICIAL_REPO` 指向独立 checkout 并配置上游脚本的凭据。将官方和内置评分分别呈现。

<div id="_4" class="legacy-anchor" aria-hidden="true"></div>

## 手册发布 {#publishing}

主分支上的手册或 VitePress 配置变更会触发 [Publish manuals](https://github.com/shengming-zhou/memfxxk/actions/workflows/docs.yml)，构建并部署中英文手册。也可以在 Actions 中手动运行该流程。修改手册时同步更新 `docs/en/` 下的英文版，两个版本共用章节锚点。

在仓库根目录用 Node.js 24 验证构建，生成文件位于 `docs/.vitepress/dist/`。

```sh
npm ci
npm run docs:build
```

编辑时可用 `npm run docs:dev` 启动本地预览。
