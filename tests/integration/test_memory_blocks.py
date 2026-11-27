import json
from collections import deque
from threading import Event, Thread

import pytest
from sqlalchemy import select

from memfxxk.config import SleeptimeSettings, StrategiesSettings
from memfxxk.db import jobs
from memfxxk.domain import Actor, Principal
from memfxxk.domain.blocks import CreateBlock, EditBlock
from memfxxk.domain.context import BuildContextRequest, PrepareCompaction
from memfxxk.domain.events import IngestEvent
from memfxxk.ingest import NotFound
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.manage.common import ManagementError
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def _configured(service, **kwargs):
    settings = service.settings.model_copy(
        update={
            "strategies": StrategiesSettings(blocks="letta"),
            "sleeptime": SleeptimeSettings(**kwargs),
        }
    )
    return MemoryService(service.engine, settings, FakeLLM({}))


def _bound(service, make_ctx, user="owner", project="private", *, human=False):
    ctx = make_ctx(user=user, agent="guide", project=project)
    if human:
        ctx = ctx.model_copy(update={"actor": Actor(type="user", id=user)})
    return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)


def _turn(owner, n, text):
    return owner.ingest(
        [
            IngestEvent(
                kind="message", role="user", content=text, session_id="dialogue", turn_id=str(n)
            ),
            IngestEvent(
                kind="message",
                role="assistant",
                content="Acknowledged.",
                session_id="dialogue",
                turn_id=str(n),
            ),
        ]
    )[0].event_id


def _fixture(service, user, agent, cited):
    service.fake_llm.fixtures["sleeptime"] = deque(
        [
            ChatResult(
                {
                    "content": json.dumps(
                        {
                            "blocks": [
                                {
                                    "block_id": str(block.id),
                                    "base_revision": block.revision,
                                    "value": value,
                                    "source_event_ids": [str(cited)],
                                }
                                for block, value in ((user, "draft preference"), (agent, "Tuesday"))
                            ],
                            "facts": [
                                {
                                    "key": "meeting",
                                    "content": "Meet Tuesday.",
                                    "source_event_ids": [str(cited)],
                                }
                            ],
                        }
                    )
                }
            )
        ]
    )


def test_block_revisions_proposals_scope_and_context_trace(service, make_ctx):
    service = _configured(service)
    owner, agent = _bound(service, make_ctx, human=True), _bound(service, make_ctx)
    protected = owner.create_block(
        CreateBlock(label="Preferences", description="User preferences", value="tea", limit=12)
    )
    assert protected.read_only
    draft = agent.edit_block(
        protected.id,
        EditBlock(
            base_revision=1,
            operation="replace",
            old_text="tea",
            text="coffee",
        ),
    )
    assert draft.block.value == "tea" and draft.proposal.status == "pending"
    with pytest.raises(ManagementError) as forbidden:
        agent.review_block(draft.proposal.id, True)
    assert forbidden.value.code == "block_requires_user"
    assert owner.review_block(draft.proposal.id, True).adopted_revision == 2
    assert agent.get_block(protected.id).value == "coffee"
    with pytest.raises(ManagementError) as stale:
        owner.edit_block(protected.id, EditBlock(base_revision=1, operation="rewrite", text="tea"))
    assert stale.value.code == "block_revision_conflict"
    with pytest.raises(ManagementError) as too_long:
        owner.edit_block(
            protected.id,
            EditBlock(
                base_revision=2,
                operation="append",
                text="americano",
            ),
        )
    assert too_long.value.details == {"char_count": 6, "requested_char_count": 16, "limit": 12}

    source = _turn(agent, 1, "Thursday plan")
    working = agent.create_block(
        CreateBlock(
            label="plan",
            description="Current work",
            value="Monday",
            owner="agent",
            limit=900,
        )
    )
    edited = agent.edit_block(
        working.id,
        EditBlock(
            base_revision=1,
            operation="append",
            text="Thursday",
            source_event_ids=[source],
        ),
    )
    assert edited.proposal is None and edited.block.value == "Monday\nThursday"
    bundle = agent.build_context(
        BuildContextRequest(
            query="Thursday",
            max_tokens=200,
            sections=["documents"],
        )
    )
    assert "Thursday" in bundle.rendered
    persisted = agent.get_bundle(bundle.bundle_id)
    ref = next(item for item in persisted.sections[0].items if item.ref_id == str(working.id))
    assert ref.record_type == "memory_block" and ref.score > 0 and ref.candidate_tokens > 0
    assert ref.decision == "selected" and ref.decision_reason
    assert "Thursday" not in persisted.model_dump_json()
    tiny = agent.build_context(BuildContextRequest(max_tokens=1, sections=["documents"]))
    assert {r.ref_id for r in tiny.sections[0].dropped} == {str(protected.id), str(working.id)}
    for outsider in (
        _bound(service, make_ctx, user="other"),
        _bound(service, make_ctx, project="other"),
    ):
        assert outsider.list_blocks() == []
        with pytest.raises(NotFound):
            outsider.get_block(working.id)
        with pytest.raises(NotFound):
            outsider.review_block(draft.proposal.id, True)
    owner.forget_event(source)
    assert agent.get_block(working.id).value == "Monday"
    restored = agent.edit_block(
        working.id,
        EditBlock(
            base_revision=1,
            operation="rewrite",
            text="Friday",
        ),
    )
    assert restored.block.revision == 3


