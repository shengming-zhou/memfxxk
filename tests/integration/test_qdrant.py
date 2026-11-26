import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

import memfxxk.qdrant as qdrant
from memfxxk.config import QdrantSettings
from memfxxk.db import jobs, qdrant_replicas, record_embeddings, records
from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import RememberRequest
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.jobs import Worker, drain
from memfxxk.service import BoundService, MemoryService

pytestmark = pytest.mark.integration


class FakeQdrant:
    def __init__(self) -> None:
        self.points: dict[str, dict] = {}
        self.dimensions: dict[str, int] = {}
        self.query_ids: list[list[str]] = []
        self.injected: list[dict] | None = None
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        parts = request.url.path.split("/")
        assert parts[1] == "collections"
        collection = parts[2]
        body = json.loads(request.content) if request.content else {}
        if len(parts) == 3:
            if request.method == "PUT":
                self.dimensions[collection] = body["vectors"]["size"]
                assert body["vectors"]["distance"] == "Cosine"
                return httpx.Response(200, json={"result": True})
            if collection not in self.dimensions:
                return httpx.Response(404)
            vectors = {"size": self.dimensions[collection], "distance": "Cosine"}
            return httpx.Response(
                200, json={"result": {"config": {"params": {"vectors": vectors}}}}
            )
        if collection not in self.dimensions:
            return httpx.Response(404)
        if request.method == "PUT":
            assert request.url.params["wait"] == "true"
            assert request.url.params["ordering"] == "strong"
            assert all(set(point["payload"]) == {"generation"} for point in body["points"])
            self.points.update((point["id"], point) for point in body["points"])
        elif parts[-1] == "delete":
            assert request.url.params["wait"] == "true"
            for point_id in body["points"]:
                self.points.pop(point_id, None)
        else:
            ids = body["filter"]["must"][0]["has_id"]
            self.query_ids.append(ids)
            if self.injected is not None:
                return httpx.Response(200, json={"result": {"points": self.injected}})
            query = body["query"]
            scored = [
                {
                    "id": point_id,
                    "payload": self.points[point_id]["payload"],
                    "score": sum(
                        a * b for a, b in zip(query, self.points[point_id]["vector"], strict=True)
                    ),
                }
                for point_id in ids
                if point_id in self.points
            ]
            scored.sort(key=lambda point: (-point["score"], point["id"]))
            return httpx.Response(200, json={"result": {"points": scored[: body["limit"]]}})
        return httpx.Response(200, json={"result": {"status": "completed"}})


@pytest.fixture
def external(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MemoryService, FakeQdrant]:
    fake = FakeQdrant()
    transport = httpx.MockTransport(fake)
    original = qdrant.QdrantClient
    monkeypatch.setattr(
        qdrant, "QdrantClient", lambda settings: original(settings, transport=transport)
    )
    settings = service.settings.model_copy(
        update={
            "search": service.settings.search.model_copy(update={"vector_backend": "qdrant"}),
            "qdrant": QdrantSettings(
                base_url="http://fixture.invalid:6333", collection_prefix="fixture"
            ),
        }
    )
    return MemoryService(service.engine, settings), fake


def _request(service: MemoryService, user: str, project: str) -> BoundService:
    return service.for_request(
        Principal(org_id="test-org", role="host"),
        RequestContext(org_id="test-org", user_id=user, project_id=project),
    )


def _record_id(service: MemoryService, memory_id: UUID) -> str:
    with service.engine.connect() as connection:
        return str(
            connection.scalar(
                select(records.c.id).where(
                    records.c.memory_id == memory_id,
                    records.c.is_current,
                )
            )
        )


