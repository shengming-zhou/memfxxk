import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import blake2b
from uuid import UUID

from sqlalchemy import Connection, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import events, record_embeddings, records, sessions
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.events import Event, IngestEvent, IngestResult
from memfxxk.ids import derived_id, new_id
from memfxxk.jobs import enqueue
from memfxxk.scope import resolve_write_scope, visible_filter
from memfxxk.text import MixedTokenizer, build_tsvector_literal


class NotFound(LookupError):
    """The requested event or session is unavailable in this context."""


class TurnScopeConflict(ValueError):
    """A turn can be indexed only under one scope."""


def _lock_batch(connection: Connection, ctx: RequestContext, batch: list[IngestEvent]) -> None:
    # Use a global ordering across session and idempotency keys in multi-session batches.
    keys = {
        (kind, value)
        for event in batch
        for kind, value in (
            ("session", event.session_id),
            ("idempotency", event.idempotency_key),
        )
        if value is not None
    }
    locks = sorted(
        {
            int.from_bytes(
                blake2b(
                    json.dumps(["memfxxk-ingest", ctx.org_id, kind, value]).encode(),
                    digest_size=8,
                ).digest(),
                signed=True,
            )
            for kind, value in keys
        }
    )
    for lock in locks:
        connection.execute(select(func.pg_advisory_xact_lock(lock)))


def _session(
    connection: Connection, ctx: RequestContext, session_id: str, *, lock: bool = False
) -> RowMapping:
    query = select(sessions).where(sessions.c.id == session_id, visible_filter(sessions, ctx))
    row = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if row is None:
        raise NotFound
    return row


def _next_sequence(
    connection: Connection, ctx: RequestContext, session_id: str, scope: Scope
) -> int:
    connection.execute(
        insert(sessions)
        .values(id=session_id, **scope.model_dump())
        .on_conflict_do_nothing(index_elements=[sessions.c.org_id, sessions.c.id])
    )
    row = _session(connection, ctx, session_id, lock=True)
    if any(value is None and row[key] is not None for key, value in scope.model_dump().items()):
        raise NotFound
    sequence = row["next_seq"]
    connection.execute(
        sessions.update()
        .where(sessions.c.id == session_id, visible_filter(sessions, ctx))
        .values(next_seq=sequence + 1, updated_at=func.now())
    )
    return sequence


def _duplicate(connection: Connection, ctx: RequestContext, key: str) -> IngestResult | None:
    row = connection.execute(
        select(events.c.id, events.c.session_seq).where(
            events.c.idempotency_key == key, visible_filter(events, ctx)
        )
    ).one_or_none()
    if row is None:
        return None
    return IngestResult(event_id=row.id, session_seq=row.session_seq, duplicate=True)


def _turn_part(event: Mapping[str, object]) -> str:
    payload = event["payload"]
    assert isinstance(payload, dict)
    kind = event["kind"]
    label = event["role"] or kind
    value = event["content"]
    if kind in ("tool_call", "tool_result"):
        label = f"{kind} {payload.get('name', 'tool')}"
        if value is None:
            key = "args" if kind == "tool_call" else "output"
            value = payload.get(key, payload)
    body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(body) > 2000:
        body = f"{body[:1500]} [省略 {len(body) - 2000} 字符] {body[-500:]}"
    return f"[{label}] {body}"


