import json
import re
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select

from memfxxk.config import EvalModelSettings, M2Settings
from memfxxk.db import events, transaction
from memfxxk.domain import Principal
from memfxxk.domain.context import BuildContextRequest, PrepareCompaction, SearchFilters
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import LinkRequest, PatchRequest, RememberRequest
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.eval.llm import ChatResult
from memfxxk.ingest import NotFound
from memfxxk.jobs import Worker
from memfxxk.m2.facts import extract
from memfxxk.service import MemoryService

pytestmark = pytest.mark.integration


def scoped(service, make_ctx, user="owner", project="main"):
    ctx = make_ctx(user=user, project=project)
    return service.for_request(Principal(org_id=ctx.org_id, role="admin"), ctx)


def turn(bound, number, message, *, session="rolling"):
    return bound.ingest(
        [
            IngestEvent(
                kind="message",
                role="user",
                content=message,
                session_id=session,
                turn_id=str(number),
                idempotency_key=f"{session}:{number}:user",
            ),
            IngestEvent(
                kind="message",
                role="assistant",
                content="Recorded",
                session_id=session,
                turn_id=str(number),
                idempotency_key=f"{session}:{number}:assistant",
            ),
        ]
    )


def test_compaction_adoption_restart_scope_and_forget(service, make_ctx):
    owner = scoped(service, make_ctx)
    other = scoped(service, make_ctx, "other")
    first = turn(owner, 1, "Signal evergreen: the launch is on Tuesday.")
    turn(owner, 2, "The replacement part goes in drawer seven.")
    owner.patch_memory(first[0].memory_ids_touched[0], PatchRequest(priority=5))
    state = owner.get_session("rolling")
    request = PrepareCompaction(
        session_id="rolling",
        revision=state.revision,
        through_seq=2,
        target_tokens=160,
    )
    draft = owner.prepare_compaction(request)
    assert owner.prepare_compaction(request) == draft
    assert owner.get_session("rolling").summary_upto_seq is None
    with pytest.raises(NotFound):
        other.commit_compaction(draft.draft_id)
    first_commit = owner.commit_compaction(draft.draft_id)
    assert owner.commit_compaction(draft.draft_id) == first_commit
    assert owner.get_event(draft.source_event_ids[0]).content.startswith("Signal evergreen")
    restarted = scoped(MemoryService(service.engine, service.settings), make_ctx)
    state = restarted.get_session("rolling")
    second = restarted.prepare_compaction(
        request.model_copy(
            update={
                "revision": state.revision,
                "through_seq": 4,
            }
        )
    )
    assert "Signal evergreen" in second.summary
    committed = restarted.commit_compaction(second.draft_id)
    assert committed.memory_id == first_commit.memory_id
    assert committed.version == 2

    turn(owner, 3, "This latest message is already in the host window.")
    bundle = restarted.build_context(
        BuildContextRequest(
            session_id="rolling",
            query="evergreen",
            max_tokens=120,
            host_history_from_seq=5,
            search_filters=SearchFilters(channels=["lexical"]),
        )
    )
    assert bundle.total_tokens <= 120
    assert "already in the host" not in bundle.rendered
    assert "Signal evergreen" in bundle.rendered
    assert any(
        hit.memory_id == committed.memory_id
        for hit in owner.search(
            SearchRequest(
                query="evergreen",
                types=["summary"],
                channels=["lexical"],
            )
        ).hits
    )
    owner.forget_event(first[0].event_id)
    assert restarted.get_session("rolling").summary_upto_seq is None
    with pytest.raises(NotFound):
        restarted.get_memory(committed.memory_id)
    with pytest.raises(NotFound):
        restarted.commit_compaction(second.draft_id)


def test_compaction_rechecks_boundary_changed_after_prepare(service, make_ctx):
    owner = scoped(service, make_ctx)
    turn(owner, 1, "Initial answer")
    draft = owner.prepare_compaction(
        PrepareCompaction(
            session_id="rolling",
            revision=owner.get_session("rolling").revision,
            through_seq=2,
            target_tokens=100,
        )
    )
    owner.ingest(
        [
            IngestEvent(
                kind="message",
                role="assistant",
                content="Later addition",
                session_id="rolling",
                turn_id="1",
            )
        ]
    )
    with pytest.raises(ServiceError, match="split a turn"):
        owner.commit_compaction(draft.draft_id)


