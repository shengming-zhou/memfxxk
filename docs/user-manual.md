# memfxxk 用户手册 {#memfxxk}

<div id="_1" class="legacy-anchor" aria-hidden="true"></div>

## 本地启动 {#local-setup}

准备 Python 3.12、uv 和 Docker Compose。在仓库根目录执行以下命令，开发数据库会在本机的 5433 端口启动。

```sh
docker compose up -d --wait db
uv sync --locked
uv run memfxxk db upgrade
uv run memfxxk keys create --org demo --role host --name local
uv run memfxxk serve
```

创建命令只显示一次明文 key。将它保存在调用方的密钥配置中。服务默认监听本机 8000 端口，交互式 API 文档在 `/docs`。

<div id="_2" class="legacy-anchor" aria-hidden="true"></div>

## 检查连接 {#check-connection}

设置调用方的 `MEMFXXK_API_KEY` 环境变量，再请求健康接口。以下示例使用 PowerShell。

```powershell
$headers = @{ Authorization = "Bearer $env:MEMFXXK_API_KEY" }
Invoke-RestMethod http://localhost:8000/v1/healthz -Headers $headers
```

服务和数据库可用时返回 `{"status":"ok"}`。请求缺少有效 key 时返回 401。可通过 `.env.example` 查看数据库和其他运行配置。

<div id="_3" class="legacy-anchor" aria-hidden="true"></div>

## 记录和回读一次对话 {#record-and-read-events}

同一会话使用同一个 `session_id`。一次用户提问、工具调用和助手回复可以共用 `turn_id`。重试同一个事件时沿用 `idempotency_key`，服务会返回已经保存的事件 ID。

```powershell
$headers = @{
    Authorization = "Bearer $env:MEMFXXK_API_KEY"
    "X-Memfxxk-User" = "demo-user"
    "X-Memfxxk-Project" = "demo-project"
}
$body = @{ events = @(
    @{
        kind = "message"
        role = "user"
        content = "The demo project uses Python 3.12"
        session_id = "demo-session"
        turn_id = "turn-1"
        idempotency_key = "demo-session-turn-1-user"
    }
) } | ConvertTo-Json -Depth 5

$saved = Invoke-RestMethod http://localhost:8000/v1/events -Method Post `
    -Headers $headers -ContentType "application/json" -Body $body
$saved
Invoke-RestMethod "http://localhost:8000/v1/events/$($saved[0].event_id)" -Headers $headers
Invoke-RestMethod "http://localhost:8000/v1/sessions/demo-session/events?from_seq=1&limit=20" `
    -Headers $headers
```

批量写入结果按输入排序，包含事件 ID、会话序号、是否重复和受影响的记忆 ID。回读请使用相同的用户和项目头。工具输出的原文保存在事件里，派生的 turn 记录对长文本只保留头尾。

<div id="_4" class="legacy-anchor" aria-hidden="true"></div>

## 运行索引作业 {#run-indexing}

默认随 API 启动一个后台 Worker，使用确定性假嵌入，适合本地开发。可通过 `MEMFXXK_EMBEDDING__PROVIDER=openai_compatible` 接入兼容的嵌入服务，并设置端点、模型和维度。要用独立 Worker，在 API 进程设置 `MEMFXXK_WORKER__EMBEDDED=false`。

```sh
uv run memfxxk worker
uv run memfxxk index ensure
uv run memfxxk index reembed
```

`index ensure` 为当前模型和维度创建 HNSW 索引，可重复执行。`index reembed` 为当前记录登记作业，可用 `--model` 显式指定模型。同一个模型标识应对应固定的向量维度。完整 HTTP 接口见服务的 `/docs`。

## 主动管理记忆 {#manage-memory}

在运行 CLI 的终端设置 `MEMFXXK_API_KEY`、`MEMFXXK_USER` 和 `MEMFXXK_PROJECT`。在 PowerShell 中可用 `$env:MEMFXXK_USER = "demo-user"` 设置环境变量。跨项目的个人笔记可显式使用 `--user-level`。

```sh
uv run memfxxk remember "发布前先跑 smoke test" --important --pin
uv run memfxxk list --important
uv run memfxxk search "发布流程"
uv run memfxxk show MEMORY_ID --window 1
uv run memfxxk mark MEMORY_ID important --off
uv run memfxxk link FAILURE_ID FIX_ID --purpose fixes
```

`important` 影响检索排序，`pinned` 将记忆加入上下文的固定区。`edit` 打开编辑器并保存新版本，`show --version` 可回读旧版本。`forget` 默认停用，`forget --delete` 永久删除并要求确认。

## 文档与上下文 {#documents-and-context}

