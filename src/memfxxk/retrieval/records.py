import re
from collections import defaultdict
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Connection,
    RowMapping,
    Select,
    and_,
    cast,
    func,
    literal,
    or_,
    select,
    union_all,
)

from memfxxk.db import events, memory_marks, memory_relations, records
from memfxxk.domain import Scope
from memfxxk.domain.retrieval import (
    MemoryMark,
    MemoryNeighbor,
    MemoryNeighbors,
    MemoryReference,
    MemoryResponse,
    MemoryVersion,
    RelatedMemory,
)
from memfxxk.ingest import NotFound
from memfxxk.scope import visible_filter

_SNIPPET_LENGTH = 500


def snippet(content: str, matches: re.Pattern[str] | None = None) -> str:
    if len(content) <= _SNIPPET_LENGTH:
        return content
    match = matches.search(content) if matches is not None else None
    start = 0 if match is None else max(0, match.start() - _SNIPPET_LENGTH // 3)
    start = min(start, len(content) - _SNIPPET_LENGTH + 1)
    prefix = "…" if start else ""
    end = start + _SNIPPET_LENGTH - len(prefix)
    if end >= len(content):
        return prefix + content[start:]
    return prefix + content[start : end - 1] + "…"


def reference(row: RowMapping) -> MemoryReference:
    return MemoryReference(
        memory_id=row["memory_id"],
        version=row["version"],
        type=row["type"],
        title=row["title"],
        event_time=row["event_time"],
        recorded_at=row["recorded_at"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        scope=Scope.model_validate(row),
        session_id=row["session_id"],
        source_pos=row["source_pos"],
    )


def marks_for(
    connection: Connection, ctx: Scope, memory_ids: list[UUID]
) -> dict[UUID, list[MemoryMark]]:
    if not memory_ids:
        return {}
    statement = (
        select(memory_marks.c.memory_id, memory_marks.c.mark)
        .where(memory_marks.c.memory_id.in_(memory_ids), visible_filter(memory_marks, ctx))
        .distinct()
        .order_by(memory_marks.c.mark)
    )
    marks: dict[UUID, list[MemoryMark]] = defaultdict(list)
    for memory_id, mark in connection.execute(statement):
        marks[memory_id].append(mark)
    return marks


def related_for(
    connection: Connection,
    ctx: Scope,
    memory_ids: list[UUID],
    limit: int | None = None,
    matches: re.Pattern[str] | None = None,
) -> dict[UUID, list[RelatedMemory]]:
    if not memory_ids or limit == 0:
        return {}
    relation = memory_relations.c
    target = records.alias("related_target")

    def direction(outgoing: bool) -> Select:
        source, endpoint = (
            (relation.from_memory_id, relation.to_memory_id)
            if outgoing
            else (relation.to_memory_id, relation.from_memory_id)
        )
        visible_source = (
            select(records.c.id)
            .where(
                records.c.memory_id == source, records.c.is_current, visible_filter(records, ctx)
            )
            .correlate(memory_relations)
            .exists()
        )
        return (
            select(
                source.label("source_id"),
                target.c.memory_id,
                relation.purpose,
                literal("outgoing" if outgoing else "incoming").label("direction"),
                target.c.type,
                target.c.content,
                target.c.event_time,
                relation.created_at,
                relation.id.label("relation_id"),
            )
            .select_from(
                memory_relations.join(target, target.c.memory_id == endpoint).outerjoin(
                    events, events.c.id == relation.source_event_id
                )
            )
            .where(
                relation.org_id == ctx.org_id,
                source.in_(memory_ids),
                visible_source,
                target.c.is_current,
                target.c.status == "active",
                visible_filter(target, ctx),
                or_(
                    events.c.id.is_(None),
                    and_(visible_filter(events, ctx), ~events.c.redacted),
                ),
            )
        )

    links = union_all(direction(outgoing=True), direction(outgoing=False)).subquery()
    ordering = (links.c.created_at.desc(), links.c.relation_id)
    if limit is None:
        statement = select(links).order_by(*ordering)
    else:
        indexed = select(
            links,
            func.row_number()
            .over(partition_by=links.c.source_id, order_by=ordering)
            .label("position"),
        ).subquery()
        statement = select(indexed).where(indexed.c.position <= limit).order_by(indexed.c.position)

    related: dict[UUID, list[RelatedMemory]] = defaultdict(list)
    for row in connection.execute(statement).mappings():
        related[row["source_id"]].append(
            RelatedMemory(
                memory_id=row["memory_id"],
                purpose=row["purpose"],
                direction=row["direction"],
                type=row["type"],
                snippet=snippet(row["content"], matches),
                event_time=row["event_time"],
            )
        )
    return related


def _neighbors(connection: Connection, ctx: Scope, row: RowMapping, window: int) -> MemoryNeighbors:
    source_pos = row["source_pos"]
    if not window or row["type"] != "turn" or not source_pos:
        return MemoryNeighbors()
    session_id = source_pos.get("session_id")
    seq_from = source_pos.get("seq_from")
    if session_id is None or session_id != row["session_id"] or not isinstance(seq_from, int):
        return MemoryNeighbors()

    seq = cast(records.c.source_pos["seq_from"].astext, BigInteger)
    statement = (
        select(records)
        .where(
            visible_filter(records, ctx),
            records.c.is_current,
            records.c.status == "active",
            records.c.type == "turn",
            records.c.session_id == session_id,
            records.c.source_pos["session_id"].astext == session_id,
            records.c.memory_id != row["memory_id"],
        )
        .limit(window)
    )
    before = statement.where(seq < seq_from).order_by(seq.desc(), records.c.id.desc())
    after = statement.where(seq > seq_from).order_by(seq, records.c.id)

    def load(statement: Select) -> list[MemoryNeighbor]:
        return [
            MemoryNeighbor(**reference(entry).model_dump(), content=entry["content"])
            for entry in connection.execute(statement).mappings()
        ]

    return MemoryNeighbors(before=load(before)[::-1], after=load(after))


def read_memory(
    connection: Connection, ctx: Scope, memory_id: UUID, version: int | None, window: int
) -> MemoryResponse:
    visible = (visible_filter(records, ctx), records.c.memory_id == memory_id)
    version_filter = records.c.is_current if version is None else records.c.version == version
    statement = select(records).where(*visible, version_filter)
    row = connection.execute(statement).mappings().one_or_none()
    if row is None:
        raise NotFound
    versions = (
        select(records.c.version, records.c.recorded_at, records.c.created_by)
        .where(*visible)
        .order_by(records.c.version)
    )
    return MemoryResponse(
        **reference(row).model_dump(),
        origin=row["origin"],
        status=row["status"],
        confirmed=row["confirmed"],
        note_attributes=row["metadata"].get("amem"),
        confidence=row["metadata"].get("confidence") if row["type"] == "opinion" else None,
        content=row["content"],
        source_event_ids=row["source_event_ids"],
        marks=marks_for(connection, ctx, [memory_id]).get(memory_id, []),
        relations=related_for(connection, ctx, [memory_id]).get(memory_id, []),
        versions=[
            MemoryVersion.model_validate(entry) for entry in connection.execute(versions).mappings()
        ],
        neighbors=_neighbors(connection, ctx, row, window),
    )
