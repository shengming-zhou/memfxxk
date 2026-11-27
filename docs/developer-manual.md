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

## T16 P0 模型调用层 {#t16-llm-routing}

迁移 `0006` 新增 `llm_calls`。服务内的压缩与事实提取通过 `memfxxk.llm.runner.TaskRunner` 调用模型。任务覆盖项优先于 `MEMFXXK_LLM__DEFAULT__*`，缺少设置时可继承旧的 `MEMFXXK_M2__MODEL__*` 配置。独立评测仍使用 `MEMFXXK_EVAL__GEN__*`、`MEMFXXK_EVAL__JUDGE__*`。任务配置可设置 `MODEL`、`BASE_URL`、`API_KEY`、`TEMPERATURE`、`MAX_OUTPUT_TOKENS`、`TIMEOUT`、`MAX_RETRIES` 和 `RESPONSE_FORMAT`。

`RESPONSE_FORMAT` 支持 `prompt`、`json_object`、`json_schema`。结构化结果会在客户端校验，第一次校验失败后用错误信息请求一次修正。两次均失败时没有派生数据写入。`llm_calls` 按调用记录任务、提示词版本、模型名、作用域、作业或请求 ID、输入哈希、状态和用量。调试存储默认关闭。显式打开并有可见原文来源时才保存请求与回复，删除原文会清掉这些正文副本。

`MEMFXXK_LLM__DAILY_LIMITS` 是带作用域和 `tokens` 的 JSON 数组。UTC 日内，匹配的组织级和更细粒度限额都会检查累计使用量及并发预留。超额同步返回 `429 llm_budget_exceeded`，后台作业延后到下一 UTC 日且不扣失败次数。预留用输入估算和输出上限，最终用量以已返回的提供商统计结算。验证命令为 `uv run pytest tests/unit/test_llm_routing.py tests/integration/test_llm_tasks.py`.

## T16 P1 结构化压缩 {#t16-structured-compaction}

### 上游怎么做 {#compaction-upstream}