def _rebuild_turn(
    connection: Connection,
    ctx: RequestContext,
    scope: Scope,
    session_id: str,
    turn_id: str,
    implicit_event: UUID | None,
    tokenizer: MixedTokenizer,
) -> UUID:
    memory_id = derived_id("turn", ctx.org_id, session_id, turn_id)
    current = (
        connection.execute(
            select(records)
            .where(
                records.c.memory_id == memory_id, records.c.is_current, visible_filter(records, ctx)
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if current is not None and Scope.model_validate(dict(current)) != scope:
        raise TurnScopeConflict("Events in the same turn must have the same scope")
    turn_filter = events.c.turn_id == turn_id
    if implicit_event is None:
        try:
            parsed = UUID(turn_id)
            implicit_event = parsed if str(parsed) == turn_id else None
        except ValueError:
            pass
    if implicit_event is not None:
        turn_filter = or_(
            turn_filter, and_(events.c.turn_id.is_(None), events.c.id == implicit_event)
        )
    source = (
        connection.execute(
            select(events)
            .where(
                events.c.session_id == session_id,
                turn_filter,
                events.c.kind.in_(("message", "tool_call", "tool_result")),
                ~events.c.redacted,
                visible_filter(events, ctx),
            )
            .order_by(events.c.session_seq)
        )
        .mappings()
        .all()
    )
    if any(Scope.model_validate(dict(row)) != scope for row in source):
        raise TurnScopeConflict("Events in the same turn must have the same scope")
    content = "\n".join(_turn_part(row) for row in source)
    values = {
        "content": content,
        "event_time": min(row["event_time"] for row in source),
        "source_event_ids": [row["id"] for row in source],
        "source_pos": {
            "session_id": session_id,
            "seq_from": source[0]["session_seq"],
            "seq_to": source[-1]["session_seq"],
        },
        "lexemes": build_tsvector_literal(tokenizer.tokenize(content)),
    }
    if current is None:
        record_id = new_id()
        generation = 0
        inserted = connection.scalar(
            insert(records)
            .values(
                id=record_id,
                memory_id=memory_id,
                version=1,
                type="turn",
                origin="auto_index",
                session_id=session_id,
                created_by=ctx.actor.model_dump(),
                **scope.model_dump(),
                **values,
            )
            .on_conflict_do_nothing()
            .returning(records.c.id)
        )
        if inserted is None:
            raise NotFound
    else:
        record_id = current["id"]
        generation = current["generation"] + 1
        connection.execute(
            records.update()
            .where(records.c.id == record_id, visible_filter(records, ctx))
            .values(**values, generation=generation)
        )
        connection.execute(
            record_embeddings.delete().where(record_embeddings.c.record_id == record_id)
        )
    enqueue(
        connection,
        "embed_record",
        {"record_id": str(record_id), "generation": generation},
        dedupe_key=f"embed:{record_id}",
    )
    return memory_id


def ingest(
    connection: Connection, ctx: RequestContext, batch: list[IngestEvent]
) -> list[IngestResult]:
    _lock_batch(connection, ctx, batch)
    results: list[IngestResult] = []
    turns: dict[tuple[str, str], list[tuple[Scope, UUID | None, IngestResult]]] = {}
    for item in batch:
        scope = resolve_write_scope(ctx, item.scope_drop)
        if item.idempotency_key is not None:
            duplicate = _duplicate(connection, ctx, item.idempotency_key)
            if duplicate is not None:
                results.append(duplicate)
                continue
        event_id = new_id()
        sequence = (
            _next_sequence(connection, ctx, item.session_id, scope)
            if item.session_id is not None
            else None
        )
        inserted = connection.scalar(
            insert(events)
            .values(
                **item.model_dump(exclude={"scope_drop", "event_time"}),
                **scope.model_dump(),
                id=event_id,
                session_seq=sequence,
                event_time=item.event_time or datetime.now(UTC),
                actor=ctx.actor.model_dump(),
            )
            .on_conflict_do_nothing(index_elements=[events.c.org_id, events.c.idempotency_key])
            .returning(events.c.id)
        )
        if inserted is None:
            raise NotFound
        result = IngestResult(event_id=event_id, session_seq=sequence)
        results.append(result)
        if item.session_id is not None and item.kind in ("message", "tool_call", "tool_result"):
            implicit_id = event_id if item.turn_id is None else None
            turns.setdefault((item.session_id, item.turn_id or str(event_id)), []).append(
                (scope, implicit_id, result)
            )
    tokenizer = MixedTokenizer()
    for (session_id, turn_id), grouped in turns.items():
        scope = grouped[0][0]
        if any(row_scope != scope for row_scope, _, _ in grouped):
            raise TurnScopeConflict("Events in the same turn must have the same scope")
        implicit_id = next((implicit for _, implicit, _ in grouped if implicit is not None), None)
        memory_id = _rebuild_turn(
            connection, ctx, scope, session_id, turn_id, implicit_id, tokenizer
        )
        for _, _, result in grouped:
            result.memory_ids_touched.append(memory_id)
    return results


def _event(row: RowMapping) -> Event:
    values = dict(row)
    if row["redacted"]:
        values.update(content=None, payload=None)
    return Event.model_validate(values)


def get_event(connection: Connection, ctx: RequestContext, event_id: UUID) -> Event:
    row = (
        connection.execute(
            select(events).where(events.c.id == event_id, visible_filter(events, ctx))
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound
    return _event(row)


def list_session_events(
    connection: Connection,
    ctx: RequestContext,
    session_id: str,
    from_seq: int | None,
    to_seq: int | None,
    limit: int,
) -> list[Event]:
    _session(connection, ctx, session_id)
    query = select(events).where(events.c.session_id == session_id, visible_filter(events, ctx))
    if from_seq is not None:
        query = query.where(events.c.session_seq >= from_seq)
    if to_seq is not None:
        query = query.where(events.c.session_seq <= to_seq)
    rows = connection.execute(query.order_by(events.c.session_seq).limit(limit)).mappings()
    return [_event(row) for row in rows]
