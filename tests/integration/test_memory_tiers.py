import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from memfxxk.config import StrategiesSettings, TierSettings
from memfxxk.db import jobs, records, tier_pages, tier_segments
from memfxxk.domain import Actor, Principal
from memfxxk.domain.blocks import CreateBlock
from memfxxk.domain.context import BuildContextRequest
from memfxxk.domain.events import IngestEvent
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.m2.tiers import DECAY_SECONDS, heat, similarity
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


class TierModel(FakeLLM):
    def complete(self, task, messages):
        assert task == "persona_update"
        data = json.loads(messages[-1]["content"])
        self.calls.append(data)
        updates = [
            {
                "block_id": block["id"],
                "base_revision": block["revision"],
                "value": "Prefers tea.",
                "source_event_ids": data["pages"][0]["source_event_ids"],
            }
            for block in data["blocks"]
        ]
        return ChatResult({"content": json.dumps({"summary": "Tea topic.", "blocks": updates})})


def _service(service, capacity=3, cutoff=5.0):
    settings = service.settings.model_copy(
        update={
            "strategies": StrategiesSettings(tiers="memoryos", blocks="letta"),
            "tiers": TierSettings(
                short_capacity=2, segment_capacity=capacity, promotion_heat=cutoff
            ),
        }
    )
    return MemoryService(service.engine, settings, TierModel({}))


def _bound(service, make_ctx, user="u", project="p", *, human=False):
    ctx = make_ctx(user=user, agent="guide", project=project)
    if human:
        ctx = ctx.model_copy(update={"actor": Actor(type="user", id=user)})
    return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)


def _add(owner, topics):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return owner.ingest(
        [
            IngestEvent(
                kind="message",
                session_id="tier-chat",
                turn_id=str(i),
                role=role,
                content=topic,
                event_time=start + timedelta(hours=i, seconds=offset),
            )
            for i, topic in enumerate(topics)
            for offset, role in enumerate(("user", "assistant"))
        ]
    )


def test_memoryos_groups_pages_promotes_through_review_and_isolates_scope(service, make_ctx):
    service = _service(service, cutoff=2.0)
    owner = _bound(service, make_ctx, human=True)
    profile = owner.create_block(
        CreateBlock(
            label="user_profile",
            description="Long-term taste",
            value="Unknown.",
        )
    )
    uploaded = _add(owner, ["tea steeping"] * 4 + ["holiday travel", "travel dates"])
    drain(service)
    with service.engine.connect() as conn:
        segment = conn.execute(select(tier_segments)).mappings().one()
        assert segment["generation"] == 4 and segment["summary"] == "Tea topic."
        assert segment["promotion_epoch"] == 1 and segment["unpromoted_pages"] == 0
        assert conn.scalars(select(tier_pages.c.state)).all().count("short") == 2
        assert set(conn.scalars(select(jobs.c.status))) == {"done"}
    assert owner.get_block(profile.id).value == "Unknown."
    proposal = owner.list_block_proposals()[0]
    assert proposal.value == "Prefers tea."
    request = BuildContextRequest(query="tea", max_tokens=500, sections=["evidence"])
    bundle = owner.build_context(request)
    selected = bundle.sections[0].items
    assert {item.reason for item in selected if item.strategy == "memoryos"} == {
        "memoryos:short",
        "memoryos:medium",
    }
    assert all(item.record_type == "turn" and item.candidate_tokens > 0 for item in selected)
    for outsider in (
        _bound(service, make_ctx, "another"),
        _bound(service, make_ctx, project="xyz"),
    ):
        assert not outsider.build_context(request).sections[0].items
        assert not outsider.list_block_proposals()
    owner.forget_event(uploaded[0].event_id)
    with service.engine.connect() as conn:
        assert conn.execute(select(tier_segments)).first() is None
    assert not owner.list_block_proposals()


def test_memoryos_evicts_the_coldest_segment_but_keeps_raw_turns(service, make_ctx):
    service = _service(service, capacity=1)
    owner = _bound(service, make_ctx)
    _add(owner, ["tea brewing", "galaxy telescope", "stage curtains", "later"])
    drain(service)
    with service.engine.connect() as conn:
        segment = conn.execute(select(tier_segments)).mappings().one()
        assert "galaxy" in segment["keywords"]
        assert sorted(conn.scalars(select(tier_pages.c.state))) == [
            "evicted",
            "medium",
            "short",
            "short",
        ]
        assert len(conn.scalars(select(records.c.id)).all()) == 4
    assert similarity([1, 0], [1, 0], {"warm", "tea"}, {"tea"}) == 1.5
    date = datetime(2025, 1, 1, tzinfo=UTC)
    assert heat(2, 3, date, date + timedelta(seconds=DECAY_SECONDS)) == pytest.approx(5.367879)
