"""Run a two-session development-agent demo with the HTTP SDK."""

from datetime import UTC, datetime, timedelta
from os import environ
from uuid import uuid4

from memfxxk.client import MemfxxkClient
from memfxxk.domain.context import BuildContextRequest, ContextBundle
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import LinkRequest, PatchRequest, RememberRequest
from memfxxk.domain.sessions import PutSessionState

QUERY = "CI 又挂了，upload timeout test_upload.py::test_large_file"
REPAIR = "已验证修复：分块上传，超时 120s，回归测试通过"
RULE_V1 = "发布前必须跑完整集成测试"
RULE_V2 = "发布前跑完整集成测试和 smoke test"


def explain(bundle: ContextBundle) -> None:
    print(f"Context {bundle.bundle_id} · {bundle.total_tokens} tokens")
    for section in bundle.sections:
        print(f"  {section.name} · {section.used_tokens}/{section.budget} tokens")
        for item in section.items:
            print(f"    {item.ref_id} · {item.tokens} tokens · {item.reason}")
            if item.related:
                print(f"      linked evidence {', '.join(map(str, item.related))}")


def run_demo(client: MemfxxkClient) -> ContextBundle:
    run = uuid4().hex[:12]
    first_session = f"demo-{run}-day-1"
    next_session = f"demo-{run}-day-2"
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    steps = (
        ("CI 最近为什么不稳定？", "先看后台任务和最近提交。"),
        ("检查测试日志和退出状态", "定位异常栈与失败步骤。"),
        (
            "CI upload timeout in test_upload.py::test_large_file",
            "CI upload timeout reproduced on test_upload.py::test_large_file.",
        ),
        ("先记录修复候选", "改为分块上传，把请求超时上限设为 120s。"),
        ("重跑回归测试", "分块路径验证成功。"),
        ("沉淀已验证结果", "记录复现条件和修改范围。"),
        ("保存今天的状态", "明天继续观察构建健康度。"),
    )

    print("Day 1 · Diagnose, repair and validate a CI failure")
    failure = None
    for index, (user, assistant) in enumerate(steps):
        tools = ()
        if index in (2, 4):
            output = (
                "CI upload timeout: test_upload.py::test_large_file timed out after 30s"
                if index == 2
                else "Regression suite passed · chunked transfer and 120s limit confirmed"
            )
            tools = (
                IngestEvent(kind="tool_call", payload={"name": "pytest", "args": ["-q"]}),
                IngestEvent(kind="tool_result", payload={"name": "pytest", "output": output}),
            )
        result = client.ingest_turn(
            first_session,
            user,
            assistant,
            tool_events=tools,
            turn_id=f"turn-{index + 1}",
            event_time=yesterday + timedelta(minutes=index),
        )
        if index == 2:
            failure = result[0].memory_ids_touched[0]
    assert failure is not None

    plan = {"plan": ["定位失败", "复现", "验证修复"], "current_step": "观察健康度"}
    client.put_session_state(first_session, PutSessionState(revision=0, state=plan))
    client.patch_memory(failure, PatchRequest(important=True))
    fix = client.remember(RememberRequest(content=REPAIR))
    client.link(LinkRequest(from_memory_id=failure, to_memory_id=fix.memory_id, purpose="fixes"))
    pinned = client.remember(RememberRequest(content="发布只在周二", pinned=True))
    first_doc = client.put_document(
        "project.md", PutDocumentRequest(content=RULE_V1, purpose="发布约定")
    )
    print(f"Marked failure {failure} as important and linked verified fix {fix.memory_id}")

    print("Day 2 · Resume the plan in a new session and recall the prior fix")
    state = client.get_session(first_session)
    assert state.state == plan
    print(f"Restored plan {state.state['plan']} · step {state.state['current_step']}")
    client.put_session_state(next_session, PutSessionState(revision=0, state=state.state))
    client.ingest_turn(next_session, QUERY, "先检查昨天的失败和验证过的修复。", event_time=now)
    request = BuildContextRequest(session_id=next_session, query=QUERY)
    recalled = client.build_context(request)
    explain(recalled)
    evidence = next(section for section in recalled.sections if section.name == "evidence")
    assert evidence.items and evidence.items[0].ref_id == str(failure), evidence.items
    assert fix.memory_id in evidence.items[0].related
    _, handle = client.tools()
    neighbor_text = handle("memory_get", {"id": str(failure), "window": 1})
    assert "分块上传" in neighbor_text, neighbor_text
    print("memory_get expanded the neighboring repair turn")

    updated_doc = client.put_document(
        "project.md", PutDocumentRequest(content=RULE_V2, base_revision=first_doc.revision)
    )
    client.patch_memory(
        pinned.memory_id,
        PatchRequest(base_version=pinned.version, content="发布只在周二，smoke test 后发布"),
    )
    updated = client.build_context(request)
    assert RULE_V2 in updated.rendered
    assert "发布只在周二，smoke test 后发布" in updated.rendered
    assert client.get_document("project.md", revision=first_doc.revision).content == RULE_V1
    assert {row.revision for row in client.list_revisions("project.md")} == {
        first_doc.revision,
        updated_doc.revision,
    }
    print("Release policy v2 loaded and v1 remains readable in history")
    explain(updated)
    return updated


def main() -> None:
    api_key = environ.get("MEMFXXK_API_KEY")
    if not api_key:
        raise SystemExit("Set MEMFXXK_API_KEY before running the demo.")
    project = environ.get("MEMFXXK_PROJECT") or f"demo-{uuid4().hex[:8]}"
    print(f"Demo project {project}")
    with MemfxxkClient(
        environ.get("MEMFXXK_URL", "http://127.0.0.1:8000"),
        api_key,
        user_id=environ.get("MEMFXXK_USER", "demo-user"),
        agent_id=environ.get("MEMFXXK_AGENT", "demo-agent"),
        project_id=project,
    ) as client:
        run_demo(client)


if __name__ == "__main__":
    main()
