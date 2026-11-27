from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, Integer, and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from memfxxk.db import content_priorities, memory_preferences, transaction
from memfxxk.documents import _resolve, _revision
from memfxxk.domain import Scope
from memfxxk.domain.documents import document_path
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.manage import PatchRequest
from memfxxk.domain.priorities import ClearPriority, PriorityResult, PriorityTarget, SetPriority
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound
from memfxxk.m2.doc_chunks import sections
from memfxxk.m2.preferences import preference_order, write_preferences
from memfxxk.manage.common import current_record, lock_user_operations, write_audit
from memfxxk.scope import resolve_write_scope, visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService

DEFAULT_PRIORITY = 3


def priority_layers[T](items: Iterable[T], key: Callable[[T], int]) -> list[tuple[int, list[T]]]:
    layers = defaultdict(list)
    for item in items:
        layers[key(item)].append(item)
    return sorted(layers.items(), key=lambda layer: layer[0], reverse=True)


@dataclass(frozen=True, slots=True)
class ContentTarget:
    target: PriorityTarget
    document_id: UUID | None = None
    project_id: str | None = None
    inherited: PriorityTarget | None = None


def _target(
    conn: Connection,
    service: BoundService,
    target: PriorityTarget,
    *,
    write: bool = False,
) -> ContentTarget:
    ctx = service.ctx
    if target.kind == "project":
        if ctx.project_id is None:
            raise ServiceError(422, "project_required", "Select a project to manage its default")
        return ContentTarget(target, project_id=ctx.project_id)
    if target.kind == "directory":
        assert target.path is not None
        path = document_path(f"{target.path.rstrip('/')}/index.md").rpartition("/")[0]
        if not path:
            raise ServiceError(422, "invalid_directory", "Use a nonempty document path prefix")
        return ContentTarget(target.model_copy(update={"path": path}), project_id=ctx.project_id)
    if target.kind == "record":
        assert target.memory_id is not None
        row = current_record(conn, ctx, target.memory_id, lock=write)
        pos = row["source_pos"] or {}
        if row["type"] == "doc_chunk" and pos.get("document_id") and pos.get("path"):
            document = _resolve(conn, ctx, pos["path"])
            if str(document["id"]) != pos["document_id"]:
                raise NotFound
            parent = PriorityTarget(
                kind="section" if pos.get("heading") else "document",
                path=pos["path"],
                heading=pos.get("heading") or None,
            )
            return ContentTarget(target, document["id"], row["project_id"], parent)
        return ContentTarget(target, project_id=row["project_id"])
    assert target.path is not None
    path = document_path(target.path)
    row = _resolve(conn, ctx, path, lock=write)
    if target.heading is not None:
        content = _revision(conn, row, None)["content"]
        if all(heading != target.heading for heading, _ in sections(content)):
            raise NotFound
    return ContentTarget(target.model_copy(update={"path": path}), row["id"], row["project_id"])


def _lineage(target: PriorityTarget) -> list[PriorityTarget]:
    result = [target]
    if target.kind == "section":
        assert target.heading is not None
        heading = target.heading
        while " / " in heading:
            heading = heading.rsplit(" / ", 1)[0]
            result.append(PriorityTarget(kind="section", path=target.path, heading=heading))
        result.append(PriorityTarget(kind="document", path=target.path))
    if target.path:
        parent = target.path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            result.append(PriorityTarget(kind="directory", path=parent))
    if target.kind != "project":
        result.append(PriorityTarget(kind="project"))
    return result


def _rule_key(target: PriorityTarget) -> str:
    return target.heading or (target.path if target.kind == "directory" else "") or ""


def _coordinates(target: PriorityTarget, document_id: UUID | None) -> dict:
    return {
        "kind": target.kind,
        "key": _rule_key(target),
        "document_id": document_id if target.kind in ("document", "section") else None,
    }


