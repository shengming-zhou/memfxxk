import json
from threading import Event, Thread

import pytest
from sqlalchemy import select

from memfxxk.db import content_resolutions
from memfxxk.domain import Principal
from memfxxk.domain.blocks import CreateBlock, EditBlock
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.resolutions import ResolutionTarget
from memfxxk.ingest import NotFound
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.llm.settings import LLMSettings, TaskModel
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration

OLD = "# Port\nThe tide is red. " + "The port has many wooden piers and plain stone walls. " * 20
NEW = OLD.replace("The tide is red.", "The tide is green.")


def _bound(service, make_ctx, user="u", project="p"):
    ctx = make_ctx(user=user, agent="writer", project=project)
    return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)


def _using_model(service, model):
    settings = service.settings.model_copy(
        update={
            "llm": LLMSettings(tasks={"resolution_summarize": TaskModel(model="fake")}),
        }
    )
    return MemoryService(service.engine, settings, model)


class Extractor(FakeLLM):
    def __init__(self, span, gate=None):
        super().__init__({})
        self.span = span
        self.gate = gate

    def complete(self, task, messages):
        assert task == "resolution_summarize"
        if self.gate:
            ready, released = self.gate
            ready.set()
            assert released.wait(10)
        return ChatResult({"content": json.dumps({"summary": [self.span], "line": self.span})})


def test_section_versions_follow_the_current_revision_and_scope(service, make_ctx):
    service = _using_model(service, Extractor("The tide is red."))
    owner = _bound(service, make_ctx)
    identity = ResolutionTarget(kind="document", path="world/port.md")
    doc = owner.put_document(identity.path, PutDocumentRequest(content=OLD, load_policy="always"))
    assert owner.get_resolutions(identity).sections[0].summary.method == "extractive_fallback"
    drain(service)
    unit = owner.get_resolutions(identity).sections[0]
    assert unit.heading == "Port" and unit.full.text == OLD
    assert unit.summary.method == unit.line.method == "llm_extract"
    assert unit.summary.text == unit.line.text == "The tide is red."
    owner.put_document(identity.path, PutDocumentRequest(content=NEW, base_revision=doc.revision))
    current = owner.get_resolutions(identity)
    assert current.generation == 2 and current.sections[0].summary.method == "extractive_fallback"
    assert "red" not in current.sections[0].full.text
    drain(service)
    assert owner.get_resolutions(identity).sections[0].line.method == "extractive_fallback"
    for companion in (
        _bound(service, make_ctx, user="other"),
        _bound(service, make_ctx, project="other"),
    ):
        with pytest.raises(NotFound):
            companion.get_resolutions(identity)
    owner.delete_document(identity.path)
    with service.engine.connect() as conn:
        assert conn.execute(select(content_resolutions)).first() is None


def test_block_fallback_versions_are_cleared_with_source(service, make_ctx):
    owner = _bound(service, make_ctx)
    block = owner.create_block(
        CreateBlock(
            label="canon",
            description="Writing constraints",
            owner="agent",
            value=OLD,
        )
    )
    target = ResolutionTarget(kind="block", id=block.id)
    drain(service)
    versions = owner.get_resolutions(target)
    assert versions.sections[0].line.method == "extractive_fallback"
    assert versions.sections[0].summary.tokens < versions.sections[0].full.tokens
    edit = owner.edit_block(
        block.id,
        EditBlock(
            base_revision=block.revision,
            operation="rewrite",
            text=NEW,
        ),
    )
    assert edit.block.revision == 2
    drain(service)
    owner.forget_event(edit.block.source_event_ids[-1])
    with service.engine.connect() as conn:
        assert conn.execute(select(content_resolutions)).first() is None
    assert owner.get_resolutions(target).sections[0].full.text == OLD


def test_worker_discards_a_projection_when_the_document_changes_mid_call(service, make_ctx):
    accepted, finish = Event(), Event()
    service = _using_model(service, Extractor("The tide is red.", (accepted, finish)))
    owner = _bound(service, make_ctx)
    target = ResolutionTarget(kind="document", path="world/port.md")
    owner.put_document(target.path, PutDocumentRequest(content=OLD, load_policy="always"))
    worker = Thread(target=drain, args=(service,), kwargs={"max_rounds": 1})
    worker.start()
    try:
        assert accepted.wait(10)
        owner.put_document(target.path, PutDocumentRequest(content=NEW, base_revision=1))
    finally:
        finish.set()
        worker.join(10)
    assert not worker.is_alive()
    with service.engine.connect() as conn:
        assert conn.execute(select(content_resolutions)).first() is None
    assert owner.get_resolutions(target).sections[0].full.text == NEW
