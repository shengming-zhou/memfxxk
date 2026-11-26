from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, func, select
from sqlalchemy.engine import RowMapping

from memfxxk import documents
from memfxxk.db import documents as document_table
from memfxxk.db import events, records, transaction
from memfxxk.domain import Scope
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.workspace import (
    ImportWorkspaceRequest,
    WorkspaceDocument,
    WorkspaceEvidence,
    WorkspaceImportResult,
    WorkspaceLesson,
    WorkspaceSnapshot,
    validate_paths,
)
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound, ingest
from memfxxk.manage.common import insert_explicit_record, lock_user_operations, write_audit
from memfxxk.scope import visible_filter

if TYPE_CHECKING:
    from memfxxk.service import BoundService


def _scope(service: BoundService) -> Scope:
    return Scope.model_validate(service.ctx.model_dump())


def _record(conn: Connection, service: BoundService, memory_id: UUID) -> RowMapping | None:
    return (
        conn.execute(
            select(records).where(
                visible_filter(records, service.ctx),
                *(records.c[key] == value for key, value in _scope(service).model_dump().items()),
                records.c.memory_id == memory_id,
                records.c.is_current,
                records.c.type == "lesson",
            )
        )
        .mappings()
        .one_or_none()
    )


def _evidence(conn: Connection, service: BoundService, ids: list[UUID]) -> list[RowMapping]:
    rows = {
        row["id"]: row
        for row in conn.execute(
            select(events).where(
                visible_filter(events, service.ctx),
                events.c.id.in_(ids),
                events.c.kind == "tool_result",
                ~events.c.redacted,
            )
        ).mappings()
    }
    if any(event_id not in rows for event_id in ids):
        raise NotFound
    return [rows[event_id] for event_id in ids]


def _evidence_model(row: RowMapping) -> WorkspaceEvidence:
    return WorkspaceEvidence(
        event_id=row["id"],
        event_time=row["event_time"],
        content=row["content"],
        payload=row["payload"],
    )


def export_workspace(service: BoundService, lesson_ids: list[UUID]) -> WorkspaceSnapshot:
    try:
        with service.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            infos = [
                doc
                for doc in documents.list_resolved(conn, service.ctx)
                if doc.scope == _scope(service)
            ]
            contents = documents.document_contents(conn, infos)
            exported_docs = [
                WorkspaceDocument(
                    path=doc.path,
                    purpose=doc.purpose,
                    load_policy=doc.load_policy,
                    document_id=doc.document_id,
                    revision=doc.revision,
                    content=contents[doc.document_id],
                )
                for doc in infos
            ]
            lessons = []
            for memory_id in dict.fromkeys(lesson_ids):
                row = _record(conn, service, memory_id)
                if row is None or row["status"] != "active" or len(row["source_event_ids"]) != 2:
                    raise NotFound
                failure, verification = _evidence(conn, service, row["source_event_ids"])
                lessons.append(
                    WorkspaceLesson(
                        memory_id=memory_id,
                        version=row["version"],
                        title=row["title"],
                        content=row["content"],
                        failure=_evidence_model(failure),
                        verification=_evidence_model(verification),
                    )
                )
            return WorkspaceSnapshot(documents=exported_docs, lessons=lessons)
    except ValidationError as exc:
        raise ServiceError(
            422, "invalid_workspace_snapshot", "This scope has entries that cannot be exported"
        ) from exc


def _import_document(
    conn: Connection, service: BoundService, entry: WorkspaceDocument, update_existing: bool
) -> Literal["created", "updated", "unchanged"]:
    matches = (
        conn.execute(
            select(document_table)
            .where(
                visible_filter(document_table, service.ctx),
                *(
                    document_table.c[key] == value
                    for key, value in _scope(service).model_dump().items()
                ),
                func.lower(document_table.c.path) == entry.path.casefold(),
            )
            .with_for_update()
        )
        .mappings()
        .all()
    )
    if matches and (len(matches) != 1 or matches[0]["path"] != entry.path):
        raise ServiceError(
            409, "workspace_conflict", "Document path conflicts", {"path": entry.path}
        )
    head = 0
    if matches:
        row = matches[0]
        head = row["head_revision"]
        info = documents._document_info(row)
        content = documents.document_contents(conn, [info])[info.document_id]
        if (
            content == entry.content
            and (entry.purpose is None or entry.purpose == info.purpose)
            and (entry.load_policy is None or entry.load_policy == info.load_policy)
        ):
            return "unchanged"
        if not update_existing or entry.document_id != info.document_id or entry.revision != head:
            raise ServiceError(
                409,
                "workspace_conflict",
                "Document has a different revision",
                {"path": entry.path, "head_revision": head},
            )
    documents.put_document_in_transaction(
        conn,
        service,
        entry.path,
        PutDocumentRequest(
            content=entry.content,
            base_revision=head,
            purpose=entry.purpose,
            load_policy=entry.load_policy,
            message="Workspace import",
        ),
    )
    return "updated" if head else "created"


