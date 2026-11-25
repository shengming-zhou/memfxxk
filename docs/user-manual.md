# memfxxk 用户手册

## 本地启动

准备 Python 3.12、uv 和 Docker Compose。在仓库根目录执行以下命令，开发数据库会在本机的 5433 端口启动。

```sh
docker compose up -d --wait db
uv sync --locked
uv run memfxxk db upgrade
uv run memfxxk keys create --org demo --role host --name local
uv run uvicorn memfxxk.api:create_app --factory
```

创建命令只显示一次明文 key。将它保存在调用方的密钥配置中。服务默认监听本机 8000 端口，交互式 API 文档在 `/docs`。

## 检查连接

设置调用方的 `MEMFXXK_KEY` 环境变量，再请求健康接口。以下示例使用 PowerShell。

```powershell
$headers = @{ Authorization = "Bearer $env:MEMFXXK_KEY" }
Invoke-RestMethod http://localhost:8000/v1/healthz -Headers $headers
```

服务和数据库可用时返回 `{"status":"ok"}`。请求缺少有效 key 时返回 401。可通过 `.env.example` 查看数据库和其他运行配置。

## 记录和回读一次对话

同一会话使用同一个 `session_id`。一次用户提问、工具调用和助手回复可以共用 `turn_id`。重试同一个事件时沿用 `idempotency_key`，服务会返回已经保存的事件 ID。

```powershell
$headers = @{
    Authorization = "Bearer $env:MEMFXXK_KEY"
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

## 运行索引作业

默认随 API 启动一个后台 Worker，使用确定性假嵌入，适合本地开发。可通过 `MEMFXXK_EMBEDDING__PROVIDER=openai_compatible` 接入兼容的嵌入服务，并设置端点、模型和维度。要用独立 Worker，在 API 进程设置 `MEMFXXK_WORKER__EMBEDDED=false`。

```sh
uv run memfxxk worker
uv run memfxxk index ensure
uv run memfxxk index reembed
```

`index ensure` 为当前模型和维度创建 HNSW 索引，可重复执行。`index reembed` 为当前记录登记作业，可用 `--model` 显式指定模型。同一个模型标识应对应固定的向量维度。对外可用的事件接口见服务的 `/docs`，检索接口在后续任务中接入。
