import asyncio
import runpy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from memfxxk.client import (
    AsyncMemfxxkClient,
    Conflict,
    MemfxxkClient,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.manage import LinkRequest, ListRequest, PatchRequest, RememberRequest
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.domain.sessions import PutSessionState
from memfxxk.service import MemoryService
from memfxxk.tools import HttpBackend, ServiceBackend, dispatch

pytestmark = pytest.mark.integration


def _key(api_client: TestClient) -> str:
    return api_client.headers["Authorization"].removeprefix("Bearer ")


def test_async_sdk_roundtrip(api_client: TestClient) -> None:
    async def check() -> None:
        async with AsyncMemfxxkClient(
            "http://testserver/v1",
            _key(api_client),
            user_id="sdk-user",
            project_id="sdk-project",
            transport=httpx.ASGITransport(app=api_client.app),
        ) as client:
            assert await client.healthz() == {"status": "ok"}
            args = ("sdk-session", "Track unique-argon", "Tracked.")
            first = await client.ingest_turn(*args, turn_id="stable")
            second = await client.ingest_turn(*args, turn_id="stable")
            assert [item.event_id for item in first] == [item.event_id for item in second]
            assert all(item.duplicate for item in second)
            assert (await client.get_event(first[0].event_id)).content == args[1]
            assert len(await client.list_session_events("sdk-session")) == 2

            request = SearchRequest(query="unique-argon", channels=["lexical"])
            hit = (await client.search(request)).hits[0]
            assert (await client.get_memory(hit.memory_id)).session_id == args[0]
            async with client.with_ctx(project_id="other") as other:
                with pytest.raises(NotFound):
                    await other.get_memory(hit.memory_id)
                assert not (await other.search(request)).hits
            assert await client.healthz() == {"status": "ok"}

            note = await client.remember(RememberRequest(content="SDK review", important=True))
            page = await client.list_memories(ListRequest(marks=["important"]))
            assert [item.memory_id for item in page.items] == [note.memory_id]
            updated = await client.patch_memory(
                note.memory_id, PatchRequest(content="SDK approved", base_version=note.version)
            )
            assert updated.version > note.version
            with pytest.raises(Conflict) as conflict:
                await client.patch_memory(
                    note.memory_id, PatchRequest(content="too old", base_version=note.version)
                )
            assert conflict.value.current_version == updated.version
            edge = await client.link(
                LinkRequest(
                    from_memory_id=hit.memory_id, to_memory_id=note.memory_id, purpose="fixes"
                )
            )
            await client.unlink(edge.relation_id)
            await client.forget(note.memory_id)
            assert (await client.get_memory(note.memory_id)).status == "inactive"

            doc_path = "team/policy.md"
            doc = await client.put_document(doc_path, PutDocumentRequest(content="Policy alpha"))
            updated_doc = await client.put_document(
                doc_path, PutDocumentRequest(content="Policy beta", base_revision=doc.revision)
            )
            assert (await client.get_document(doc_path, doc.revision)).content == "Policy alpha"
            assert (await client.get_document(doc_path)).content == "Policy beta"
            assert {item.revision for item in await client.list_revisions(doc_path)} == {
                doc.revision,
                updated_doc.revision,
            }
            assert [item.path for item in await client.list_documents()] == [doc_path]
            await client.delete_document(doc_path)
            with pytest.raises(NotFound):
                await client.get_document(doc_path)

            session = await client.get_session(args[0])
            state = await client.put_session_state(
                args[0], PutSessionState(revision=session.revision, state={"step": 1})
            )
            assert state.state == {"step": 1}
            context = await client.build_context(args[0], "unique-argon", evidence=100)
            assert "unique-argon" in context.rendered
            assert (await client.get_bundle(context.bundle_id)).total_tokens == context.total_tokens
            _, handle = client.tools()
            assert "unique-argon" in await handle("memory_search", {"query": "unique-argon"})

            await client.forget_event(first[0].event_id)
            with pytest.raises(NotFound):
                await client.get_event(first[0].event_id)

        async with AsyncMemfxxkClient(
            "http://testserver", "invalid", transport=httpx.ASGITransport(app=api_client.app)
        ) as unauthorized:
            with pytest.raises(PermissionDenied):
                await unauthorized.healthz()

    asyncio.run(check())


class _AppTransport(httpx.BaseTransport):
    def __init__(self, api_client: TestClient) -> None:
        self.api_client = api_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        forwarded = self.api_client.build_request(
            request.method,
            str(request.url),
            content=request.read(),
        )
        forwarded.headers.clear()
        forwarded.headers.update(request.headers.raw)
        response = self.api_client.send(forwarded)
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )


def test_tool_backend_parity_and_host_example(
    api_client: TestClient, service: MemoryService
) -> None:
    ctx = RequestContext(org_id="test-org", user_id="sdk-user", project_id="sdk-project")
    local = ServiceBackend(service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx))
    with MemfxxkClient(
        "http://testserver",
        _key(api_client),
        user_id=ctx.user_id,
        project_id=ctx.project_id,
        transport=_AppTransport(api_client),
    ) as client:
        client.remember(RememberRequest(content="Tool parity cobalt"))
        client.put_document("policy.md", PutDocumentRequest(content="Policy tool parity"))
        http = HttpBackend(client)
        for name, args in (
            ("memory_search", {"query": "cobalt"}),
            ("memory_get", {"id": "doc:policy.md"}),
        ):
            direct = dispatch(local, name, args)
            assert direct == dispatch(http, name, args)
            assert "parity" in direct
        with pytest.raises(ValidationError):
            client.list_session_events("not-started", limit=0)

        script = Path(__file__).resolve().parents[2] / "examples" / "host_loop.py"
        run_turn = runpy.run_path(str(script))["run_turn"]
        assert run_turn(client, "example", "Track weekly builds.").startswith("Model call skipped.")
        events = client.list_session_events("example")
        assert events[0].content == "Track weekly builds."
        assert events[1].role == "assistant"
