# T08 · 验收套件与演示场景

依赖：T05。可与 T07 并行。参考：调研报告 §6“产品行为”“用户管理”、§7 首个演示场景；`docs/roadmap.md`“M1 完成定义”第 2 条。

## 目标

把 M1 的产品承诺写成一组端到端测试，以后每次加后台整理、索引重建或新机制都复跑；再把首个演示场景做成可自动运行的脚本和一份上手文档。

## 范围

### 1. 验收套件 `tests/e2e/`

全部通过 **SDK → HTTP → 服务**（ASGI transport 或 uvicorn 子进程），fake embedder，`SimpleCounter`。测试数据为中英混合，放在 `tests/fixtures/acceptance/`。

| 编号 | 场景 | 断言 |
| --- | --- | --- |
| A01 | 会话接续 | 会话 S1 写入若干轮并 `put_session_state`（计划与当前步骤）；新客户端实例 `get_session` 恢复相同 state，`build_context(S1)` 的 working 段包含最近轮次 |
| A02 | 跨项目隔离 | p1、p2 各有同主题记录；所有读接口（search、list、get、build_context、工具）在 p1 下看不到 p2 的项目级记录；user 级记录两边都可见 |
| A03 | 跨用户隔离 | 同一 project 下 u1 的 user 级记录 u2 看不到；project 级共享记录两人都可见 |
| A04 | 显式保存跨会话 | S1 中 remember → S2（同 user/project）search 命中，`get` 能读 |
| A05 | 更正与历史 | note 连续更正 v1→v2→v3；search 只返回 v3；`get(version=1)` 读回 v1；并发两次以同一 `base_version` 更正，恰好一次成功一次 409 |
| A06 | 重要标记提升排名 | 构造与查询中等相关的记录 R，未标记时排名 > 3；标记 important 后进入 top 3；取消标记后恢复 |
| A07 | 固定项装载 | 在 p1 固定一条记录 → p1 的 build_context pinned 段包含它，p2 不包含；解除固定后不再装载 |
| A08 | 关联带回证据 | R1 关联 R2（purpose=fixes）；查询只命中 R1 时结果的 related 带回 R2；`unlink` 后不再带回；R2 移到 u1 不可见的作用域后不带回 |
| A09 | 文档修订冲突 | 两个客户端基于同一 revision 修改 `project.md`，后者 409；重新基于 head 提交成功；build_context 装入新版本 |
| A10 | 索引重建 | 执行 `index reembed` + `drain()`（以及清空 `record_embeddings` 后重建）后，复跑 A04–A08 的断言全部仍成立，标记与关联未丢失 |
| A11 | 停用与删除 | deactivate 后 search/list/context 都排除、`include_inactive` 可见；delete 后所有接口 404，相关 user_op 事件已 redact，embedding 行不存在，旧 context bundle 只剩引用 |
| A12 | 删除与后台竞态 | remember 后立即 delete，再 drain → 无残留；ingest 后 forget_event，再 drain → 无残留 |
| A13 | 上下文可追溯 | build_context 的每一项都能通过 bundle 元数据查到 reason；pinned 项的 reason 包含标记作用域，evidence 项包含排名和 important 标志 |

把 A04–A08 的断言写成可复用的检查函数，A10 以及以后 M2 的后台整理测试调用同一组函数。

### 2. 演示场景 `examples/demo_dev_agent.py`

不需要真实模型：脚本用 SDK 模拟一个开发 Agent 的两天工作，每一步打印发生了什么以及 `context --explain` 风格的上下文摘要。

1. **第 1 天，会话 S1**：用户与 Agent 排查 CI 失败。写入 6–8 轮（含 `tool_call`/`tool_result`：跑测试、看日志），其中一轮工具输出是 `test_upload.py::test_large_file` 超时，另一轮记录了修复（把上传改成分块 + 调大超时，重跑通过）。`put_session_state` 记录计划与进度。
2. 用户通过 SDK 把“CI 超时失败”那一轮标为 important，用 remember 保存一条“已验证修复：分块上传 + 超时 120s”，并 `link(失败轮, 修复 note, purpose='fixes')`。
3. 用户写入 `project.md`（发布约定 v1：“发布前必须跑完整集成测试”）并固定一条“发布只在周二”的 note。
4. **第 2 天，会话 S2**：恢复 S1 的 session state（打印恢复的计划）；用户说“CI 又挂了，upload 相关测试超时”——`build_context` 的 evidence 段首位是被标 important 的失败轮，related 带回修复 note；Agent 调用 `memory_get(window=1)` 读到相邻的修复过程。
5. 用户更新 `project.md` 到 v2（“发布前跑完整集成测试和 smoke test”）并更新固定 note；下一次 `build_context` 自动装载 v2；`doc history` 仍能读回 v1。

脚本结尾以断言形式检查第 4、5 步的关键事实（失败轮在 evidence 第一位、修复 note 在 related、文档为 v2、v1 可读），失败时非零退出。另加一个 e2e 测试直接运行该脚本。

### 3. 文档

- `README.md`（仓库根）：一句话介绍、架构图链接、快速开始（docker compose → 迁移 → 建 key → serve + worker → 跑 demo）、MCP 接入片段（指向 T05 写的部分）、评测入口。
- `docs/usage.md`：宿主集成指南——自动装载（build_context）与自动捕获（ingest_turn）放在模型调用循环的哪里、ctx 如何选择（agent/user/project 的划分建议）、工具如何注册、预算如何调。

## 不做

新功能。若写验收测试时发现 T01–T05 的缺陷，修复它（最小改动），并在交付说明里逐条列出。

## 验收标准

- A01–A13 全部通过。
- `uv run python examples/demo_dev_agent.py` 在干净数据库上退出码 0，输出可读。
- README 的快速开始步骤在全新克隆上照做可以跑通（在交付说明里写明你实际执行过的命令）。
