from uuid import UUID

import pytest

from memfxxk.domain import Principal
from memfxxk.domain.context import AllocationTrace, BuildContextRequest
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.llm import ChatResult
from memfxxk.llm.runner import FakeLLM
from memfxxk.llm.settings import LLMSettings, TaskModel
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration

WORLD = "world/gates.md"
TEXT = (
    "# Gates\nA scarlet flare closes the north gate.\n\n"
    + "The stones on the road are rough and dark. " * 30
    + "\n\n## Storms\nThe ferry is tied during winter storms."
)


@pytest.fixture
def world(api_client):
    headers = {"X-Memfxxk-User": "author", "X-Memfxxk-Project": "novel"}
    response = api_client.put(
        f"/v1/documents/{WORLD}", headers=headers, json={"content": TEXT, "load_policy": "always"}
    )
    assert response.status_code == 200
    doc = response.json()
    priority = api_client.put(
        "/v1/priorities",
        headers=headers,
        json={"target": {"kind": "section", "path": WORLD, "heading": "Gates"}, "priority": 80},
    )
    assert priority.status_code == 200
    return headers, doc


def test_build_respects_host_budget_reports_resolution_and_hides_original(api_client, world):
    headers, doc = world
    request = {"allocator": "adaptive", "query": "scarlet flare north gate", "max_tokens": 95}
    response = api_client.post("/v1/context/build", headers=headers, json=request)
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert bundle["allocator"] == "adaptive"
    assert bundle["total_tokens"] <= 95
    assert bundle["pressure_ratio"] == bundle["total_tokens"] / 95
    assert bundle["pressure"] == ("warning" if bundle["pressure_ratio"] > 0.7 else "normal")
    section = bundle["sections"][0]
    assert {item["section_index"] for item in section["items"] + section["dropped"]} == {0, 1}
    first = section["items"][0]
    assert first["ref_id"] == doc["document_id"]
    assert first["effective_priority"] == 80
    assert first["resolution"] in ("line", "summary")
    assert "scarlet flare" in bundle["rendered"]
    url = f"/v1/context/bundles/{bundle['bundle_id']}"
    stored = api_client.get(url, headers=headers)
    assert stored.status_code == 200
    assert AllocationTrace.model_validate(stored.json()["allocation"]) == (
        AllocationTrace.model_validate(bundle["allocation"])
    )
    assert "scarlet flare" not in stored.text
    for alternate in (
        {**headers, "X-Memfxxk-User": "another"},
        {**headers, "X-Memfxxk-Project": "another"},
    ):
        foreign = api_client.post("/v1/context/build", headers=alternate, json=request).json()
        assert foreign["rendered"] == ""
        assert api_client.get(url, headers=alternate).status_code == 404


@pytest.mark.parametrize("capacity", [0, 30, 240])
def test_fixed_and_adaptive_explain_same_source_with_a_global_limit(api_client, world, capacity):
    headers, doc = world
    for allocator in ("fixed", "adaptive"):
        response = api_client.post(
            "/v1/context/build",
            headers=headers,
            json={"allocator": allocator, "max_tokens": capacity, "sections": ["documents"]},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total_tokens"] <= capacity
        items = payload["sections"][0]["items"] + payload["sections"][0]["dropped"]
        assert {entry["ref_id"] for entry in items} == {doc["document_id"]}
        assert all(entry["decision"] and entry["decision_reason"] for entry in items)


def test_plan_clamps_filters_tracks_and_falls_back_on_invalid_payload(service, make_ctx):
    scoped = make_ctx(user="writer", project="novel")
    principal = Principal(org_id=scoped.org_id, role="host")
    settings = service.settings.model_copy(
        update={"llm": LLMSettings(tasks={"context_plan": TaskModel(model="fake")})}
    )
    original = service.for_request(principal, scoped)
    original.put_document(WORLD, PutDocumentRequest(content=TEXT, load_policy="always"))
    query = BuildContextRequest(query="A scarlet flare", allocator="adaptive", max_tokens=170)
    baseline = original.build_context(query)
    enabled = query.model_copy(update={"plan_with_llm": True})
    valid = FakeLLM(
        {"context_plan": [ChatResult({"content": '{"coefficients":{"3":100,"999":1.1}}'})]}
    )
    with_plan = MemoryService(service.engine, settings, valid).for_request(principal, scoped)
    actual = with_plan.build_context(enabled)
    assert actual.allocation.plan_coefficients == {3: 1.3}
    assert isinstance(actual.allocation.plan_call_id, UUID)
    invalid = FakeLLM({"context_plan": [ChatResult({"content": "broken response"})] * 2})
    broken = MemoryService(service.engine, settings, invalid).for_request(principal, scoped)
    fallback = broken.build_context(enabled)
    assert fallback.rendered == baseline.rendered
    assert fallback.allocation == baseline.allocation
