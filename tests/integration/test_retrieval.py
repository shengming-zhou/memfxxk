import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import quantiles
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection

from memfxxk.config import Settings
from memfxxk.db import memory_marks, memory_relations, record_embeddings, records
from memfxxk.domain import Principal, RequestContext, Scope
from memfxxk.domain.retrieval import SearchHit, SearchRequest
from memfxxk.embedding import FakeEmbeddingProvider, ensure_embedding_index
from memfxxk.ids import new_id
from memfxxk.retrieval import search
from memfxxk.service import BoundService, MemoryService
from memfxxk.text import MixedTokenizer, build_tsvector_literal

pytestmark = pytest.mark.integration
_START = datetime(2026, 11, 25, tzinfo=UTC)
_ACTOR = {"type": "user", "id": "u1"}


def _scope(project: str | None = "p1", user: str | None = "u1") -> Scope:
    return Scope(org_id="test-org", user_id=user, project_id=project)


def _bound(
    service: MemoryService,
    *,
    project: str = "p1",
    user: str = "u1",
    **search_options: int,
) -> BoundService:
    settings = service.settings.model_copy(
        update={"search": service.settings.search.model_copy(update=search_options)}
    )
    ctx = RequestContext(**_scope(project, user).model_dump())
    return MemoryService(service.engine, settings).for_request(
        Principal(org_id=ctx.org_id, role="host"), ctx
    )


def _record(
    connection: Connection,
    settings: Settings,
    content: str,
    *,
    scope: Scope | None = None,
    session: str = "session",
    seq: int = 1,
    kind: str = "turn",
    status: str = "active",
    vector: list[float] | None = None,
) -> UUID:
    model = settings.embedding.model
    dim = settings.embedding.dim
    record_id, memory_id = new_id(), new_id()
    connection.execute(
        records.insert().values(
            id=record_id,
            memory_id=memory_id,
            version=1,
            **(scope or _scope()).model_dump(),
            type=kind,
            content=content,
            status=status,
            origin="auto_index",
            event_time=_START + timedelta(seconds=seq),
            session_id=session,
            source_pos={"session_id": session, "seq_from": seq, "seq_to": seq},
            created_by=_ACTOR,
            lexemes=build_tsvector_literal(MixedTokenizer().tokenize(content)),
        )
    )
    vector = vector if vector is not None else FakeEmbeddingProvider(model, dim).embed([content])[0]
    connection.execute(
        record_embeddings.insert().values(
            record_id=record_id, model=model, dim=dim, embedding=vector
        )
    )
    return memory_id


@pytest.fixture
def corpus(service: MemoryService) -> list[UUID]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval_corpus.json"
    contents = json.loads(path.read_text(encoding="utf-8"))
    with service.engine.begin() as connection:
        ids = [
            _record(connection, service.settings, content, seq=index)
            for index, content in enumerate(contents, 1)
        ]
        for index, content in enumerate(contents, 1):
            _record(connection, service.settings, content, scope=_scope("p2"), seq=index)
    return ids


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("虚拟环境迁移", 0),
        ("multipart boundary", 1),
        ("Python 3.12 迁移", 0),
        ("parseHTTPResponse", 2),
        ("test_upload.py", 3),
    ],
)
def test_relevant_hits(
    service: MemoryService, corpus: list[UUID], query: str, expected: int
) -> None:
    response = _bound(service).search(SearchRequest(query=query, k=3))
    assert corpus[expected] in {hit.memory_id for hit in response.hits}
    assert all(hit.scope.project_id == "p1" for hit in response.hits)


@pytest.mark.parametrize("channel", ["lexical", "vector"])
def test_single_channel(service: MemoryService, corpus: list[UUID], channel: str) -> None:
    response = _bound(service).search(
        SearchRequest(query="multipart boundary", k=3, channels=[channel])
    )
    assert corpus[1] in {hit.memory_id for hit in response.hits}
    for hit in response.hits:
        assert (hit.score.lexical_rank is not None) is (channel == "lexical")
        assert (hit.score.lexical_score is not None) is (channel == "lexical")
        assert (hit.score.vector_rank is not None) is (channel == "vector")
        assert (hit.score.vector_sim is not None) is (channel == "vector")
        assert not hit.score.important