def effective(
    conn: Connection,
    ctx: Scope,
    item: ContentTarget,
) -> PriorityResult:
    target = item.target
    if target.kind == "record":
        matched = (
            conn.execute(
                select(memory_preferences)
                .where(
                    visible_filter(memory_preferences, ctx),
                    memory_preferences.c.memory_id == target.memory_id,
                    memory_preferences.c.priority.is_not(None),
                )
                .order_by(*preference_order())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if matched is not None:
            return PriorityResult(
                target=target,
                priority=int(matched["priority"]),
                source=target,
                source_scope=Scope.model_validate(dict(matched)),
            )
    lineage = _lineage(item.inherited or target)
    choices = {
        (selection.kind, _rule_key(selection)): selection
        for selection in lineage
        if selection.kind != "record" and (selection.kind != "project" or ctx.project_id)
    }
    if choices:
        filters = []
        for candidate in choices.values():
            coord = _coordinates(candidate, item.document_id)
            conditions = [content_priorities.c[key] == value for key, value in coord.items()]
            if candidate.kind == "project":
                conditions.append(content_priorities.c.project_id == ctx.project_id)
            filters.append(and_(*conditions))
        columns = [
            content_priorities.c[dimension].is_not(None)
            for dimension in ("project_id", "agent_id", "user_id")
        ]
        rows = conn.execute(
            select(content_priorities)
            .where(
                visible_filter(content_priorities, ctx),
                or_(*filters),
            )
            .order_by(
                sum(column.cast(Integer) for column in columns).desc(),
                *(column.desc() for column in columns),
                content_priorities.c.id,
            )
        ).mappings()
        nearest = {}
        for row in rows:
            nearest.setdefault((row["kind"], row["key"]), row)
        for key, source in choices.items():
            if hit := nearest.get(key):
                return PriorityResult(
                    target=target,
                    priority=int(hit["priority"]),
                    source=source,
                    source_scope=Scope.model_validate(dict(hit)),
                )
    return PriorityResult(target=target, priority=DEFAULT_PRIORITY)


def get(service: BoundService, target: PriorityTarget) -> PriorityResult:
    with service.engine.connect() as conn:
        return effective(conn, service.ctx, _target(conn, service, target))


def change(service: BoundService, request: SetPriority | ClearPriority) -> PriorityResult:
    ctx = service.ctx
    service.require_write(request.scope_drop)
    scope = resolve_write_scope(ctx, request.scope_drop)
    if request.target.kind == "project" and scope.project_id is None:
        raise ServiceError(422, "project_required", "Keep the project scope for its default")
    with transaction(service.engine) as conn:
        lock_user_operations(conn, ctx)
        selected = _target(conn, service, request.target, write=True)
        service.require_resource_write(selected.project_id)
        target = selected.target
        value = request.priority if isinstance(request, SetPriority) else None
        if target.kind == "record":
            assert target.memory_id is not None
            audit_id = target.memory_id
            write_preferences(conn, scope, target.memory_id, PatchRequest(priority=value))
        else:
            coordinates = _coordinates(target, selected.document_id)
            key = derived_id(
                "content-priority",
                scope.model_dump_json(),
                coordinates["kind"],
                coordinates["key"],
                str(coordinates["document_id"] or ""),
            )
            audit_id = selected.document_id or key
            if value is None:
                conn.execute(
                    content_priorities.delete().where(
                        content_priorities.c.id == key,
                        visible_filter(content_priorities, ctx),
                    )
                )
            else:
                statement = insert(content_priorities).values(
                    id=key,
                    **scope.model_dump(),
                    **coordinates,
                    priority=value,
                )
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=[content_priorities.c.id],
                        set_={"priority": value},
                    )
                )
        result = effective(conn, ctx, selected)
        write_audit(
            conn,
            ctx,
            "clear_priority" if value is None else "set_priority",
            audit_id,
            request.model_dump(mode="json"),
            {"priority": value},
        )
        return result
