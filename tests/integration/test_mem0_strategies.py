import json
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from memfxxk.config import StrategiesSettings
from memfxxk.db import records
from memfxxk.domain import Principal
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.embedding import CachedEmbedder, FakeEmbeddingProvider
from memfxxk.ingest import NotFound
from memfxxk.jobs.worker import Worker
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def _activate(service, strategy, fake, *, ranking=False):
    settings = service.settings.model_copy(
        update={"strategies": StrategiesSettings(facts=strategy, mem0_additive_ranking=ranking)}
    )
    return MemoryService(service.engine, settings, fake)


def _bound(service, make_ctx, *, user="owner", project="atlas"):
    ctx = make_ctx(user=user, project=project)
    return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)


def _fixture(fake, task, data):
    fake.fixtures[task] = deque([ChatResult({"content": json.dumps(data)})])


def _feed(owner, day, content):
    now = datetime(2025, 1, day, tzinfo=UTC)
    turn_id = uuid4().hex
    return owner.ingest(
        [
            IngestEvent(
                kind="message",
                session_id="s",
                turn_id=turn_id,
                role="user",
                content=content,
                event_time=now,
            ),
            IngestEvent(
                kind="message",
                session_id="s",
                turn_id=turn_id,
                role="assistant",
                content="Acknowledged.",
                event_time=now,
            ),
        ]
    )[0].event_id


def _run_classic(owner, worker, fake, day, content, actions):
    raw_id = _feed(owner, day, content)
    _fixture(fake, "extract", {"facts": [{"content": content, "source_event_ids": [str(raw_id)]}]})
    _fixture(fake, "reconcile", {"memory": actions})
    assert worker.run_once({"extract_facts"}) == 1
    return raw_id


def _query(text="atlas", **kwargs):
    return SearchRequest(query=text, types=["fact"], channels=["lexical"], **kwargs)


def test_classic_writes_temporal_versions_and_ignores_bad_actions(service, make_ctx):
    fake = FakeLLM({})
    service = _activate(service, "mem0_classic", fake)
    owner = _bound(service, make_ctx)
    worker = Worker(service)
    paris = "Atlas uses the Paris hub."
    lyon = "Atlas uses the Lyon hub."
    add = {"candidate_id": 0, "event": "ADD"}
    source = _run_classic(owner, worker, fake, 1, paris, [add])
    (first,) = owner.search(_query()).hits
    _run_classic(owner, worker, fake, 2, paris, [add])
    assert owner.search(_query()).hits[0].version == 1
    assert len(owner.search(_query()).hits) == 1

    change = {"candidate_id": 0, "id": 0, "event": "UPDATE", "old_memory": paris, "text": lyon}
    _run_classic(owner, worker, fake, 3, lyon, [change])
    second = owner.get_memory(first.memory_id)
    assert (second.version, second.content) == (2, lyon)
    assert source in second.source_event_ids
    prompt = json.loads(fake.calls[-1][2][1]["content"])
    assert prompt["existing_memories"] == [
        {"id": 0, "text": paris, "valid_from": "2025-01-01T00:00:00+00:00"}
    ]

    _run_classic(
        owner,
        worker,
        fake,
        4,
        "Atlas hub was retired.",
        [{"candidate_id": 0, "id": 99, "event": "DELETE"}],
    )
    _run_classic(owner, worker, fake, 5, lyon, [{"candidate_id": 0, "event": "NONE"}, add])
    assert owner.get_memory(first.memory_id).version == 2
    _run_classic(
        owner,
        worker,
        fake,
        6,
        "Atlas hub was retired.",
        [{"candidate_id": 0, "id": 0, "event": "DELETE"}],
    )
    closed = owner.get_memory(first.memory_id)
    assert (closed.version, closed.status) == (3, "inactive")
    assert owner.search(_query()).hits == []
    assert owner.search(_query(as_of_valid=datetime(2025, 1, 5, tzinfo=UTC))).hits[0].version == 2
    assert owner.search(_query(as_of_recorded=first.recorded_at)).hits[0].version == 1
    for user, project in (("outsider", "atlas"), ("owner", "elsewhere")):
        outsider = _bound(service, make_ctx, user=user, project=project)
        assert outsider.search(_query()).hits == []
        with pytest.raises(NotFound):
            outsider.get_memory(first.memory_id, version=1)
    owner.forget_event(source)
    with pytest.raises(NotFound):
        owner.get_memory(first.memory_id)


