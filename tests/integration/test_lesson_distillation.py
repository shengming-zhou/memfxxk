import json
from uuid import uuid4

import pytest

from memfxxk.config import StrategiesSettings
from memfxxk.domain import Principal
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.lessons import FinishTask, ReviewLesson
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.ingest import NotFound
from memfxxk.jobs.worker import drain
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


class TaskModel(FakeLLM):
    def __init__(self, outcome):
        super().__init__({})
        self.outcome = outcome

    def complete(self, task, messages):
        data = json.loads(messages[-1]["content"])
        self.calls.append((task, data))
        if task == "trajectory_judge":
            reply = {"outcome": self.outcome, "rationale": "Looked at actual output."}
        else:
            reply = {
                "lessons": [
                    {
                        "title": "CSV import",
                        "description": "For CSV sensor logs.",
                        "content": "Check the delimiter and verify the header before importing.",
                        "source_event_ids": [data["trajectory"][1]["id"]],
                    },
                    {
                        "title": "Invalid",
                        "description": "Should be rejected.",
                        "content": "Unknown.",
                        "source_event_ids": [str(uuid4())],
                    },
                ]
            }
        return ChatResult({"content": json.dumps(reply)})


def _service(service, strategy, outcome, visibility="pending"):
    settings = service.settings.model_copy(
        update={
            "strategies": StrategiesSettings(lessons=strategy, lesson_visibility=visibility),
        }
    )
    return MemoryService(service.engine, settings, TaskModel(outcome))


def _bound(service, make_ctx, user="u", project="p", *, human=False):
    ctx = make_ctx(user=user, project=project)
    if human:
        ctx = ctx.model_copy(update={"actor": ctx.actor.model_copy(update={"type": "user"})})
    return service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)


def _trajectory(bound, session):
    return bound.ingest(
        [
            IngestEvent(
                kind="message",
                role="user",
                content="Import the CSV.",
                session_id=session,
                turn_id="t",
            ),
            IngestEvent(
                kind="tool_result",
                role="tool",
                content="CSV imported 3 rows.",
                session_id=session,
                turn_id="t",
                payload={"outcome": "success"},
            ),
            IngestEvent(
                kind="message",
                role="assistant",
                content="Imported and verified.",
                session_id=session,
                turn_id="t",
            ),
        ]
    )


def test_lesson_distillation_and_explicit_acceptance(service, make_ctx):
    service = _service(service, "both", "success")
    host = _bound(service, make_ctx, human=True)
    model = _bound(service, make_ctx)
    events = _trajectory(model, "import-task")
    task = FinishTask(task="Import the sensor CSV and verify row counts.")
    finished = model.finish_task("import-task", task)
    assert finished.jobs and model.finish_task("import-task", task) == finished
    drain(service)
    assert [task for task, _ in service.fake_llm.calls] == [
        "trajectory_judge",
        "lesson_distill",
        "workflow_induce",
    ]
    proposals = host.list_lesson_proposals()
    assert {item.strategy for item in proposals} == {"reasoningbank", "awm"}
    assert all(item.status == "pending" and item.decision == "success" for item in proposals)
    assert all(
        {events[1].event_id, finished.event_id} <= set(item.source_event_ids) for item in proposals
    )
    query = SearchRequest(query="CSV import", types=["lesson"])
    assert model.search(query).hits == []
    for outsider in (
        _bound(service, make_ctx, user="other"),
        _bound(service, make_ctx, project="other"),
    ):
        assert outsider.list_lesson_proposals() == []
        with pytest.raises(NotFound):
            outsider.finish_task("import-task", task)
        with pytest.raises(NotFound):
            outsider.review_lesson(
                proposals[0].memory_id, ReviewLesson(base_version=1, accept=True)
            )
    with pytest.raises(ServiceError) as untrusted:
        model.review_lesson(proposals[0].memory_id, ReviewLesson(base_version=1, accept=True))
    assert untrusted.value.code == "lesson_requires_user"
    result = host.review_lesson(proposals[0].memory_id, ReviewLesson(base_version=1, accept=True))
    assert result.version == 2 and result.status == "accepted"
    assert model.get_memory(result.memory_id).confirmed
    drain(service)
    bundle = model.build_context(
        BuildContextRequest(
            query="CSV import",
            sections=["evidence"],
            max_tokens=500,
        )
    )
    item = next(item for item in bundle.sections[0].items if item.ref_id == str(result.memory_id))
    assert item.decision == "selected" and item.strategy == result.strategy
    assert item.record_type == "lesson" and item.candidate_tokens > 0
    assert "Check the delimiter" not in model.get_bundle(bundle.bundle_id).model_dump_json()
    host.forget_event(events[1].event_id)
    assert model.search(query).hits == [] and host.list_lesson_proposals() == []


def test_searchable_failure_is_not_auto_loaded_and_awm_skips_it(service, make_ctx):
    service = _service(service, "both", "failure", visibility="searchable")
    host = _bound(service, make_ctx, human=True)
    _trajectory(host, "failed-task")
    host.finish_task("failed-task", FinishTask(task="Import the sensor CSV."))
    drain(service)
    assert [task for task, _ in service.fake_llm.calls] == ["trajectory_judge", "lesson_distill"]
    item = host.list_lesson_proposals()[0]
    assert item.status == "searchable" and item.decision == "failure"
    assert host.search(SearchRequest(query="CSV", types=["lesson"])).hits
    bundle = host.build_context(
        BuildContextRequest(
            query="CSV",
            sections=["evidence"],
            search_filters=SearchFilters(types=["lesson"]),
        )
    )
    assert not bundle.sections[0].items
    assert bundle.sections[0].dropped[0].decision_reason == "lesson_requires_review"
    rejected = host.review_lesson(item.memory_id, ReviewLesson(base_version=1, accept=False))
    assert rejected.status == "rejected"
    assert not host.search(SearchRequest(query="CSV", types=["lesson"], include_inactive=True)).hits
