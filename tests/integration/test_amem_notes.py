import json
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from memfxxk.config import StrategiesSettings
from memfxxk.db import amem_links, transaction
from memfxxk.domain import Principal, Scope
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import LinkRequest, RememberRequest
from memfxxk.domain.notes import RollbackNote
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.ingest import NotFound
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.m2.storage import derived_record
from memfxxk.manage.common import ManagementError
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def _fixture(fake, task, data):
    fake.fixtures[task] = deque([ChatResult({"content": json.dumps(data)})])


def _attrs(context):
    return {"context": context, "keywords": ["Atlas"], "tags": ["pipeline"]}


def _auto_note(service, owner, content):
    source = owner.ingest([IngestEvent(kind="message", content=content, role="user")])[0]
    memory_id = uuid4()
    with transaction(service.engine) as conn:
        derived_record(
            conn,
            owner.ctx,
            Scope.model_validate(owner.ctx.model_dump()),
            memory_id=memory_id,
            version=1,
            kind="note",
            content=content,
            sources=[source.event_id],
            event_time=datetime.now(UTC),
        )
    return memory_id, source.event_id


def test_amem_keeps_originals_and_rolls_back_scoped_attributes(service, make_ctx):
    fake = FakeLLM({})
    settings = service.settings.model_copy(update={"strategies": StrategiesSettings(notes="amem")})
    service = MemoryService(service.engine, settings, fake)

    def scoped(user="u", project="p"):
        ctx = make_ctx(user=user, project=project)
        return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)

    owner = scoped()
    old_content = "Atlas nightly imports used Paris."
    old, old_source = _auto_note(service, owner, old_content)
    _fixture(fake, "note_enrich", _attrs("Paris import configuration"))
    drain(service)
    assert owner.get_memory(old).note_attributes.revision == 1

    explicit = owner.remember(
        RememberRequest(content="Atlas has a manual release policy.")
    ).memory_id
    _fixture(fake, "note_enrich", _attrs("User-authored release policy"))
    _fixture(fake, "note_link", {"suggested_connections": []})
    _fixture(fake, "note_evolve", {"should_evolve": False, "actions": []})
    drain(service)
    assert owner.get_memory(explicit).note_attributes.context == "User-authored release policy"

    new, new_source = _auto_note(service, owner, "Atlas nightly imports moved to Lyon.")
    owner.link(LinkRequest(from_memory_id=old, to_memory_id=explicit, purpose="manual"))
    _fixture(fake, "note_enrich", _attrs("New Lyon pipeline"))
    _fixture(fake, "note_link", {"suggested_connections": [0, 1, 99]})
    _fixture(
        fake,
        "note_evolve",
        {
            "should_evolve": True,
            "actions": ["update_neighbor"],
            "attributes": _attrs("Historical Paris configuration after the migration"),
        },
    )
    drain(service)
    updated = owner.get_memory(old)
    assert updated.content == old_content and updated.version == 1
    assert updated.note_attributes.revision == 2
    assert updated.note_attributes.context.startswith("Historical")
    assert owner.get_memory(explicit).note_attributes.revision == 1
    result = owner.search(SearchRequest(query="Atlas", types=["note"]))
    assert any(hit.score.note_rank is not None for hit in result.hits)
    small = owner.build_context(
        BuildContextRequest(
            query="Atlas",
            sections=["evidence"],
            max_tokens=1,
            search_filters=SearchFilters(types=["note"]),
        )
    )
    persisted = owner.get_bundle(small.bundle_id)
    dropped = persisted.sections[0].dropped
    assert dropped and not persisted.sections[0].items and not persisted.sections[0].rendered
    assert all(
        ref.record_type == "note"
        and ref.score is not None
        and ref.candidate_tokens > 0
        and ref.decision == "dropped"
        and ref.decision_reason
        for ref in dropped
    )
    assert scoped(user="other").search(SearchRequest(query="Atlas", types=["note"])).hits == []
    with pytest.raises(NotFound):
        scoped(project="other").rollback_note(old, RollbackNote(base_revision=2, target_revision=1))
    with pytest.raises(ManagementError) as error:
        owner.rollback_note(old, RollbackNote(base_revision=1, target_revision=1))
    assert error.value.code == "note_revision_conflict"
    reverted = owner.rollback_note(old, RollbackNote(base_revision=2, target_revision=1))
    assert reverted.revision == 3 and reverted.context == "Paris import configuration"
    assert [item.operation for item in owner.note_history(old)] == ["rollback", "evolve", "enrich"]

    with service.engine.connect() as conn:
        auto = conn.execute(select(amem_links)).mappings().all()
        assert len(auto) == 2 and all(item["llm_call_id"] for item in auto)
        assert all(new_source in item["source_event_ids"] for item in auto)
    owner.forget_event(new_source)
    assert [item.purpose for item in owner.get_memory(old).relations] == ["manual"]
    assert owner.get_memory(old).note_attributes.revision == 3
    assert [item.revision for item in owner.note_history(old)] == [3, 1]
    with pytest.raises(NotFound):
        owner.get_memory(new)
    owner.forget_event(old_source)
    with pytest.raises(NotFound):
        owner.get_memory(old)
