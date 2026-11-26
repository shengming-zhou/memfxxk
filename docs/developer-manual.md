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

本机 PostgreSQL 17、pgvector 0.8.7 上实测 25 万条合成 turn。以 64 维测试向量预热后，128 个串行混合检索查询的 p50 为 122.68 ms，p95 为 162.15 ms，超过 100 ms 闸门。耗时包含数据库与融合，排除查询嵌入和 HTTP。该测试独立于日常回归。实际语义检索、并发负载和真实开发 Agent 的长期使用另行验收。

```bash
uv run pytest -m scale -s -q tests/integration/test_retrieval.py::test_search_performance_250k
```

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

### 30 题配对答题 {#m2-paired-qa}

`eval/suites/longmemeval-s-m2-paired30.json` 固定了知识更新、时间推理、跨会话各十题及 LongMemEval S 的校验和。三组都取八条候选，用相同的 1,500 token 记忆预算和 384 token 答案额度。摘要组先给两次本地滚动压缩的结果留空间，图组从原始用户消息里重复出现的低频名字建立跨会话边，查询走两跳。题目用于检索和答题，标签用于衡量证据和打分。

先按下文启动本地 BGE 并配置向量端点，再运行 `prepare`。它在测试数据库中新建独立的 PostgreSQL schema，共用同一批原文和向量，逐题保存证据。配置 `MEMFXXK_EVAL__GEN__*` 指向 `gpt-5.4-mini` 后运行 `score`，评分也用该模型。可从同一目录恢复未完成的阶段。

换答题模型时，把 `prepared.jsonl` 和 `manifest.json` 复制到新的结果目录，更新清单里的 `answer_model` 并配置对应的生成端点。保留 `judge_model`，通过 `MEMFXXK_EVAL__JUDGE__*` 配置评分模型。这样可以用同一份上下文和同一个评委比较两个答题模型。

```bash
uv run python eval/run_m2_paired.py prepare --output eval/results/m2-paired30
uv run python eval/run_m2_paired.py score --output eval/results/m2-paired30
```

`diagnostics.jsonl` 记录漏召回、入选与装载截断，`metrics.json` 汇总配对输赢、p50/p95 和答题及评分 token。证据完整指装载文本里含整个标注消息。部分内容也可能足以答对。

用同一份 30 题、BGE 语义向量和 1,500 token 上限复测图装载。图预算从 0 改为 350 后，图候选实际装入从 0/19 升到 19/19，召回至少一条标注证据仍是 27/30。装入至少一条完整标注消息从 21/30 变为 18/30，装入全部完整标注消息从 7/30 变为 9/30。在 350 token 图预算下，混合、摘要、图三组的记忆处理 p50/p95 分别为 177/266、174/238、199/379 ms。这次只测证据，模型答题和评分调用均为零。预算默认仍为 0。

```bash
uv run python eval/run_m2_paired.py prepare --graph-evidence-tokens 350 --output eval/results/m2-paired30-graph350
```

## M3 程序性记忆 {#m3-procedural-memory}

`POST /v1/lessons` 和同步及异步 SDK 的 `save_lesson` 共用 `SaveLessonRequest`。记录使用当前请求作用域和已有的 `lesson` 类型，保存为用户确认的文本版本，同时在审计事件里记录创建参数。失败与验证事件必须可见，并带各自的工具结果。验证发生的顺序在同会话按事件序号判断，跨会话按事件时间判断。相同幂等键和相同参数返回首次创建的版本。

经验沿用原有检索、版本管理和向量作业。两个来源都保存在 `source_event_ids`，对应的角色由 `source_pos` 区分。删掉任一原文就会清理全部经验版本。自动上下文通过 `search_filters.types` 或用户固定来启用经验。经验文本交给宿主判断和使用。

回归用真实 PostgreSQL 和本地 CSV 数据测一次失败、修复和下一会话复用，并覆盖 API、SDK、MCP、作用域及删除。AMA-Bench 和真实 Agent 的第二次成功率属于 M3 后续验收。

```bash
uv run pytest tests/integration/test_m3_lessons.py tests/integration/test_cli_mcp.py tests/integration/test_sdk_tools.py
```

## M3 Markdown 工作区 {#m3-markdown-workspace}

