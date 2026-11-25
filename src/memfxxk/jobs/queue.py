from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import ColumnElement

from memfxxk.db import jobs
from memfxxk.ids import new_id

LOCK_TIMEOUT = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    locked_by: str
    locked_at: datetime


def enqueue(
    conn: Connection,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
) -> UUID | None:
    statement = insert(jobs).values(
        id=new_id(),
        kind=kind,
        payload=payload,
        dedupe_key=dedupe_key,
        run_after=run_after if run_after is not None else func.now(),
    )
    excluded = statement.excluded
    # A newer turn generation must replace an older, still queued embedding.
    both_embeddings = and_(jobs.c.kind == "embed_record", excluded.kind == "embed_record")
    old_generation = jobs.c.payload["generation"].as_integer()
    new_generation = excluded.payload["generation"].as_integer()
    newer_embedding = and_(both_embeddings, new_generation > old_generation)
    older_embedding = and_(both_embeddings, new_generation < old_generation)
    statement = statement.on_conflict_do_update(
        index_elements=[jobs.c.dedupe_key],
        set_={
            "kind": excluded.kind,
            "payload": excluded.payload,
            "status": "queued",
            "attempts": 0,
            "run_after": excluded.run_after,
            "locked_by": None,
            "locked_at": None,
            "last_error": None,
            "updated_at": func.now(),
        },
        where=and_(~older_embedding, or_(jobs.c.status != "queued", newer_embedding)),
    )
    return conn.execute(statement.returning(jobs.c.id)).scalar_one_or_none()


def claim(conn: Connection, worker_id: str, kinds: Collection[str] | None, limit: int) -> list[Job]:
    now = func.now()
    ready = or_(
        and_(jobs.c.status == "queued", jobs.c.run_after <= now),
        and_(jobs.c.status == "running", jobs.c.locked_at <= now - LOCK_TIMEOUT),
    )
    candidates = select(jobs.c.id).where(ready)
    if kinds is not None:
        candidates = candidates.where(jobs.c.kind.in_(kinds))
    candidates = (
        candidates.order_by(jobs.c.run_after, jobs.c.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .cte("claimable")
    )
    statement = (
        update(jobs)
        .where(jobs.c.id.in_(select(candidates.c.id)))
        .values(
            status="running",
            locked_by=worker_id,
            locked_at=func.clock_timestamp(),
            updated_at=now,
        )
        .returning(
            jobs.c.id,
            jobs.c.kind,
            jobs.c.payload,
            jobs.c.attempts,
            jobs.c.max_attempts,
            jobs.c.locked_by,
            jobs.c.locked_at,
        )
    )
    return [Job(**row) for row in conn.execute(statement).mappings()]


def owned_claim(job: Job) -> ColumnElement[bool]:
    # The lease timestamp changes on reclaim even when the same worker reclaims it.
    return and_(
        jobs.c.id == job.id,
        jobs.c.status == "running",
        jobs.c.locked_by == job.locked_by,
        jobs.c.locked_at == job.locked_at,
    )


def complete(conn: Connection, job: Job) -> None:
    conn.execute(
        update(jobs)
        .where(owned_claim(job))
        .values(
            status="done",
            locked_by=None,
            locked_at=None,
            last_error=None,
            updated_at=func.now(),
        )
    )


def fail(conn: Connection, job: Job, error: Exception) -> None:
    attempts = job.attempts + 1
    delay = timedelta(seconds=min(2 ** min(attempts, 10), 600))
    now = func.now()
    conn.execute(
        update(jobs)
        .where(owned_claim(job))
        .values(
            status="dead" if attempts >= job.max_attempts else "queued",
            attempts=attempts,
            run_after=now + delay,
            locked_by=None,
            locked_at=None,
            last_error=str(error)[:2000],
            updated_at=now,
        )
    )
