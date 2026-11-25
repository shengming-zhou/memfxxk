import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from typer.testing import CliRunner

from memfxxk.cli import app
from memfxxk.client import Conflict, MemfxxkClient, NotFound
from memfxxk.db import events, record_embeddings, records
from memfxxk.domain.context import BuildContextRequest, ContextBundle, ContextSection, SearchFilters
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.manage import LinkRequest, ListRequest, PatchRequest, RememberRequest
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.domain.sessions import PutSessionState
from memfxxk.jobs import drain
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


@pytest.fixture
def data() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "acceptance" / "memories.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(params=(False, True), ids=("live", "A10-reembedded"))
def checkpoint(
    request: pytest.FixtureRequest,
    engine: Engine,
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], None]:
    def process() -> None:
        drain(service)
        if request.param:
            monkeypatch.setenv(
                "MEMFXXK_DATABASE_URL", engine.url.render_as_string(hide_password=False)
            )
            for clear_index in (False, True):
                if clear_index:
                    with engine.begin() as connection:
                        connection.execute(record_embeddings.delete())
                result = CliRunner().invoke(app, ["index", "reembed"])
                assert result.exit_code == 0, result.output
                drain(service)
            with engine.connect() as connection:
                current = set(connection.scalars(select(records.c.id).where(records.c.is_current)))
                embedded = set(connection.scalars(select(record_embeddings.c.record_id)))
            assert current <= embedded

    return process


def _remember(client: MemfxxkClient, content: str, **kwargs: object) -> UUID:
    return client.remember(RememberRequest(content=content, **kwargs)).memory_id


def _search(client: MemfxxkClient, query: str, **kwargs: object) -> list[UUID]:
    return [hit.memory_id for hit in client.search(SearchRequest(query=query, **kwargs)).hits]


def _section(bundle: ContextBundle, name: str) -> ContextSection:
    return next(section for section in bundle.sections if section.name == name)


def _ids(section: ContextSection) -> set[str]:
    return {item.ref_id for item in section.items}


def test_a01_session_state_and_recent_turns_survive_new_client(sdk: MemfxxkClient) -> None:
    sdk.ingest_turn("s1", "检查 upstream CI", "Retrieved logs")
    sdk.ingest_turn("s1", "重跑 pytest", "All current tests pass")
    state = {"plan": ["检查日志", "rerun tests"], "current_step": "rerun tests"}
    sdk.put_session_state("s1", PutSessionState(revision=0, state=state))

    with sdk.with_ctx() as reopened:
        assert reopened.get_session("s1").state == state
        context = reopened.build_context(BuildContextRequest(session_id="s1", auto_recall=False))
    working = _section(context, "working")
    assert "重跑 pytest" in working.rendered
    assert "All current tests pass" in working.rendered


def test_a02_cross_project_filters_every_retrieval_path(
    sdk: MemfxxkClient, data: dict[str, str], service: MemoryService
) -> None:
    with sdk.with_ctx(project_id="p2") as p2:
        theirs = _remember(p2, data["project_two"])
        shared = _remember(p2, data["user_shared"], scope_drop={"project_id"})
        ours = _remember(sdk, data["project_one"])
        drain(service)
        assert shared in _search(p2, "releasequality")
        assert theirs in _search(p2, "releasequality")
    assert set(_search(sdk, "releasequality")) == {ours, shared}
    assert {item.memory_id for item in sdk.list_memories().items} == {ours, shared}
    with pytest.raises(NotFound):
        sdk.get_memory(theirs)
    context = sdk.build_context(BuildContextRequest(query="releasequality", sections=["evidence"]))
    assert _ids(_section(context, "evidence")) == {str(ours), str(shared)}
    _, handler = sdk.tools()
    assert str(theirs) not in handler("memory_search", {"query": "releasequality"})
    assert data["project_two"] not in handler("memory_get", {"id": str(theirs)})