Codex 向模型提交对话历史，在摘要前加固定前缀，并保留最近的用户消息，额度为 20,000 token。请求超窗时会丢掉最老的消息后重试。Codex 0.172.0 的自动压缩默认上限是模型窗口的 90%，有更低配置时取较小值。MemGPT 把被逐出的消息与上一次摘要合并交给模型，达到软阈值时可以先提醒模型保存重点，逐出的历史仍能通过检索找回。 [Codex 压缩提示词](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/prompts/templates/compact/prompt.md) [Codex 压缩实现](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/compact.rs) [Codex 阈值](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/protocol/src/openai_models.rs) [MemGPT §2.2](https://arxiv.org/html/2310.08560#S2.SS2)

### 本项目的实现 {#compaction-implementation}

迁移 `0007` 给 `compactions` 加 `method`、`checkpoint` 和 `retained_user_event_ids`。配置了 `compact` 任务模型时，`prepare_compaction` 会调用 `compact-v1`，将上一版检查点与本次新增、已结束的事件一起发给模型，保留事件顺序、角色及显式标记。返回的每条陈述必须带来源 UUID。服务丢掉带有范围外 UUID 的整条陈述，并记录被丢弃的条数。检查点与可读摘要受请求的 `target_tokens` 约束。提交时复核代次和修订后才发布新的工作摘要。

不配置模型时得到 `extractive_fallback`，迁移前的记录标记为 `legacy`。配置了模型时，输出校验失败会请求一次修正。两次失败返回 `llm_invalid_output`。兼容网关可用 `MEMFXXK_LLM__TASKS__COMPACT__RESPONSE_FORMAT=json_object`，较长对话也可能需要提高 `MAX_OUTPUT_TOKENS`。用 `MEMFXXK_M2__RECENT_USER_TOKENS` 限制所选原话，默认 1,024，设为 0 可关闭。实际装载还受 `working` 及总预算约束。

### 与上游实现的差异 {#compaction-differences}

宿主可在 `pressure_ratio` 超过 `MEMFXXK_CONTEXT__PRESSURE_THRESHOLD` 时用 `pressure=warning` 调度压缩。比例的分母是请求与各启用分区的有效预算。参考宿主会先拼装，再压缩符合条件的已关闭前缀，并重建上下文。单次压缩输入超过 `MEMFXXK_M2__COMPACT_INPUT_TOKENS`（默认 128,000）时返回 `compaction_input_too_large`，宿主可缩小待处理的区间。检查点引用和原话保留以完整条目为单位。持久化的 context bundle 只存引用和计数。

### 发布日期改期示例 {#compaction-example}

第一次准备压缩时，输入是空的上一版检查点、已关闭的事件和目标 token 数。模型把「周五发布」和对应事件 UUID 写入 `latest_decisions`。宿主提交草案后，`records` 生成第一版 `summary`。用户改期后，第二次压缩的输入是第一版检查点和包含改期原话的增量。输出是新的 `latest_decisions`，内容为周二发布，来源是更正事件的 UUID。第二次提交后，当前版本为 2，旧版本与原始会话仍可回查。这个例子用 DeepSeek Flash 真实跑过 66 轮对话，两次成功压缩之间重新连接了服务，并检查了装入上下文中的决定和更正引用。

### 真实模型对照 {#compaction-live-evaluation}

在准备好固定数据集、专用 PostgreSQL 测试库、真实嵌入服务和两种模型后，运行 `uv run python eval/run_m2_paired.py all --structured-summary --output eval/results/t16/p1`。用 `--prepare-workers 4` 可在四个独立数据库区域并行准备。对照组共用 30 道题、每题 1,500 token 的记忆预算，以及相同的答题和评分模型。两个摘要组有相同的摘要上限与压缩边界，装入阶段比较摘要正文，原话保留另用流程测试核对。诊断文件把逐字保留的标准证据和摘要引用的标准证据分别计数，指标文件记录答题、评分和压缩用量。

2026-11-27 的实跑采用 BGE-small-en-v1.5 向量、`deepseek-flash` 压缩和 `gpt-5.4-mini` 答题与评分，回放 6,667 轮。压缩用 `json_object` 模式，输出上限 4,000 token。三组都取 top 8，摘要目标 240 token，装载摘要最多 320 token。结果目录为 `eval/results/t16/p1-live`。

| 策略 | 答对 | 装入至少一条完整标准原文 | 答题端到端 p50 / p95 | 答题输入 / 输出 token | 评分输入 / 输出 token |
| --- | ---: | ---: | ---: | ---: | ---: |
| 混合检索 | 7/30 | 21/30 | 1.23 / 1.79 秒 | 47,594 / 756 | 3,152 / 126 |
| 混合检索 + 截断摘要 | 7/30 | 18/30 | 1.09 / 1.40 秒 | 47,505 / 622 | 3,018 / 120 |
| 混合检索 + 模型摘要 | 8/30 | 18/30 | 1.20 / 1.52 秒 | 47,440 / 699 | 3,095 / 120 |

三组各有 4 题没召回标准证据、5 题召回后未装入。加摘要后另有 3 题只装入部分标准原文。模型摘要有 5 题引用了标准来源，逐字保留标准消息为 0 题。模型摘要相对截断答对了 2 道原先错的题，也答错了 1 道原先对的题。时间推理由 2/10 到 3/10，知识更新均为 5/10，多会话均为 0/10。本轮样本支持继续把模型压缩作为可配置的会话能力，默认问答仍用混合检索。

模型压缩单独用了 60 次调用、4,125,762 输入和 83,614 输出 token。每题两次压缩合计 p50 / p95 为 16.88 / 23.22 秒，截断摘要为 0.55 / 0.78 秒。这段构建时间不计入表中的答题延时。按本次回放折算，每千轮约 618,833 输入与 12,541 输出 token。总用量含三组答题与评分为 4,363,623 token，单独的网关诊断调用不在本表内。

## T16 P2 事实写入策略 {#t16-fact-strategies}

`MEMFXXK_STRATEGIES__FACTS` 接受 `off`、`key_proposal`、`mem0_classic` 和 `mem0_additive`。未设置时继续由 `MEMFXXK_M2__EXTRACT_FACTS` 控制原有待审模式，默认关闭。切换配置需同时更新写入服务与 worker。两套 Mem0 策略都在轮次结束后处理原文，通过作业代次、租约、可见性与目标版本检查写回。

### Mem0 classic {#mem0-classic}

#### 原版怎么做 {#mem0-classic-upstream}

论文用会话摘要、最近 10 条消息和最新一对消息抽出事实。对每条候选找 10 条相似旧记忆，再调一次模型决定 ADD、UPDATE、DELETE 或 NONE。旧版 OSS 给已有记忆编临时 ID，UPDATE 映射回旧 ID，DELETE 移除向量记录，SQLite 历史表记旧值、新值、操作及删除标记。 [Mem0 §2.1](https://arxiv.org/html/2504.19413#S2.SS1) [v0.1.118 写入](https://github.com/mem0ai/mem0/blob/v0.1.118/mem0/memory/main.py) [历史表](https://github.com/mem0ai/mem0/blob/v0.1.118/mem0/memory/storage.py)

#### 这里怎么做 {#mem0-classic-implementation}

`m2/mem0.py` 从同一会话读取关闭的轮次、前十条消息和能够覆盖更早内容的已提交摘要。配置 `extract` 和 `reconcile` 两个任务模型。新的陈述必须引用这一轮原文。每条陈述用余弦抓取最多 10 条可见、有效的当前事实，刚写入且还没建索引的事实使用现算向量参加比较。更新或删除的整数必须来自这个候选的检索集合。UPDATE 同时核对 `old_memory` 和当前版本。

#### 和原版的差异 {#mem0-classic-differences}

UPDATE 在 `records` 增加版本。DELETE 写入有效期为零的失效版本，使用同一个记忆 ID。有效时间和记录时间的约束让旧版本可以历史回查。用户明确保存、确认或标记的事实生成待审提议。迁移 `0008` 为 `fact_proposals` 加 `operation` 和 `base_version`，防止把提议套在已经更新的版本上。降级会把这两列与状态存进 JSON，并将待审写入暂时标为拒绝，升级时还原。

#### 一个具体例子 {#mem0-classic-example}

原文「Atlas 用巴黎站点」产生候选，第二个模型返回 `ADD`，事实版本为 1。下一轮用户改到里昂时，抽取先产出「Atlas 用里昂站点」。第二次调用把巴黎记忆列为 ID `0`，模型返回 `UPDATE`、替换文本和原始巴黎文本。自动记录写成版本 2。若巴黎已由用户确认，版本 1 在提议获批前仍然生效。

#### 评测结果 {#mem0-classic-evaluation}

用 `uv run python eval/run_t16_facts.py --output eval/results/t16/p2-oracle` 重跑。固定 3 道知识更新和 3 道时间推理题，三组都用同一份 LongMemEval Oracle 历史、top 8 和 1,500 token。Oracle 历史是数据集标好的相关会话，这一设计评测写入和答案变化，完整干扰历史的效果由后续对照验证。

最终实跑使用 `deepseek-flash` 提取和合并、`gpt-5.4-mini` 答题和评分、BGE-small-en-v1.5 嵌入。每组回放 51 轮。G3 答对 2/6，classic 答对 3/6，其中知识更新从 2/3 到 3/3，时间推理均为 0/3。classic 有 3 题装入带标准来源的事实。101 次写入模型调用共用 181,548 输入和 10,224 输出 token。每题导入、写入和索引的 p50 / p95 为 24.52 / 33.05 秒，G3 为 1.30 / 2.55 秒。classic 检索和装载的 p50 / p95 为 64 / 153 毫秒，G3 为 190 / 245 毫秒。

### Mem0 additive {#mem0-additive}

#### 原版怎么做 {#mem0-additive-upstream}

开源 v3 从当前消息向量找 10 条现有记忆，将近期消息、现有记忆和最新对话一道交给模型，再批量写入提取的记忆。模型区分用户陈述和助手建议，输出可带相关旧记忆编号。内容重复时去重，现有记忆继续保留。 [Mem0 提示词](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py) [Mem0 写入](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)

#### 这里怎么做 {#mem0-additive-implementation}

使用 `extract` 任务的一次模型调用。检查发言人和本轮来源，把去重哈希、标准化后的实体和 `attributed_to` 存在事实的 JSON 元数据里。已有事实的编号校验通过后，关联进入 `memory_relations`。实体以 NFKC、大小写和空白归一化做精确匹配，再和已召回的实体名计算余弦，阈值为 0.95。语义匹配采用旧别名时会保存其来源依赖，删除来源时相关加工结果一并清除。

#### 和原版的差异 {#mem0-additive-differences}

打开 `MEMFXXK_STRATEGIES__MEM0_ADDITIVE_RANKING=true` 后，对混合检索选出的 additive 事实候选计算一轮 BM25，并按实体匹配和有效时间加权。BM25 的语料是本次召回候选，保留现有 PostgreSQL 全文索引和向量通道。关联两端重新检查可见性和活跃状态，删掉任一事实所依赖的原文会通过现有删除传播清掉该端与边。

#### 一个具体例子 {#mem0-additive-example}

用户原先说「Atlas 用巴黎站点」，后来改到里昂。抽取看到带临时编号的巴黎记录，回传里昂陈述、用户来源、关联 ID 和实体 `Atlas`。最终两条事实都存在，里昂相关查询仍会看到生效时间较新的记录；巴黎原文可以从关联回查。

#### 评测结果 {#mem0-additive-evaluation}

沿用 [classic 的固定题集](#mem0-classic-evaluation)，开启事实加权。最终答对 3/6，知识更新 2/3、时间推理 1/3。相对 G3 多对 2 道、少对 1 道，6 题都装入了带标准来源的事实。56 次写入调用含 5 次格式修正前的失败，总计 174,095 输入和 25,331 输出 token，51 个写入作业最终完成。每题导入、写入和索引的 p50 / p95 为 25.92 / 45.41 秒。检索和装载的 p50 / p95 为 70 / 141 毫秒。两次试跑分数有波动，两套策略继续作为可选配置。最终实跑总用量为 419,464 token。逐题结果和清单位于 `eval/results/t16/p2-oracle-validated`。

## T16 P3 时序知识图 {#t16-temporal-graph}

### 原版怎么做 {#temporal-graph-upstream}

Graphiti 提取实体，用全文与名称向量找候选，再让模型判断同一性。关系抽取带生效和失效时间。已有同端点的关系用于查重，其他相关关系可用于判断失效。更晚生效的矛盾关系截断旧边，系统另记发现矛盾的时间。论文用近 4 个 episode。核对的上游代码里，`RELEVANT_SCHEMA_LIMIT` 和默认检索数均为 10，向量门槛是 0.6，MMR λ 是 0.5。 [Zep 论文](https://arxiv.org/html/2501.13956) [召回配置](https://github.com/getzep/graphiti/blob/2a4f079ccedf32c36693e503d0a1cdfa2821d223/graphiti_core/search/search_utils.py) [关系更新](https://github.com/getzep/graphiti/blob/2a4f079ccedf32c36693e503d0a1cdfa2821d223/graphiti_core/utils/maintenance/edge_operations.py)

### 这里怎么做 {#temporal-graph-implementation}

`MEMFXXK_STRATEGIES__GRAPH=graphiti` 为关闭的 turn 排入 `build_entity_graph`。作业读取原始事件和前四个 turn，按顺序做实体抽取、必要时批量消歧、关系抽取、逐条比较有旧边的新关系、批量实体摘要。五个任务可分别配模型。源码位于 `m2/temporal_graph/`，提示词均带版本与结构校验。迁移 `0009` 增加 `entities`、`entity_edges`、`entity_mentions`。一次事务检查轮次代次、租约与图记录代次后提交写入。删除原文移除对应支持，若这条原文让旧边失效，也会撤销该失效依据。

### 和原版的差异 {#temporal-graph-differences}

图存 PostgreSQL。词项、余弦、邻接三条路分别找实体或关系，BM25 作用于全文候选集。用 `MEMFXXK_STRATEGIES__GRAPH_RERANKER` 选 `rrf`、`mmr`、`node_distance` 或 `episode_mentions`。查重按同端点、同关系类型找，状态变化会额外比较同主体、同关系类型的旧边。通过 `graph_hops` 控制展开，`graph_evidence_tokens` 按 T13 分配图证据空间。搜索结果可带 `graph_fact`，持久化记忆包只存 `graph_edge_id`、原始 turn 引用和计数。已有显式关联仍可用 `graph=links`。

### 一个具体例子 {#temporal-graph-example}

1 月 1 日 Rae 说住在北京，模型抽出人、城市和 `LIVES_IN` 关系，带原文引用。1 月 3 日 Rae Tang 说搬到上海。消歧把两种姓名映射到同一实体。新边开始于 1 月 3 日，旧边的 `invalid_at` 也写入 1 月 3 日，`expired_at` 记录服务学到更正的时间。查询 1 月 2 日与 1 月 4 日分别得到北京和上海。删除搬家那条原文后，旧边恢复原先的有效区间。

### 评测结果 {#temporal-graph-evaluation}

重跑命令为 `uv run python eval/run_t16_graph.py --output eval/results/t16/p3-oracle`。三组共用同一份 LongMemEval Oracle 历史、top 8 和每题 1,500 token。两个图组在总额度里各留 300 token，最多两跳。固定 3 道多会话和 3 道时序题，每题至少两个会话。链接组从原文生成共享名称链接，建图过程无需题目和标准答案。

实跑每组回放 60 轮，BGE-small-en-v1.5 做向量，`deepseek-flash` 建图，`gpt-5.4-mini` 答题和评分。混合检索、链接图、时序图分别答对 1/6、1/6、0/6。两个命中均在多会话类，三组时序题均为 0/3。三组均有 5/6 装入至少一条标准来源。链接和时序图分别有 2、5 题装入图展开结果，其中标准来源计数均为 0。时序图在 6 题装入的条目上附带了边引用。

时序图写入成功完成 60 个作业，239 次模型调用包含 5 次格式修正前的失败。共用 870,987 输入、89,513 输出 token。每题写入构建 p50 / p95 为 68.77 / 175.23 秒。三个策略的检索与装载 p50 / p95 分别为 208 / 253、80 / 100、124 / 183 毫秒，同次比较的冷启动主要落在第一组。图构建这份回放的每千轮折算约 1,452 万输入、149 万输出 token。加上答题和评分后总用量为 991,779 token。保留 `links` 为默认图策略。

## T16 P4 A-Mem 笔记网络 {#t16-amem}

### 原版怎么做 {#amem-upstream}

论文先从正文生成关键词、标签和上下文描述，再把这几项和正文一同向量化。先向量取近邻，再让模型选择有价值的关系。演化时逐个检查旧近邻的描述。B.3 的 JSON 有 `should_evolve`、`actions`、`suggested_connections`、`tags_to_update`、`new_context_neighborhood` 和 `new_tags_neighborhood`。论文示例的动作名单和文字说明不同，MIT 源码实际处理 `strengthen` 和 `update_neighbor`。 [A-Mem §3 和附录 B.3](https://arxiv.org/pdf/2502.12110) [实现](https://github.com/agiresearch/A-mem/blob/ceffb860f0712bbae97b184d440df62bc910ca8d/agentic_memory/memory_system.py)

### 这里怎么做 {#amem-implementation}

设置 `MEMFXXK_STRATEGIES__NOTES=amem` 后，`note`、`fact`、`lesson` 在普通向量作业后安排 `build_amem_note`。依次做一次 `note_enrich`、有近邻时一次 `note_link`、对最多 10 个可编辑近邻逐条做 `note_evolve`。迁移 `0010` 增加 `amem_revisions` 和 `amem_links`，存独立增强向量、演化修订、来源和调用 ID。`records.metadata.amem` 指向当前加工属性。在向量检索里融合增强近邻，返回 `score.note_rank` 和 `score.note_similarity`。关联 `purpose=auto_link`，现有关系展开可用。

### 和原版的差异 {#amem-differences}

演化只更新派生属性，保留原文和用户手工标签，跳过显式保存及已确认的近邻。建链和演化分别校验输出，来源按模型看到的材料记录。持久化前再校验记录代次、前一版属性、作业租约和原文。删除会清除依赖该来源的修订与自动关联，并在可用时恢复上一份加工属性。用 `GET /v1/memories/{id}/note-attributes/revisions` 查看属性历史。回滚用 `POST /v1/memories/{id}/note-attributes/rollback`，传 `base_revision` 和 `target_revision`，返回新属性版本。找不到本作用域内的目标返回 404，版本冲突返回 `409 note_revision_conflict`。

增强检索和已有来源共用 `build_context` 的候选接口。bundle 中每条候选保留 `ref_id`、`record_type`、相关度 `score`、完整候选的 `candidate_tokens` 和实际装入的 `tokens`。`decision` 与 `decision_reason` 解释入选、截断以及舍弃。重复或已由宿主装载的条目也可审计。持久化只存引用和这些元数据。

### 一个具体例子 {#amem-example}

「Atlas 夜间导入以前跑在巴黎」形成原文笔记和第一版检索描述。后来一条笔记记录迁到里昂，`note_link` 选中巴黎笔记，`note_evolve` 将原来的自动描述调整为「巴黎导入的历史配置」。正文和正文版本继续保留。回滚以第一版属性写出第三版，用户可从 `memory_get` 或 HTTP / SDK 的 `get_memory` 读 `note_attributes`。

### 评测结果 {#amem-evaluation}

`uv run python eval/run_t16_notes.py --output eval/results/t16/p4-oracle-shared-budget` 重跑固定的 LongMemEval Oracle 两道多会话、两道知识更新题。两组都从相同轮次原文构建引用型笔记，共用 1,500 token 总额度。A-Mem 组开启增强向量和一跳自动关联，图证据与普通证据竞争同一额度。

最终实跑用 BGE-small-en-v1.5 向量、`deepseek-flash` 加工笔记、`gpt-5.4-mini` 答题和评分。两组都答对 4/4，也都装入 4/4 的标准来源。A-Mem 每题均装入了增强笔记，这轮没有装入额外的图展开证据。40 个加工任务完成，共 258 次模型调用，含一次首次校验失败后的修正。用量为 1,116,819 输入、8,921 输出 token，算上答题和评分共 1,139,453 token。每题写入构建 p50 / p95 为 75.16 / 92.89 秒，检索与装载为 100 / 135 毫秒。基线分别为 1.92 / 2.59 秒和 178 / 196 毫秒。每千轮折算约 2,792 万输入、22.3 万输出 token。默认保持关闭。

## T16 P5 自编辑记忆块 {#t16-memory-blocks}

### 原版怎么做 {#memory-block-upstream}

Letta 将标签、用途、正文、字符上限与 `read_only` 一起交给模型。旧版 Python 实现有 `core_memory_append(agent_state, label, content)`、`core_memory_replace(agent_state, label, old_content, new_content)` 和 `rethink_memory(agent_state, new_memory, target_block_label)`。其通用 `memory` 工具还支持按路径查看、修改和插入。后台 dreaming 可在执行步数达到阈值或压缩时运行，允许另开一轮复查。 [Letta Apache-2.0 旧版源码](https://github.com/letta-ai/letta/blob/archive/letta/functions/function_sets/base.py) [后台整理文档](https://docs.letta.com/configuration/memory)

### 这里怎么做 {#memory-block-implementation}

迁移 `0011` 新增 `memory_blocks`、`block_revisions`、`block_proposals`。块有当前版本指针和只增版本序列。每次采用编辑都会新建一版，保留修改者和来源事件。用户明确写入的初始内容记录为 `user_op`。作用域由核心服务按组织、用户、Agent、项目判断。新策略只开关候选装载和自动整理，显式 API 操作仍可用。

| 操作 | HTTP | SDK 和 MCP |
| --- | --- | --- |
| 创建 | `POST /v1/memory-blocks` | `create_block(CreateBlock(...))` |
| 列出或读取 | `GET /v1/memory-blocks` 或 `GET /v1/memory-blocks/{id}` | `list_blocks`、`get_block` 与 `memory_block_list`、`memory_block_get` |
| 编辑 | `PATCH /v1/memory-blocks/{id}` | `edit_block(id, EditBlock(...))` 与 `memory_block_edit` |
| 查提议 | `GET /v1/block-proposals` | `list_block_proposals` |
| 审核 | `POST /v1/block-proposals/{id}/review`，正文 `{"accept":true}` | `review_block(id, True)` |

编辑请求含 `base_revision`、`operation`、`text`，替换时还要传只匹配一处的 `old_text`。有来源时用 `source_event_ids`。版本冲突返回 `409 block_revision_conflict`，正文超限返回 `422 block_limit_exceeded` 和 `char_count`、`requested_char_count`、`limit`。不可见的块与未知块都返回 404。

`sleeptime_consolidate` 按闭合轮次和已采用的压缩安排。它加载最近的原文、最新可用摘要和当前块，再调用 `sleeptime`。写回时锁定作业租约，检查各来源是否仍在、历史记录的代次以及块修订。可写 Agent 块直接更新，受保护块和事实走提议。忘记来源会清除依赖的版本或提议，并把当前块回退到最近仍可用的版本。

### 和原版的差异 {#memory-block-differences}

单独建表用于修订冲突检查、来源删除和用户审批。共享沿用 memfxxk 的作用域，宿主可为用户块选择对多个 Agent 可见的范围。编辑权限单独判断。一次整理使用一个结构化模型调用。上下文用 `<memory_block>` 渲染用途、字符用量和正文，由 Harness 决定放入消息的什么位置。块走 `_Candidate` 和原有文档装载流程，候选含块 ID、类型、相关度、token 数与装载决定，bundle 只存引用。

### 一个具体例子 {#memory-block-example}

Agent 先读到交接块第 1 版。调用 MCP `memory_block_edit`，传 `{"block_id":"…","edit":{"base_revision":1,"operation":"rewrite","text":"周四交接"}}`。下轮 Harness 请求 `context_build` 时可装入第 2 版。同一次操作对用户块只会返回提议，用户采用后才改变正文。 [参考 Harness 集成测试](https://github.com/shengming-zhou/memfxxk/blob/main/tests/integration/test_mcp_harness.py)

### 评测结果 {#memory-block-evaluation}

`uv run python eval/run_t16_blocks.py --output eval/results/t16/p5-long-dialogue` 重跑固定 64 轮、四题和 1,500 token 总额度对照。BGE-small-en-v1.5 负责检索，`deepseek-flash` 负责整理，`gpt-5.4-mini` 答题和判分。两组都答对 2/2 偏好题和 2/2 改期题，原文证据都装入了 4/4。记忆块记录了相同的偏好和改期信息，这组题的混合检索已经找到足够原文。

四次整理无失败，输入 12,289、输出 1,321 token。每千轮约 192,016 输入和 20,641 输出 token。基线和记忆块组的 64 轮写入总耗时为 2.94 / 12.80 秒，组装 p50 / p95 分别是 135 / 171 毫秒和 69 / 108 毫秒，答题分别是 850 / 1,421 毫秒和 614 / 814 毫秒。四题的答题 token 总量为 3,609 / 4,759。加上判分与整理，两组共用 22,647 token。四题延时受首次调用和运行顺序影响。该小样本用于确认功能，自动整理维持关闭。

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
