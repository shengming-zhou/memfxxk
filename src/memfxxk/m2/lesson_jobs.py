from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import events, jobs, records, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.lessons import FinishedTask, FinishTask, LessonProposal, ReviewLesson
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound
from memfxxk.jobs.queue import Job, enqueue, owned_claim
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2 import blocks
from memfxxk.m2.storage import derived_record
from memfxxk.manage.common import lock_user_operations, write_audit
from memfxxk.prompts import lesson_distill, trajectory_judge, workflow_induce
from memfxxk.scope import visible_filter
from memfxxk.sessions import read_session

if TYPE_CHECKING:
    from memfxxk.service import BoundService, MemoryService


def _events(scope: Scope, session: str, start: int, end: int) -> Select:
    return (
        select(events)
        .where(
            visible_filter(events, scope),
            events.c.session_id == session,
            events.c.session_seq.between(start, end),
            ~events.c.redacted,
        )
        .order_by(events.c.session_seq)
    )


def finish(service: BoundService, session_id: str, request: FinishTask) -> FinishedTask:
    ctx = service.ctx
    strategy = service.settings.strategies.lessons
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        session = read_session(conn, ctx, session_id, lock=True)
        service.require_resource_write(session.scope.project_id)
        end = request.through_seq or session.last_seq
        if (
            request.from_seq > end
            or end > session.last_seq
            or not conn.execute(
                _events(session.scope, session_id, request.from_seq, end).limit(1)
            ).first()
        ):
            raise ServiceError(
                409, "invalid_task_boundary", "Provide a non-empty saved event range"
            )
        info = {
            "session_id": session_id,
            "from_seq": request.from_seq,
            "through_seq": end,
            "task": request.task,
        }
        scope = session.scope
        event_id = derived_id(
            "finish-task",
            scope.org_id,
            *(getattr(scope, key) or "" for key in ("user_id", "agent_id", "project_id")),
            session_id,
            str(request.from_seq),
            str(end),
            request.task,
        )
        conn.execute(
            insert(events)
            .values(
                id=event_id,
                **scope.model_dump(),
                kind="user_op",
                actor=ctx.actor.model_dump(),
                payload={"op": "finish_task", "args": info},
                event_time=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[events.c.id])
        )
        pending = []
        if strategy != "off":
            key = f"distill-task:{event_id}:{strategy}"
            job_id = conn.scalar(select(jobs.c.id).where(jobs.c.dedupe_key == key))
            if job_id is None:
                job_id = enqueue(
                    conn,
                    "distill_task",
                    {
                        "event_id": str(event_id),
                        "scope": scope.model_dump(),
                        "strategy": strategy,
                    },
                    dedupe_key=key,
                )
            if job_id is not None:
                pending.append(job_id)
        return FinishedTask(
            event_id=event_id,
            session_id=session_id,
            from_seq=request.from_seq,
            through_seq=end,
            jobs=pending,
        )