def test_a03_cross_user_filters_private_memory_but_allows_project_shared(
    sdk: MemfxxkClient, data: dict[str, str], service: MemoryService
) -> None:
    private = _remember(sdk, data["user_private"], scope_drop={"project_id"})
    shared = _remember(sdk, data["project_shared"], scope_drop={"user_id"})
    drain(service)
    with sdk.with_ctx(user_id="u2") as other:
        assert set(_search(other, "cafepreference")) == {shared}
        assert {item.memory_id for item in other.list_memories().items} == {shared}
        with pytest.raises(NotFound):
            other.get_memory(private)
    assert set(_search(sdk, "cafepreference")) == {private, shared}


def test_a04_explicit_memory_recalls_in_new_session(
    sdk: MemfxxkClient, data: dict[str, str], checkpoint: Callable[[], None]
) -> None:
    sdk.ingest_turn("s1", "准备存储部署", "Planning")
    memory = _remember(sdk, data["recall"])
    checkpoint()

    with sdk.with_ctx() as reopened:
        reopened.ingest_turn("s2", "继续 deployment", "Resuming")
        assert memory in _search(reopened, "redisresumable", types=["note"])
        assert reopened.get_memory(memory).content == data["recall"]


def test_a05_latest_version_history_and_concurrent_correction(
    sdk: MemfxxkClient, data: dict[str, str], checkpoint: Callable[[], None]
) -> None:
    memory = _remember(sdk, data["v1"])
    sdk.patch_memory(memory, PatchRequest(base_version=1, content=data["v2"]))
    sdk.patch_memory(memory, PatchRequest(base_version=2, content=data["v3"]))
    checkpoint()

    [current] = sdk.search(SearchRequest(query="deploymentbudget")).hits
    assert (current.memory_id, current.version) == (memory, 3)
    assert sdk.get_memory(memory, version=1).content == data["v1"]
    assert sdk.get_memory(memory).content == data["v3"]

    ready = Barrier(2, timeout=15)

    def update(label: str) -> int:
        with sdk.with_ctx() as client:
            ready.wait()
            try:
                client.patch_memory(memory, PatchRequest(base_version=3, content=label))
            except Conflict:
                return 409
            return 200

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(update, "并发修订 A")
        two = executor.submit(update, "Concurrent edit B")
        assert sorted([one.result(timeout=30), two.result(timeout=30)]) == [200, 409]
    assert sdk.get_memory(memory).version == 4


def test_a06_important_promotes_candidate_and_can_be_removed(
    sdk: MemfxxkClient, data: dict[str, str], service: MemoryService, checkpoint: Callable[[], None]
) -> None:
    query = data["rank_query"]
    for index in range(4):
        _remember(sdk, f"{query} {query} report-{index}")
    target = _remember(sdk, data["rank_target"])
    drain(service)
    baseline = _search(sdk, query)
    assert baseline.index(target) >= 3

    sdk.patch_memory(target, PatchRequest(important=True))
    checkpoint()
    assert target in _search(sdk, query, k=3)
    sdk.patch_memory(target, PatchRequest(important=False))
    assert _search(sdk, query) == baseline


def test_a07_pinned_mark_is_specific_to_project_and_removable(
    sdk: MemfxxkClient, data: dict[str, str], checkpoint: Callable[[], None]
) -> None:
    target = _remember(sdk, data["pin"], scope_drop={"project_id"})
    sdk.patch_memory(target, PatchRequest(pinned=True))
    checkpoint()
    request = BuildContextRequest(sections=["pinned"], auto_recall=False)
    assert str(target) in _ids(_section(sdk.build_context(request), "pinned"))
    with sdk.with_ctx(project_id="p2") as p2:
        assert p2.get_memory(target).content == data["pin"]
        assert str(target) not in _ids(_section(p2.build_context(request), "pinned"))
    sdk.patch_memory(target, PatchRequest(pinned=False))
    assert str(target) not in _ids(_section(sdk.build_context(request), "pinned"))


