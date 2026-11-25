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