def _matches_evidence(row: RowMapping, source: WorkspaceEvidence) -> bool:
    return (
        (
            row["id"] == source.event_id
            or row["source"].get("workspace_event_id") == str(source.event_id)
        )
        and row["content"] == source.content
        and row["payload"] == source.payload
        and row["event_time"] == source.event_time
    )


def _import_lesson(
    conn: Connection, service: BoundService, entry: WorkspaceLesson
) -> Literal["created", "unchanged"]:
    scope = _scope(service)
    identity = json.dumps(scope.model_dump(), sort_keys=True)
    new_id = derived_id("workspace-lesson", identity, str(entry.memory_id))
    sources = [entry.failure, entry.verification]
    current = _record(conn, service, entry.memory_id)
    if current is None:
        current = _record(conn, service, new_id)
    if current is not None:
        existing_sources = _evidence(conn, service, current["source_event_ids"])
        if (
            current["content"] == entry.content
            and current["title"] == entry.title
            and len(existing_sources) == 2
            and all(
                _matches_evidence(row, source) for row, source in zip(existing_sources, sources)
            )
        ):
            return "unchanged"
        raise ServiceError(
            409,
            "workspace_conflict",
            "Lesson has different content or evidence",
            {"memory_id": str(entry.memory_id)},
        )
    audit_id = write_audit(
        conn,
        service.ctx,
        "import_lesson",
        new_id,
        entry.model_dump(mode="json"),
        {"memory_id": str(new_id), "version": 1},
        idempotency_key=f"workspace-lesson:{new_id}",
        namespace=service.idempotency_namespace,
    )
    if audit_id is None:
        raise NotFound
    stored = ingest(
        conn,
        service.ctx,
        [
            IngestEvent(
                kind="tool_result",
                content=source.content,
                payload=source.payload,
                source={"uri": "workspace:lesson", "workspace_event_id": str(source.event_id)},
                event_time=source.event_time,
                idempotency_key=str(derived_id("workspace-event", identity, str(source.event_id))),
            )
            for source in sources
        ],
        write_project=service.write_project,
        namespace=service.idempotency_namespace,
    )
    source_ids = [result.event_id for result in stored]
    evidence = _evidence(conn, service, source_ids)
    if not all(
        _matches_evidence(row, source) for row, source in zip(evidence, sources, strict=True)
    ):
        raise ServiceError(409, "workspace_conflict", "An imported event has different content")
    insert_explicit_record(
        conn,
        service.ctx,
        scope,
        memory_id=new_id,
        kind="lesson",
        content=entry.content,
        title=entry.title,
        sources=source_ids,
        event_time=entry.verification.event_time,
        session_id=None,
        origin="import",
        source_pos={
            "failure_event_id": str(source_ids[0]),
            "verification_event_id": str(source_ids[1]),
            "workspace_memory_id": str(entry.memory_id),
            "workspace_version": entry.version,
        },
    )
    return "created"


def import_workspace(
    service: BoundService, request: ImportWorkspaceRequest
) -> WorkspaceImportResult:
    result = WorkspaceImportResult()
    with transaction(service.engine) as conn:
        lock_user_operations(conn, service.ctx)
        if request.snapshot.documents:
            paths = {
                doc.path
                for doc in documents.list_resolved(conn, service.ctx)
                if doc.scope == _scope(service)
            }
            paths.update(doc.path for doc in request.snapshot.documents)
            try:
                validate_paths(list(paths))
            except ValueError as exc:
                raise ServiceError(409, "workspace_conflict", str(exc)) from exc
        for entry in sorted(request.snapshot.documents, key=lambda item: item.path):
            match _import_document(conn, service, entry, request.update_existing):
                case "created":
                    result.documents_created += 1
                case "updated":
                    result.documents_updated += 1
                case "unchanged":
                    result.documents_unchanged += 1
        for lesson in request.snapshot.lessons:
            if _import_lesson(conn, service, lesson) == "created":
                result.lessons_created += 1
            else:
                result.lessons_unchanged += 1
    return result
