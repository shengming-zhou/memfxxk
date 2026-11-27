import json
import logging
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Connection, Select, select
from sqlalchemy.dialects.postgresql import insert

from memfxxk.db import events, fact_proposals, jobs, records, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.events import IngestEvent, IngestResult
from memfxxk.domain.facts import FactCandidate, FactProposal
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound, _turn_part
from memfxxk.jobs import enqueue
from memfxxk.jobs.queue import Job, owned_claim
from memfxxk.llm import ChatModel
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2.prompts import EXTRACT
from memfxxk.m2.storage import derived_record
from memfxxk.manage.common import ManagementError, lock_user_operations, write_audit
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService, MemoryService

logger = logging.getLogger(__name__)
_facts = TypeAdapter(list[FactCandidate])


def enqueue_closed_turns(
    conn: Connection, ctx: RequestContext, batch: list[IngestEvent], results: list[IngestResult]
) -> None:
    order = {
        memory_id: index
        for index, (event, result) in enumerate(zip(batch, results, strict=True))
        if event.kind == "message" and event.role == "assistant" and not result.duplicate
        for memory_id in result.memory_ids_touched
    }
    if not order:
        return
    closed = conn.execute(
        select(
            records.c.id,
            records.c.generation,
            records.c.memory_id,
            *(records.c[key] for key in Scope.model_fields),
        ).where(records.c.memory_id.in_(order), records.c.is_current, visible_filter(records, ctx))
    )
    for row in sorted(closed, key=lambda row: order[row.memory_id]):
        enqueue(
            conn,
            "extract_facts",
            {
                "record_id": str(row.id),
                "generation": row.generation,
                "scope": Scope.model_validate(dict(row._mapping)).model_dump(),
            },
            dedupe_key=f"facts:{row.id}:{row.generation}",
        )


def _source_query(scope: Scope, ids: list[UUID]) -> Select:
    return (
        select(events)
        .where(events.c.id.in_(ids), ~events.c.redacted, visible_filter(events, scope))
        .order_by(events.c.session_seq)
    )


def extract(service: "MemoryService", job: Job, model: ChatModel | None = None) -> None:
    started = perf_counter()
    statement = select(records).where(
        records.c.id == UUID(job.payload["record_id"]),
        records.c.generation == job.payload["generation"],
        records.c.type == "turn",
        records.c.is_current,
        records.c.status == "active",
    )
    with service.engine.connect() as conn:
        record = conn.execute(statement).mappings().one_or_none()
        if record is None:
            return
        scope = Scope.model_validate(dict(record))
        ctx = RequestContext(**scope.model_dump(), actor={"type": "system"})
        source_ids = record["source_event_ids"]
        source = list(conn.execute(_source_query(scope, source_ids)).mappings())
        summary_hint = conn.execute(
            select(records.c.content, records.c.source_event_ids)
            .where(
                records.c.session_id == record["session_id"],
                records.c.type == "summary",
                records.c.is_current,
                records.c.status == "active",
                records.c.source_pos["seq_to"].as_integer() < record["source_pos"]["seq_from"],
                visible_filter(records, scope),
            )
            .limit(1)
        ).one_or_none()
    if not source or len(source) != len(source_ids):
        return
    last = source[-1]
    if last["kind"] != "message" or last["role"] != "assistant":
        return
    # Tool results may contain long payloads and instructions. Extract facts from messages.
    data = {row["id"]: row for row in source if row["kind"] == "message"}
    transcript = "\n".join(f"[{event_id}] {_turn_part(row)}" for event_id, row in data.items())
    debug_sources = list(data)
    if summary_hint:
        transcript += "\n\nPrior summary for context only (do not cite):\n" + summary_hint.content
        debug_sources.extend(summary_hint.source_event_ids)
    messages = [{"role": "system", "content": EXTRACT}, {"role": "user", "content": transcript}]
    if model is None:
        task = TaskRunner.for_service(service, "extract").complete(
            "extract",
            scope,
            messages,
            schema=_facts,
            prompt_version="m2-extract-v1",
            job_id=job.id,
            source_event_ids=debug_sources,
            max_tokens=1500,
        )
        response, candidates = task.response, (task.parsed or [])[:10]
    else:
        response = model.complete(messages, max_tokens=1500)
        content = response.message.get("content")
        if not isinstance(content, str):
            raise ValueError("Fact extraction returned no text")
        try:
            candidates = _facts.validate_json(content)[:10]
        except ValueError:
            raise ValueError("Fact extraction returned invalid JSON") from None
    added = 0
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        if (
            conn.execute(statement.where(visible_filter(records, ctx)).with_for_update()).first()
            is None
        ):
            return
        if conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None:
            return
        for raw in candidates:
            if not set(raw.source_event_ids) <= data.keys():
                continue
            key = " ".join(raw.key.casefold().split())
            value = raw.content.strip()
            if not key or not value:
                continue
            ids = sorted(set(raw.source_event_ids))
            entities = sorted(
                {name.strip().casefold() for name in raw.entities if 0 < len(name.strip()) <= 80}
            )
            fact = raw.model_copy(
                update={
                    "key": key,
                    "content": value,
                    "source_event_ids": ids,
                    "entities": entities,
                }
            )
            identity = _memory_id(scope, key)
            # Repeated assertions of the same current value should not flood the review queue.
            latest = conn.execute(
                select(records.c.content, records.c.valid_from).where(
                    records.c.memory_id == identity,
                    records.c.is_current,
                    visible_filter(records, ctx),
                )
            ).first()
            if (
                latest
                and latest.content == value
                and (fact.valid_from is None or fact.valid_from == latest.valid_from)
            ):
                continue
            effective_value = fact.model_dump(mode="json").get("valid_from")
            effective_column = fact_proposals.c.fact["valid_from"].as_string()
            duplicate = conn.scalar(
                select(fact_proposals.c.id)
                .where(
                    fact_proposals.c.memory_id == identity,
                    visible_filter(fact_proposals, ctx),
                    fact_proposals.c.status.in_(("pending", "rejected")),
                    fact_proposals.c.fact["content"].as_string() == value,
                    effective_column.is_not_distinct_from(effective_value),
                )
                .limit(1)
            )
            if duplicate is not None:
                continue
            proposal_id = derived_id(
                "fact-proposal",
                str(identity),
                value,
                fact.valid_from.isoformat() if fact.valid_from else "",
                str(record["id"]),
                str(record["generation"]),
            )
            added += conn.execute(
                insert(fact_proposals)
                .values(
                    id=proposal_id,
                    **scope.model_dump(),
                    source_event_ids=ids,
                    fact=fact.model_dump(mode="json"),
                    memory_id=identity,
                )
                .on_conflict_do_nothing()
            ).rowcount
    logger.info(
        "facts extracted candidates=%d proposals=%d input_tokens=%d output_tokens=%d ms=%.1f",
        len(candidates),
        added,
        response.input_tokens,
        response.output_tokens,
        (perf_counter() - started) * 1000,
    )