`GET /v1/workspace` 与 SDK `export_workspace` 读取当前精确作用域的文档快照，并按传入的经验 ID 读取两条执行证据。导出使用一致的数据库快照。CLI 把 UTF-8 正文写到 `documents/` 和 `lessons/`，清单保存版本、用途、加载策略和原始工具结果。

`POST /v1/workspace` 与 SDK `import_workspace` 用同一事务处理文档和经验。文档复用已有版本冲突检查、分块和索引。明确更新时，导出清单里的文档 ID 和版本必须匹配目标。经验保留原始 ID 并创建本地事件引用，普通删除逻辑会清理引用该事件的经验。导入时源项目和目标项目各自保留自己的数据。

```bash
uv run pytest tests/integration/test_workspace.py tests/integration/test_cli_mcp.py tests/unit/test_workspace_files.py
```

## M3 项目凭据与 MCP Harness {#m3-project-harness}

迁移 `0003` 新增项目授权表。认证时读取凭据绑定的用户、项目、可选 Agent 和读写权限，`BoundService` 统一绑定请求上下文与审计身份。项目范围外的可见继承数据支持读取，项目写入检查拦截越界目标。项目降级会撤销项目凭据。旧宿主凭据继续支持可信 Harness 代行用户。详见 [项目凭据决策](/decisions/0002-project-credentials)。

项目凭据可以停用和恢复记忆。永久删除由管理员或获准的宿主执行。项目凭据删除或重用已有关系时，还会核对创建关联的审计事件是否可见。会话 ID 在组织内唯一，宿主可用 UUID 创建会话并保存该 ID 以便恢复。

迁移 `0005` 按绑定的项目、用户和 Agent 隔离项目凭据使用的幂等键。同一身份续发凭据可继续重试，既有宿主幂等键沿用组织范围。降级会移除项目范围的重试元数据，原始事件仍保留。

`examples/mcp_harness.py` 通过 stdio 接入真实 MCP，使用工具允许列表把模型的检索和主动管理与宿主的自动装载、压缩、记录区分开。宿主用同一会话 ID 恢复状态。工具结果、角色与原始文本按轮保存。

图证据额度独立计入已有的证据区和整窗上限。图段先用最多 `graph_evidence_tokens` 组装，未用额度交给普通证据，渲染时普通证据在前。字段默认值为零。

```bash
uv run alembic upgrade head
uv run pytest tests/integration/test_project_credentials.py tests/integration/test_mcp_harness.py tests/integration/test_context_graph_budget.py
```

## M3 向量后端与模型网关 {#m3-vector-and-gateway}

选择 `MEMFXXK_SEARCH__VECTOR_BACKEND=qdrant` 时，配置 Qdrant 地址和部署专用的集合前缀。数据库先核对候选范围和记录代次，再查询 Qdrant。索引滞后、服务不可用或范围超限时，向量检索回到 pgvector。迁移到 `0004` 后用下面的命令建索引、补向量和运行作业。改用另一个 Qdrant 地址时，原地址仍需对应 worker 完成待处理删除。

```sh
uv run alembic upgrade head
uv run memfxxk index ensure
uv run memfxxk index reembed
uv run memfxxk worker
```

Qdrant 恢复后可用 `memfxxk index retry-qdrant` 重试已追踪的镜像。默认向量后端为 pgvector。

降级到 `0003` 或更早前，先停掉写入镜像的 worker，在 Qdrant 删除该部署专用的镜像集合，包括旧地址和前缀仍保留的集合。确认远端清理完成后清空 PostgreSQL 的 `qdrant_replicas` 表，再执行 Alembic 降级。`0004` 在镜像登记非空时会中止降级。

网关在配置上游后开放 `POST /v1/chat/completions`，请求经过同一个 `BoundService`。记忆额度由 `MEMFXXK_GATEWAY__MEMORY_TOKENS` 控制，取回的记忆作为当前用户消息中的引用资料传给模型。成功答复才记录未经注入的本轮用户原文和模型原文。幂等键用于入库去重，上游请求可以重发。MCP 继续处理模型主动管理和宿主生命周期。

仓库的 GitHub CI 在真实 PostgreSQL 上运行测试与 Ruff，双语站点由手册发布工作流处理。 [uv 与 GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)

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
