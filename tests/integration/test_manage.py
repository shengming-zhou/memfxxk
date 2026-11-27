from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from memfxxk import manage
from memfxxk.config import Settings
from memfxxk.db import events, memory_marks, record_embeddings, records
from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import LinkRequest, ListRequest, PatchRequest, RememberRequest, Span
from memfxxk.ingest import NotFound
from memfxxk.jobs import drain
from memfxxk.service import BoundService, MemoryService

pytestmark = pytest.mark.integration


def bound(service: MemoryService, user: str = "u1", project: str = "p1") -> BoundService:
    return service.for_request(
        Principal(org_id="org", role="host"),
        RequestContext(org_id="org", user_id=user, project_id=project),
    )


def test_remember_indexes_transactionally_and_is_idempotent(
    service: MemoryService, engine: Engine
) -> None:
    owner = bound(service)
    original = manage.remember(
        owner, RememberRequest(content="修复 parseHTTPResponse 超时", idempotency_key="retry")
    )
    repeated = manage.remember(owner, RememberRequest(content="retry", idempotency_key="retry"))
    assert repeated == original
    page = manage.list_memories(owner, ListRequest(q="parseHTTPResponse"))
    assert [row.memory_id for row in page.items] == [original.memory_id]
    assert page.items[0].marks == []
    assert drain(service, kinds={"embed_record"}) == 1
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(record_embeddings)) == 1
        audit = conn.execute(select(events.c.kind, events.c.payload)).one()
        assert audit.kind == "user_op"
        assert set(audit.payload) == {"op", "target", "args", "result"}
        assert audit.payload["result"] == original.model_dump(mode="json")


def test_remember_from_source_and_isolate_other_contexts(
    service: MemoryService, engine: Engine
) -> None:
    owner = bound(service)
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    source = owner.ingest(
        [IngestEvent(kind="message", content="看源码理解契约", event_time=timestamp)]
    )[0]
    request = RememberRequest(from_event_id=source.event_id, span=Span(start=1, end=3))
    remembered = manage.remember(owner, request)
    with engine.connect() as conn:
        row = conn.execute(select(records).where(records.c.memory_id == remembered.memory_id)).one()
        assert (row.content, row.event_time, row.source_event_ids) == (
            "源码",
            timestamp,
            [source.event_id],
        )
    with pytest.raises(NotFound):
        manage.remember(owner, request.model_copy(update={"scope_drop": {"project_id"}}))
    for outsider in (bound(service, user="u2"), bound(service, project="p2")):
        with pytest.raises(NotFound):
            manage.remember(outsider, request)
        with pytest.raises(NotFound):
            manage.patch_memory(outsider, remembered.memory_id, PatchRequest(important=True))
        with pytest.raises(NotFound):
            manage.forget(outsider, remembered.memory_id, "delete")
        with pytest.raises(NotFound):
            manage.forget_event(outsider, source.event_id)
        assert not manage.list_memories(outsider, ListRequest()).items


def test_correction_preserves_version_and_mark(service: MemoryService, engine: Engine) -> None:
    owner = bound(service)
    first = manage.remember(owner, RememberRequest(content="v1", important=True))
    second = manage.patch_memory(owner, first.memory_id, PatchRequest(base_version=1, content="v2"))
    assert second.version == 2
    with engine.connect() as conn:
        rows = conn.execute(
            select(records.c.content, records.c.is_current).order_by(records.c.version)
        ).all()
        assert rows == [("v1", False), ("v2", True)]
    assert manage.list_memories(owner, ListRequest()).items[0].marks == ["important"]
    with pytest.raises(manage.ManagementError) as conflict:
        manage.patch_memory(owner, first.memory_id, PatchRequest(base_version=1, content="v3"))
    assert (conflict.value.status_code, conflict.value.code) == (409, "version_conflict")
    assert conflict.value.details == {"current_version": 2}
    turn = owner.ingest([IngestEvent(kind="message", session_id="s", content="original")])[0]
    with pytest.raises(manage.ManagementError) as immutable:
        manage.patch_memory(
            owner, turn.memory_ids_touched[0], PatchRequest(base_version=1, title="changed")
        )
    assert (immutable.value.status_code, immutable.value.code) == (422, "immutable_record")