def _prompt(messages: str, payload: dict) -> list[dict]:
    return [
        {"role": "system", "content": messages},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def distill(service: MemoryService, job: Job) -> None:
    ctx = RequestContext(**job.payload["scope"], actor={"type": "system"})
    scope = Scope.model_validate(ctx.model_dump())
    marker_id = UUID(job.payload["event_id"])
    with service.engine.connect() as conn:
        marker = (
            conn.execute(
                select(events).where(
                    visible_filter(events, ctx),
                    events.c.id == marker_id,
                    ~events.c.redacted,
                )
            )
            .mappings()
            .one_or_none()
        )
        if marker is None or marker["payload"].get("op") != "finish_task":
            return
        task = marker["payload"]["args"]
        original_query = _events(scope, task["session_id"], task["from_seq"], task["through_seq"])
        rows = list(conn.execute(original_query).mappings())
        expected = {row["id"] for row in rows}
        if not rows:
            return
        previous = (
            list(
                conn.execute(
                    select(records)
                    .where(
                        visible_filter(records, ctx),
                        records.c.type == "lesson",
                        records.c.is_current,
                        records.c.status == "active",
                        records.c.confirmed,
                        records.c.metadata["strategy"].as_string() == "awm",
                        records.c.metadata["trajectory_outcome"].as_string() == "success",
                    )
                    .order_by(records.c.recorded_at.desc())
                    .limit(3)
                ).mappings()
            )
            if job.payload["strategy"] in ("awm", "both")
            else []
        )
    final_output = next(
        (
            row["content"]
            for row in reversed(rows)
            if row["kind"] == "message" and row["role"] == "assistant"
        ),
        None,
    )
    evidence = {
        "task": task["task"],
        "final_output": final_output,
        "trajectory": [
            {
                "id": str(row["id"]),
                "kind": row["kind"],
                "seq": row["session_seq"],
                "role": row["role"],
                "content": row["content"],
                "payload": row["payload"],
            }
            for row in rows
        ],
    }
    original_sources = sorted(expected | {marker_id})
    judge = (
        TaskRunner.for_service(service, "trajectory_judge")
        .complete(
            "trajectory_judge",
            ctx,
            _prompt(trajectory_judge.PROMPT, evidence),
            schema=trajectory_judge.SCHEMA,
            prompt_version=trajectory_judge.VERSION,
            source_event_ids=original_sources,
            job_id=job.id,
        )
        .parsed
    )
    if judge is None or judge.outcome == "unclear":
        return
    strategy = job.payload["strategy"]
    collected = []
    if strategy in ("reasoningbank", "both"):
        reply = (
            TaskRunner.for_service(service, "lesson_distill")
            .complete(
                "lesson_distill",
                ctx,
                _prompt(lesson_distill.PROMPT, {**evidence, "judgment": judge.model_dump()}),
                schema=lesson_distill.SCHEMA,
                prompt_version=lesson_distill.VERSION,
                source_event_ids=original_sources,
                job_id=job.id,
            )
            .parsed
        )
        if reply is not None:
            collected.append(
                ("reasoningbank", lesson_distill.VERSION, reply.lessons, original_sources)
            )
    if strategy in ("awm", "both") and judge.outcome == "success":
        prior_sources = {key for row in previous for key in row["source_event_ids"]}
        sources = sorted(set(original_sources) | prior_sources)
        reply = (
            TaskRunner.for_service(service, "workflow_induce")
            .complete(
                "workflow_induce",
                ctx,
                _prompt(
                    workflow_induce.PROMPT,
                    {
                        **evidence,
                        "previous_workflows": [row["content"] for row in previous],
                    },
                ),
                schema=workflow_induce.SCHEMA,
                prompt_version=workflow_induce.VERSION,
                source_event_ids=sources,
                job_id=job.id,
            )
            .parsed
        )
        if reply is not None:
            collected.append(("awm", workflow_induce.VERSION, reply.lessons, sources))
    _write(
        service,
        ctx,
        job,
        task,
        original_query,
        expected,
        marker_id,
        rows[-1],
        judge.outcome,
        collected,
    )


def _write(
    service: MemoryService,
    ctx: RequestContext,
    job: Job,
    task: dict,
    original_query: Select,
    expected: set[UUID],
    marker_id: UUID,
    tail: RowMapping,
    outcome: str,
    results: list[tuple[str, str, list[lesson_distill.LessonDraft], list[UUID]]],
) -> None:
    scope = Scope.model_validate(ctx.model_dump())
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        if {
            row["id"] for row in conn.execute(original_query.with_for_update()).mappings()
        } != expected or conn.scalar(
            select(jobs.c.id).where(owned_claim(job)).with_for_update()
        ) is None:
            return
        for strategy, prompt_version, proposed, dependencies in results:
            if not blocks.available_sources(conn, scope, set(dependencies)):
                continue
            for index, item in enumerate(proposed):
                if not set(item.source_event_ids) <= expected:
                    continue
                identity = derived_id("distilled-lesson", str(marker_id), strategy, str(index))
                if (
                    conn.scalar(
                        select(records.c.id)
                        .where(
                            visible_filter(records, ctx),
                            records.c.memory_id == identity,
                        )
                        .limit(1)
                    )
                    is not None
                ):
                    continue
                mode = service.settings.strategies.lesson_visibility
                derived_record(
                    conn,
                    ctx,
                    scope,
                    memory_id=identity,
                    version=1,
                    kind="lesson",
                    title=item.title,
                    content=f"# {item.title}\n\n{item.description}\n\n{item.content}",
                    sources=dependencies,
                    event_time=tail["event_time"],
                    session_id=task["session_id"],
                    source_pos={
                        "session_id": task["session_id"],
                        "seq_from": task["from_seq"],
                        "seq_to": task["through_seq"],
                    },
                    metadata={
                        "strategy": strategy,
                        "prompt_version": prompt_version,
                        "judge_prompt_version": trajectory_judge.VERSION,
                        "lesson_state": mode,
                        "trajectory_outcome": outcome,
                        "citations": list(map(str, item.source_event_ids)),
                    },
                )


def _proposals(ctx: Scope) -> Select:
    return select(records).where(
        visible_filter(records, ctx),
        records.c.is_current,
        records.c.type == "lesson",
        records.c.origin == "auto_extract",
        records.c.metadata["strategy"].as_string().in_(("reasoningbank", "awm")),
    )


def _view(row: RowMapping) -> LessonProposal:
    metadata = row["metadata"]
    return LessonProposal(
        memory_id=row["memory_id"],
        version=row["version"],
        title=row["title"],
        content=row["content"],
        strategy=metadata["strategy"],
        decision=metadata["trajectory_outcome"],
        status=metadata["lesson_state"],
        source_event_ids=row["source_event_ids"],
    )


def proposals(service: BoundService, limit: int) -> list[LessonProposal]:
    with service.engine.connect() as conn:
        return [
            _view(row)
            for row in conn.execute(
                _proposals(service.ctx)
                .where(
                    records.c.metadata["lesson_state"].as_string().in_(("pending", "searchable")),
                    records.c.status == "active",
                )
                .order_by(records.c.recorded_at.desc(), records.c.id)
                .limit(limit)
            ).mappings()
        ]


def review(service: BoundService, identity: UUID, request: ReviewLesson) -> LessonProposal:
    service.require_write()
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        target = _proposals(ctx).where(records.c.memory_id == identity)
        row = conn.execute(target.with_for_update()).mappings().one_or_none()
        if row is None:
            raise NotFound
        service.require_resource_write(row["project_id"])
        if ctx.actor.type != "user":
            raise ServiceError(403, "lesson_requires_user", "A user must review this lesson")
        status: Literal["accepted", "rejected"] = "accepted" if request.accept else "rejected"
        prior = row["metadata"]["lesson_state"]
        if prior == status:
            return _view(row)
        if prior not in ("pending", "searchable") or row["version"] != request.base_version:
            raise ServiceError(409, "lesson_revision_conflict", "The lesson has changed")
        scope = Scope.model_validate(dict(row))
        source_ids = row["source_event_ids"]
        if not blocks.available_sources(conn, scope, set(source_ids)):
            raise NotFound
        derived_record(
            conn,
            ctx,
            scope,
            memory_id=identity,
            version=row["version"] + 1,
            kind="lesson",
            title=row["title"],
            content=row["content"],
            sources=source_ids,
            event_time=row["event_time"],
            session_id=row["session_id"],
            source_pos=row["source_pos"],
            metadata={**row["metadata"], "lesson_state": status},
            confirmed=request.accept,
            status="active" if request.accept else "inactive",
        )
        write_audit(
            conn,
            ctx,
            "review_lesson",
            identity,
            {"accept": request.accept},
            {"status": status},
            scope=scope,
        )
        return _view(conn.execute(target).mappings().one())
