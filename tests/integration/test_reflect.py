import json
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from memfxxk.config import StrategiesSettings
from memfxxk.domain import Principal
from memfxxk.domain.blocks import CreateBlock
from memfxxk.domain.context import BuildContextRequest
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.reflect import ReflectRequest
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.ingest import NotFound
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def _scoped(service, make_ctx, user="u", project="p"):
    ctx = make_ctx(user=user, agent="a", project=project)
    return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)


def _turn(owner, text, day, session="earlier"):
    key = str(uuid4())
    date = datetime(2025, 4, day, tzinfo=UTC)
    return owner.ingest(
        [
            IngestEvent(
                kind="message",
                role="user",
                content=text,
                event_time=date,
                turn_id=key,
                session_id=session,
            ),
            IngestEvent(
                kind="message",
                role="assistant",
                content="Recorded.",
                event_time=date,
                turn_id=key,
                session_id=session,
            ),
        ]
    )[0]


def _json(payload):
    return ChatResult({"content": json.dumps(payload)})


def test_hindsight_time_recall_and_rerank_are_scoped(service, make_ctx):
    fake = FakeLLM({})
    settings = service.settings.model_copy(
        update={
            "search": service.settings.search.model_copy(update={"reranker": "llm"}),
            "strategies": StrategiesSettings(recall="hindsight"),
        }
    )
    service = MemoryService(service.engine, settings, fake)
    owner = _scoped(service, make_ctx)
    _turn(_scoped(service, make_ctx, user="different"), "Secret cobalt plan.", 8, "other")
    same = _turn(owner, "Teal release at the park.", 8)
    later = _turn(owner, "Teal release in the office.", 9)
    drain(service)
    fake.fixtures["rerank"] = deque([_json({"order": [1, 1, 99, 0]})])
    candidates = owner.search(
        SearchRequest(
            query="What was last Tuesday?",
            channels=["time"],
            query_time=datetime(2025, 4, 10, tzinfo=UTC),
        )
    ).hits
    assert [row.memory_id for row in candidates] == [
        later.memory_ids_touched[0],
        same.memory_ids_touched[0],
    ]
    assert [row.score.time_rank for row in candidates] == [2, 1]
    assert [row.score.rerank_position for row in candidates] == [1, 2]
    assert "Secret cobalt" not in fake.calls[0][2][-1]["content"]


def test_reflect_cites_retrieved_ids_and_updates_a_versioned_opinion(service, make_ctx):
    settings = service.settings.model_copy(
        update={
            "strategies": StrategiesSettings(
                recall="hindsight", reflect="hindsight", blocks="letta"
            ),
        }
    )
    fake = FakeLLM({})
    service = MemoryService(service.engine, settings, fake)
    owner = _scoped(service, make_ctx)
    owner.create_block(
        CreateBlock(
            label="review",
            description="Meeting preferences",
            value="Ask for exact dates.",
            owner="agent",
        )
    )
    added = _turn(owner, "Robin says morning meetings help them concentrate.", 8)
    different = _turn(_scoped(service, make_ctx, project="different"), "Secret.", 8, "other")
    drain(service)
    source, hidden = (str(row.memory_ids_touched[0]) for row in (added, different))
    fake.fixtures["reflect"] = deque(
        [
            _json({"action": "recall", "query": "Robin morning meetings"}),
            _json(
                {
                    "action": "answer",
                    "answer": "Robin seems to prefer mornings.",
                    "citations": [source, hidden],
                    "opinions": [
                        {
                            "key": "meeting time",
                            "content": "Robin probably prefers morning meetings.",
                            "confidence": 0.7,
                            "citations": [source],
                        },
                        {
                            "key": "unseen",
                            "content": "Do not store.",
                            "confidence": 0.9,
                            "citations": [hidden],
                        },
                    ],
                }
            ),
        ]
    )
    reply = owner.reflect(
        ReflectRequest(
            query="When should Robin meet?",
            max_rounds=2,
            store_opinions=True,
        )
    )
    assert [str(citation.id) for citation in reply.citations] == [source]
    assert list(map(str, reply.rejected_citations)) == [hidden]
    assert len(reply.proposals) == len(reply.opinion_changes) == 1
    opinion = reply.opinion_changes[0]
    assert owner.get_memory(opinion.memory_id).confidence == 0.7
    assert "Ask for exact dates." in fake.calls[0][2][-1]["content"]
    drain(service)
    refs = (
        owner.build_context(
            BuildContextRequest(
                query="Robin morning meeting",
                sections=["evidence"],
                max_tokens=700,
            )
        )
        .sections[0]
        .items
    )
    assert any(ref.record_type == "opinion" and ref.strategy == "hindsight" for ref in refs)
    assert all(ref.decision == "selected" and ref.candidate_tokens > 0 for ref in refs)
    with pytest.raises(NotFound):
        _scoped(service, make_ctx, user="another").get_memory(opinion.memory_id)
    fake.fixtures["opinion_update"] = deque(
        [
            _json(
                {
                    "assessments": [
                        {"id": str(opinion.memory_id), "effect": "contradicts"},
                    ]
                }
            )
        ]
    )
    _turn(owner, "Robin corrected this. Afternoon meetings work better than mornings.", 9)
    drain(service)
    latest = owner.get_memory(opinion.memory_id)
    assert latest.confidence == pytest.approx(0.5)
    assert [version.version for version in latest.versions] == [1, 2]
    assert owner.get_memory(opinion.memory_id, version=1).confidence == 0.7
    owner.forget_event(added.event_id)
    with pytest.raises(NotFound):
        owner.get_memory(opinion.memory_id)
