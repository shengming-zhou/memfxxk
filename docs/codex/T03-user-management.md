# T03 · 用户管理操作

依赖：T01。可与 T02 并行。参考：`docs/architecture.md` §5（records 版本、memory_marks、memory_relations）、§6.4。

## 目标

用户能显式保存、更正、标记重要/固定、关联、停用和删除记忆，并能浏览列表。每个操作可审计。

## 范围

所有方法挂在 `BoundService` 上，同时在 API 暴露。每个写操作在同一事务里写一条 `events(kind='user_op', payload={op, target, args, result})`，其 `id` 作为相关标记/关联的 `source_event_id`。

### 1. `remember`

`RememberRequest`：`content?, title?, from_event_id?, span?: {start, end}（字符偏移）, type='note'（M1 只允许 note）, scope_drop?, important=false, pinned=false, links?: [{memory_id, purpose}], idempotency_key?`

- `content` 与 `from_event_id` 二选一；后者从事件原文截取片段，`source_event_ids=[from_event_id]`，事件对 ctx 不可见 → 404。
- 记录：`origin='user_explicit'`, `confirmed=true`, `version=1`, `event_time=now`（选段时用原事件时间），同步计算 `lexemes`，登记 `embed_record`。
- `important/pinned/links` 在同一事务内完成。
- 幂等键语义与 ingest 相同。
- 返回 `{memory_id, version}`。
- API：`POST /v1/memories`。

### 2. `patch_memory`

`PatchRequest`：`base_version?, content?, title?, important?: bool, pinned?: bool, mark_scope_drop?`

- 改 `content/title`：只允许 `note|fact|lesson`（`turn` → 422 `immutable_record`）；必须带 `base_version`，与当前版本不一致 → 409 `version_conflict`（details 带当前版本）。成功则插入新版本行（`version+1`，继承 scope、origin、confirmed=true、`created_by` 为当前 actor），旧行 `is_current=false`；登记新行的嵌入作业。
- `important/pinned=true`：在 `resolve_write_scope(ctx, mark_scope_drop)` 作用域下插入 `memory_marks`（已存在则无操作）；`false`：删除**该作用域**下的标记（其他作用域的同名标记不受影响）。
- 可以同时改内容和标记。
- API：`PATCH /v1/memories/{id}`。

### 3. `link` / `unlink`

- `link(from_memory_id, to_memory_id, purpose, note?)`：两端都必须对 ctx 可见（否则 404），不允许自环；同一 `(from, to, purpose)` 重复建立返回已有关联。返回 `relation_id`。
- `unlink(relation_id)`：两端至少一端对 ctx 可见才能删（否则 404）。
- API：`POST /v1/relations`，`DELETE /v1/relations/{id}`。

### 4. `forget`

`forget(memory_id, mode: 'deactivate' | 'reactivate' | 'delete')`

- `deactivate`：当前版本 `status='inactive'`，`generation+1`；标记和关联保留（恢复后仍生效）。`reactivate` 反之。
- `delete`：需要 principal 允许硬删除（`admin`，或 `host` 且 `allow_host_hard_delete`），否则 403。删除该 `memory_id` 的全部版本行（嵌入级联）、`memory_marks`、两个方向的 `memory_relations`；把 `payload.target` 指向它的 `user_op` 事件以及 `remember` 产生的事件 `content=NULL, payload={op, target, redacted: true}, redacted=true`。`turn` 记录也可以删除，但不删原文事件（删原文用 `forget_event`）。
- `forget_event(event_id)`：硬删除原文事件；删除 `source_event_ids` 包含它的所有记录（turn 记录整条删除——同一轮其他事件仍在，下一次该轮有新事件时会重建，这是预期行为，写进 docstring）。
- 返回受影响的对象计数 `{records, embeddings, marks, relations, events_redacted}`。
- API：`POST /v1/memories/{id}/forget`，`POST /v1/events/{id}/forget`。

### 5. `list_memories`

过滤：`types?, origin?, marks?（important|pinned）, status?（默认 active）, session_id?, scope_level?（只看某一级，例如只看 user 级：project_id IS NULL AND user_id = ctx.user）, created_from?, created_to?, q?（可选，走 lexical 简单过滤）`；排序 `recorded_at desc`；游标分页（`cursor` 为最后一条的 `(recorded_at, id)` 编码）。每项含 `memory_id, version, type, title, snippet, marks, status, origin, scope, recorded_at, relation_count`。

API：`GET /v1/memories`。

## 不做

检索排序（T02）、文档与 context（T04）、CLI（T05）。

## 验收标准

- remember 的记录在同一请求返回后立刻可被 lexical 检索到（直接用 SQL 查 `lexemes @@`，不依赖 T02）；`drain()` 后有嵌入。
- 从事件选段 remember：内容与原文切片一致，`event_time` 继承原事件。
- 更正：v1 → v2 后，当前读 v2、`get(version=1)` 仍可读 v1（直接查表验证即可，T02 合入后补 API 用例）；用旧 `base_version` 再次更正 → 409；`turn` 记录更正 → 422。
- 标记跨版本：v1 标 important，更正为 v2 后标记仍在。
- 标记作用域：在 p1 标 pinned（不去维度）后在 p2 的 ctx 下 `list_memories(marks=pinned)` 看不到；`mark_scope_drop={"project_id"}` 标记后两边都看到。取消 p1 的标记不影响 user 级标记。
- link：任一端不可见 → 404，自环 → 422，重复建立幂等。
- deactivate → 列表默认不显示、`status=inactive` 能列出；reactivate 后恢复，标记关联仍在。
- delete：全部版本、嵌入、标记、关联消失；相关 `user_op`/remember 事件被 redact（content 为 NULL）；`host` 在 `allow_host_hard_delete=false` 时 403。
- delete 竞态：remember 后不 drain，直接 delete，再 drain → 无报错、无残留嵌入。
- forget_event：派生的 turn 记录被删除。
- 每个写操作都有对应的 `user_op` 事件，payload 字段齐全。
- 所有操作都有“其他 user / 其他 project 不可操作”的测试。