def test_qdrant_prefilters_visibility_and_falls_back_for_untrusted_candidates(
    external: tuple[MemoryService, FakeQdrant],
) -> None:
    service, fake = external
    visible = _request(service, "owner", "p1")
    target = visible.remember(RememberRequest(content="amber visible", important=True))
    hidden_project = _request(service, "owner", "p2").remember(RememberRequest(content="amber"))
    hidden_user = _request(service, "another", "p1").remember(RememberRequest(content="amber"))
    drain(service)
    target_id = _record_id(service, target.memory_id)
    hidden_ids = [_record_id(service, item.memory_id) for item in (hidden_project, hidden_user)]
    assert set(fake.points) == {target_id, *hidden_ids}
    request = SearchRequest(query="amber", channels=["vector", "important"], k=1)
    assert [hit.memory_id for hit in visible.search(request).hits] == [target.memory_id]
    assert fake.query_ids == [[target_id], [target_id]]

    fake.injected = [
        {"id": hidden_ids[0], "score": 1, "payload": {"generation": 0}},
        {"id": target_id, "score": 1, "payload": {"generation": 999}},
    ]
    assert [hit.memory_id for hit in visible.search(request).hits] == [target.memory_id]
    assert fake.query_ids[-2:] == [[target_id], [target_id]]

    visible.remember(RememberRequest(content="bamboo visible"))
    drain(service)
    queries_before = len(fake.query_ids)
    capped_settings = service.settings.model_copy(
        update={"qdrant": service.settings.qdrant.model_copy(update={"max_prefilter_ids": 1})}
    )
    capped = _request(MemoryService(service.engine, capped_settings), "owner", "p1")
    vector_request = SearchRequest(query="amber", channels=["vector"], k=1)
    assert [hit.memory_id for hit in capped.search(vector_request).hits] == [target.memory_id]
    assert len(fake.query_ids) == queries_before


def test_qdrant_never_ranks_unpublished_generations_and_removes_deleted_points(
    external: tuple[MemoryService, FakeQdrant],
) -> None:
    service, fake = external
    visible = _request(service, "owner", "p1")
    event = IngestEvent(
        kind="message",
        role="user",
        content="amber before",
        session_id="work",
        turn_id="turn",
    )
    created = visible.ingest([event])[0]
    memory_id = created.memory_ids_touched[0]
    record_id = _record_id(service, memory_id)
    drain(service)
    assert fake.points[record_id]["payload"]["generation"] == 0
    visible.ingest([event.model_copy(update={"role": "assistant", "content": "amber after"})])
    drain(service, kinds={"embed_record"})
    request = SearchRequest(query="amber", channels=["vector"])
    assert [hit.memory_id for hit in visible.search(request).hits] == [memory_id]
    assert fake.query_ids == []
    drain(service)
    assert fake.points[record_id]["payload"]["generation"] == 1
    assert [hit.memory_id for hit in visible.search(request).hits] == [memory_id]
    assert fake.query_ids == [[record_id]]

    visible.forget_event(created.event_id)
    assert visible.search(request).hits == []
    assert record_id in fake.points
    # A worker on the old endpoint may finish erasure even when searches return to pgvector.
    normal = MemoryService(
        service.engine,
        service.settings.model_copy(
            update={
                "search": service.settings.search.model_copy(update={"vector_backend": "pgvector"}),
            }
        ),
    )
    drain(normal)
    assert record_id not in fake.points
    with service.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(qdrant_replicas)) == 0


def test_qdrant_outage_keeps_embeddings_and_removal_retryable(
    external: tuple[MemoryService, FakeQdrant],
) -> None:
    service, fake = external
    visible = _request(service, "owner", "p1")
    saved = visible.remember(RememberRequest(content="amber pending"))
    fake.down = True
    worker = Worker(service)
    assert worker.run_once({"embed_record"}) == 1
    assert worker.run_once({qdrant.SYNC_KIND}) == 1
    with service.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(record_embeddings)) == 1
        state = connection.execute(
            select(jobs.c.status, jobs.c.attempts).where(
                jobs.c.kind == qdrant.SYNC_KIND,
            )
        ).one()
        assert state == ("queued", 1)
    request = SearchRequest(query="amber", channels=["vector"])
    assert [hit.memory_id for hit in visible.search(request).hits] == [saved.memory_id]
    fake.down = False
    with service.engine.begin() as connection:
        connection.execute(
            jobs.update()
            .where(jobs.c.kind == qdrant.SYNC_KIND)
            .values(
                run_after=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    drain(service)
    assert fake.points
    fake.down = True
    assert [hit.memory_id for hit in visible.search(request).hits] == [saved.memory_id]
    visible.forget(saved.memory_id, "delete")
    assert worker.run_once({qdrant.SYNC_KIND}) == 1
    assert fake.points and visible.search(request).hits == []
    with service.engine.begin() as connection:
        connection.execute(
            jobs.update().where(jobs.c.kind == qdrant.SYNC_KIND).values(status="dead")
        )
    alternate = service.settings.model_copy(
        update={
            "qdrant": QdrantSettings(base_url="http://different.invalid:6333"),
        }
    )
    assert MemoryService(service.engine, alternate).requeue_qdrant() == 0
    assert service.requeue_qdrant() == 1
    assert Worker(MemoryService(service.engine, alternate)).run_once({qdrant.SYNC_KIND}) == 0
    fake.down = False
    drain(service)
    assert fake.points == {}