def _memory_id(scope: Scope, key: str) -> UUID:
    return derived_id("fact", json.dumps(scope.model_dump(), sort_keys=True), key)


def list_pending(service: "BoundService", limit: int) -> list[FactProposal]:
    with service.engine.connect() as conn:
        rows = conn.execute(
            select(fact_proposals)
            .where(
                visible_filter(fact_proposals, service.ctx),
                fact_proposals.c.status == "pending",
            )
            .order_by(fact_proposals.c.created_at.desc(), fact_proposals.c.id)
            .limit(limit)
        ).mappings()
        return [FactProposal.model_validate(dict(row)) for row in rows]


def review(service: "BoundService", proposal_id: UUID, accept: bool) -> FactProposal:
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        target = (fact_proposals.c.id == proposal_id, visible_filter(fact_proposals, ctx))
        row = (
            conn.execute(select(fact_proposals).where(*target).with_for_update()).mappings().first()
        )
        if row is None:
            raise NotFound
        service.require_resource_write(row["project_id"])
        decision = "accepted" if accept else "rejected"
        if row["status"] == decision:
            return FactProposal.model_validate(dict(row))
        if row["status"] != "pending":
            raise ManagementError(
                409, "already_reviewed", "This proposal has already been reviewed"
            )
        memory_id, version = row["memory_id"], None
        if accept:
            scope = Scope.model_validate(dict(row))
            fact = FactCandidate.model_validate(row["fact"])
            source = list(conn.execute(_source_query(scope, fact.source_event_ids)).mappings())
            if len(source) != len(fact.source_event_ids):
                raise NotFound
            timestamp = max(item["event_time"] for item in source)
            if row["operation"] == "propose":
                memory_id, version = _accept_key(conn, ctx, scope, fact, timestamp)
            else:
                from .mem0 import accept_action

                version = accept_action(conn, ctx, row, fact, timestamp)
        saved = (
            conn.execute(
                fact_proposals.update()
                .where(*target)
                .values(
                    status=decision,
                    memory_id=memory_id,
                    memory_version=version,
                )
                .returning(fact_proposals)
            )
            .mappings()
            .one()
        )
        write_audit(
            conn,
            ctx,
            "review_fact",
            memory_id or proposal_id,
            {"accept": accept},
            {"proposal_id": str(proposal_id)},
        )
        return FactProposal.model_validate(dict(saved))


def _accept_key(
    conn: Connection, ctx: RequestContext, scope: Scope, fact: FactCandidate, timestamp: datetime
) -> tuple[UUID, int]:
    memory_id = _memory_id(scope, fact.key)
    versions = (
        conn.execute(
            select(records)
            .where(records.c.memory_id == memory_id, visible_filter(records, ctx))
            .order_by(records.c.version.desc())
            .limit(1)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if versions is not None and versions["status"] == "inactive":
        raise ManagementError(409, "inactive_fact", "Reactivate the existing fact first")
    if (
        versions is not None
        and versions["content"] == fact.content
        and (fact.valid_from is None or versions["valid_from"] == fact.valid_from)
    ):
        return memory_id, versions["version"]
    version = 1 if versions is None else versions["version"] + 1
    derived_record(
        conn,
        ctx,
        scope,
        memory_id=memory_id,
        version=version,
        kind="fact",
        content=fact.content,
        sources=fact.source_event_ids,
        event_time=timestamp,
        valid_from=fact.valid_from or timestamp,
        confirmed=True,
        metadata={"fact_key": fact.key, "entities": fact.entities},
    )
    return memory_id, version