def test_sleeptime_cadence_and_compaction_with_protected_proposals(service, make_ctx):
    service = _configured(service, every_turns=2, after_compaction=True)
    owner, agent = _bound(service, make_ctx, human=True), _bound(service, make_ctx)
    user_block = owner.create_block(CreateBlock(label="profile", description="User profile"))
    agent_block = agent.create_block(
        CreateBlock(label="plans", description="Meeting plans", owner="agent", value="Monday")
    )
    first = _turn(agent, 1, "The original plan was Monday.")
    drain(service)
    assert not service.fake_llm.calls
    cited = _turn(agent, 2, "Move our meeting to Tuesday.")
    _fixture(service, user_block, agent_block, cited)
    drain(service)
    assert agent.get_block(agent_block.id).value == "Tuesday"
    assert agent.get_block(user_block.id).value == ""
    assert len(owner.list_block_proposals()) == 1
    fact = owner.list_fact_proposals()[0]
    assert fact.fact.source_event_ids == [cited]
    confirmed = owner.review_fact(fact.id, True)
    assert {first, cited} <= set(owner.get_memory(confirmed.memory_id).source_event_ids)

    extractive = _bound(MemoryService(service.engine, service.settings), make_ctx, human=True)
    state = extractive.get_session("dialogue")
    draft = extractive.prepare_compaction(
        PrepareCompaction(
            session_id="dialogue",
            through_seq=state.last_seq,
            revision=state.revision,
            target_tokens=300,
        )
    )
    extractive.commit_compaction(draft.draft_id)
    _fixture(service, user_block, agent.get_block(agent_block.id), cited)
    drain(service)
    with service.engine.connect() as conn:
        assert set(conn.scalars(select(jobs.c.status))) == {"done"}
    calls = service.fake_llm.calls
    assert len(calls) == 2 and json.loads(calls[1][2][-1]["content"])["prior_summary"]
    owner.forget_event(first)
    assert agent.get_block(agent_block.id).value == "Monday" and not owner.list_block_proposals()
    with pytest.raises(NotFound):
        owner.get_memory(confirmed.memory_id)


def test_sleeptime_discards_model_write_after_source_deletion(service, make_ctx, monkeypatch):
    service = _configured(service, every_turns=1)
    owner, agent = _bound(service, make_ctx, human=True), _bound(service, make_ctx)
    user_block = owner.create_block(CreateBlock(label="profile", description="User profile"))
    agent_block = agent.create_block(
        CreateBlock(label="plans", description="Meeting plans", owner="agent", value="Monday")
    )
    source = _turn(agent, 1, "Move our meeting to Tuesday.")
    _fixture(service, user_block, agent_block, source)
    before_write, proceed = Event(), Event()
    complete = service.fake_llm.complete

    def wait_for_edit(task, messages):
        result = complete(task, messages)
        before_write.set()
        assert proceed.wait(10)
        return result

    monkeypatch.setattr(service.fake_llm, "complete", wait_for_edit)
    worker = Thread(target=drain, args=(service,))
    worker.start()
    try:
        assert before_write.wait(10)
        owner.forget_event(source)
    finally:
        proceed.set()
        worker.join(10)
    assert not worker.is_alive()
    assert agent.get_block(agent_block.id).value == "Monday"
    assert owner.list_block_proposals() == [] and owner.list_fact_proposals() == []
