from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _headers(project: str, user: str = "operator") -> dict[str, str]:
    return {"X-Memfxxk-Project": project, "X-Memfxxk-User": user}


def test_workspace_moves_documents_and_selected_lesson_evidence(api_client: TestClient) -> None:
    source, target = _headers("source"), _headers("target")
    put = api_client.put(
        "/v1/documents/guide/setup.md",
        headers=source,
        json={
            "content": "# Setup\n\nUse UTF-8.\n",
            "purpose": "Import checklist",
            "load_policy": "always",
        },
    )
    assert put.status_code == 200
    assert (
        api_client.put(
            "/v1/documents/user.md",
            headers=source,
            json={
                "content": "Personal copy",
                "scope_drop": ["project_id"],
            },
        ).status_code
        == 200
    )
    assert (
        api_client.put(
            "/v1/documents/private.md",
            headers=_headers("source", "other"),
            json={
                "content": "Private copy",
            },
        ).status_code
        == 200
    )

    failed, verified = api_client.post(
        "/v1/events",
        headers=source,
        json={
            "events": [
                {
                    "kind": "tool_result",
                    "content": "Wrong encoding",
                    "payload": {"outcome": "failure"},
                },
                {
                    "kind": "tool_result",
                    "content": "Three rows imported",
                    "payload": {"outcome": "success"},
                },
            ]
        },
    ).json()
    saved = api_client.post(
        "/v1/lessons",
        headers=source,
        json={
            "title": "UTF-8 import",
            "scenario": "File import failed",
            "steps": ["Select UTF-8"],
            "result": "Three rows imported",
            "failure_event_id": failed["event_id"],
            "verification_event_id": verified["event_id"],
        },
    )
    lesson_id = saved.json()["memory_id"]
    edited = api_client.patch(
        f"/v1/memories/{lesson_id}",
        headers=source,
        json={
            "base_version": 1,
            "content": "# UTF-8 import\n\nReviewed procedure.\n",
        },
    )
    assert edited.json()["version"] == 2
    plain = api_client.get("/v1/workspace", headers=source).json()
    assert not plain["lessons"]
    assert [item["path"] for item in plain["documents"]] == ["guide/setup.md"]
    snapshot = api_client.get(
        "/v1/workspace",
        headers=source,
        params={
            "lesson_ids": lesson_id,
        },
    ).json()
    assert snapshot["lessons"][0]["verification"]["content"] == "Three rows imported"
    for other in (target, _headers("source", "other")):
        forbidden = api_client.get("/v1/workspace", headers=other, params={"lesson_ids": lesson_id})
        missing = api_client.get(
            "/v1/workspace", headers=other, params={"lesson_ids": str(uuid4())}
        )
        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.json() == missing.json()

    def transfer(headers: dict[str, str]) -> dict:
        response = api_client.post("/v1/workspace", headers=headers, json={"snapshot": snapshot})
        assert response.status_code == 200, response.text
        return response.json()

    same = transfer(source)
    assert same["documents_unchanged"] == same["lessons_unchanged"] == 1
    copied = transfer(target)
    assert copied["documents_created"] == copied["lessons_created"] == 1
    same = transfer(target)
    assert same["documents_unchanged"] == same["lessons_unchanged"] == 1
    moved = api_client.get("/v1/documents/guide/setup.md", headers=target).json()
    assert moved["load_policy"] == "always" and moved["purpose"] == "Import checklist"
    assert moved["content"] == put.json()["content"] and moved["revision"] == 1
    assert moved["document_id"] != put.json()["document_id"]
    listings = api_client.get("/v1/memories", headers=target, params={"types": "lesson"}).json()
    (imported_id,) = [item["memory_id"] for item in listings["items"]]
    assert imported_id != lesson_id
    imported = api_client.get(f"/v1/memories/{imported_id}", headers=target).json()
    assert imported["content"] == snapshot["lessons"][0]["content"]
    assert imported["source_pos"]["workspace_memory_id"] == lesson_id
    assert imported["source_pos"]["workspace_version"] == 2
    assert imported["origin"] == "import"
    event_id = imported["source_event_ids"][1]
    assert event_id != verified["event_id"]
    event = api_client.get(f"/v1/events/{event_id}", headers=target).json()
    assert event["source"]["workspace_event_id"] == verified["event_id"]
    assert event["content"] == "Three rows imported"
    assert api_client.get(f"/v1/events/{event_id}", headers=source).status_code == 404
    changed = deepcopy(snapshot)
    changed["documents"].append({"path": "new.md", "content": "Should roll back"})
    changed["lessons"][0]["content"] = "Conflicting lesson"
    response = api_client.post("/v1/workspace", headers=target, json={"snapshot": changed})
    assert response.status_code == 409
    assert api_client.get("/v1/documents/new.md", headers=target).status_code == 404
    assert api_client.post(f"/v1/events/{event_id}/forget", headers=target).status_code == 200
    assert api_client.get(f"/v1/memories/{imported_id}", headers=target).status_code == 404
    assert api_client.get(f"/v1/memories/{lesson_id}", headers=source).status_code == 200
    assert (
        api_client.post(
            "/v1/workspace",
            headers=target,
            json={
                "snapshot": snapshot,
            },
        ).status_code
        == 404
    )


def test_workspace_documents_abort_on_conflict_and_require_current_revision(api_client: TestClient):
    source, target = _headers("source"), _headers("target")
    for path in ("a.md", "z.md"):
        assert (
            api_client.put(
                f"/v1/documents/{path}", headers=source, json={"content": "Original"}
            ).status_code
            == 200
        )
    assert (
        api_client.put(
            "/v1/documents/z.md", headers=target, json={"content": "Keep my edit"}
        ).status_code
        == 200
    )
    snapshot = api_client.get("/v1/workspace", headers=source).json()
    for update in (False, True):
        conflict = api_client.post(
            "/v1/workspace",
            headers=target,
            json={
                "snapshot": snapshot,
                "update_existing": update,
            },
        )
        assert conflict.status_code == 409
        assert api_client.get("/v1/documents/a.md", headers=target).status_code == 404
    assert api_client.get("/v1/documents/z.md", headers=target).json()["content"] == "Keep my edit"

    snapshot["documents"][0]["content"] = "A new revision from disk"
    request = {"snapshot": snapshot, "update_existing": True}
    result = api_client.post("/v1/workspace", headers=source, json=request)
    assert result.json()["documents_updated"] == 1
    assert (
        api_client.post("/v1/workspace", headers=source, json=request).json()["documents_unchanged"]
        == 2
    )
    snapshot["documents"][0]["content"] = "Old snapshot edited again"
    assert api_client.post("/v1/workspace", headers=source, json=request).status_code == 409
    original = api_client.get("/v1/documents/a.md/revisions/1", headers=source).json()
    assert original["content"] == "Original"
    current = api_client.get("/v1/documents/a.md", headers=source).json()
    assert current["content"] == "A new revision from disk" and current["revision"] == 2
