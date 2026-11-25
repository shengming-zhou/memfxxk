# 给 Codex 的委托书

按 [roadmap.md](../roadmap.md) 的依赖顺序逐个交给 Codex。每份委托书都是自包含的任务说明，但默认 Codex 会先读仓库根目录的 `AGENTS.md` 和 `docs/architecture.md`。

## 顺序

```
T00 → T01 → (T02 ∥ T03) → T04 → T05 → (T07 ∥ T08)
                 T02 → T06 ─────────────↗ T07
```

## 发给 Codex 的开场模板

```
请完成 docs/codex/<文件名>.md 中的任务。
先读 AGENTS.md、docs/architecture.md 和该委托书。按委托书的“验收标准”自测，
全部通过后按 AGENTS.md 的“交付说明格式”汇报。不要做委托书范围以外的事。
```

## 每个任务回来后的检查清单

- [ ] `uv run pytest` 全绿，`ruff` 干净。
- [ ] 委托书里列出的每条验收测试都能在测试文件里找到对应用例。
- [ ] 新查询都用了 `visible_filter`，并有跨 user / 跨 project 的隔离测试。
- [ ] 没有改动 `docs/research/`、没有修改已合入的迁移。
- [ ] 交付说明里的“偏离”逐条看过；需要改契约的，回到 architecture.md 更新后再继续下一个任务。

## 任务一览

| 文件 | 标题 |
| --- | --- |
| [T00-foundation.md](T00-foundation.md) | 仓库骨架与地基 |
| [T01-ingest-and-indexing.md](T01-ingest-and-indexing.md) | 事件写入、turn 记录与索引作业 |
| [T02-retrieval.md](T02-retrieval.md) | 混合检索与读取 |
| [T03-user-management.md](T03-user-management.md) | 用户管理操作 |
| [T04-documents-sessions-context.md](T04-documents-sessions-context.md) | 文档、会话状态与上下文组装 |
| [T05-sdk-cli-mcp.md](T05-sdk-cli-mcp.md) | Agent 工具、SDK、CLI、MCP |
| [T06-retrieval-eval.md](T06-retrieval-eval.md) | LongMemEval 检索评测 |
| [T07-qa-eval.md](T07-qa-eval.md) | LongMemEval 问答对照组 |
| [T08-acceptance-demo.md](T08-acceptance-demo.md) | 验收套件与演示场景 |
