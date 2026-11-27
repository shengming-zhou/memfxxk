import json
from collections import deque
from uuid import uuid4

import pytest

from memfxxk.config import ContextSettings, M2Settings
from memfxxk.domain import Principal
from memfxxk.domain.context import BuildContextRequest, PrepareCompaction
from memfxxk.domain.events import IngestEvent
from memfxxk.ingest import NotFound
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def _append(bound, text):
    return bound.ingest(
        [
            IngestEvent(
                kind="message", role="user", session_id="s", turn_id=uuid4().hex, content=text
            ),
            IngestEvent(kind="message", role="assistant", session_id="s", content="Acknowledged."),
        ]
    )[0].event_id


def _fixture(decision: str, source) -> ChatResult:
    return ChatResult(
        {
            "content": json.dumps(
                {
                    "latest_decisions": [
                        {"text": decision, "source_event_ids": [str(source)]},
                        {"text": "Unsupported content", "source_event_ids": [str(uuid4())]},
                    ]
                }
            )
        }
    )


def _compact(bound):
    state = bound.get_session("s")
    return bound.prepare_compaction(
        PrepareCompaction(
            session_id="s",
            revision=state.revision,
            through_seq=state.last_seq,
            target_tokens=120,
        )
    )


def test_structured_replay_restart_and_retained_user_text(engine, test_settings, make_ctx):
    ctx = make_ctx(user="owner", project="atlas")
    host = Principal(org_id=ctx.org_id, role="host")
    settings = test_settings.model_copy(
        update={
            "m2": M2Settings(recent_user_tokens=10),
            "context": ContextSettings(pressure_threshold=0.001),
        }
    )
    fake = FakeLLM({})
    first_host = MemoryService(engine, settings, fake).for_request(host, ctx)
    old = _append(first_host, "Ship Friday.")
    fake.fixtures["compact"] = deque([_fixture("Ship Friday.", old)])
    first = _compact(first_host)
    assert first.method == "llm" and first.retained_user_event_ids == [old]
    assert first.checkpoint and len(first.checkpoint.latest_decisions) == 1
    assert "Unsupported" not in first.summary
    first_host.commit_compaction(first.draft_id)

    restart = MemoryService(engine, settings, fake).for_request(host, ctx)
    new = _append(restart, "Correction: ship Tuesday after review.")
    fake.fixtures["compact"] = deque([_fixture("Ship Tuesday after review.", new)])
    second = _compact(restart)
    prompt = json.loads(fake.calls[-1][2][1]["content"])
    assert prompt["previous_checkpoint"]["latest_decisions"][0]["text"] == "Ship Friday."
    assert {event["seq"] for event in prompt["events"]} == {3, 4}
    assert second.checkpoint.latest_decisions[0].source_event_ids == [new]
    assert second.retained_user_event_ids == [new]
    assert restart.commit_compaction(second.draft_id).method == "llm"
    bundle = restart.build_context(BuildContextRequest(session_id="s", sections=["working"]))
    assert bundle.pressure == "warning"
    assert "Correction: ship Tuesday after review." in bundle.rendered
    assert "Friday" not in bundle.rendered
    assert any(ref.compaction_method == "llm" for ref in bundle.sections[0].items)
    assert restart.get_bundle(bundle.bundle_id).pressure == "warning"
    for user, project in (("other", "atlas"), ("owner", "other")):
        outsider = MemoryService(engine, settings, fake).for_request(
            host, make_ctx(user=user, project=project)
        )
        with pytest.raises(NotFound):
            outsider.get_bundle(bundle.bundle_id)
        with pytest.raises(NotFound):
            outsider.commit_compaction(second.draft_id)


def test_unconfigured_compactor_marks_extractive_fallback(service, make_ctx):
    ctx = make_ctx(user="owner", project="atlas")
    owner = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
    _append(owner, "Keep this note.")
    draft = _compact(owner)
    assert draft.method == "extractive_fallback"
    owner.commit_compaction(draft.draft_id)
    bundle = owner.build_context(BuildContextRequest(session_id="s", sections=["working"]))
    assert bundle.sections[0].items[0].compaction_method == "extractive_fallback"
