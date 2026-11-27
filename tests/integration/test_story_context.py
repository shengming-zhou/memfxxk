import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_temporal_graph import _episode, _store

from memfxxk.config import StrategiesSettings
from memfxxk.db import entity_edges, transaction
from memfxxk.domain import Principal, Scope
from memfxxk.domain.consistency import ConsistencyRequest
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.domain.story import StoryAnnotation, StoryTime
from memfxxk.jobs.worker import Worker
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.m2.storage import derived_record
from memfxxk.service import MemoryService
from memfxxk.tools import ServiceBackend, dispatch

pytestmark = pytest.mark.integration
HEADERS = {"X-Memfxxk-User": "writer", "X-Memfxxk-Project": "novel"}
WORLD = "world/charter.md"
LORE = "# Silver Watch\nThe Silver Watch, also known as the Dawnward, guards the north gate."
RULE = "The north gate must remain shut during a red tide."


@pytest.mark.parametrize(
    "query,expected", [("Ask the Moth", True), ("Dawnward", True), ("Mothwing", False)]
)
def test_lore_trigger_matches_aliases_as_whole_words(api_client, query, expected):
    path = f"/v1/documents/{WORLD}"
    response = api_client.put(
        path,
        headers=HEADERS,
        json={
            "content": LORE,
            "lore": {"Silver Watch": {"aliases": ["Moth"]}},
            "load_policy": "on_demand",
        },
    )
    assert response.status_code == 200, response.text
    assert api_client.put(
        path,
        headers=HEADERS,
        json={"content": LORE + " The rule is unchanged.", "base_revision": 1},
    ).json()["lore"]["Silver Watch"]["aliases"] == ["Moth"]
    request = {
        "query": query,
        "allocator": "adaptive",
        "max_tokens": 600,
        "auto_recall": False,
        "include_on_demand_index": False,
    }
    response = api_client.post("/v1/context/build", headers=HEADERS, json=request)
    assert response.status_code == 200, response.text
    assert ("Silver Watch" in response.json()["rendered"]) == expected
    if expected:
        assert response.json()["sections"][0]["items"][0]["reason"] == "lore_trigger"
    for name, value in (("User", "other"), ("Project", "other")):
        foreign = {**HEADERS, f"X-Memfxxk-{name}": value}
        assert (
            api_client.post("/v1/context/build", headers=foreign, json=request).json()["rendered"]
            == ""
        )


def test_outline_composes_cached_short_versions_and_respects_visibility(api_client):
    first = api_client.put(
        "/v1/documents/chapter-a.md",
        headers=HEADERS,
        json={
            "content": f"# Gate\n{RULE}\n\nThe dark stones of the bridge are old. " + "Rain. " * 60
        },
    )
    assert first.status_code == 200, first.text
    assert (
        api_client.put(
            "/v1/documents/chapter-b.md",
            headers=HEADERS,
            json={"content": "# Ferry\nThe ferry runs after dawn. " + "Water. " * 60},
        ).status_code
        == 200
    )
    data = {"paths": ["chapter-a.md", "chapter-b.md"], "max_tokens": 30}
    result = api_client.post("/v1/context/outline", json=data, headers=HEADERS)
    assert result.status_code == 200, result.text
    assert result.json()["summary"]["tokens"] <= 30
    assert len(result.json()["chapters"]) == 2
    assert "full" not in result.json()["chapters"][0]["sections"][0]
    assert (
        api_client.post(
            "/v1/context/outline", json=data, headers={**HEADERS, "X-Memfxxk-User": "other"}
        ).status_code
        == 404
    )


def _bound(service, make_ctx, user="writer", project="novel"):
    scope = make_ctx(user=user, agent="agent", project=project)
    return service.for_request(Principal(org_id=scope.org_id, role="host"), scope)


