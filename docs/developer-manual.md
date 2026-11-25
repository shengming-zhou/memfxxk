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

M1 关口还需要全量 LongMemEval_S 的检索、真实模型问答和约 25 万条 turn 上的 p95，以及用真实开发 Agent 接入 MCP 一周的记录。完成这些验收后再启动 M2。

## 重跑基线 {#run-evaluation}

使用独立 PostgreSQL 数据库。在 `.env.example` 查看嵌入、生成和评分的配置。检索跑分记录数据 SHA、模型、各类问题的指标和延时。QA 还记录各组 token、工具调用与模型延时。

仓库保留了官方 Oracle 数据的假嵌入检索报告和两题假模型的 QA 烟测。 [M1 评测产物](https://github.com/shengming-zhou/memfxxk/tree/main/eval/results/m1-smoke)

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