def test_mark_scope_and_relation_permissions(service: MemoryService, engine: Engine) -> None:
    owner = bound(service)
    other_project = bound(service, project="p2")
    memory = manage.remember(
        owner, RememberRequest(content="shared preference", scope_drop={"project_id"})
    )
    private = manage.remember(owner, RememberRequest(content="project p1"))
    relation = LinkRequest(
        from_memory_id=memory.memory_id, to_memory_id=private.memory_id, purpose="related"
    )
    with pytest.raises(NotFound):
        manage.link(other_project, relation)
    with pytest.raises(manage.ManagementError) as self_link:
        manage.link(owner, relation.model_copy(update={"to_memory_id": memory.memory_id}))
    assert self_link.value.status_code == 422
    linked = manage.link(owner, relation)
    assert manage.link(owner, relation) == linked
    with pytest.raises(NotFound):
        manage.unlink(bound(service, user="u2", project="p2"), linked.relation_id)
    assert manage.list_memories(other_project, ListRequest()).items[0].relation_count == 0
    manage.unlink(other_project, linked.relation_id)

    manage.patch_memory(owner, memory.memory_id, PatchRequest(pinned=True))
    pinned = ListRequest(marks=["pinned"])
    assert manage.list_memories(owner, pinned).items
    assert not manage.list_memories(other_project, pinned).items
    manage.patch_memory(
        owner, memory.memory_id, PatchRequest(pinned=True, mark_scope_drop={"project_id"})
    )
    manage.patch_memory(owner, memory.memory_id, PatchRequest(pinned=False))
    assert manage.list_memories(owner, pinned).items
    assert manage.list_memories(other_project, pinned).items
    with engine.connect() as conn:
        assert conn.execute(select(memory_marks.c.project_id)).all() == [(None,)]


def test_deactivate_reactivate_and_redact_hard_deleted_memory(
    service: MemoryService, engine: Engine
) -> None:
    owner = bound(service)
    memory = manage.remember(owner, RememberRequest(content="delete-old value", pinned=True))
    peer = manage.remember(owner, RememberRequest(content="still visible"))
    manage.link(
        owner,
        LinkRequest(from_memory_id=peer.memory_id, to_memory_id=memory.memory_id, purpose="x"),
    )
    manage.patch_memory(
        owner, memory.memory_id, PatchRequest(base_version=1, content="delete-new value")
    )
    assert drain(service, kinds={"embed_record"}) == 3
    manage.forget(owner, memory.memory_id, "deactivate")
    assert not manage.list_memories(owner, ListRequest(marks=["pinned"])).items
    assert manage.list_memories(owner, ListRequest(status="inactive")).items[0].marks == ["pinned"]
    manage.forget(owner, memory.memory_id, "reactivate")
    active = manage.list_memories(owner, ListRequest(marks=["pinned"])).items
    assert [(row.memory_id, row.relation_count) for row in active] == [(memory.memory_id, 1)]

    restricted = bound(MemoryService(engine, Settings(allow_host_hard_delete=False)))
    with pytest.raises(manage.ManagementError) as forbidden:
        manage.forget(restricted, memory.memory_id, "delete")
    assert forbidden.value.status_code == 403
    deleted = manage.forget(owner, memory.memory_id, "delete")
    assert (deleted.records, deleted.embeddings, deleted.marks, deleted.relations) == (2, 2, 1, 1)
    assert deleted.events_redacted == 5
    assert drain(service, kinds={"embed_record"}) == 1
    with engine.connect() as conn:
        assert conn.execute(select(records.c.memory_id)).all() == [(peer.memory_id,)]
        assert conn.scalar(select(func.count()).select_from(record_embeddings)) == 1
        audit = conn.execute(select(events.c.content, events.c.payload).where(events.c.redacted))
        for content, payload in audit:
            assert content is None
            assert set(payload) == {"op", "target", "redacted"}


def test_forget_event_deletes_derived_turn_and_note_without_touching_other_sources(
    service: MemoryService, engine: Engine
) -> None:
    owner = bound(service)
    first, second = owner.ingest(
        [
            IngestEvent(kind="message", content="A", session_id="s", turn_id="turn"),
            IngestEvent(kind="message", content="B", session_id="s", turn_id="turn"),
        ]
    )
    note = manage.remember(owner, RememberRequest(from_event_id=first.event_id))
    assert manage.forget_event(owner, first.event_id).records == 2
    with pytest.raises(NotFound):
        owner.get_event(first.event_id)
    assert owner.get_event(second.event_id).content == "B"
    assert drain(service) == 2
    with engine.connect() as conn:
        assert not conn.execute(select(record_embeddings.c.record_id)).all()
    with pytest.raises(NotFound):
        manage.forget(owner, note.memory_id, "deactivate")
    owner.ingest([IngestEvent(kind="message", content="C", session_id="s", turn_id="turn")])
    assert manage.list_memories(owner, ListRequest()).items[0].snippet == "[message] B\n[message] C"


def test_list_cursor_and_scopes(service: MemoryService) -> None:
    owner = bound(service)
    ids = [
        manage.remember(owner, RememberRequest(content=f"project sample {index}")).memory_id
        for index in range(3)
    ]
    shared = manage.remember(owner, RememberRequest(content="personal", scope_drop={"project_id"}))
    page = manage.list_memories(owner, ListRequest(limit=2, q="sample"))
    assert page.next_cursor is not None
    rest = manage.list_memories(owner, ListRequest(limit=2, q="sample", cursor=page.next_cursor))
    assert [item.memory_id for item in page.items + rest.items] == list(reversed(ids))
    assert rest.next_cursor is None
    personal = manage.list_memories(owner, ListRequest(scope_level="user"))
    assert [item.memory_id for item in personal.items] == [shared.memory_id]
    with pytest.raises(manage.ManagementError) as invalid:
        manage.list_memories(owner, ListRequest(cursor="invalid:"))
    assert invalid.value.code == "invalid_cursor"
