import csv
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memfxxk.domain import Principal
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import PatchRequest, SaveLessonRequest
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.ingest import NotFound
from memfxxk.jobs.worker import drain
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def _export(data: str, delimiter: str = ",") -> int:
    return sum(
        int(row["quantity"]) for row in csv.DictReader(data.splitlines(), delimiter=delimiter)
    )


def test_save_recall_and_reuse_verified_export_steps(service: MemoryService, make_ctx) -> None:
    ctx = make_ctx(user="operator", project="warehouse")
    owner = service.for_request(Principal(org_id=ctx.org_id, role="admin"), ctx)
    data = "name;quantity\nplum;3\npear;2"
    with pytest.raises(KeyError, match="quantity"):
        _export(data)
    assert _export(data, delimiter=";") == 5
    failure, check = owner.ingest(
        [
            IngestEvent(
                kind="tool_result",
                session_id="first-export",
                payload={"outcome": "failure", "error": "Missing quantity column"},
            ),
            IngestEvent(
                kind="tool_result",
                session_id="first-export",
                payload={"outcome": "success", "quantity": 5},
            ),
        ]
    )
    request = SaveLessonRequest(
        title="Warehouse export with semicolon",
        scenario="The semicolon export reports a missing quantity column.",
        prerequisites=["The header uses semicolons."],
        steps=["Read the file with delimiter=; and sum the quantity column."],
        result="Quantity total is 5.",
        failure_event_id=failure.event_id,
        verification_event_id=check.event_id,
        idempotency_key="warehouse-recipe",
    )
    saved = owner.save_lesson(request)
    assert owner.save_lesson(request) == saved
    owner.ingest([IngestEvent(kind="message", session_id="next-export", content="Run the export")])
    search = SearchRequest(query="semicolon export", types=["lesson"], channels=["lexical"])
    assert [hit.memory_id for hit in owner.search(search).hits] == [saved.memory_id]
    drain(service, kinds={"embed_record"})
    vector = search.model_copy(update={"channels": ["vector"]})
    assert [hit.memory_id for hit in owner.search(vector).hits] == [saved.memory_id]
    lesson = owner.get_memory(saved.memory_id)
    assert lesson.confirmed and lesson.origin == "user_explicit"
    assert lesson.source_event_ids == [failure.event_id, check.event_id]
    assert lesson.source_pos == {
        "failure_event_id": str(failure.event_id),
        "verification_event_id": str(check.event_id),
    }
    assert owner.get_event(check.event_id).payload["quantity"] == 5
    delimiter = ";" if "delimiter=;" in lesson.content else ","
    assert _export(data, delimiter) == 5

    context = BuildContextRequest(
        session_id="next-export", query=search.query, sections=["evidence"]
    )
    assert str(saved.memory_id) not in owner.build_context(context).rendered
    context.search_filters = SearchFilters(types=["lesson"], channels=["lexical"])
    bundle = owner.build_context(context)
    assert bundle.sections[0].items[0].ref_id == str(saved.memory_id)
    assert "The header uses semicolons" in bundle.rendered

    edited = owner.patch_memory(
        saved.memory_id, PatchRequest(title="Reviewed warehouse export", base_version=saved.version)
    )
    assert edited.version == 2
    assert owner.get_memory(saved.memory_id, version=1).title == request.title
    owner.forget_event(check.event_id)
    drain(service, kinds={"embed_record"})
    assert owner.search(search).hits == []
    for version in (1, 2):
        with pytest.raises(NotFound):
            owner.get_memory(saved.memory_id, version=version)
    with pytest.raises(NotFound):
        owner.save_lesson(request)


def test_lesson_api_checks_outcomes_order_and_source_visibility(api_client: TestClient) -> None:
    owner = {"X-Memfxxk-User": "owner", "X-Memfxxk-Project": "p1"}
    sent = api_client.post(
        "/v1/events",
        headers=owner,
        json={
            "events": [
                {"kind": "tool_result", "session_id": "source", "payload": {"outcome": "success"}},
                {"kind": "tool_result", "session_id": "source", "payload": {"outcome": "failure"}},
                {"kind": "tool_result", "session_id": "source", "payload": {"outcome": "success"}},
                {"kind": "message", "session_id": "source", "payload": {"outcome": "success"}},
            ]
        },
    )
    assert sent.status_code == 200
    early, failed, verified, unrelated = [event["event_id"] for event in sent.json()]
    body = {
        "title": "Export lesson",
        "scenario": "Report import failed",
        "steps": ["Select the report format"],
        "result": "The report imported",
        "failure_event_id": failed,
        "verification_event_id": verified,
        "idempotency_key": "export",
    }
    for bad in (early, unrelated):
        res = api_client.post(
            "/v1/lessons",
            headers=owner,
            json={
                **body,
                "verification_event_id": bad,
            },
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "invalid_lesson_evidence"
    saved = api_client.post("/v1/lessons", headers=owner, json=body)
    assert saved.status_code == 200
    assert (
        api_client.post(
            "/v1/lessons",
            headers=owner,
            json={
                **body,
                "result": "Different result",
            },
        ).status_code
        == 409
    )
    for scope in ({"X-Memfxxk-User": "other"}, {"X-Memfxxk-Project": "p2"}):
        headers = {**owner, **scope}
        private = api_client.post(
            "/v1/lessons",
            headers=headers,
            json={
                **body,
                "idempotency_key": None,
            },
        )
        missing = api_client.post(
            "/v1/lessons",
            headers=headers,
            json={
                **body,
                "failure_event_id": str(uuid4()),
                "idempotency_key": None,
            },
        )
        assert private.status_code == missing.status_code == 404
        assert private.json() == missing.json()
        memory_id = saved.json()["memory_id"]
        assert api_client.get(f"/v1/memories/{memory_id}", headers=headers).status_code == 404
        hits = api_client.post(
            "/v1/search",
            headers=headers,
            json={
                "query": "Export lesson",
                "types": ["lesson"],
                "channels": ["lexical"],
            },
        )
        assert hits.json()["hits"] == []