def test_scope_and_metadata_filter_before_top_n(service: MemoryService) -> None:
    settings = service.settings
    with service.engine.begin() as connection:
        exact = "upgrade targeted atomic endpoint"
        for index in range(100):
            _record(connection, settings, exact, scope=_scope("p2"), seq=index)
        _record(connection, settings, exact, session="wrong-type")
        _record(connection, settings, exact, session="wrong-session", kind="note")
        target = _record(connection, settings, exact + " regression", session="target", kind="note")
        shared = _record(connection, settings, "shared preference", scope=_scope(None), kind="note")
        ensure_embedding_index(connection, settings.embedding.model, settings.embedding.dim)

    p1 = _bound(service, n_lexical=1, n_vector=1)
    for channel in (["vector"], ["lexical"]):
        filtered = p1.search(
            SearchRequest(query=exact, channels=channel, types=["note"], session_id="target", k=1)
        )
        assert [hit.memory_id for hit in filtered.hits] == [target]

    for project in ["p1", "p2"]:
        shared_result = _bound(service, project=project).search(
            SearchRequest(query="shared preference", channels=["lexical"])
        )
        assert [hit.memory_id for hit in shared_result.hits] == [shared]
    assert not _bound(service, user="u2").search(SearchRequest(query=exact)).hits


def test_important_recall_and_scope(service: MemoryService) -> None:
    settings = service.settings
    config = settings.embedding
    query = "cobalt migration release"
    exact = FakeEmbeddingProvider(config.model, config.dim).embed([query])[0]
    orthogonal = [0.0] * config.dim
    orthogonal[exact.index(0)] = 1.0
    almost = [0.8 * term + 0.6 * other for term, other in zip(exact, orthogonal, strict=True)]

    with service.engine.begin() as connection:
        baseline = _record(connection, settings, query, scope=_scope(None), vector=exact)
        important = _record(
            connection, settings, "related review", scope=_scope(None), vector=almost
        )
        unrelated = _record(
            connection, settings, "unrelated", scope=_scope(None), vector=orthogonal
        )
        connection.execute(
            memory_marks.insert(),
            [
                dict(
                    id=new_id(),
                    memory_id=memory_id,
                    mark="important",
                    created_by=_ACTOR,
                    source_event_id=new_id(),
                    **_scope("p1").model_dump(),
                )
                for memory_id in (important, unrelated)
            ],
        )

    request = SearchRequest(query=query, channels=["vector", "important"])
    p1 = _bound(service, n_vector=1)
    assert [hit.memory_id for hit in p1.search(request).hits] == [important, baseline]
    mark_hit = p1.search(request).hits[0]
    assert mark_hit.score.important
    assert mark_hit.marks == ["important"]
    assert [
        hit.memory_id for hit in _bound(service, project="p2", n_vector=1).search(request).hits
    ] == [baseline]
    vector_only = request.model_copy(update={"channels": ["vector"]})
    assert [hit.memory_id for hit in p1.search(vector_only).hits] == [baseline]


def test_relations_and_inactive(service: MemoryService) -> None:
    settings = service.settings
    with service.engine.begin() as connection:
        start = _record(connection, settings, "cobalt migration source")
        allowed = _record(connection, settings, "verified remedy")
        hidden = _record(connection, settings, "other project link", scope=_scope("p2"))
        inactive = _record(connection, settings, "deprecated fix", status="inactive")
        connection.execute(
            memory_relations.insert(),
            [
                dict(
                    id=new_id(),
                    from_memory_id=source,
                    to_memory_id=target,
                    purpose="fixes",
                    org_id="test-org",
                    created_by=_ACTOR,
                    source_event_id=new_id(),
                )
                for source, target in [(start, allowed), (hidden, start), (inactive, start)]
            ],
        )
    bound = _bound(service)
    hit = bound.search(SearchRequest(query="cobalt", channels=["lexical"])).hits[0]
    assert [(link.memory_id, link.direction) for link in hit.related] == [(allowed, "outgoing")]
    reverse = bound.get_memory(allowed).relations
    assert [(link.memory_id, link.direction) for link in reverse] == [(start, "incoming")]
    deprecated = SearchRequest(query="deprecated", channels=["lexical"])
    assert not bound.search(deprecated).hits
    assert [
        hit.memory_id
        for hit in bound.search(deprecated.model_copy(update={"include_inactive": True})).hits
    ] == [inactive]