def test_graph_hops_and_path_stop_at_cross_scope_edge(service, make_ctx):
    owner = scoped(service, make_ctx)
    other = scoped(service, make_ctx, project="other")
    anchor = owner.remember(RememberRequest(content="tungsten anchor", scope_drop={"project_id"}))
    bridge = owner.remember(RememberRequest(content="middle route", scope_drop={"project_id"}))
    answer = owner.remember(RememberRequest(content="end result peach", scope_drop={"project_id"}))
    for a, b in ((anchor, bridge), (bridge, answer)):
        owner.link(
            LinkRequest(from_memory_id=a.memory_id, to_memory_id=b.memory_id, purpose="dependency")
        )
    request = SearchRequest(query="tungsten", k=4, channels=["lexical"], expand_relations=False)
    assert [hit.memory_id for hit in owner.search(request).hits] == [anchor.memory_id]
    assert answer.memory_id not in {
        hit.memory_id for hit in owner.search(request.model_copy(update={"graph_hops": 1})).hits
    }
    results = owner.search(request.model_copy(update={"graph_hops": 2}))
    assert results.hits[-1].graph_path == [anchor.memory_id, bridge.memory_id, answer.memory_id]
    assert [
        hit.memory_id for hit in other.search(request.model_copy(update={"graph_hops": 2})).hits
    ] == [anchor.memory_id]
    owner.forget(bridge.memory_id, "delete")
    assert [
        hit.memory_id for hit in owner.search(request.model_copy(update={"graph_hops": 2})).hits
    ] == [anchor.memory_id]


def test_preferences_and_document_chunks(service, make_ctx):
    owner = scoped(service, make_ctx)
    other = scoped(service, make_ctx, project="other")
    note = owner.remember(RememberRequest(content="decision sapphire", scope_drop={"project_id"}))
    owner.patch_memory(note.memory_id, PatchRequest(priority=5, tags=[" Novel ", "novel"]))
    request = SearchRequest(query="sapphire", tags=["NOVEL"], channels=["lexical"])
    assert owner.search(request).hits[0].priority == 5
    assert owner.get_memory(note.memory_id).tags == ["novel"]
    assert other.search(request).hits == []
    assert other.get_memory(note.memory_id).priority is None
    owner.patch_memory(note.memory_id, PatchRequest(priority=1))
    assert owner.search(request).hits[0].tags == ["novel"]
    document = owner.put_document(
        "world.md",
        PutDocumentRequest(
            content="# World\n## Customs\nCinnabar is banned.\n## Food\nDurian is allowed.",
            purpose="fiction world reference",
            load_policy="on_demand",
        ),
    )
    query = SearchRequest(query="cinnabar", types=["doc_chunk"], channels=["lexical"])
    hit = owner.search(query).hits[0]
    assert hit.source_pos["heading"] == "World / Customs"
    assert hit.source_pos["revision"] == document.revision
    assert "Durian" not in hit.snippet
    assert other.search(query).hits == []
    owner.put_document(
        "world.md",
        PutDocumentRequest(
            content="# Customs\nHalite is banned.",
            base_revision=document.revision,
        ),
    )
    assert owner.search(query).hits == []
    owner.delete_document("world.md")
    with pytest.raises(NotFound):
        owner.get_memory(hit.memory_id, version=hit.version)


class FactModel:
    model = "fake"

    def complete(self, messages, **kwargs):
        first = messages[1]["content"].splitlines()[0]
        event_id = re.search(r"\[([a-f0-9-]{36})\]", first)[1]
        day, value = re.search(r"day (\d) holds (\w+)", first).groups()
        key, content = (
            ("vault registry", f"vault registry {value}")
            if "registry" in first
            else ("Sapphire Rule", f"sapphire rule {value}")
        )
        return ChatResult(
            {
                "content": json.dumps(
                    [
                        {
                            "key": key,
                            "content": content,
                            "source_event_ids": [event_id],
                            "valid_from": f"2025-01-0{day}T00:00:00Z",
                            "entities": ["Sapphire"],
                        },
                        {
                            "key": "invented",
                            "content": "bad",
                            "source_event_ids": [str(UUID(int=13))],
                        },
                    ]
                )
            }
        )