```sh
uv run memfxxk doc put project.md --content "Release on Tuesdays"
uv run memfxxk doc history project.md
uv run memfxxk context demo-session "如何发布？" --explain
```

`persona.md` 和 `project.md` 随作用域加载。上下文按预算组装文档、固定记忆、近期会话和检索证据。`--explain` 显示每段的 token 使用量及选入原因。开发 Agent 演示可用 `uv run python examples/demo_dev_agent.py`，它会验证重要标记、故障与修复的关联、跨会话恢复及发布约定的版本历史。

## 接入 Agent {#connect-an-agent}

用仓库工作目录和与 API 相同的 key 启动 stdio MCP 命令 `uv run memfxxk mcp`。宿主需向该进程传入 API 地址和用户、项目、Agent 作用域。默认工具为 `memory_search`、`memory_get`、`memory_remember`。显式设置 `MEMFXXK_MCP_ENABLE_MANAGE=true` 可增加 `memory_mark`、`memory_link`。Python 宿主可以使用同步或异步 SDK 直接组装上下文。

[宿主接入指南和演示](https://github.com/shengming-zhou/memfxxk/blob/main/docs/usage.md)

仓库提供通用 MCP Harness 示例。重复使用会话 ID 可以从已保存的会话接着聊。宿主直接调用建上下文、压缩和事件工具，只把允许的检索与管理工具交给模型。

```sh
uv run python -m examples.mcp_harness --session inventory --manage "查一下上次的库存导入经验"
```

设置 `MEMFXXK_DEMO_MODEL_URL`、`MEMFXXK_DEMO_MODEL` 和需要时的 `MEMFXXK_DEMO_MODEL_API_KEY` 即可连接模型端点。省去模型配置时运行本地演示路径。使用 `--help` 查看上下文与输出预算参数。

## 团队项目凭据 {#project-credentials}

管理员可给成员签发绑定用户和项目的凭据。成员可省略作用域请求头，服务会从凭据填入对应身份。用 `--access read` 签发只读凭据，用 `--access write` 允许项目内协作，并可用 `--agent` 绑定指定 Agent。

项目写入凭据可以停用和恢复记忆。永久删除记忆、原文或文档由管理员或获准的宿主执行。

```sh
uv run memfxxk keys create --org acme --role project --name alice-inventory --project inventory --user alice --access write
uv run memfxxk keys list --org acme
```

在项目里保存共享记忆时，去掉用户和 Agent 两个写入维度，保留项目维度。成员按作用域读取项目共用记录和自己的私有记录。管理员使用 `keys revoke KEY_ID --org acme` 撤销访问。

## 保存和复用经验 {#procedural-memory}

遇到一次失败并验证了修复后，宿主可以把这次做法保存成经验。先用事件接口保存两个 `tool_result`，分别在 `payload.outcome` 里写入 `failure` 和 `success`。确认适用场景、前置条件和步骤后，用 `POST /v1/lessons` 保存，Python SDK 也可以直接调用。

```python
from memfxxk.domain.manage import SaveLessonRequest

saved = client.save_lesson(
    SaveLessonRequest(
        title="导入分号分隔的库存文件",
        scenario="导入时报缺少 quantity 列",
        prerequisites=["文件表头使用分号分隔"],
        steps=["读取表头并选择分号作为分隔符", "导入后核对数量合计"],
        result="quantity 列可读，数量合计与原文件一致",
        failure_event_id=failed_event_id,
        verification_event_id=verified_event_id,
        idempotency_key="warehouse-export-v1",
    )
)
```

下次遇到类似任务时，用 `memory_search` 或 `POST /v1/search` 搜索，并传 `types: ["lesson"]`。用记忆 ID 打开全文和出处，再核对当前任务的前置条件。需要宿主自动装载时，在 `context_build` 的 `search_filters.types` 中加入 `lesson`，也可以固定常用经验。开启 MCP 管理工具后，`memory_save_lesson` 接收相同的保存参数。开启宿主工具后，可以用 `event_get` 回读原文。

## 迁移 Markdown 工作区 {#markdown-workspace}

按当前请求的作用域导出文档，再切换项目导入。导出目录需要是新目录，导入目录也可以是已有的 UTF-8 Markdown 文件夹。

```sh
uv run memfxxk --project source workspace export ./workspace-copy
uv run memfxxk --project target workspace import ./workspace-copy
```

快照保留文档正文、用途和装载策略。导出时可以重复传 `--lesson` 指定要带走的经验 ID。选中的经验保存为 Markdown，原始失败和验证结果随 `workspace.json` 一起迁移。导入后会生成新的本地引用，可以在目标项目回查证据。

相同内容重复导入会跳过。若目标已有不同的内容，导入会报冲突并保留目标内容。编辑本项目刚导出的 `documents/` 后，可以加 `--update` 导回本项目，服务会检查导出时的版本是否仍是当前版本。快照迁移的是当前正文，历史版本和用户标记留在原工作区。

## 长会话与图检索 {#long-sessions-and-graph}

Harness 决定什么时候取记忆、压缩旧消息和调用模型。每轮先给系统提示、工具定义、当前输入和输出预留空间，再向 `POST /v1/context/build` 传 `max_tokens`。若 Harness 已带上了从某个会话序号开始的连续原文，可传 `host_history_from_seq` 避免重复装载。

压缩走两步。先通过 `POST /v1/context/compactions` 提交会话、当前修订、已经结束的轮次截止序号和摘要上限。检查草案后，用 `POST /v1/context/compactions/commit` 提交 `draft_id`。之后还能用摘要里引用的事件 ID 回查原文。仓库里的参考程序是 `examples/host_loop.py`。

重要度可通过 `PATCH /v1/memories/{id}` 设置 `priority` 1 到 5，并设置 `tags`。实用做法是把短而稳定的规则固定，把长世界观存成 `on_demand` 文档。写入新修订时会按 Markdown 标题切块，搜索结果会给出章节和修订。已有文档可先按路径读取，并在下次保存时生成可检索的章节。

图检索需要在搜索里传 `graph_hops: 1` 或 `2`，上下文装载则放在 `search_filters`。它先查关键词和向量，再查候选记忆的关联或已审核事实的共享实体。结果包含 `graph_path`，可逐条核对这条路径。

如需把沿图找到的证据装进本轮上下文，可在 `context_build` 传 `graph_evidence_tokens`。例如总额度 1,500 token 时给图保留最多 300 token，图证据用不完的额度会回给普通证据。

如果证据预算很紧，可以把 `evidence_mode` 设为 `excerpt`。上下文会优先把搜索得到的相关片段放进去，通过原记忆的 ID 和版本仍能回查全文。

开启后台事实提取后，可以用 `GET /v1/fact-proposals` 查看提议，通过 `POST /v1/fact-proposals/{id}/review` 传 `{"accept": true}` 或 `{"accept": false}`。查询事实时，`as_of_valid` 指要问的生效日期，`as_of_recorded` 指当时系统掌握信息的截止时间。

## 配置记忆模型 {#memory-model-configuration}

可以给压缩和事实提取分别配模型。`MEMFXXK_LLM__DEFAULT__MODEL`、`MEMFXXK_LLM__DEFAULT__BASE_URL` 和 `MEMFXXK_LLM__DEFAULT__API_KEY` 设默认值。`MEMFXXK_LLM__TASKS__COMPACT__MODEL` 和 `MEMFXXK_LLM__TASKS__EXTRACT__MODEL` 可分别覆盖。事实提取还需要打开 `MEMFXXK_M2__EXTRACT_FACTS=true`。模型调用成本取决于历史大小与触发频率。使用量可从 `llm_calls` 汇总，也可给组织或项目设每日 token 限额。 [模型层开发说明](/developer-manual/#t16-llm-routing)

## 查看证据评测 {#evidence-evaluation}

证据评测检查记忆库能否找回支持答案的历史，并记录查询延时。要复跑，可在独立的评测数据库使用固定的 LongMemEval_S 题集。这一步无需配置聊天模型。 [开发手册的评测步骤](https://shengming-zhou.github.io/memfxxk/developer-manual/#evidence-comparison)

问答评测用同一个模型比较无历史、近期原文和不同记忆后端。 [首轮真实模型报告](https://github.com/shengming-zhou/memfxxk/blob/main/eval/results/m1-smoke/qa-real.md)

30 题配对评测用相同的题目和记忆预算比较混合检索、加摘要和加图检索，逐题记录证据从哪里丢失。 [配对评测步骤](/developer-manual/#m2-paired-qa)

## 模型网关兼容入口 {#model-gateway}

需要从兼容客户端直接接入时，给服务配置 `MEMFXXK_GATEWAY__UPSTREAM_BASE_URL` 并启动 API。客户端把 Chat Completions 的 API 根地址指向 memfxxk 的 `/v1`，使用 memfxxk 凭据。上游模型使用服务单独配置的 `MEMFXXK_GATEWAY__UPSTREAM_API_KEY`。

当前入口支持非流式、单回复文本聊天。用 `X-Memfxxk-Session` 指定可复用会话，用 `Idempotency-Key` 去重保存当轮消息。需要模型主动查找和管理记忆时，使用 MCP Harness。