def test_a08_related_fix_is_scoped_and_unlinkable(
    sdk: MemfxxkClient, data: dict[str, str], checkpoint: Callable[[], None]
) -> None:
    failure = _remember(sdk, data["failure"], scope_drop={"project_id"})
    fix = _remember(sdk, data["fix"])
    link = sdk.link(LinkRequest(from_memory_id=failure, to_memory_id=fix, purpose="fixes"))
    checkpoint()
    request = SearchRequest(query="uploadregression", channels=["lexical"])
    [hit] = sdk.search(request).hits
    assert hit.memory_id == failure
    assert [(item.memory_id, item.purpose) for item in hit.related] == [(fix, "fixes")]
    with sdk.with_ctx(project_id="p2") as p2:
        [accessible] = p2.search(request).hits
        assert accessible.memory_id == failure
        assert accessible.related == []
    sdk.unlink(link.relation_id)
    assert sdk.search(request).hits[0].related == []


def test_corrected_fix_keeps_pin_and_link_in_later_session(
    sdk: MemfxxkClient, checkpoint: Callable[[], None]
) -> None:
    sdk.ingest_turn("initial", "Investigate a release failure", "Draft fix documented")
    failure = _remember(sdk, "releasetrace rollback under load", scope_drop={"project_id"})
    original = "Retry after 20 seconds"
    corrected = "Retry after 45 seconds"
    fix = _remember(sdk, original, pinned=True)
    sdk.link(LinkRequest(from_memory_id=failure, to_memory_id=fix, purpose="fixes"))
    sdk.patch_memory(fix, PatchRequest(base_version=1, content=corrected))
    checkpoint()

    with sdk.with_ctx() as reopened:
        reopened.ingest_turn("follow-up", "Check the current fix", "Verify before release")
        request = BuildContextRequest(
            session_id="follow-up",
            query="releasetrace",
            sections=["pinned", "evidence"],
            search_filters=SearchFilters(channels=["lexical"]),
        )
        bundle = reopened.build_context(request)
        pin = _section(bundle, "pinned").items[0]
        assert (pin.ref_id, pin.version) == (str(fix), 2)
        assert _section(bundle, "evidence").items[0].related == [fix]
        assert corrected in bundle.rendered and original not in bundle.rendered
        assert reopened.get_memory(fix, version=1).content == original

        with reopened.with_ctx(project_id="p2") as other:
            other_request = request.model_copy(update={"session_id": None})
            other_bundle = other.build_context(other_request)
            assert _ids(_section(other_bundle, "evidence")) == {str(failure)}
            assert _section(other_bundle, "evidence").items[0].related == []
            assert _section(other_bundle, "pinned").items == []

        reopened.forget(fix, "deactivate")
        after = reopened.build_context(request)
        assert _section(after, "pinned").items == []
        assert _section(after, "evidence").items[0].related == []
        assert corrected not in after.rendered


def test_a09_document_revision_conflict_and_context_reload(
    sdk: MemfxxkClient, data: dict[str, str]
) -> None:
    created = sdk.put_document(
        "project.md", PutDocumentRequest(content=data["document_v1"], purpose="发布约定")
    )
    with sdk.with_ctx() as other:
        base = other.get_document("project.md")
        assert base.revision == created.revision
        updated = sdk.put_document(
            "project.md",
            PutDocumentRequest(content=data["document_v2"], base_revision=created.revision),
        )
        with pytest.raises(Conflict):
            other.put_document(
                "project.md", PutDocumentRequest(content="old edit", base_revision=base.revision)
            )
        resolved = "发布前跑 integration tests 和 smoke test，并检查 changelog"
        head = other.put_document(
            "project.md", PutDocumentRequest(content=resolved, base_revision=updated.revision)
        )
    assert head.revision == updated.revision + 1
    bundle = sdk.build_context(BuildContextRequest(sections=["documents"], auto_recall=False))
    document = _section(bundle, "documents")
    assert resolved in document.rendered
    assert document.items[0].revision == head.revision
    assert sdk.get_document("project.md", revision=created.revision).content == data["document_v1"]


