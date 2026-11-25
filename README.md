# memfxxk

memfxxk 为 Agent 提供会话记录、混合检索、可编辑的长期记忆和按预算组装的上下文。用户可以手动标记重要记忆、固定发布约定，或把故障记录关联到修复。

[文档站](https://shengming-zhou.github.io/memfxxk/) · [发布记录](https://github.com/shengming-zhou/memfxxk/actions/workflows/docs.yml)

- [用户手册](docs/user-manual.md) 介绍本地启动、记忆管理和 MCP。
- [开发手册](docs/developer-manual.md) 介绍代码边界、配置、测试和实现进度。
- [宿主接入指南](docs/usage.md) 包含 Python SDK、工具调用和上下文注入用法。

## 快速开始

```sh
docker compose up -d --wait db
uv sync --locked
uv run memfxxk db upgrade
uv run memfxxk keys create --org demo --role host --name local
uv run memfxxk serve
```

在另一终端将生成的 key 设为 `MEMFXXK_API_KEY`，运行 `uv run python examples/demo_dev_agent.py`。演示无需外部模型。

## MCP 接入

在主机的环境中设置 `MEMFXXK_API_KEY`。下列示例使用本机 API 和独立的 `dogfood` 项目。API 地址可通过 `MEMFXXK_URL` 修改。

Codex 的 `config.toml` 中将 `cwd` 指向仓库绝对路径。

```toml
[mcp_servers.memfxxk]
command = "uv"
args = ["run", "--locked", "memfxxk", "mcp"]
cwd = "/absolute/path/to/memfxxk"
env_vars = ["MEMFXXK_API_KEY"]

[mcp_servers.memfxxk.env]
MEMFXXK_USER = "developer"
MEMFXXK_AGENT = "codex"
MEMFXXK_PROJECT = "dogfood"
```

[Codex MCP 配置](https://developers.openai.com/codex/mcp)

Claude Code 从仓库工作目录启动时可读取以下 `.mcp.json`。

```json
{
  "mcpServers": {
    "memfxxk": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--locked", "memfxxk", "mcp"],
      "env": {
        "MEMFXXK_API_KEY": "${MEMFXXK_API_KEY}",
        "MEMFXXK_USER": "developer",
        "MEMFXXK_AGENT": "claude-code",
        "MEMFXXK_PROJECT": "dogfood"
      }
    }
  }
}
```

[Claude Code MCP 配置](https://code.claude.com/docs/en/mcp)
