# AGENTS.md — memfxxk

memfxxk 是独立于 Agent 框架的记忆服务：保存原文与时间，提供混合检索、用户可管理的记忆（保存/更正/重要/固定/关联/遗忘）、可版本化的 Persona 文档、会话工作记忆和按 token 预算组装的上下文。

开工前必读：
- [docs/architecture.md](docs/architecture.md) —— 契约。表结构、作用域规则、流程、接口以此为准。
- [docs/roadmap.md](docs/roadmap.md) —— 当前里程碑和任务依赖。
- 当前任务的委托书：`docs/codex/Txx-*.md`。
- 背景调研：[docs/research/agent-memory-landscape.md](docs/research/agent-memory-landscape.md)（只读，不要修改）。

交付时同步维护 [用户手册](docs/user-manual.md) 和 [开发手册](docs/developer-manual.md)，以及 `docs/en/` 下对应的英文版。用户手册记录当前可用功能、配置和用法，开发手册记录实现约定、验证命令和已知问题。中英手册共用章节锚点，文档站用 `npm ci` 和 `npm run docs:build` 验证。

## 命令

```bash
docker compose up -d db              # 开发库 :5433，测试库同实例的 memfxxk_test
uv sync
uv run alembic upgrade head
uv run pytest                        # 全部测试（需要 db 容器）
uv run pytest -m "not integration"   # 只跑纯单元测试
uv run ruff check . && uv run ruff format --check .
uv run memfxxk --help
```

## 硬性规则

1. **作用域只在核心层判断。** 所有读写都经过 `MemoryService`，所有查询都用 `scope.visible_filter(...)` 生成可见性条件。不要在路由、CLI、MCP 里另写权限逻辑。新增查询必须附带“另一个 user/project 看不到”的测试。
2. **不可见与不存在返回同样的 404。**
3. **测试用真实 PostgreSQL**（`MEMFXXK_TEST_DATABASE_URL`），不要加 SQLite 回退，不要 mock 数据库。外部服务（嵌入、LLM）在测试里用 `fake` Provider，测试不得访问网络。
4. **原文是真源。** 派生数据（turn 记录、嵌入、以后的事实/摘要）必须能从事件重建；后台任务写回前校验 `generation`。
5. **用户的显式选择不能被自动流程覆盖**（`origin=user_explicit`、`confirmed=true`、标记、关联）。
6. **context bundle 不存记忆内容**，只存引用。
7. schema 变更只通过 Alembic 迁移，迁移必须可降级。不改已合入迁移，只追加。
8. 不要超出当前委托书的范围。发现架构文档有问题或缺口时，在 PR/交付说明里列出，不要自行改契约；确需偏离时在 `docs/decisions/` 加一条简短说明。

## 代码风格

- Python 3.12，类型注解齐全；ruff（line-length 100）。
- SQLAlchemy 2.0 Core + 手写 SQL，同步 psycopg 3。不用 ORM 映射类。
- 领域对象用 Pydantic v2（`src/memfxxk/domain/`），API 和 SDK 共用。
- 时间一律 `timestamptz` / 带时区 `datetime`（UTC）。
- 依赖新增要克制；需要新依赖时在交付说明里写明用途。

## Git 提交署名

Codex 参与完成并创建的提交沿用仓库作者身份。在提交说明正文之后空一行，附上共同作者署名。

```text
Co-authored-by: Codex <codex@openai.com>
```

## 交付说明格式

每个任务结束时在回复中给出：做了什么、偏离委托书的地方及原因、已知问题、如何验证（命令）、建议的下一步。
