# T00 · 仓库骨架与地基

依赖：无。参考：`docs/architecture.md` §2、§3、§4、§5、§8。

## 目标

搭好一个能跑测试的空服务：项目结构、配置、全部 M1 表的迁移、作用域与鉴权、测试基础设施。之后的任务只往里填功能。

## 范围

1. **项目初始化**
   - `git init`，`.gitignore`（Python、`.venv`、`eval/data/`、`eval/results/*/raw/`、`.env`）。
   - `pyproject.toml`（uv，Python 3.12，src 布局，包名 `memfxxk`，脚本入口 `memfxxk = memfxxk.cli:app`）。
   - 依赖：fastapi、uvicorn、sqlalchemy>=2、psycopg[binary]>=3.2、alembic、pgvector、pydantic>=2、pydantic-settings、httpx、typer、uuid-utils、jieba、tiktoken；开发依赖 pytest、ruff。
   - ruff 配置（line-length 100）。
2. **docker-compose.yml**：服务 `db` 用 `pgvector/pgvector:pg17`，端口映射 `5433:5432`，初始化脚本建 `memfxxk` 和 `memfxxk_test` 两个库并在两个库里 `CREATE EXTENSION vector`。数据卷命名。
3. **配置** `src/memfxxk/config.py`：pydantic-settings，前缀 `MEMFXXK_`，嵌套分隔符 `__`，覆盖 architecture.md §8 列出的全部配置项并给默认值（embedding 默认 `fake`、dim 默认 1024，token counter 默认 `tiktoken`）。提供 `.env.example`。
4. **数据库层** `src/memfxxk/db/`：engine 工厂、`transaction()` 上下文管理器、全部表的 SQLAlchemy Core `Table` 定义（architecture.md §5 全部表）。
5. **Alembic 迁移**：一条初始迁移建全部表、约束、索引（GIN on `lexemes`、作用域 btree、`records` 的部分唯一索引 `UNIQUE(memory_id) WHERE is_current`、`memory_marks` 用 coalesce 表达式的唯一索引、`documents` 同理）。`record_embeddings.embedding` 用不定维 `vector`，**不**在迁移里建 HNSW（T01 的 `index ensure` 负责）。迁移必须可 `downgrade base`。
6. **ID**：`src/memfxxk/ids.py` 提供 `new_id()`（uuid7）和 `derived_id(namespace, *parts)`（uuid5，T01 用来派生 turn 记录的 `memory_id`）。
7. **领域模型** `src/memfxxk/domain/`：`Scope`（org_id, user_id, agent_id, project_id）、`RequestContext`、`Principal`（org_id, role: `admin|host`）、`Actor`（type, id）。其他实体模型由后续任务按需添加。
8. **作用域** `src/memfxxk/scope/`：
   - `visible_filter(table, ctx) -> ColumnElement[bool]`，实现 architecture.md §4.2 规则。
   - `resolve_write_scope(ctx, drop: set[str]) -> Scope`：默认等于 ctx，允许去掉维度，不允许填入不同值（抛 `ScopeError`）。
   - `mark_applies(mark_row, ctx)` 同一规则的 Python 版（供内存内判断）。
9. **鉴权**：`api_keys` 存 SHA-256 哈希；`auth.authenticate(raw_key) -> Principal`；CLI 命令 `memfxxk keys create --org <org> --role host --name <n>` 打印一次明文 key；`memfxxk db upgrade` 包装 alembic。
10. **服务骨架** `src/memfxxk/service.py`：`MemoryService(engine, settings)`，方法签名留空即可，但提供 `for_request(principal, ctx) -> BoundService` 模式：所有业务方法都在绑定了 principal+ctx 的对象上调用。
11. **API 骨架** `src/memfxxk/api/`：FastAPI app 工厂，依赖注入：解析 Bearer key → Principal，解析 `X-Memfxxk-User/Agent/Project/Actor` 头 → ctx；统一错误格式 `{error:{code,message,details}}`；`GET /v1/healthz`（检查 DB 连接）。
12. **测试基础设施** `tests/conftest.py`：
    - 读取 `MEMFXXK_TEST_DATABASE_URL`（默认 `postgresql+psycopg://postgres:postgres@localhost:5433/memfxxk_test`），session 级别跑迁移到 head，每个测试后 TRUNCATE 全部业务表。
    - fixtures：`engine`、`service`、`make_ctx(user=, agent=, project=)`、`api_client`（FastAPI TestClient + 预置 host key）。
    - pytest marker `integration`（需要 DB）。
    - DB 不可用时 integration 测试给出清晰的报错信息（提示 `docker compose up -d db`），不要静默跳过。

## 不做

任何业务逻辑（写入、检索、管理）。嵌入 Provider、分词器（T01）。

## 验收标准

- `docker compose up -d db && uv sync && uv run alembic upgrade head && uv run pytest` 全绿。
- `uv run alembic downgrade base` 后再 `upgrade head` 成功（写一个测试或脚本验证）。
- 单元测试覆盖 `visible_filter` 的真值表：对象作用域 ×（ctx 有/无对应维度）× 值相等/不等，至少包括：
  - user 级对象在同 user 的任意 project 可见，在其他 user 不可见；
  - project 级共享对象（user=NULL）对该 project 内任意 user 可见，在其他 project 不可见；
  - org 不同永远不可见。
  这些用例用真实表（插入假行再查询）验证，不只测 Python 版。
- `resolve_write_scope`：去维度成功；试图写入与 ctx 不同的值报错。
- API：无 key / 错 key → 401；有效 key → `/v1/healthz` 200；错误响应格式符合约定。
- `ruff check` 与 `ruff format --check` 干净。

## 交付

按 AGENTS.md 的交付说明格式汇报，并附上 `uv run pytest` 的输出摘要。
