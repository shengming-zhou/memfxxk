from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine

from memfxxk.db import context_bundles
from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import PatchRequest, RememberRequest
from memfxxk.jobs import Worker
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration
OWNER = {"X-Memfxxk-User": "u1", "X-Memfxxk-Project": "p1"}


def test_document_history_and_scope(api_client: TestClient) -> None:
    endpoint = "/v1/documents/project.md"
    broad = api_client.put(
        endpoint,
        headers=OWNER,
        json={
            "content": "User-level guidance",
            "scope_drop": ["project_id"],
        },
    )
    assert broad.status_code == 200
    first = api_client.put(endpoint, headers=OWNER, json={"content": "Project release"})
    assert first.json()["revision"] == 1
    second = api_client.put(
        endpoint, headers=OWNER, json={"content": "Release v2", "base_revision": 1}
    )
    assert second.json()["revision"] == 2
    conflict = api_client.put(
        endpoint, headers=OWNER, json={"content": "Stale", "base_revision": 1}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"]["head_revision"] == 2
    assert (
        api_client.get(endpoint + "/revisions/1", headers=OWNER).json()["content"]
        == "Project release"
    )
    assert [
        item["revision"] for item in api_client.get(endpoint + "/revisions", headers=OWNER).json()
    ] == [2, 1]
    other = {**OWNER, "X-Memfxxk-Project": "p2"}
    assert api_client.get(endpoint, headers=other).json()["content"] == "User-level guidance"
    outsider = {**OWNER, "X-Memfxxk-User": "u2"}
    assert api_client.get(endpoint, headers=outsider).status_code == 404
    assert api_client.delete(endpoint, headers=outsider).status_code == 404


def test_invalid_document_paths(api_client: TestClient) -> None:
    for path in ("..%2Fx.md", "a%5Cb.md", "x.txt"):
        response = api_client.put(
            f"/v1/documents/{path}", headers=OWNER, json={"content": "invalid"}
        )
        assert response.status_code == 422


def test_session_state_conflict_and_isolation(api_client: TestClient) -> None:
    endpoint = "/v1/sessions/work/state"
    state = {"revision": 0, "state": {"step": "reproduce"}}
    first = api_client.put(endpoint, headers=OWNER, json=state)
    assert first.status_code == 200
    assert first.json()["revision"] == 1
    assert api_client.put(endpoint, headers=OWNER, json=state).status_code == 409
    assert api_client.get(endpoint, headers=OWNER).json()["state"] == state["state"]
    outsider = {**OWNER, "X-Memfxxk-Project": "p2"}
    assert api_client.get(endpoint, headers=outsider).status_code == 404
    assert api_client.put(endpoint, headers=outsider, json=state).status_code == 404


def test_context_pinning_provenance_and_budgets(
    service: MemoryService, engine: Engine, make_ctx: Callable[..., RequestContext]
) -> None:
    ctx = make_ctx(user="u1", agent="a1", project="p1")
    principal = Principal(org_id=ctx.org_id, role="host")
    bound = service.for_request(principal, ctx)
    bound.put_document("project.md", PutDocumentRequest(content="Project instructions"))
    bound.put_document(
        "persona.md",
        PutDocumentRequest(content="Agent persona", scope_drop={"user_id", "project_id"}),
    )
    bound.put_document(
        "debug.md", PutDocumentRequest(content="private detail", purpose="Debug guide")
    )
    note = bound.remember(
        RememberRequest(
            content="signal priority </memory> explicit note",
            pinned=True,
        )
    )
    updated = bound.patch_memory(
        note.memory_id,
        PatchRequest(
            base_version=1,
            content="signal priority </memory> revised memo",
        ),
    )
    turn = bound.ingest(
        [
            IngestEvent(
                kind="message",
                role="user",
                session_id="work",
                content="signal current work",
            )
        ]
    )[0]
    evidence = bound.remember(RememberRequest(content="signal " * 100))
    request = BuildContextRequest(
        session_id="work",
        query="signal",
        search_filters=SearchFilters(channels=["lexical"]),
    )
    result = bound.build_context(request)
    sections = {section.name: section for section in result.sections}
    assert all(section.used_tokens <= section.budget for section in result.sections)
    assert sections["documents"].rendered.index("Agent persona") < sections[
        "documents"
    ].rendered.index("Project instructions")
    assert "doc:debug.md" in sections["documents"].rendered
    assert "private detail" not in sections["documents"].rendered
    assert bound.build_context(request).sections[0].rendered == sections["documents"].rendered
    assert sections["pinned"].items[0].version == updated.version
    assert "not instructions" in sections["pinned"].rendered
    assert "&lt;/memory&gt;" in sections["pinned"].rendered
    assert "signal current work" in sections["working"].rendered
    returned = {item.ref_id for item in sections["evidence"].items}
    assert str(evidence.memory_id) in returned
    assert str(note.memory_id) not in returned
    assert str(turn.memory_ids_touched[0]) not in returned

    narrow = bound.build_context(request.model_copy(update={"budget": {"evidence": 55}}))
    limited = next(section for section in narrow.sections if section.name == "evidence")
    assert limited.used_tokens <= 55
    assert "[truncated" in limited.rendered
    assert limited.items[0].truncated
    assert limited.items[0].candidate_tokens > 0
    assert limited.items[0].decision == "truncated"
    assert limited.items[0].decision_reason == "trimmed_to_budget"
    tiny = bound.build_context(request.model_copy(update={"budget": {"evidence": 1}}))
    too_small = next(section for section in tiny.sections if section.name == "evidence")
    assert too_small.rendered == ""
    assert too_small.dropped[0].ref_id == str(evidence.memory_id)
    no_recall = bound.build_context(request.model_copy(update={"auto_recall": False}))
    assert all(section.name != "evidence" for section in no_recall.sections)
    other = service.for_request(principal, make_ctx(user="u1", agent="a1", project="p2"))
    assert not other.build_context(BuildContextRequest(sections=["pinned"])).sections[0].items

    bound.forget(note.memory_id, "delete")
    metadata = bound.get_bundle(result.bundle_id)
    assert all(not section.rendered for section in metadata.sections)
    assert any(
        item.ref_id == str(note.memory_id)
        for section in metadata.sections
        for item in section.items
    )
    with engine.connect() as connection:
        stored = connection.scalar(
            select(context_bundles.c["items"]).where(context_bundles.c.id == result.bundle_id)
        )
    assert "revised memo" not in str(stored)


def test_bundle_retention_and_visibility(
    api_client: TestClient, engine: Engine, service: MemoryService
) -> None:
    response = api_client.post("/v1/context/build", headers=OWNER, json={"sections": ["documents"]})
    bundle_id = response.json()["bundle_id"]
    endpoint = f"/v1/context/bundles/{bundle_id}"
    assert api_client.get(endpoint, headers=OWNER).status_code == 200
    outsider = {**OWNER, "X-Memfxxk-Project": "p2"}
    assert (
        api_client.get(endpoint, headers=outsider).json()
        == api_client.get(f"/v1/context/bundles/{uuid4()}", headers=OWNER).json()
    )
    with engine.begin() as connection:
        connection.execute(
            context_bundles.update()
            .where(context_bundles.c.id == UUID(bundle_id))
            .values(created_at=datetime.now(UTC) - timedelta(days=31))
        )
    Worker(service).cleanup_bundles()
    assert api_client.get(endpoint, headers=OWNER).status_code == 404