def test_fact_pipeline_recorded_and_effective_time_and_no_resurrection(service, make_ctx):
    settings = service.settings.model_copy(
        update={
            "m2": M2Settings(model=EvalModelSettings(model="fake"), extract_facts=True),
        }
    )
    service = MemoryService(service.engine, settings)
    owner = scoped(service, make_ctx)
    other = scoped(service, make_ctx, project="other")
    worker = Worker(service)
    worker.register(
        "extract_facts", lambda jobs: [extract(service, job, FactModel()) for job in jobs]
    )

    original = turn(owner, 1, "day 1 holds copper")
    assert worker.run_once({"extract_facts"}) == 1
    (proposal,) = owner.list_fact_proposals()
    assert other.list_fact_proposals() == []
    with pytest.raises(NotFound):
        other.review_fact(proposal.id, True)
    first = owner.review_fact(proposal.id, True)
    assert first == owner.review_fact(proposal.id, True)
    old = owner.get_memory(first.memory_id)
    assert old.confirmed

    turn(owner, 2, "day 3 holds bronze")
    assert worker.run_once({"extract_facts"}) == 1
    (proposal,) = owner.list_fact_proposals()
    # The old fact is still the current answer until the user accepts the edit.
    query = SearchRequest(query="sapphire", types=["fact"], channels=["lexical"])
    assert "copper" in owner.search(query).hits[0].snippet
    owner.review_fact(proposal.id, True)
    assert "bronze" in owner.search(query).hits[0].snippet
    assert (
        "copper"
        in owner.search(
            query.model_copy(
                update={
                    "as_of_valid": datetime(2025, 1, 2, tzinfo=UTC),
                }
            )
        )
        .hits[0]
        .snippet
    )
    assert (
        "copper"
        in owner.search(
            query.model_copy(
                update={
                    "as_of_recorded": old.recorded_at,
                }
            )
        )
        .hits[0]
        .snippet
    )
    assert other.search(query).hits == []
    turn(owner, 2, "day 3 holds bronze")
    assert worker.run_once({"extract_facts"}) == 0

    turn(owner, 3, "day 4 holds zinc")
    owner.forget_event(original[0].event_id)
    assert owner.search(query).hits == []
    with transaction(service.engine) as conn:
        assert conn.scalar(select(events.c.id).where(events.c.id == original[0].event_id)) is None
    late = turn(owner, 4, "day 5 holds silver")
    owner.forget_event(late[0].event_id)
    assert worker.run_once({"extract_facts"}) == 2
    assert all("silver" not in item.fact.content for item in owner.list_fact_proposals())


def test_graph_can_follow_entities_from_reviewed_facts(service, make_ctx):
    settings = service.settings.model_copy(
        update={
            "m2": M2Settings(model=EvalModelSettings(model="fake"), extract_facts=True),
        }
    )
    service = MemoryService(service.engine, settings)
    owner = scoped(service, make_ctx)
    worker = Worker(service)
    worker.register(
        "extract_facts", lambda jobs: [extract(service, job, FactModel()) for job in jobs]
    )
    turn(owner, 1, "day 1 holds copper")
    turn(owner, 2, "day 1 holds parchment registry")
    assert worker.run_once({"extract_facts"}) == 2
    query = SearchRequest(query="sapphire", channels=["lexical"], k=2, types=["fact"], graph_hops=1)
    assert owner.search(query).hits == []
    saved = {
        item.fact.key: owner.review_fact(item.id, True).memory_id
        for item in owner.list_fact_proposals()
    }
    hit = owner.search(query).hits[-1]
    assert hit.graph_path == [saved["sapphire rule"], saved["vault registry"]]
    assert scoped(service, make_ctx, project="other").search(query).hits == []