def test_consistency_reports_only_grounded_sections_and_does_not_edit(service, make_ctx):
    fake = FakeLLM(
        {
            "consistency_check": [
                ChatResult(
                    {
                        "content": json.dumps(
                            {
                                "conflicts": [
                                    {
                                        "source": "e0",
                                        "excerpt": "opened the north gate",
                                        "source_excerpt": RULE,
                                        "explanation": "The rule requires the gate to be closed.",
                                    },
                                    {
                                        "source": "e9",
                                        "excerpt": "north gate",
                                        "source_excerpt": RULE,
                                        "explanation": "invalid",
                                    },
                                    {
                                        "source": "e0",
                                        "excerpt": "imaginary",
                                        "source_excerpt": RULE,
                                        "explanation": "invalid",
                                    },
                                    {
                                        "source": "e0",
                                        "excerpt": "red tide",
                                        "source_excerpt": "wrong",
                                        "explanation": "invalid",
                                    },
                                ]
                            }
                        )
                    }
                ),
                ChatResult({"content": '{"conflicts":[]}'}),
            ]
        }
    )
    service = MemoryService(service.engine, service.settings, fake)
    bound = _bound(service, make_ctx)
    source = bound.put_document(WORLD, PutDocumentRequest(content=f"# Gate rules\n{RULE}"))
    draft = "During the red tide, Tess opened the north gate."
    outcome = bound.check_consistency(ConsistencyRequest(text=draft))
    assert len(outcome.conflicts) == 1
    mark = outcome.conflicts[0]
    assert draft[mark.start : mark.end] == mark.excerpt
    assert mark.target.ref_id == str(source.document_id) and mark.target.section_index == 0
    safe = bound.check_consistency(
        ConsistencyRequest(text="The north gate stayed shut in the red tide.")
    )
    assert safe.conflicts == []
    assert bound.get_document(WORLD).revision == source.revision
    assert (
        _bound(service, make_ctx, project="other")
        .check_consistency(ConsistencyRequest(text=draft))
        .checked
        == []
    )
    assert len(fake.calls) == 2


def test_story_time_intervals_filter_facts_and_graph_independently_of_recording(service, make_ctx):
    fake = FakeLLM({})
    service = MemoryService(
        service.engine,
        service.settings.model_copy(update={"strategies": StrategiesSettings(graph="graphiti")}),
        fake,
    )
    bound = _bound(service, make_ctx)
    private = _bound(service, make_ctx, user="other")
    identity = uuid4()
    fact = "The lighthouse beacon is violet in the opening scene."
    event = bound.ingest([IngestEvent(kind="tool_result", role="tool", content=fact)])[0]
    with transaction(service.engine) as conn:
        derived_record(
            conn,
            bound.ctx,
            Scope.model_validate(bound.ctx.model_dump()),
            memory_id=identity,
            version=1,
            kind="fact",
            content=fact,
            sources=[event.event_id],
            event_time=datetime(2025, 5, 4, tzinfo=UTC),
        )
    early = StoryTime(label="Book One, before the siege", start=10, end=30)
    bound.annotate_story_time(StoryAnnotation(id=identity, kind="fact", time=early))
    req = SearchRequest(query="lighthouse beacon", channels=["lexical"], as_of_story=20)
    hits = bound.search(req).hits
    assert [(hit.memory_id, hit.story_time) for hit in hits] == [(identity, early)]
    arguments = {"query": "lighthouse beacon", "as_of_story": 20}
    assert early.label in dispatch(ServiceBackend(bound), "memory_search", arguments)
    assert not bound.search(req.model_copy(update={"as_of_story": 30})).hits
    assert not private.search(req).hits
    assert not _bound(service, make_ctx, project="another").search(req).hits

    event_id = _store(bound, 4, "Tess", "Stoneport")
    _episode(fake, event_id, 4, "Tess", "Stoneport")
    assert Worker(service).run_once({"build_entity_graph"}) == 1
    with service.engine.connect() as conn:
        edge_id = conn.scalar(select(entity_edges.c.id))
    later = StoryTime(label="Book Two", start=40)
    bound.annotate_story_time(StoryAnnotation(id=edge_id, kind="edge", time=later))
    graph = SearchRequest(
        query="Stoneport", channels=["lexical"], types=["turn"], graph_hops=2, as_of_story=45
    )
    matching = bound.search(graph).hits
    assert matching and matching[0].graph_fact.story_time == later
    assert not bound.search(graph.model_copy(update={"as_of_story": 39})).hits
    assert not private.search(graph).hits
    bound.annotate_story_time(StoryAnnotation(id=edge_id, kind="edge", time=None))
    assert not bound.search(graph).hits
