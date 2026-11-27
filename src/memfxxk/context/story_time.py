from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, Connection, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from memfxxk.db import entity_edges, events, records, story_annotations, transaction
from memfxxk.domain import Scope
from memfxxk.domain.story import StoryAnnotation, StoryTime
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound
from memfxxk.manage.common import lock_user_operations, write_audit
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService

Kind = Literal["fact", "edge"]
_SCOPE = (
    story_annotations.c.project_id,
    story_annotations.c.agent_id,
    story_annotations.c.user_id,
)
_ORDER = (func.num_nonnulls(*_SCOPE).desc(), *(col.is_not(None).desc() for col in _SCOPE))


def _visible(scope: Scope, kind: Kind, target: UUID | ColumnElement[UUID]) -> tuple:
    return (
        story_annotations.c[f"{kind}_id"] == target,
        visible_filter(story_annotations, scope),
        select(events.c.id)
        .where(
            events.c.id == story_annotations.c.source_event_id,
            visible_filter(events, scope),
            ~events.c.redacted,
        )
        .correlate(story_annotations)
        .exists(),
    )


def at(scope: Scope, kind: Kind, target: ColumnElement[UUID], key: int) -> ColumnElement[bool]:
    return (
        select(
            (story_annotations.c.start <= key)
            & or_(story_annotations.c.end.is_(None), story_annotations.c.end > key)
        )
        .where(*_visible(scope, kind, target))
        .order_by(*_ORDER)
        .limit(1)
        .scalar_subquery()
        .is_(True)
    )


def get(conn: Connection, scope: Scope, kind: Kind, target: UUID) -> StoryTime | None:
    row = (
        conn.execute(
            select(*(story_annotations.c[key] for key in StoryTime.model_fields))
            .where(*_visible(scope, kind, target))
            .order_by(*_ORDER)
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    return StoryTime.model_validate(dict(row)) if row else None


def annotate(service: BoundService, request: StoryAnnotation) -> StoryAnnotation:
    service.require_write()
    ctx = service.ctx
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        table = records if request.kind == "fact" else entity_edges
        conditions = [visible_filter(table, ctx)]
        if request.kind == "fact":
            conditions.extend(
                [
                    records.c.memory_id == request.id,
                    records.c.is_current,
                    records.c.status == "active",
                    records.c.type == "fact",
                ]
            )
        else:
            conditions.append(entity_edges.c.id == request.id)
        row = (
            conn.execute(select(table).where(*conditions).with_for_update())
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound
        service.require_resource_write(row["project_id"])
        scope = Scope.model_validate(ctx.model_dump())
        key = derived_id("story-time", scope.model_dump_json(), request.kind, str(row["id"]))
        audit = write_audit(
            conn, ctx, "story_time", request.id, request.model_dump(mode="json"), {}
        )
        if request.time is None:
            conn.execute(
                story_annotations.delete().where(
                    visible_filter(story_annotations, ctx), story_annotations.c.id == key
                )
            )
        else:
            record = insert(story_annotations).values(
                id=key,
                **scope.model_dump(),
                **{f"{request.kind}_id": row["id"]},
                **request.time.model_dump(),
                source_event_id=audit,
            )
            conn.execute(
                record.on_conflict_do_update(
                    index_elements=[story_annotations.c.id],
                    set_={
                        name: record.excluded[name]
                        for name in (*StoryTime.model_fields, "source_event_id")
                    },
                )
            )
    return request
