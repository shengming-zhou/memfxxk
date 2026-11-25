# 宿主如何接入 memfxxk

宿主从自己的身份系统取得用户、Agent 和项目 ID，再用服务端保存的 host API key 调用 SDK。会话 ID 由宿主指定，在组织内保持唯一。部署和首个演示见[快速开始](../README.md)。

## 在模型调用前装载记忆

在新会话开始时创建会话状态。处理后续请求时沿用同一个 ID，宿主可读取 `get_session` 里的计划与步骤继续执行。在每次调用模型前构建上下文，传入当前用户问题作为召回查询。

```python
from memfxxk.client import MemfxxkClient
from memfxxk.domain.context import BuildContextRequest
from memfxxk.domain.sessions import PutSessionState

with MemfxxkClient(
    base_url=memfxxk_url,
    api_key=host_key,
    user_id=current_user_id,
    agent_id=current_agent_id,
    project_id=current_project_id,
) as memory:
    # 仅在宿主创建新会话时执行。
    memory.put_session_state(session_id, PutSessionState(revision=0, state={}))

    context = memory.build_context(
        BuildContextRequest(
            session_id=session_id,
            query=user_message,
            budget={"working": 2000, "evidence": 1200},
        )
    )
    tools, handle_tool = memory.tools()

    # 将 context.rendered 和 tools 交给宿主的模型调用循环。
    # 对模型请求的工具调用，执行 handle_tool(call_name, call_arguments)。
    print(context.rendered)
```

常驻文档适合宿主授权的 Persona 和项目约定。召回内容带来源，模型可以把它作为证据。工具参数用解析后的字典传入。工具调用结果作为新的工具消息发给模型，模型可继续调用工具或输出给用户。

## 在模型完成回复后捕获一轮

把用户消息、工具调用与结果、模型最终回复按同一个会话和 turn 写入。使用宿主已有的稳定请求 ID 作为 `turn_id`，同一轮重试时沿用该 ID 和输入。

```python
from memfxxk.domain.events import IngestEvent

tool_events = (
    IngestEvent(kind="tool_call", payload={"name": "test_runner", "args": ["integration"]}),
    IngestEvent(kind="tool_result", payload={"name": "test_runner", "output": "passed"}),
)
memory.ingest_turn(
    session_id,
    user_message,
    assistant_reply,
    tool_events=tool_events,
    turn_id=host_request_id,
)
```

turn 的关键词检索在写入完成后可用。部署后台 worker 后，它还会加入向量索引。

## 选择作用域和预算

用稳定的 `user_id`、`agent_id`、`project_id` 描述当前操作。默认写入使用这些限制。若用户明确要求在各项目使用同一偏好，可在保存时用 `scope_drop={"project_id"}`。为项目共享一条记忆时，可用 `scope_drop={"user_id"}`。组织由 API key 决定，宿主根据真实登录身份选择上下文。

上下文的 `documents`、`pinned`、`working`、`evidence` 有各自的 token 预算。给工作状态和最相关的证据预留空间，通过 `budget` 调整每段，或通过 `sections` 仅获取本轮所需的段。使用 `get_bundle(context.bundle_id)` 可以复核来源 ID、入选原因、分数与 token 数。