def test_classic_requires_review_for_confirmed_memory(service, make_ctx):
    fake = FakeLLM({})
    key = _activate(service, "key_proposal", fake)
    owner = _bound(key, make_ctx)
    paris = "Atlas uses the Paris hub."
    lyon = "Atlas uses the Lyon hub."
    source = _feed(owner, 1, paris)
    _fixture(
        fake, "extract", [{"key": "atlas", "content": paris, "source_event_ids": [str(source)]}]
    )
    assert Worker(key).run_once({"extract_facts"}) == 1
    (original,) = owner.list_fact_proposals()
    first = owner.review_fact(original.id, True)
    assert owner.get_memory(first.memory_id).confirmed

    classic = _activate(service, "mem0_classic", fake)
    owner = _bound(classic, make_ctx)
    _run_classic(
        owner,
        Worker(classic),
        fake,
        2,
        lyon,
        [{"candidate_id": 0, "id": 0, "event": "UPDATE", "old_memory": paris, "text": lyon}],
    )
    (proposal,) = owner.list_fact_proposals()
    assert (proposal.memory_id, proposal.operation, proposal.base_version) == (
        first.memory_id,
        "update",
        1,
    )
    assert owner.get_memory(first.memory_id).content == paris
    outsider = _bound(classic, make_ctx, project="elsewhere")
    assert outsider.list_fact_proposals() == []
    with pytest.raises(NotFound):
        outsider.review_fact(proposal.id, True)
    accepted = owner.review_fact(proposal.id, True)
    assert owner.review_fact(proposal.id, True) == accepted
    updated = owner.get_memory(first.memory_id)
    assert updated.content == lyon and updated.confirmed and updated.version == 2


def test_additive_keeps_conflicts_and_links_sources(service, make_ctx):
    fake = FakeLLM({})
    service = _activate(service, "mem0_additive", fake, ranking=True)
    owner = _bound(service, make_ctx)
    worker = Worker(service)

    def append(day, content, links):
        source = _feed(owner, day, content)
        fact = {
            "content": content,
            "source_event_ids": [str(source)],
            "attributed_to": "user",
            "entities": ["  ATLAS ", "Atlas"],
            "linked_memory_ids": links,
        }
        _fixture(fake, "extract", {"memory": [fact, {**fact, "attributed_to": "assistant"}]})
        assert worker.run_once({"extract_facts"}) == 1
        return source

    first_source = append(1, "Atlas uses the Paris hub.", [])
    (initial,) = owner.search(_query()).hits
    second_source = append(2, "Atlas uses the Lyon hub.", [0, 999])
    append(3, "Atlas uses the Lyon hub.", [])
    hits = owner.search(_query()).hits
    assert len(hits) == 2
    assert hits[0].score.fact_bm25 > 0 and hits[0].score.fact_entity_boost == 0.15
    assert "Lyon" in hits[0].snippet and "Paris" in hits[1].snippet
    related = owner.get_memory(hits[0].memory_id)
    assert [item.memory_id for item in related.relations] == [initial.memory_id]
    assert related.source_event_ids == [second_source]
    assert {task for task, *_ in fake.calls} == {"extract"}
    with service.engine.connect() as conn:
        metadata = conn.scalar(
            select(records.c.metadata).where(records.c.memory_id == hits[0].memory_id)
        )
    assert metadata["entities"] == ["atlas"] and metadata["attributed_to"] == "user"
    assert _bound(service, make_ctx, user="guest").search(_query()).hits == []
    owner.forget_event(first_source)
    (remaining,) = owner.search(_query()).hits
    assert "Lyon" in remaining.snippet and remaining.related == []


def test_additive_semantic_alias_keeps_both_sources(service, make_ctx):
    class AliasProvider(FakeEmbeddingProvider):
        def embed(self, texts):
            return super().embed(["paris" if text == "ville lumiere" else text for text in texts])

    fake = FakeLLM({})
    service = _activate(service, "mem0_additive", fake)
    owner = _bound(service, make_ctx)
    embedding = service.settings.embedding
    worker = Worker(
        service, CachedEmbedder(service.engine, AliasProvider(embedding.model, embedding.dim))
    )
    raw = []
    for day, name in enumerate(("Paris", "Ville Lumiere"), start=1):
        content = f"Atlas hired employee {day} in {name}."
        origin = _feed(owner, day, content)
        raw.append(origin)
        _fixture(
            fake,
            "extract",
            {
                "memory": [
                    {
                        "content": content,
                        "source_event_ids": [str(origin)],
                        "attributed_to": "user",
                        "entities": [name],
                    }
                ]
            },
        )
        assert worker.run_once({"extract_facts"}) == 1
    (alias,) = owner.search(_query("lumiere")).hits
    assert set(owner.get_memory(alias.memory_id).source_event_ids) == set(raw)
    with service.engine.connect() as conn:
        data = conn.scalar(select(records.c.metadata).where(records.c.memory_id == alias.memory_id))
    assert data["entities"] == ["paris"]
    owner.forget_event(raw[0])
    with pytest.raises(NotFound):
        owner.get_memory(alias.memory_id)
