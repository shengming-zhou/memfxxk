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

配置压缩模型后，草案的 `method` 为 `llm`，检查点保留最新决定、待办、关键标识符、工具结果、用户偏好与更正，以及待核实事项。下一次压缩会接着上一版检查点整理新关闭的轮次。最近的用户原话可随摘要进入上下文，条目会标为 `retained_user`。未配置模型时，`method` 为 `extractive_fallback`，按重要度与最近性保留原文片段。

`context_build` 返回当前记忆包的 `pressure` 和 `pressure_ratio`。默认超过可用记忆额度的 70% 时返回 `warning`。Harness 可视当前轮次状态决定是否提前压缩。此比例只统计 memfxxk 装入的记忆。系统提示、工具定义及答复预留仍由 Harness 分配。 [压缩开发说明](/developer-manual/#t16-structured-compaction)

一次 30 题长对话回放里，DeepSeek Flash 每千轮压缩约用 62 万输入 token、1.25 万输出 token。实际用量取决于消息长度和压缩频率。这个小样本里，加模型摘要答对 8 题，单用混合检索答对 7 题。模型压缩适合需要跨轮接续决定和待办的会话。 [测试条件与结果](/developer-manual/#compaction-live-evaluation)

用 `PATCH /v1/memories/{id}` 给记忆设整数 `priority` 和 `tags`。长世界观可存成 `on_demand` 文档，写入新修订后，检索结果会显示匹配章节和修订号。短而稳定的规则可固定到上下文。

### 给设定和章节设优先级 {#content-priority-usage}

你可以把小说的 `world/` 目录设为 40，把 `world/ports.md` 中的「港口 / 灯号」设为 75。这个章节用 75，同文档里未单独设置的章节继承 40。也可以用 0 或负数。数字越大，层越靠前，数字相同的内容归在同一层。任何数字的用途都由你决定。

在同一项目作用域下，用 `PUT /v1/priorities` 或 MCP `memory_priority_set` 保存。以下是给一个章节设优先级的请求体。

```json
{"target":{"kind":"section","path":"world/ports.md","heading":"港口 / 灯号"},"priority":75}
```

用 `POST /v1/priorities/resolve` 或 `memory_priority_get` 查询，传上面 `target` 的内容。返回的 `source` 和 `source_scope` 会告诉你是哪条规则生效。用 `POST /v1/priorities/clear` 或 `memory_priority_clear` 清除本次作用域的设置，系统会返回重新继承的值。[优先级实现与迁移](/developer-manual/#t17-priority-inheritance)

### 给写作上下文一份总预算 {#adaptive-context-usage}

在 HTTP `POST /v1/context/build` 或 MCP `context_build` 传 `{"allocator":"adaptive","max_tokens":1200,"query":"写北港暴风雨时的场景"}`。这 1,200 token 是给 memfxxk 的，宿主仍负责系统提示、工具、它保留的消息和回复空间。设定、对话和检索结果共用这份额度。各优先级层有机会先带一条短版，相关内容再展开。优先级高的候选在最终文本里排在前面。

宿主把响应的 `rendered` 放进模型请求，然后可以用 `GET /v1/context/bundles/{id}` 查这次的取舍。看 `sections[].items` 中的 `resolution` 和 `decision_reason`，以及 `sections[].dropped`。文档的 `ref_id`、`revision`、`section_index` 指向具体版本和章节。需要核实一条设定时，根据搜索和文档索引打开原文。默认分配方式为 `fixed`。[分配、阈值与追踪](/developer-manual/#t17-adaptive-context)

图检索需要在搜索里传 `graph_hops: 1` 或 `2`，上下文装载则放在 `search_filters`。它先查关键词和向量，再查候选记忆的关联或已审核事实的共享实体。结果包含 `graph_path`，可逐条核对这条路径。

如需把沿图找到的证据装进本轮上下文，可在 `context_build` 传 `graph_evidence_tokens`。例如总额度 1,500 token 时给图保留最多 300 token，图证据用不完的额度会回给普通证据。

如果证据预算很紧，可以把 `evidence_mode` 设为 `excerpt`。上下文会优先把搜索得到的相关片段放进去，通过原记忆的 ID 和版本仍能回查全文。

开启后台事实提取后，可以用 `GET /v1/fact-proposals` 查看提议，通过 `POST /v1/fact-proposals/{id}/review` 传 `{"accept": true}` 或 `{"accept": false}`。查询事实时，`as_of_valid` 指要问的生效日期，`as_of_recorded` 指当时系统掌握信息的截止时间。

### 使用带时间的图记忆 {#temporal-graph-usage}

设置 `MEMFXXK_STRATEGIES__GRAPH=graphiti` 后，新的对话轮次会在后台生成实体和关系。可共用默认模型，也可分别给 `entity_extract`、`entity_resolve`、`edge_extract`、`edge_resolve` 和 `node_summary` 配模型。查询传 `graph_hops=1` 或 `2`，并用 `as_of_valid` 查指定日期有效的关系。需要限定系统当时知道什么时，一并传 `as_of_recorded`。搜索返回的 `graph_fact` 包含关系文字、时间和原文 ID。用 `MEMFXXK_STRATEGIES__GRAPH=links` 可走原有图检索。 [时序图开发说明](/developer-manual/#t16-temporal-graph)

六题回放里，时序图没提高答题准确率。按这批历史的消息长度折算，每千轮建图约用 1,452 万输入和 149 万输出 token。默认仍用原有链接图检查关联。 [时序图实测](/developer-manual/#temporal-graph-evaluation)

### 查看原文、摘要或一句话 {#content-resolutions-usage}

用 `POST /v1/context/resolutions` 查看三个版本。世界观文档传 `{"kind":"document","path":"world/ports.md"}`。笔记、事实、经验和对话记录传 `{"kind":"record","id":"<memory_id>"}`，记忆块传 `{"kind":"block","id":"<block_id>"}`。文档按 Markdown 标题分节返回。修改后可以立即读取新版原文及暂用的摘录，后台会补齐新版，删除原文后也无法读到派生摘要。

短版的 `method` 会标出来源。`llm_extract` 是模型挑选的原句，没有配置摘要模型或调用失败时会得到 `extractive_fallback`。原文可用于查证具体措辞。 [详细程度与后台生成](/developer-manual/#t17-content-resolutions)

## 配置记忆模型 {#memory-model-configuration}

可以给压缩和事实提取分别配模型。`MEMFXXK_LLM__DEFAULT__MODEL`、`MEMFXXK_LLM__DEFAULT__BASE_URL` 和 `MEMFXXK_LLM__DEFAULT__API_KEY` 设默认值。`MEMFXXK_LLM__TASKS__COMPACT__MODEL` 和 `MEMFXXK_LLM__TASKS__EXTRACT__MODEL` 可分别覆盖。使用量可从 `llm_calls` 汇总，也可给组织或项目设每日 token 限额。 [模型层开发说明](/developer-manual/#t16-llm-routing)

### 选择事实写入方式 {#fact-strategy-settings}

- `MEMFXXK_STRATEGIES__FACTS=key_proposal` 会从已结束轮次提出事实，等用户确认后才写成事实记忆。原有的 `MEMFXXK_M2__EXTRACT_FACTS=true` 也能开启这条路径。
- `MEMFXXK_STRATEGIES__FACTS=mem0_classic` 会先提取，再判断旧事实要更新、失效还是保留。用户确认、显式保存或标记过的事实仍走待审。需要同时配置 `extract` 和 `reconcile` 两个任务。
- `MEMFXXK_STRATEGIES__FACTS=mem0_additive` 每轮用一次抽取，新增事实和旧说法并存，后续可以查两个版本的原话。加上 `MEMFXXK_STRATEGIES__MEM0_ADDITIVE_RANKING=true` 可在召回后给匹配的实体和较新的有效时间加权。

选择 `off` 暂停后台提取。切换策略后，已经存下的事实仍可读取。 [事实写入开发说明](/developer-manual/#t16-fact-strategies)

本次 51 轮回放折算每千轮，classic 写入约用 356 万输入、20 万输出 token，additive 约用 341 万输入、50 万输出 token。实际花费随消息长度和已有事实数量变化。 [实测明细](/developer-manual/#mem0-classic-evaluation)

### 为笔记补检索属性 {#amem-usage}

设 `MEMFXXK_STRATEGIES__NOTES=amem` 让系统为新笔记、事实和经验生成检索用的描述、关键词和标签，并给相关条目建自动关联。需要配置 `note_enrich`、`note_link`、`note_evolve`，可共用默认模型。用 `get_memory` 查看 `note_attributes`，再用 `note_history` 和 `rollback_note` 查看与恢复属性。正文和手工标签继续由用户维护。

本次四题对照，两组均答对四题。按这批历史估算，每千轮加工约用 2,792 万输入和 22.3 万输出 token。 [实现与复现实验](/developer-manual/#t16-amem)

### 给 Agent 一块可以自己编辑的记忆 {#memory-block-usage}

记忆块适合维护少量经常用的约定、偏好或交接事项。创建时填 `label`、`description`、`value`、字符上限 `limit` 和 `owner`。设 `MEMFXXK_STRATEGIES__BLOCKS=letta` 后，符合当前作用域的块会和其他文档候选一起争取上下文空间。关掉装载策略后仍可通过 API 或 SDK 打开块。

- 宿主为用户创建块时，使用用户身份 `X-Memfxxk-Actor: {"type":"user"}`。用户拥有的块默认为只读，对 Agent 的改动进入提议，由用户确认。
- 创建 `owner="agent"` 的块时指定 Agent ID。给 MCP 打开管理工具后，Agent 用 `memory_block_list` 或 `memory_block_get` 读取当前 `revision`，再用 `memory_block_edit` 追加、更换一段文字或重写。下次装载会用新版本。发生冲突时先读最新版本。
- 设置 `MEMFXXK_SLEEPTIME__EVERY_TURNS=16`，让后台每 16 个闭合对话轮检查块。也可另外设置 `MEMFXXK_SLEEPTIME__AFTER_COMPACTION=true`，让宿主确认一次压缩后触发。开启自动整理时需配置 `sleeptime` 模型，并让 Worker 处理作业。

64 轮实测里，两组都答对两道偏好题和两道改期题。后台四次整理额外用了 13,610 个 token，默认关闭自动装载和整理。 [API、复跑命令与指标](/developer-manual/#t16-memory-blocks)

### 让长期对话按主题积累 {#memoryos-usage}

默认的混合检索对全部原文做关键词和语义搜索。可选的 `MEMFXXK_STRATEGIES__TIERS=memoryos` 会让最新的 7 轮先进入候选列表，更早的轮次按主题分组，再查相关分组。两个来源共用已有的上下文上限。需要为 `persona_update` 配置模型并运行 Worker。

如果希望系统建议长期修改，可先创建 `user_profile`、`user_knowledge` 或 `agent_traits` 块并启用块装载。主题积累到热度阈值时，Worker 可能创建修改提议。可以在 `GET /v1/block-proposals` 检查和采纳，用户原有设置仍由用户确认。在一次批量回放中，写入按每千轮折算约 8 万 token。分批写入的调用频率可能更高。两组测试中分层装载都少答对一题，功能默认关闭。 [实现、配置和实测成本](/developer-manual/#t16-memoryos)

### 按日期检索并拿到带引用的答案 {#hindsight-usage}

在搜索请求的 `channels` 里加入 `time` 可使用「上个月」「三月初」或 `last Tuesday` 等表达。传带时区的 `query_time` 可以指定这些相对日期从哪天算。设置 `MEMFXXK_STRATEGIES__RECALL=hindsight` 会自动加时间排序，并让已启用的关系图一同参与评分。

启用 `MEMFXXK_STRATEGIES__REFLECT=hindsight` 并配置 `reflect`、`opinion_update` 后，可通过 `POST /v1/reflect` 或 MCP 管理工具 `memory_reflect` 请求带引用的答案。默认只返回答案和建议，只有提交 `store_opinions=true` 才会把有证据的判断存成观点。以往的判断和置信度可按版本回查。

六题验收中，反思加重排平均每题约用 4,072 token，混合组约 1,751，均包含答题和评分。不开启反思重排时，时间通道只做本地计算。反思和模型重排默认关闭。 [API、模型用量和实测结果](/developer-manual/#t16-hindsight)

### 把任务做法提炼成待审核经验 {#automatic-lesson-usage}

启用 `MEMFXXK_STRATEGIES__LESSONS=reasoningbank` 可从成功和失败轨迹总结做法。用 `awm` 可从成功轨迹归纳可复用的流程。设置 `both` 会先共用一次结果判断，再运行适用的提炼方法。需要配置记忆模型并运行 Worker。

宿主在保存最后的工具结果后调用 `POST /v1/sessions/{id}/finish-task` 或 MCP 宿主工具 `session_finish_task`。在 `GET /v1/lesson-proposals` 看待审核经验，采用后相关经验可以装入下一次任务的上下文。设置 `MEMFXXK_STRATEGIES__LESSON_VISIBILITY=searchable` 后，待审核经验可主动搜索，自动装载仍须审核通过。以前手工保存的经验继续可用。小样本里每个任务平均写入约 1.14 万到 1.66 万 token，成本随每项任务的轨迹长度变化。两种自动提炼默认关闭。 [详细用法和本轮成本](/developer-manual/#t16-automatic-lessons)

### 从一套预设开始 {#strategy-presets}

在 API 与 Worker 的环境中设置 `MEMFXXK_PRESET`，再重启服务。可选 `baseline`（混合检索）、`mem0`（加自动事实）、`graphiti`（加时序图）、`letta`（加记忆块和后台整理）、`full`（整合实验策略）。已有的 `MEMFXXK_STRATEGIES__*` 和任务模型配置能覆盖预设。保持未设置预设时会沿用原先配置。

`full` 会增加多种后台模型调用，也会在查询时使用模型重排。它适合用独立作用域试跑，你可以用 `GET /v1/context/bundles/{id}` 检查每条候选由什么策略提供，有多少 token，以及最后如何处理。 [各预设的实测与使用范围](/memory-strategy-results/)

## 查看证据评测 {#evidence-evaluation}

证据评测检查记忆库能否找回支持答案的历史，并记录查询延时。要复跑，可在独立的评测数据库使用固定的 LongMemEval_S 题集。这一步无需配置聊天模型。 [开发手册的评测步骤](https://shengming-zhou.github.io/memfxxk/developer-manual/#evidence-comparison)

问答评测用同一个模型比较无历史、近期原文和不同记忆后端。 [首轮真实模型报告](https://github.com/shengming-zhou/memfxxk/blob/main/eval/results/m1-smoke/qa-real.md)

30 题配对评测用相同的题目和记忆预算比较混合检索、加摘要和加图检索，逐题记录证据从哪里丢失。 [配对评测步骤](/developer-manual/#m2-paired-qa)

## 模型网关兼容入口 {#model-gateway}

需要从兼容客户端直接接入时，给服务配置 `MEMFXXK_GATEWAY__UPSTREAM_BASE_URL` 并启动 API。客户端把 Chat Completions 的 API 根地址指向 memfxxk 的 `/v1`，使用 memfxxk 凭据。上游模型使用服务单独配置的 `MEMFXXK_GATEWAY__UPSTREAM_API_KEY`。

当前入口支持非流式、单回复文本聊天。用 `X-Memfxxk-Session` 指定可复用会话，用 `Idempotency-Key` 去重保存当轮消息。需要模型主动查找和管理记忆时，使用 MCP Harness。
