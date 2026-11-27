import json
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from memfxxk.config import StrategiesSettings
from memfxxk.db import entities, entity_edges
from memfxxk.domain import Principal
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.jobs.worker import Worker, drain
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def _scope(service, make_ctx, user="owner", project="p"):
    ctx = make_ctx(user=user, project=project)
    return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)


def _fixture(fake, task, data):
    fake.fixtures[task] = deque([ChatResult({"content": json.dumps(data)})])


def _date(day):
    return datetime(2025, 1, day, tzinfo=UTC)


def _store(bound, day, person, place):
    token = uuid4().hex
    saved = bound.ingest(
        [
            IngestEvent(
                kind="message",
                role="user",
                session_id="s",
                turn_id=token,
                content=f"{person} now lives in {place}.",
                event_time=_date(day),
            ),
            IngestEvent(
                kind="message",
                role="assistant",
                session_id="s",
                turn_id=token,
                content="Acknowledged.",
                event_time=_date(day),
            ),
        ]
    )
    return saved[0].event_id


def _episode(fake, source, day, person, place):
    refs = [str(source)]
    _fixture(
        fake,
        "entity_extract",
        {
            "entities": [
                {"name": person, "entity_type": "person", "source_event_ids": refs},
                {"name": place, "entity_type": "place", "source_event_ids": refs},
            ]
        },
    )
    _fixture(
        fake,
        "edge_extract",
        {
            "edges": [
                {
                    "source": 0,
                    "target": 1,
                    "relation_type": "LIVES_IN",
                    "fact": f"{person} lives in {place}.",
                    "valid_at": _date(day).isoformat(),
                    "source_event_ids": refs,
                }
            ]
        },
    )
    _fixture(
        fake,
        "node_summary",
        {
            "summaries": [
                {
                    "id": 0,
                    "summary": f"{person} resides in {place}.",
                    "source_event_ids": refs,
                }
            ]
        },
    )


def test_temporal_graph_resolves_aliases_and_propagates_erasure(service, make_ctx):
    fake = FakeLLM({})
    service = MemoryService(
        service.engine,
        service.settings.model_copy(update={"strategies": StrategiesSettings(graph="graphiti")}),
        fake,
    )
    owner = _scope(service, make_ctx)
    worker = Worker(service)
    original = _store(owner, 1, "Rae", "Beijing")
    _episode(fake, original, 1, "Rae", "Beijing")
    assert worker.run_once({"build_entity_graph"}) == 1
    before = owner.search(SearchRequest(query="Beijing", types=["turn"], graph_hops=2))
    assert any(
        hit.graph_fact and hit.graph_fact.fact == "Rae lives in Beijing." for hit in before.hits
    )
    with service.engine.connect() as conn:
        recorded = conn.scalar(select(entity_edges.c.created_at))

    current = _store(owner, 3, "Rae Tang", "Shanghai")
    _episode(fake, current, 3, "Rae Tang", "Shanghai")
    _fixture(fake, "entity_resolve", {"resolutions": [{"id": 0, "duplicate_candidate_id": 0}]})
    _fixture(fake, "edge_resolve", {"duplicate_facts": [], "contradicted_facts": [0, 1000]})
    assert worker.run_once({"build_entity_graph"}) == 1
    drain(service, kinds={"embed_record"})
    with service.engine.connect() as conn:
        labels = list(conn.scalars(select(entities.c.name).order_by(entities.c.name)))
        assert labels == ["Beijing", "Rae", "Shanghai"]
        old, new = conn.execute(
            select(entity_edges).order_by(entity_edges.c.asserted_at)
        ).mappings()
        assert old["invalid_at"] == _date(3) and old["expired_at"] is not None
        assert new["source_id"] == old["source_id"]
    calls = [task for task, *_ in fake.calls]
    assert calls.count("entity_extract") == 2 and calls.count("edge_resolve") == 1

    def city(day, service=owner):
        req = SearchRequest(query="Rae", types=["turn"], graph_hops=2, as_of_valid=_date(day))
        return [hit.graph_fact.fact for hit in service.search(req).hits if hit.graph_fact]

    assert city(2) == ["Rae lives in Beijing."]
    assert city(4) == ["Rae Tang lives in Shanghai."]
    assert city(4, _scope(service, make_ctx, user="other")) == []
    assert city(4, _scope(service, make_ctx, project="other")) == []
    knowledge_at = owner.search(
        SearchRequest(
            query="Rae",
            types=["turn"],
            graph_hops=2,
            as_of_valid=_date(4),
            as_of_recorded=recorded,
        )
    )
    assert [hit.graph_fact.fact for hit in knowledge_at.hits if hit.graph_fact] == [
        "Rae lives in Beijing."
    ]

    bundle = owner.build_context(
        BuildContextRequest(
            query="Rae",
            sections=["evidence"],
            graph_evidence_tokens=300,
            search_filters=SearchFilters(types=["turn"], graph_hops=2, as_of_valid=_date(4)),
        )
    )
    assert any(item.graph_edge_id for item in bundle.sections[0].items)
    owner.forget_event(current)
    assert city(4) == ["Rae lives in Beijing."]
    owner.forget_event(original)
    assert city(4) == []
