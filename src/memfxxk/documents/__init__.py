from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, case, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import document_revisions, documents, events, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.documents import (
    Document,
    DocumentInfo,
    DocumentRevision,
    PutDocumentRequest,
    document_path,
)
from memfxxk.domain.errors import ServiceError
from memfxxk.ids import new_id
from memfxxk.ingest import NotFound
from memfxxk.scope import resolve_write_scope, visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService

_DIMENSIONS = (documents.c.project_id, documents.c.agent_id, documents.c.user_id)
_SPECIFICITY = sum(case((column.is_not(None), 1), else_=0) for column in _DIMENSIONS)
_PRECEDENCE = (_SPECIFICITY.desc(), *(column.is_not(None).desc() for column in _DIMENSIONS))


def _document_info(row: RowMapping) -> DocumentInfo:
    return DocumentInfo(
        document_id=row["id"],
        path=row["path"],
        scope=Scope.model_validate(dict(row)),
        purpose=row["purpose"],
        load_policy=row["load_policy"],
        revision=row["head_revision"],
        updated_at=row["updated_at"],
    )


def _resolve(
    connection: Connection, ctx: RequestContext, path: str, *, lock: bool = False
) -> RowMapping:
    query = (
        select(documents)
        .where(documents.c.path == path, visible_filter(documents, ctx))
        .order_by(*_PRECEDENCE)
        .limit(1)
    )
    row = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if row is None:
        raise NotFound
    return row


def _revision(connection: Connection, row: RowMapping, revision: int | None) -> RowMapping:
    number = row["head_revision"] if revision is None else revision
    stored = (
        connection.execute(
            select(document_revisions).where(
                document_revisions.c.document_id == row["id"],
                document_revisions.c.revision == number,
            )
        )
        .mappings()
        .one_or_none()
    )
    if stored is None:
        raise NotFound
    return stored


def _as_document(row: RowMapping, revision: RowMapping) -> Document:
    info = _document_info(row)
    return Document(
        **info.model_dump(exclude={"revision", "updated_at"}),
        revision=revision["revision"],
        updated_at=revision["created_at"],
        content=revision["content"],
    )


def list_resolved(connection: Connection, ctx: RequestContext) -> list[DocumentInfo]:
    rows = connection.execute(
        select(documents)
        .where(visible_filter(documents, ctx))
        .order_by(documents.c.path, *_PRECEDENCE)
    ).mappings()
    resolved: dict[str, DocumentInfo] = {}
    for row in rows:
        if row["path"] not in resolved:
            resolved[row["path"]] = _document_info(row)
    return list(resolved.values())


def document_contents(connection: Connection, items: list[DocumentInfo]) -> dict[UUID, str]:
    if not items:
        return {}
    eligible = [(item.document_id, item.revision) for item in items]
    result = connection.execute(
        select(document_revisions.c.document_id, document_revisions.c.content).where(
            tuple_(document_revisions.c.document_id, document_revisions.c.revision).in_(eligible)
        )
    )
    return {document_id: path for document_id, path in result}


def get_document(service: BoundService, path: str, revision: int | None = None) -> Document:
    with service.engine.connect() as connection:
        row = _resolve(connection, service.ctx, document_path(path))
        return _as_document(row, _revision(connection, row, revision))


def list_documents(service: BoundService) -> list[DocumentInfo]:
    with service.engine.connect() as connection:
        return list_resolved(connection, service.ctx)


def list_revisions(service: BoundService, path: str) -> list[DocumentRevision]:
    with service.engine.connect() as connection:
        row = _resolve(connection, service.ctx, document_path(path))
        revisions = connection.execute(
            select(document_revisions)
            .where(document_revisions.c.document_id == row["id"])
            .order_by(document_revisions.c.revision.desc())
        ).mappings()
        return [DocumentRevision.model_validate(dict(revision)) for revision in revisions]


def put_document(service: BoundService, path: str, request: PutDocumentRequest) -> Document:
    path = document_path(path)
    scope = resolve_write_scope(service.ctx, request.scope_drop)
    scope_values = scope.model_dump()
    always = path in {"persona.md", "user.md", "project.md"}
    with transaction(service.engine) as connection:
        if request.base_revision in (None, 0):
            connection.execute(
                insert(documents)
                .values(
                    id=new_id(),
                    path=path,
                    head_revision=0,
                    purpose=request.purpose or "",
                    load_policy=request.load_policy or ("always" if always else "on_demand"),
                    **scope_values,
                )
                .on_conflict_do_nothing()
            )
        row = (
            connection.execute(
                select(documents)
                .where(
                    documents.c.path == path,
                    visible_filter(documents, service.ctx),
                    *(documents.c[key] == value for key, value in scope_values.items()),
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        head = row["head_revision"] if row is not None else 0
        base = request.base_revision if request.base_revision is not None else 0
        if row is None or base != head:
            excerpt = _revision(connection, row, None)["content"][:200] if head and row else ""
            raise ServiceError(
                409,
                "revision_conflict",
                "Document revision has changed",
                {"head_revision": head, "head_excerpt": excerpt},
            )
        stored = (
            connection.execute(
                document_revisions.insert()
                .values(
                    document_id=row["id"],
                    revision=head + 1,
                    content=request.content,
                    author=service.ctx.actor.model_dump(),
                    base_revision=request.base_revision,
                    message=request.message,
                )
                .returning(document_revisions)
            )
            .mappings()
            .one()
        )
        updated = (
            connection.execute(
                documents.update()
                .where(documents.c.id == row["id"], visible_filter(documents, service.ctx))
                .values(
                    head_revision=head + 1,
                    updated_at=func.now(),
                    purpose=row["purpose"] if request.purpose is None else request.purpose,
                    load_policy=row["load_policy"]
                    if request.load_policy is None
                    else request.load_policy,
                )
                .returning(documents)
            )
            .mappings()
            .one()
        )
        _audit(connection, service, scope, "put_document", updated, request.content)
        return _as_document(updated, stored)


def _audit(
    connection: Connection,
    service: BoundService,
    scope: Scope,
    op: str,
    row: RowMapping,
    content: str | None = None,
) -> None:
    payload = {"op": op, "path": row["path"], "revision": row["head_revision"]}
    if content is not None:
        payload["content_sha256"] = sha256(content.encode()).hexdigest()
    connection.execute(
        events.insert().values(
            id=new_id(),
            **scope.model_dump(),
            kind="user_op",
            payload=payload,
            actor=service.ctx.actor.model_dump(),
            event_time=func.now(),
        )
    )


def delete_document(service: BoundService, path: str) -> None:
    with transaction(service.engine) as connection:
        row = _resolve(connection, service.ctx, document_path(path), lock=True)
        if service.principal.role != "admin" and not service.settings.allow_host_hard_delete:
            raise ServiceError(403, "forbidden", "Hard deletion is disabled for hosts")
        connection.execute(
            documents.delete().where(
                documents.c.id == row["id"], visible_filter(documents, service.ctx)
            )
        )
        _audit(connection, service, Scope.model_validate(dict(row)), "delete_document", row)