def test_a11_deactivation_and_hard_delete_remove_memory_but_keep_reference(
    sdk: MemfxxkClient, engine: Engine, service: MemoryService
) -> None:
    content = "obsoletedeploy 长期记忆，需要彻底删除"
    memory = _remember(sdk, content, pinned=True)
    request = BuildContextRequest(query="obsoletedeploy")
    drain(service)
    original = sdk.build_context(request)
    assert str(memory) in _ids(_section(original, "pinned"))
    with engine.connect() as connection:
        record_id = connection.execute(
            select(records.c.id).where(records.c.memory_id == memory)
        ).scalar_one()
        assert (
            connection.execute(
                select(record_embeddings.c.record_id).where(
                    record_embeddings.c.record_id == record_id
                )
            ).scalar_one()
            == record_id
        )

    sdk.forget(memory, "deactivate")
    assert memory not in _search(sdk, "obsoletedeploy")
    assert memory not in {item.memory_id for item in sdk.list_memories().items}
    assert content not in sdk.build_context(request).rendered
    assert memory in _search(sdk, "obsoletedeploy", include_inactive=True)
    assert sdk.list_memories(ListRequest(status="inactive")).items[0].memory_id == memory

    sdk.forget(memory, "delete")
    with pytest.raises(NotFound):
        sdk.get_memory(memory)
    assert memory not in _search(sdk, "obsoletedeploy", include_inactive=True)
    assert not sdk.list_memories(ListRequest(status="inactive")).items
    assert content not in sdk.build_context(request).rendered
    _, handler = sdk.tools()
    assert content not in handler("memory_get", {"id": str(memory)})
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(record_embeddings.c.record_id).where(
                    record_embeddings.c.record_id == record_id
                )
            )
            is None
        )
        audit = connection.execute(
            select(events.c.id, events.c.redacted, events.c.content, events.c.payload).where(
                events.c.payload["target"].as_string() == str(memory),
                events.c.payload["op"].as_string() == "remember",
            )
        ).one()
    assert audit.redacted and audit.content is None
    assert content not in json.dumps(audit.payload, ensure_ascii=False)
    assert sdk.get_event(audit.id).content is None
    metadata = sdk.get_bundle(original.bundle_id)
    assert str(memory) in {item.ref_id for section in metadata.sections for item in section.items}
    assert content not in metadata.model_dump_json()


def test_a12_pending_embeddings_cannot_resurrect_deleted_sources(
    sdk: MemfxxkClient, engine: Engine, service: MemoryService
) -> None:
    note = _remember(sdk, "abortednote 删除待索引 note")
    sdk.forget(note, "delete")
    revised = sdk.ingest_turn("race", "abortedsource 删除原文", "Pending response")
    event = revised[0].event_id
    [turn] = revised[0].memory_ids_touched
    sdk.forget_event(event)
    drain(service)

    for memory in (note, turn):
        with pytest.raises(NotFound):
            sdk.get_memory(memory)
    with pytest.raises(NotFound):
        sdk.get_event(event)
    with engine.connect() as connection:
        assert not connection.scalars(select(records.c.id)).all()
        assert not connection.scalars(select(record_embeddings.c.record_id)).all()


def test_a13_bundle_explains_visible_pins_and_retrieval_rank(
    sdk: MemfxxkClient, data: dict[str, str], service: MemoryService
) -> None:
    pinned = _remember(sdk, data["pin"], pinned=True)
    important = _remember(sdk, data["failure"], important=True)
    sdk.put_document("project.md", PutDocumentRequest(content=data["document_v1"]))
    sdk.ingest_turn("context-test", "开始排查", "Looking at current work")
    drain(service)
    bundle = sdk.build_context(
        BuildContextRequest(session_id="context-test", query="uploadregression 大文件上传 CI 失败")
    )
    metadata = sdk.get_bundle(bundle.bundle_id)
    references = {item.ref_id: item for section in metadata.sections for item in section.items}
    assert references.keys() == {
        item.ref_id for section in bundle.sections for item in section.items
    }
    assert all(item.reason for item in references.values())
    assert '"project_id": "p1"' in references[str(pinned)].reason
    evidence = references[str(important)]
    assert "rank=" in evidence.reason and "important=true" in evidence.reason
    assert evidence.score is not None
    assert all(not section.rendered for section in metadata.sections)