def test_window_and_notfound(
    service: MemoryService, corpus: list[UUID], api_client: TestClient
) -> None:
    with service.engine.begin() as connection:
        _record(connection, service.settings, "other session", session="other", seq=3)
        other_user = _record(connection, service.settings, "other user", scope=_scope(user="u2"))
    response = _bound(service).get_memory(corpus[2], window=2)
    assert [item.memory_id for item in response.neighbors.before] == corpus[:2]
    assert [item.memory_id for item in response.neighbors.after] == corpus[3:5]
    assert "parseHTTPResponse" in response.content
    assert [item.version for item in response.versions] == [1]
    assert _bound(service).get_memory(corpus[0], window=2).neighbors.before == []

    headers = {"X-Memfxxk-User": "u1", "X-Memfxxk-Project": "p1"}
    hidden = api_client.get(f"/v1/memories/{other_user}", headers=headers)
    absent = api_client.get(f"/v1/memories/{new_id()}", headers=headers)
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()
    searched = api_client.post("/v1/search", headers=headers, json={"query": "CI", "k": 2})
    assert searched.status_code == 200
    assert searched.json()["hits"]


def test_return_rechecks_visibility_after_reranking(service: MemoryService) -> None:
    with service.engine.begin() as connection:
        target = _record(connection, service.settings, "unique cobalt")

    class RetireMemory:
        def rerank(self, request: SearchRequest, hits: list[SearchHit]) -> list[SearchHit]:
            with service.engine.begin() as connection:
                connection.execute(
                    records.update().where(records.c.memory_id == target).values(status="inactive")
                )
            return hits

    request = SearchRequest(query="cobalt", channels=["lexical"])
    assert not search(_bound(service), request, reranker=RetireMemory()).hits


@pytest.mark.slow
def test_search_performance(service: MemoryService) -> None:
    settings = service.settings.model_copy(
        update={
            "embedding": service.settings.embedding.model_copy(
                update={"model": "performance-smoke", "dim": 32}
            )
        }
    )
    service = MemoryService(service.engine, settings)
    embedding = settings.embedding
    provider = FakeEmbeddingProvider(embedding.model, embedding.dim)
    vector = provider.embed(["cobalt pipeline"])[0]
    vector_values = [vector, provider.embed(["ordinary random documentation"])[0]]
    lexemes = [
        build_tsvector_literal(MixedTokenizer().tokenize(content))
        for content in ("cobalt pipeline", "ordinary random documentation")
    ]
    with service.engine.begin() as connection:
        for start in range(0, 20_000, 1000):
            keys = [(new_id(), new_id()) for _ in range(1000)]
            connection.execute(
                records.insert(),
                [
                    dict(
                        id=record_id,
                        memory_id=memory_id,
                        version=1,
                        **_scope().model_dump(),
                        type="turn",
                        content="cobalt pipeline" if index % 64 == 0 else "ordinary documentation",
                        event_time=_START,
                        origin="auto_index",
                        lexemes=lexemes[int(index % 64 != 0)],
                        created_by=_ACTOR,
                    )
                    for index, (record_id, memory_id) in enumerate(keys, start)
                ],
            )
            connection.execute(
                record_embeddings.insert(),
                [
                    dict(
                        record_id=record_id,
                        model=embedding.model,
                        dim=embedding.dim,
                        embedding=vector_values[int(index % 64 != 0)],
                    )
                    for index, (record_id, _) in enumerate(keys, start)
                ],
            )
        ensure_embedding_index(connection, embedding.model, embedding.dim)
        connection.exec_driver_sql("ANALYZE records")
        connection.exec_driver_sql("ANALYZE record_embeddings")
    bound = _bound(service)
    request = SearchRequest(query="cobalt pipeline")
    assert bound.search(request).hits
    samples = [bound.search(request).timings_ms.total for _ in range(40)]
    assert quantiles(samples, n=100)[94] < 150
