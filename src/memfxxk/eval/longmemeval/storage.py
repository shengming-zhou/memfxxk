from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, delete, func, select
from sqlalchemy.engine import Connection

from memfxxk.db import (
    context_bundles,
    events,
    jobs,
    record_embeddings,
    records,
    sessions,
    transaction,
)
from memfxxk.domain import Actor, Principal, RequestContext
from memfxxk.domain.events import IngestEvent
from memfxxk.idempotency import LEGACY_NAMESPACE
from memfxxk.ingest import ingest
from memfxxk.scope import ScopeError, visible_filter
from memfxxk.service import BoundService

from .dataset import Dataset, Question, iter_turns

EVAL_ORG = "eval"
EVAL_USER = "eval-user"
EVAL_PRINCIPAL = Principal(org_id=EVAL_ORG, role="admin")
_SOURCE_URI = "longmemeval:completed"
_RESET_LOCK = int.from_bytes(b"mfxxkevl", signed=True)


class DatasetConflict(ValueError):
    """A different revision or split occupies this question's evaluation scope."""


def eval_context(question_id: str | None = None) -> RequestContext:
    return RequestContext(
        org_id=EVAL_ORG,
        user_id=EVAL_USER if question_id is not None else None,
        project_id=question_id,
        actor=Actor(type="import", id="longmemeval"),
    )


def _require_scope(bound: BoundService, question_id: str | None = None) -> None:
    if bound.principal != EVAL_PRINCIPAL or bound.ctx != eval_context(question_id):
        raise ScopeError("LongMemEval maintenance requires the reserved evaluation context")


def _marker_query() -> Select:
    return select(events).where(
        visible_filter(events, eval_context()),
        events.c.idempotency_namespace == LEGACY_NAMESPACE,
        events.c.kind == "user_op",
        events.c.source["uri"].as_string() == _SOURCE_URI,
    )


def has_question(bound: BoundService, dataset: Dataset, sha256: str) -> bool:
    question_id = bound.ctx.project_id
    if question_id is None:
        raise ScopeError("Select a LongMemEval question to check its ingest status")
    _require_scope(bound, question_id)
    expected = {"question_id": question_id, "dataset": dataset, "sha256": sha256}
    with bound.engine.connect() as connection:
        payload = connection.scalar(
            _marker_query()
            .with_only_columns(events.c.payload)
            .where(events.c.idempotency_key == f"longmemeval:completed:{question_id}")
        )
    return payload == expected


def evidence_coverage(bound: BoundService, cutoff: datetime) -> tuple[int, int]:
    question_id = bound.ctx.project_id
    if question_id is None:
        raise ScopeError("Select a LongMemEval question to check evidence coverage")
    _require_scope(bound, question_id)
    embedding = bound.settings.embedding
    vectors = records.outerjoin(
        record_embeddings,
        (records.c.id == record_embeddings.c.record_id)
        & (record_embeddings.c.model == embedding.model)
        & (record_embeddings.c.dim == embedding.dim),
    )
    with bound.engine.connect() as connection:
        records_count, vectors_count = connection.execute(
            select(func.count(), func.count(record_embeddings.c.record_id))
            .select_from(vectors)
            .where(
                visible_filter(records, bound.ctx),
                records.c.project_id == question_id,
                records.c.event_time <= cutoff,
                records.c.type == "turn",
                records.c.status == "active",
                records.c.is_current,
            )
        ).one()
    return records_count, vectors_count


def ingest_question(bound: BoundService, question: Question, dataset: Dataset, sha256: str) -> bool:
    _require_scope(bound, question.question_id)
    completion_key = f"longmemeval:completed:{question.question_id}"
    completion_payload = {"question_id": question.question_id, "dataset": dataset, "sha256": sha256}
    with transaction(bound.engine) as connection:
        connection.execute(select(func.pg_advisory_xact_lock(_RESET_LOCK)))
        existing = (
            connection.execute(_marker_query().where(events.c.idempotency_key == completion_key))
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["payload"] != completion_payload:
                raise DatasetConflict(
                    f"{question.question_id} was ingested from a different dataset. "
                    "Run ingest with --reset to replace existing evaluation data."
                )
            return False
        batch = [
            IngestEvent(
                kind="message",
                role=message.role,
                content=message.content,
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                event_time=turn.event_time,
                source={
                    "uri": f"longmemeval:{dataset}",
                    "external_id": f"{turn.turn_id}:{message_index}",
                    "version": sha256,
                },
                idempotency_key=f"longmemeval:event:{turn.turn_id}:{message_index}",
            )
            for turn in iter_turns(question)
            for message_index, message in enumerate(turn.messages)
        ]
        batch.append(
            IngestEvent(
                kind="user_op",
                event_time=question.asked_at,
                source={"uri": _SOURCE_URI},
                payload=completion_payload,
                idempotency_key=completion_key,
                scope_drop={"user_id", "project_id"},
            )
        )
        ingest(connection, bound.ctx, batch)
        return True


def _delete_question(connection: Connection, question_id: str) -> None:
    ctx = eval_context(question_id)
    record_ids = connection.scalars(
        select(records.c.id)
        .where(visible_filter(records, ctx), records.c.project_id == question_id)
        .with_for_update()
    ).all()
    if record_ids:
        from memfxxk.qdrant import queue_removal

        queue_removal(connection, record_ids)
        connection.execute(
            delete(jobs).where(
                jobs.c.kind == "embed_record",
                jobs.c.payload["record_id"]
                .as_string()
                .in_([str(record_id) for record_id in record_ids]),
            )
        )
    for table in (context_bundles, records, events, sessions):
        # Org-level completion markers are removed after all question data.
        connection.execute(
            delete(table).where(visible_filter(table, ctx), table.c.project_id == question_id)
        )


def reset_eval(bound: BoundService) -> int:
    _require_scope(bound)
    with transaction(bound.engine) as connection:
        connection.execute(select(func.pg_advisory_xact_lock(_RESET_LOCK)))
        markers = connection.execute(_marker_query()).mappings().all()
        for marker in markers:
            _delete_question(connection, marker["payload"]["question_id"])
        connection.execute(
            delete(events).where(
                visible_filter(events, bound.ctx),
                events.c.id.in_([marker["id"] for marker in markers]),
            )
        )
    return len(markers)


def count_records(bound: BoundService) -> int:
    _require_scope(bound)
    with bound.engine.connect() as connection:
        markers = connection.execute(_marker_query()).mappings().all()
        return sum(
            connection.scalar(
                select(func.count())
                .select_from(records)
                .where(
                    visible_filter(records, eval_context(marker["payload"]["question_id"])),
                    records.c.project_id == marker["payload"]["question_id"],
                )
            )
            or 0
            for marker in markers
        )
